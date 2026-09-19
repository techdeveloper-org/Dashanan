"""QA pytest suite for DASH-STORY-004 (Zone 2 capacity cap and MaxAge
forced-archival backstop, OAQ-4, FR-002).

QA subtask (backlog_draft.json 20% split), an independent verification
pass on top of the Dev subtask's own suite
(tests/test_zone2_capacity_backstop_dash004.py), matching the dev/QA
split already established by DASH-STORY-002/003/006/008/009 (see e.g.
test_qa_episodic_memory_zone2.py's identical "independent verification
pass" framing). This suite re-derives its own fakes independently rather
than importing the dev suite's doubles, so a bug shared between a dev
fake and the implementation is still caught.

Acceptance criteria under test, verbatim from
docs/phase-7-routing/implementation_execution_plan.json's DASH-STORY-004
qa_prompt/dev_prompt (identical criteria cited by both):

  FR-002 (verbatim): "The system SHALL provide an Episodic Memory zone
  that stores chronological interaction records with recency-based
  decay, and SHALL retrieve them via keyset (cursor) pagination or
  direct primary-key lookup."

  AC-002-CAP-1: "Given Zone 2 reaches capacity cap or an item exceeds
  MaxAge while Compressed, the backstop sweep forcibly evicts the
  lowest-ranked-by-MemoryScore item(s) so the zone never grows
  unbounded."

  AC-002-CAP-2: "Given cap/MaxAge are deployment config (NFR-009), an
  operator change is used by the sweep's next run without a code
  change."

  AC-002-CAP-DPDP-1 (REDACTED, no PII examples): "Forced eviction of
  PII-bearing items SHALL satisfy, never bypass, a pending DPDP erasure
  obligation, and SHALL NOT leave PII partially erased across Zone 2 and
  any zone that referenced it."

Test scenario enumeration (rule 33/40/41 "enumerate before code"
convention), one row per AC:

  AC-002-CAP-1:
    - happy: capacity overflow evicts the single lowest-scored excess
      item, tagged CAPACITY.
    - happy: MaxAge-while-Compressed evicts every matching item,
      regardless of score, tagged MAX_AGE.
    - boundary: exactly at cap -> nothing evicted; one over cap ->
      exactly one evicted.
    - boundary: age exactly equal to max_age_seconds -> NOT evicted
      (strict `>` per the domain module's own docstring); one unit over
      -> evicted.
    - adverse: an Active (non-Compressed) item that is old never
      triggers the MaxAge path, however old it is.
    - adverse: a combined trigger (both capacity overflow AND MaxAge
      eligible) is evicted exactly once, tagged MAX_AGE (the harder
      guarantee wins), never duplicated.
  AC-002-CAP-2:
    - happy: config is loaded via the port on every `run()` call, never
      cached on the sweep instance or constructor.
    - happy: a wider cap between two runs changes the second run's
      outcome with zero code change -- proven by calling the same
      `Zone2CapacityBackstopSweep` instance twice with only the fake
      config port's return value changed between calls.
    - adverse: a config-load failure aborts the whole sweep (a
      precondition, not a per-item failure) and evicts nothing.
  AC-002-CAP-DPDP-1:
    - happy: an item with a pending DPDP obligation is fulfilled via the
      cascade port ONLY -- the ordinary evict() port is never invoked
      for that item (satisfy, never bypass).
    - happy: an item with no pending obligation is still checked first,
      then evicted via the ordinary port.
    - adverse: a DPDP cascade failure never falls back to the ordinary
      evict() path (that fallback would BE the bypass this AC forbids)
      -- it is recorded as a per-item failure instead, and no
      `memory.evicted` event is published for it.
    - adverse (replay/idempotency proxy): running the sweep a second
      time over a candidate set from which the previously-evicted item
      has already been removed does not re-evict or re-check it, since
      the candidate source is the single source of "what is still in
      Zone 2."
    - golden regression case: the exact "mixed batch, one pending, one
      not" scenario, kept as one golden test per this story's own
      dev-suite convention.

Architecture-fitness (HLD Section 3.0, invariant 1), scoped to this
story's own two new modules: neither `domain/zone2_capacity_backstop.py`
nor `application/zone2_capacity_backstop_sweep.py` may import from
`dashanan.infrastructure` -- the repo-wide sweep lives in
test_memory_orchestrator.py; this is the story-scoped, independent proof
(matching DASH-STORY-002/003's identical convention).

Runtime assumptions (rule 33/40/41 test-roadmap conventions):
  - clock: FakeClock, fixed at 2026-06-01T00:00:00+00:00 (deliberately
    different from the dev suite's 2026-01-01 fixture, so this suite's
    pass/fail is not an artifact of sharing the dev suite's exact
    numbers) -- deterministic, no wall-clock dependency.
  - tenant_id: "qa-tenant-1" for all requests unless a test states
    otherwise.
  - No fixture seed is required; nothing in this suite is randomized.

PII NOTE: per the dev_prompt's PII constraint, this suite carries only
eviction-ranking and config-surface mechanics -- pseudonymized item_id
values, MemoryScore floats, boolean/enum flags, timestamps -- never zone
payload/fact content, and no DPDP-erasure example that could read as
real PII content, anywhere below.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from dashanan.application.zone2_capacity_backstop_sweep import (
    BackstopSweepResult,
    EvictionFailure,
    EvictionOutcome,
    Zone2BackstopPortError,
    Zone2CapacityBackstopSweep,
)
from dashanan.domain.zone2_capacity_backstop import (
    EvictionCandidate,
    EvictionDecision,
    EvictionReason,
    Zone2BackstopConfig,
    Zone2CapacityBackstopError,
    plan_backstop_eviction,
)

DOMAIN_DIR = Path(__file__).resolve().parents[1] / "src" / "dashanan" / "domain"
APPLICATION_DIR = Path(__file__).resolve().parents[1] / "src" / "dashanan" / "application"
DOMAIN_FILE = DOMAIN_DIR / "zone2_capacity_backstop.py"
APPLICATION_FILE = APPLICATION_DIR / "zone2_capacity_backstop_sweep.py"

_FIXED_TS = datetime(2026, 6, 1, tzinfo=UTC)
_TENANT = "qa-tenant-1"


class QaFakeClock:
    """Deterministic Clock double, independent of the dev suite's own fake."""

    def __init__(self, fixed: datetime = _FIXED_TS) -> None:
        self._fixed = fixed

    def now(self) -> datetime:
        return self._fixed


class QaRecordingEventBus:
    """EventBus double: records every publish() call, never raises."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, object]]] = []

    def publish(self, event_type: str, payload: dict[str, object]) -> None:
        self.published.append((event_type, dict(payload)))


class QaSequencedConfigPort:
    """Zone2BackstopConfigPort double returning configs in call order.

    Independent re-derivation of the dev suite's FakeConfigPort, proving
    AC-002-CAP-2's "operator change used by the NEXT run, no code
    change" against this suite's own double rather than reusing the dev
    suite's.
    """

    def __init__(self, configs: list[Zone2BackstopConfig]) -> None:
        self._configs = list(configs)
        self.call_count = 0
        self.calls: list[str] = []

    def load(self, tenant_id: str) -> Zone2BackstopConfig:
        self.calls.append(tenant_id)
        config = self._configs[min(self.call_count, len(self._configs) - 1)]
        self.call_count += 1
        return config


class QaRaisingConfigPort:
    """Zone2BackstopConfigPort double that always raises (whole-sweep precondition failure)."""

    def load(self, tenant_id: str) -> Zone2BackstopConfig:
        raise Zone2BackstopPortError("qa: config store unreachable")


class QaCandidateSource:
    """Zone2CandidateSource double serving a fixed candidate list."""

    def __init__(self, candidates: list[EvictionCandidate]) -> None:
        self._candidates = list(candidates)
        self.calls: list[tuple[str, datetime]] = []

    def fetch_candidates(self, tenant_id: str, as_of: datetime) -> list[EvictionCandidate]:
        self.calls.append((tenant_id, as_of))
        return list(self._candidates)


class QaEvictionPort:
    """Zone2EvictionPort double: records evict() calls, can raise for specific items."""

    def __init__(self, raise_for: frozenset[str] = frozenset()) -> None:
        self.evicted: list[tuple[str, str]] = []
        self._raise_for = raise_for

    def evict(self, tenant_id: str, item_id: str) -> None:
        if item_id in self._raise_for:
            raise Zone2BackstopPortError(f"qa: eviction store unreachable for {item_id}")
        self.evicted.append((tenant_id, item_id))


class QaRetrievalIndexEviction:
    """RetrievalIndexEvictionPort double: records delete_item() calls, can raise for specific items.

    P1 remediation (DSHN-58, attempt 1) test double -- exercises the new
    port `Zone2CapacityBackstopSweep._execute_one` now calls for every
    eviction decision, closing the "Zone2EvictionPort.evict is Zone-2-only
    so ordinary eviction leaves Zone 6 retrievable" finding.
    """

    def __init__(self, raise_for: frozenset[str] = frozenset()) -> None:
        self.deleted: list[tuple[str, str]] = []
        self._raise_for = raise_for

    def delete_item(self, tenant_id: str, item_id: str) -> None:
        if item_id in self._raise_for:
            raise Zone2BackstopPortError(f"qa: retrieval index unreachable for {item_id}")
        self.deleted.append((tenant_id, item_id))


class QaDpdpPort:
    """DpdpErasureCascadePort double: item_ids in `pending` have an obligation."""

    def __init__(
        self,
        pending: frozenset[str] = frozenset(),
        raise_for: frozenset[str] = frozenset(),
    ) -> None:
        self._pending = pending
        self._raise_for = raise_for
        self.calls: list[tuple[str, str]] = []
        self.fulfilled: list[tuple[str, str]] = []

    def fulfil_pending_erasure(self, tenant_id: str, item_id: str) -> bool:
        self.calls.append((tenant_id, item_id))
        if item_id in self._raise_for:
            raise Zone2BackstopPortError(f"qa: erasure cascade unreachable for {item_id}")
        if item_id in self._pending:
            self.fulfilled.append((tenant_id, item_id))
            return True
        return False


def _candidate(
    item_id: str,
    memory_score: float = 0.5,
    is_compressed: bool = False,
    age_seconds: float = 0.0,
    written_at: datetime = _FIXED_TS,
) -> EvictionCandidate:
    return EvictionCandidate(
        item_id=item_id,
        memory_score=memory_score,
        is_compressed=is_compressed,
        age_seconds=age_seconds,
        written_at=written_at,
    )


def _config(cap: int = 100_000, max_age_seconds: float = 180 * 86400) -> Zone2BackstopConfig:
    return Zone2BackstopConfig(capacity_cap=cap, max_age_seconds=max_age_seconds)


def _sweep(
    *,
    configs: list[Zone2BackstopConfig] | None = None,
    candidates: list[EvictionCandidate] | None = None,
    eviction_port: QaEvictionPort | None = None,
    dpdp_port: QaDpdpPort | None = None,
    retrieval_index_eviction: QaRetrievalIndexEviction | None = None,
    config_port: object | None = None,
    candidate_source: object | None = None,
) -> tuple[
    Zone2CapacityBackstopSweep,
    object,
    QaCandidateSource | object,
    QaEvictionPort,
    QaDpdpPort,
    QaRecordingEventBus,
]:
    cfg_port = config_port if config_port is not None else QaSequencedConfigPort(
        configs if configs is not None else [_config()]
    )
    cand_source = candidate_source if candidate_source is not None else QaCandidateSource(
        candidates if candidates is not None else []
    )
    eviction = eviction_port if eviction_port is not None else QaEvictionPort()
    dpdp = dpdp_port if dpdp_port is not None else QaDpdpPort()
    retrieval_index = (
        retrieval_index_eviction
        if retrieval_index_eviction is not None
        else QaRetrievalIndexEviction()
    )
    bus = QaRecordingEventBus()
    sweep = Zone2CapacityBackstopSweep(
        config_port=cfg_port,
        candidate_source=cand_source,
        eviction_port=eviction,
        dpdp_port=dpdp,
        retrieval_index_eviction=retrieval_index,
        event_bus=bus,
        clock=QaFakeClock(),
    )
    return sweep, cfg_port, cand_source, eviction, dpdp, bus


class TestFR002TraceabilityAndPackageWiring:
    """FR-002 verbatim trace and baseline wiring, per rule 33/40 Layer-1 conventions."""

    def test_fr002_is_cited_verbatim_in_this_suite(self) -> None:
        fr_002_fragments = [
            "The system SHALL provide an Episodic Memory zone",
            "that stores chronological interaction records with recency-based",
            "decay, and SHALL retrieve them via keyset (cursor) pagination or",
            "direct primary-key lookup.",
        ]
        assert __doc__ is not None
        for fragment in fr_002_fragments:
            assert fragment in __doc__, f"FR-002 fragment missing from module docstring: {fragment!r}"

    def test_sweep_constructs_from_qa_independent_fakes(self) -> None:
        sweep, *_ = _sweep()
        assert isinstance(sweep, Zone2CapacityBackstopSweep)


class TestAC002CAP1HappyPath:
    """AC-002-CAP-1 happy-path: capacity overflow and MaxAge-while-Compressed triggers."""

    def test_capacity_overflow_evicts_lowest_scored_excess_item(self) -> None:
        candidates = [
            _candidate("keep-a", memory_score=0.8),
            _candidate("keep-b", memory_score=0.6),
            _candidate("evict-lowest", memory_score=0.05),
        ]
        sweep, _cfg, _src, eviction, _dpdp, bus = _sweep(
            configs=[_config(cap=2)], candidates=candidates
        )

        result = sweep.run(_TENANT)

        assert [o.item_id for o in result.evicted] == ["evict-lowest"]
        assert result.evicted[0].reason is EvictionReason.CAPACITY
        assert eviction.evicted == [(_TENANT, "evict-lowest")]
        assert bus.published == [
            (
                "memory.evicted",
                {
                    "tenant_id": _TENANT,
                    "item_id": "evict-lowest",
                    "zone": "episodic",
                    "reason": "capacity",
                    "evicted_at": _FIXED_TS.isoformat(),
                },
            )
        ]

    def test_max_age_while_compressed_evicts_every_matching_item_regardless_of_score(
        self,
    ) -> None:
        candidates = [
            _candidate("stale-high-score", memory_score=0.95, is_compressed=True, age_seconds=200 * 86400),
            _candidate("stale-low-score", memory_score=0.02, is_compressed=True, age_seconds=181 * 86400),
        ]
        sweep, *_r, eviction, _dpdp, bus = _sweep(
            configs=[_config(cap=100_000, max_age_seconds=180 * 86400)],
            candidates=candidates,
        )

        result = sweep.run(_TENANT)

        assert {o.item_id for o in result.evicted} == {"stale-high-score", "stale-low-score"}
        assert all(o.reason is EvictionReason.MAX_AGE for o in result.evicted)
        assert {i for _t, i in eviction.evicted} == {"stale-high-score", "stale-low-score"}
        assert len(bus.published) == 2


class TestAC002CAP1BoundaryCases:
    """AC-002-CAP-1 boundaries: cap edge, MaxAge edge, Active-state exclusion."""

    def test_zone_exactly_at_cap_evicts_nothing(self) -> None:
        candidates = [_candidate("a"), _candidate("b"), _candidate("c")]
        sweep, *_r, eviction, _dpdp, bus = _sweep(
            configs=[_config(cap=3)], candidates=candidates
        )

        result = sweep.run(_TENANT)

        assert result.evicted == ()
        assert eviction.evicted == []
        assert bus.published == []

    def test_zone_one_item_over_cap_evicts_exactly_one(self) -> None:
        candidates = [
            _candidate("a", memory_score=0.9),
            _candidate("b", memory_score=0.5),
            _candidate("c", memory_score=0.1),
        ]
        sweep, *_r, eviction, _dpdp, bus = _sweep(
            configs=[_config(cap=2)], candidates=candidates
        )

        result = sweep.run(_TENANT)

        assert len(result.evicted) == 1
        assert result.evicted[0].item_id == "c"

    def test_age_exactly_equal_to_max_age_is_not_evicted(self) -> None:
        candidates = [_candidate("boundary-item", is_compressed=True, age_seconds=100.0)]
        sweep, *_r, eviction, _dpdp, bus = _sweep(
            configs=[_config(cap=100_000, max_age_seconds=100.0)],
            candidates=candidates,
        )

        result = sweep.run(_TENANT)

        assert result.evicted == ()
        assert eviction.evicted == []

    def test_age_one_unit_past_max_age_is_evicted(self) -> None:
        candidates = [_candidate("just-over", is_compressed=True, age_seconds=100.0 + 1e-6)]
        sweep, *_r, eviction, _dpdp, bus = _sweep(
            configs=[_config(cap=100_000, max_age_seconds=100.0)],
            candidates=candidates,
        )

        result = sweep.run(_TENANT)

        assert [o.item_id for o in result.evicted] == ["just-over"]


class TestAC002CAP1AdverseCases:
    """AC-002-CAP-1 adverse/negative cases: Active-state exclusion, combined triggers."""

    def test_active_item_never_evicted_by_max_age_however_old(self) -> None:
        candidates = [
            _candidate("ancient-but-active", is_compressed=False, age_seconds=10_000 * 86400)
        ]
        sweep, *_r, eviction, _dpdp, bus = _sweep(
            configs=[_config(cap=100_000, max_age_seconds=1.0)],
            candidates=candidates,
        )

        result = sweep.run(_TENANT)

        assert result.evicted == (), (
            "AC-002-CAP-1's MaxAge trigger applies only WHILE Compressed -- "
            "an Active item must never be forcibly evicted by age alone"
        )
        assert eviction.evicted == []

    def test_item_matching_both_triggers_is_evicted_exactly_once_tagged_max_age(self) -> None:
        candidates = [
            _candidate("both-triggers", memory_score=0.01, is_compressed=True, age_seconds=999.0),
            _candidate("kept", memory_score=0.9),
        ]
        sweep, *_r, eviction, _dpdp, bus = _sweep(
            configs=[_config(cap=1, max_age_seconds=100.0)],
            candidates=candidates,
        )

        result = sweep.run(_TENANT)

        assert [o.item_id for o in result.evicted] == ["both-triggers"]
        assert result.evicted[0].reason is EvictionReason.MAX_AGE
        assert eviction.evicted == [(_TENANT, "both-triggers")], (
            "an item matching both triggers must be evicted exactly once, "
            "never once per trigger"
        )
        assert len(bus.published) == 1


class TestAC002CAP2ConfigIsFreshDeploymentConfig:
    """AC-002-CAP-2: cap/MaxAge are deployment config, used by the next run, zero code change."""

    def test_config_port_load_called_exactly_once_per_run_never_cached(self) -> None:
        sweep, config_port, *_r = _sweep(candidates=[_candidate("x")])

        sweep.run(_TENANT)
        sweep.run(_TENANT)
        sweep.run(_TENANT)

        assert config_port.calls == [_TENANT, _TENANT, _TENANT]
        assert config_port.call_count == 3

    def test_operator_narrowing_cap_between_runs_changes_next_run_outcome(self) -> None:
        candidates = [
            _candidate("a", memory_score=0.9),
            _candidate("b", memory_score=0.1),
        ]
        permissive = _config(cap=100_000)
        tightened = _config(cap=1)
        sweep, config_port, _src, eviction, _dpdp, _bus = _sweep(
            configs=[permissive, tightened], candidates=candidates
        )

        first = sweep.run(_TENANT)
        second = sweep.run(_TENANT)

        assert first.evicted == (), "under the permissive cap, nothing is evicted on run 1"
        assert [o.item_id for o in second.evicted] == ["b"], (
            "the SAME sweep object, given only a new config value from the "
            "port on run 2, must evict under the operator-tightened cap -- "
            "with zero code change to the sweep itself"
        )
        assert config_port.call_count == 2

    def test_sweep_object_never_caches_a_config_value_as_an_attribute(self) -> None:
        sweep, *_r = _sweep()
        forbidden_attrs = ("_capacity_cap", "_max_age_seconds", "_config", "_cap", "_cached_config")
        for attr in forbidden_attrs:
            assert not hasattr(sweep, attr), (
                f"Zone2CapacityBackstopSweep must never cache a config value "
                f"as {attr!r} -- AC-002-CAP-2 requires a fresh load every run"
            )

    def test_config_load_failure_aborts_whole_sweep_evicts_nothing(self) -> None:
        sweep, *_r, eviction, _dpdp, bus = _sweep(
            config_port=QaRaisingConfigPort(), candidates=[_candidate("never-reached")]
        )

        with pytest.raises(Zone2BackstopPortError):
            sweep.run(_TENANT)

        assert eviction.evicted == []
        assert bus.published == []


class TestAC002CAPDPDP1SatisfyNeverBypass:
    """AC-002-CAP-DPDP-1 (REDACTED, no PII examples): satisfy, never bypass, a pending erasure."""

    def test_pending_obligation_routes_through_cascade_only_never_ordinary_evict(
        self,
    ) -> None:
        candidates = [_candidate("has-obligation", is_compressed=True, age_seconds=999.0)]
        dpdp = QaDpdpPort(pending=frozenset({"has-obligation"}))
        eviction = QaEvictionPort()
        sweep, *_r, ev_port, dpdp_port, bus = _sweep(
            configs=[_config(cap=100_000, max_age_seconds=100.0)],
            candidates=candidates,
            eviction_port=eviction,
            dpdp_port=dpdp,
        )

        result = sweep.run(_TENANT)

        assert result.evicted[0].via_dpdp_cascade is True
        assert dpdp.fulfilled == [(_TENANT, "has-obligation")]
        assert eviction.evicted == [], (
            "an item with a pending DPDP erasure obligation must NEVER also "
            "go through the ordinary evict() path -- that is exactly the "
            "bypass AC-002-CAP-DPDP-1 forbids"
        )

    def test_no_pending_obligation_is_still_checked_first_then_ordinary_evict(
        self,
    ) -> None:
        candidates = [_candidate("no-obligation", is_compressed=True, age_seconds=999.0)]
        dpdp = QaDpdpPort(pending=frozenset())
        eviction = QaEvictionPort()
        sweep, *_r, ev_port, dpdp_port, bus = _sweep(
            configs=[_config(cap=100_000, max_age_seconds=100.0)],
            candidates=candidates,
            eviction_port=eviction,
            dpdp_port=dpdp,
        )

        result = sweep.run(_TENANT)

        assert dpdp.calls == [(_TENANT, "no-obligation")], (
            "the erasure-obligation check must run for EVERY eviction "
            "target, even one that turns out to have no obligation pending"
        )
        assert result.evicted[0].via_dpdp_cascade is False
        assert eviction.evicted == [(_TENANT, "no-obligation")]

    def test_dpdp_cascade_failure_never_falls_back_to_ordinary_eviction(self) -> None:
        candidates = [_candidate("cascade-unreachable", is_compressed=True, age_seconds=999.0)]
        dpdp = QaDpdpPort(raise_for=frozenset({"cascade-unreachable"}))
        eviction = QaEvictionPort()
        sweep, *_r, ev_port, dpdp_port, bus = _sweep(
            configs=[_config(cap=100_000, max_age_seconds=100.0)],
            candidates=candidates,
            eviction_port=eviction,
            dpdp_port=dpdp,
        )

        result = sweep.run(_TENANT)

        assert result.evicted == ()
        assert eviction.evicted == [], (
            "a DPDP cascade failure must never silently fall back to the "
            "ordinary evict() path -- that fallback would itself be the "
            "PII-partial-erasure bypass this AC forbids"
        )
        assert len(result.failed) == 1
        assert result.failed[0].item_id == "cascade-unreachable"
        assert bus.published == [], "no memory.evicted event may be published for a failed eviction"

    def test_replay_second_sweep_over_shrunk_candidate_set_does_not_recheck_already_evicted_item(
        self,
    ) -> None:
        """Adverse/replay proxy: once an item is gone from the candidate
        source (because the DPDP cascade already removed it from Zone 2 on
        run 1), a second sweep run must not re-check or re-evict it --
        proving no double-fulfilment / no partial-erasure window survives
        a second sweep.
        """
        dpdp = QaDpdpPort(pending=frozenset({"already-erased"}))
        eviction = QaEvictionPort()
        cfg_port = QaSequencedConfigPort([_config(cap=100_000, max_age_seconds=100.0)])
        first_run_source = QaCandidateSource(
            [_candidate("already-erased", is_compressed=True, age_seconds=999.0)]
        )
        sweep = Zone2CapacityBackstopSweep(
            config_port=cfg_port,
            candidate_source=first_run_source,
            eviction_port=eviction,
            dpdp_port=dpdp,
            retrieval_index_eviction=QaRetrievalIndexEviction(),
            event_bus=QaRecordingEventBus(),
            clock=QaFakeClock(),
        )

        first = sweep.run(_TENANT)
        assert first.evicted[0].item_id == "already-erased"
        assert dpdp.calls == [(_TENANT, "already-erased")]

        # Simulate the cascade's own removal: run 2 sees a candidate
        # source that no longer contains the erased item.
        sweep._candidate_source = QaCandidateSource([])  # type: ignore[attr-defined]
        second = sweep.run(_TENANT)

        assert second.evicted == ()
        assert dpdp.calls == [(_TENANT, "already-erased")], (
            "the erasure cascade must not be re-invoked for an item that "
            "is no longer among the current candidates"
        )

    def test_golden_mixed_batch_one_pending_one_ordinary(self) -> None:
        """Golden regression case (one per AC, per this story's own dev-suite convention)."""
        candidates = [
            _candidate("golden-pending", is_compressed=True, age_seconds=999.0),
            _candidate("golden-ordinary", is_compressed=True, age_seconds=999.0),
        ]
        dpdp = QaDpdpPort(pending=frozenset({"golden-pending"}))
        eviction = QaEvictionPort()
        sweep, *_r, ev_port, dpdp_port, bus = _sweep(
            configs=[_config(cap=100_000, max_age_seconds=100.0)],
            candidates=candidates,
            eviction_port=eviction,
            dpdp_port=dpdp,
        )

        result = sweep.run(_TENANT)

        by_id = {o.item_id: o for o in result.evicted}
        assert by_id["golden-pending"].via_dpdp_cascade is True
        assert by_id["golden-ordinary"].via_dpdp_cascade is False
        assert eviction.evicted == [(_TENANT, "golden-ordinary")]
        assert dpdp.fulfilled == [(_TENANT, "golden-pending")]
        assert {i for _t, i in eviction.evicted} == {"golden-ordinary"}


class TestMustNotDeviateOrderingAtQaLayer:
    """Must-not-deviate item 2 (ar1_assignments.json AR1-004): lowest-MemoryScore-first,
    re-proven independently at the QA layer with a larger, score-interleaved batch.
    """

    def test_five_item_batch_evicted_in_strict_ascending_score_order(self) -> None:
        candidates = [
            _candidate("score-70", memory_score=0.70, is_compressed=True, age_seconds=999.0),
            _candidate("score-10", memory_score=0.10, is_compressed=True, age_seconds=999.0),
            _candidate("score-50", memory_score=0.50, is_compressed=True, age_seconds=999.0),
            _candidate("score-30", memory_score=0.30, is_compressed=True, age_seconds=999.0),
            _candidate("score-90", memory_score=0.90, is_compressed=True, age_seconds=999.0),
        ]
        sweep, *_r, eviction, _dpdp, bus = _sweep(
            configs=[_config(cap=100_000, max_age_seconds=100.0)],
            candidates=candidates,
        )

        result = sweep.run(_TENANT)

        expected_order = ["score-10", "score-30", "score-50", "score-70", "score-90"]
        assert [o.item_id for o in result.evicted] == expected_order
        assert [i for _t, i in eviction.evicted] == expected_order
        assert [payload["item_id"] for _etype, payload in bus.published] == expected_order


class TestPerItemFailureIsolationAtQaLayer:
    """A single item's operational failure never blocks the rest of the sweep (HLD Section 6)."""

    def test_one_failing_eviction_does_not_prevent_others_from_succeeding(self) -> None:
        candidates = [
            _candidate("will-fail", is_compressed=True, age_seconds=999.0),
            _candidate("will-succeed-1", is_compressed=True, age_seconds=999.0),
            _candidate("will-succeed-2", is_compressed=True, age_seconds=999.0),
        ]
        eviction = QaEvictionPort(raise_for=frozenset({"will-fail"}))
        sweep, *_r, ev_port, dpdp_port, bus = _sweep(
            configs=[_config(cap=100_000, max_age_seconds=100.0)],
            candidates=candidates,
            eviction_port=eviction,
        )

        result = sweep.run(_TENANT)

        assert {o.item_id for o in result.evicted} == {"will-succeed-1", "will-succeed-2"}
        assert [f.item_id for f in result.failed] == ["will-fail"]
        assert isinstance(result.failed[0], EvictionFailure)
        assert "will-fail" not in {i for _t, i in eviction.evicted}


class TestBoundaryAndNegativeInputValidation:
    """Rule 33/40 boundary/negative roadmap: constructor guards and malformed input."""

    def test_run_rejects_blank_tenant_id(self) -> None:
        sweep, *_r = _sweep()
        with pytest.raises(ValueError, match="tenant_id"):
            sweep.run("")

    def test_run_rejects_whitespace_only_tenant_id(self) -> None:
        sweep, *_r = _sweep()
        with pytest.raises(ValueError, match="tenant_id"):
            sweep.run("   \t  ")

    def test_zone2backstopconfig_rejects_zero_cap(self) -> None:
        with pytest.raises(Zone2CapacityBackstopError):
            Zone2BackstopConfig(capacity_cap=0, max_age_seconds=1.0)

    def test_zone2backstopconfig_rejects_negative_max_age(self) -> None:
        with pytest.raises(Zone2CapacityBackstopError):
            Zone2BackstopConfig(capacity_cap=1, max_age_seconds=-5.0)

    def test_eviction_candidate_rejects_score_above_one(self) -> None:
        with pytest.raises(Zone2CapacityBackstopError, match="memory_score"):
            _candidate("bad", memory_score=2.0)

    def test_eviction_candidate_rejects_score_below_zero(self) -> None:
        with pytest.raises(Zone2CapacityBackstopError, match="memory_score"):
            _candidate("bad", memory_score=-0.5)

    def test_eviction_decision_equality_by_value(self) -> None:
        assert EvictionDecision(item_id="x", reason=EvictionReason.CAPACITY) == EvictionDecision(
            item_id="x", reason=EvictionReason.CAPACITY
        )

    def test_plan_backstop_eviction_with_no_candidates_returns_empty(self) -> None:
        assert plan_backstop_eviction([], _config()) == []

    def test_candidate_fetch_failure_aborts_whole_sweep(self) -> None:
        class RaisingSource:
            def fetch_candidates(self, tenant_id: str, as_of: datetime) -> list[EvictionCandidate]:
                raise Zone2BackstopPortError("qa: candidate source unreachable")

        sweep, *_r, eviction, _dpdp, bus = _sweep(candidate_source=RaisingSource())

        with pytest.raises(Zone2BackstopPortError):
            sweep.run(_TENANT)
        assert eviction.evicted == []
        assert bus.published == []


class TestArchitectureFitnessScopedToStory:
    """HLD Section 3.0 invariant 1, scoped to this story's own two new
    modules: neither `domain/zone2_capacity_backstop.py` (pure domain)
    nor `application/zone2_capacity_backstop_sweep.py` (orchestration)
    may import from `dashanan.infrastructure`. The repo-wide sweep
    already lives in test_memory_orchestrator.py; this is the QA
    subtask's own independent, story-scoped proof, matching
    DASH-STORY-002/003's identical convention.
    """

    def _imported_module_names(self, source_path: Path) -> list[str]:
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        names: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module is not None:
                    names.append(node.module)
        return names

    @pytest.mark.parametrize("source_path", [DOMAIN_FILE, APPLICATION_FILE])
    def test_module_exists(self, source_path: Path) -> None:
        assert source_path.is_file(), f"{source_path} not found"

    @pytest.mark.parametrize("source_path", [DOMAIN_FILE, APPLICATION_FILE])
    def test_module_does_not_import_infrastructure(self, source_path: Path) -> None:
        imported = self._imported_module_names(source_path)
        bad = [
            name
            for name in imported
            if name == "dashanan.infrastructure"
            or name.startswith("dashanan.infrastructure.")
        ]
        assert not bad, (
            "HLD 3.0 invariant 1 violated -- "
            f"{source_path.name} imports infrastructure: {bad}"
        )

    def test_domain_module_does_not_import_application_module(self) -> None:
        """Layering: the pure domain module must not depend upward on the
        application-layer orchestration module (the dependency runs the
        other way, application -> domain, per this story's own docstring
        split)."""
        imported = self._imported_module_names(DOMAIN_FILE)
        bad = [
            name
            for name in imported
            if name == "dashanan.application.zone2_capacity_backstop_sweep"
            or name.startswith("dashanan.application.")
        ]
        assert not bad, f"domain module must not import application/**: {bad}"

    def test_domain_config_value_object_is_frozen_immutable(self) -> None:
        import dataclasses

        assert dataclasses.is_dataclass(Zone2BackstopConfig)
        params = getattr(Zone2BackstopConfig, "__dataclass_params__")
        assert params.frozen is True

    def test_eviction_reason_enum_has_no_ttl_member(self) -> None:
        """EvictionReason must never produce Zone 1's own 'ttl' reason --
        this backstop is a distinct trigger from Working memory's TTL
        eviction (HLD Section 12A)."""
        assert "ttl" not in {r.value for r in EvictionReason}
