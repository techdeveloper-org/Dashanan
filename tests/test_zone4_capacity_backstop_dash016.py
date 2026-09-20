"""Test suite for DASH-STORY-016: Zone 4 (Procedural) capacity/MaxAge backstop.

Traces to FR-004 in SRS.md. FR-004 (verbatim): "The system SHALL provide a
Procedural Memory zone holding learned task procedures, tool-use patterns,
and successful action sequences."

Covers, by AC ID:
  - AC-004-CAP-1: capacity-cap and MaxAge-while-Compressed triggers,
    lowest-MemoryScore-first eviction ordering, boundary and combined-
    trigger cases, mirroring the Zone 2 backstop pattern (DASH-STORY-004).
  - AC-004-CAP-4: cap/MaxAge are deployment config, read fresh per sweep
    run, with no code change needed for an operator's config update to
    take effect on the next run.

Also covers every must-not-deviate item verbatim from this story's
dev_prompt (see each `TestMustNotDeviate*` class below), plus boundary and
adverse cases per rules 33/40's roadmap (a MaxAge exactly at the boundary,
cap exactly at threshold, per-item operational-failure isolation, a
blank tenant_id).

Runtime assumptions (rule 33/40 test-roadmap conventions):
  - clock: `FakeClock`, fixed at `2026-01-01T00:00:00+00:00` unless a
    test states otherwise -- deterministic, no wall-clock dependency.
  - tenant_id: `"tenant-1"` for all requests unless a test states
    otherwise.
  - No fixture seed is required; nothing in this suite is randomized.

PII NOTE: per the dev_prompt's PII constraint, this suite carries only
eviction-ranking and config-surface mechanics -- pseudonymized item_id
values, MemoryScore floats, boolean/enum flags, timestamps -- never a
Procedure's `steps` payload, anywhere below. Zone 4 carries no DPDP
erasure acceptance criterion (dev_prompt PII note), so this suite has no
DPDP-cascade scenario, unlike its Zone 2 analogue.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from dashanan.application.zone4_capacity_backstop_sweep import (
    BackstopSweepResult,
    EvictionOutcome,
    Zone4BackstopPortError,
    Zone4CapacityBackstopSweep,
)
from dashanan.domain.zone4_capacity_backstop import (
    EvictionDecision,
    EvictionReason,
    ProcedureEvictionCandidate,
    Zone4BackstopConfig,
    Zone4CapacityBackstopError,
    plan_backstop_eviction,
    select_capacity_overflow,
    select_max_age_exceeded,
)

_FIXED_TS = datetime(2026, 1, 1, tzinfo=UTC)
_TENANT = "tenant-1"


class FakeClock:
    """Deterministic Clock double: fixed instant, injectable (testing-core DI)."""

    def __init__(self, fixed: datetime = _FIXED_TS) -> None:
        self._fixed = fixed

    def now(self) -> datetime:
        return self._fixed


class RecordingEventBus:
    """EventBus double: records every publish() call, never raises."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, object]]] = []

    def publish(self, event_type: str, payload: dict[str, object]) -> None:
        self.published.append((event_type, dict(payload)))


class FakeConfigPort:
    """Zone4BackstopConfigPort double: returns configs in sequence, records calls."""

    def __init__(self, configs: list[Zone4BackstopConfig]) -> None:
        self._configs = list(configs)
        self.calls: list[str] = []
        self._call_index = 0

    def load(self, tenant_id: str) -> Zone4BackstopConfig:
        self.calls.append(tenant_id)
        config = self._configs[min(self._call_index, len(self._configs) - 1)]
        self._call_index += 1
        return config


class RaisingConfigPort:
    """Zone4BackstopConfigPort double that always raises."""

    def load(self, tenant_id: str) -> Zone4BackstopConfig:
        raise Zone4BackstopPortError("config store unreachable")


class FakeCandidateSource:
    """Zone4CandidateSource double: returns a fixed candidate list, records calls."""

    def __init__(self, candidates: list[ProcedureEvictionCandidate]) -> None:
        self._candidates = candidates
        self.calls: list[tuple[str, datetime]] = []

    def fetch_candidates(
        self, tenant_id: str, as_of: datetime
    ) -> list[ProcedureEvictionCandidate]:
        self.calls.append((tenant_id, as_of))
        return list(self._candidates)


class RaisingCandidateSource:
    """Zone4CandidateSource double that always raises."""

    def fetch_candidates(
        self, tenant_id: str, as_of: datetime
    ) -> list[ProcedureEvictionCandidate]:
        raise Zone4BackstopPortError("procedural repository unreachable")


class FakeEvictionPort:
    """Zone4EvictionPort double: records evict() calls, can raise for specific items."""

    def __init__(self, raise_for: frozenset[str] = frozenset()) -> None:
        self.evicted: list[tuple[str, str]] = []
        self._raise_for = raise_for

    def evict(self, tenant_id: str, item_id: str) -> None:
        if item_id in self._raise_for:
            raise Zone4BackstopPortError(f"store unreachable for {item_id}")
        self.evicted.append((tenant_id, item_id))


class FakeRetrievalIndexEviction:
    """Zone4RetrievalIndexEvictionPort double: records delete_item() calls, can raise."""

    def __init__(self, raise_for: frozenset[str] = frozenset()) -> None:
        self.deleted: list[tuple[str, str]] = []
        self._raise_for = raise_for

    def delete_item(self, tenant_id: str, item_id: str) -> None:
        if item_id in self._raise_for:
            raise Zone4BackstopPortError(f"retrieval index unreachable for {item_id}")
        self.deleted.append((tenant_id, item_id))


def _candidate(
    item_id: str,
    memory_score: float = 0.5,
    is_compressed: bool = False,
    age_seconds: float = 0.0,
    last_used_at: datetime = _FIXED_TS,
) -> ProcedureEvictionCandidate:
    return ProcedureEvictionCandidate(
        item_id=item_id,
        memory_score=memory_score,
        is_compressed=is_compressed,
        age_seconds=age_seconds,
        last_used_at=last_used_at,
    )


def _default_config(
    cap: int = 50_000, max_age_seconds: float = 730 * 86400
) -> Zone4BackstopConfig:
    return Zone4BackstopConfig(capacity_cap=cap, max_age_seconds=max_age_seconds)


def _make_sweep(
    *,
    configs: list[Zone4BackstopConfig] | None = None,
    candidates: list[ProcedureEvictionCandidate] | None = None,
    eviction_port: FakeEvictionPort | None = None,
    retrieval_index_eviction: FakeRetrievalIndexEviction | None = None,
    event_bus: RecordingEventBus | None = None,
    clock: FakeClock | None = None,
    config_port: object | None = None,
    candidate_source: object | None = None,
) -> tuple[
    Zone4CapacityBackstopSweep,
    object,
    object,
    FakeEvictionPort,
    FakeRetrievalIndexEviction,
    RecordingEventBus,
]:
    cfg_port = (
        config_port
        if config_port is not None
        else FakeConfigPort(configs if configs is not None else [_default_config()])
    )
    cand_source = (
        candidate_source
        if candidate_source is not None
        else FakeCandidateSource(candidates if candidates is not None else [])
    )
    eviction = eviction_port if eviction_port is not None else FakeEvictionPort()
    retrieval_index = (
        retrieval_index_eviction
        if retrieval_index_eviction is not None
        else FakeRetrievalIndexEviction()
    )
    bus = event_bus if event_bus is not None else RecordingEventBus()
    sweep = Zone4CapacityBackstopSweep(
        config_port=cfg_port,
        candidate_source=cand_source,
        eviction_port=eviction,
        retrieval_index_eviction=retrieval_index,
        event_bus=bus,
        clock=clock if clock is not None else FakeClock(),
    )
    return sweep, cfg_port, cand_source, eviction, retrieval_index, bus


class TestPackageWiring:
    """Baseline: the sweep and its ports construct and are importable."""

    def test_sweep_constructs_with_fake_ports(self) -> None:
        sweep, *_ = _make_sweep()
        assert sweep is not None

    def test_zone4backstopconfig_constructs_with_hld_12a_defaults(self) -> None:
        config = Zone4BackstopConfig(capacity_cap=50_000, max_age_seconds=730 * 86400)
        assert config.capacity_cap == 50_000
        assert config.max_age_seconds == pytest.approx(730 * 86400)

    def test_zone4backstopconfig_rejects_non_positive_cap(self) -> None:
        with pytest.raises(Zone4CapacityBackstopError):
            Zone4BackstopConfig(capacity_cap=0, max_age_seconds=1.0)

    def test_zone4backstopconfig_rejects_non_positive_max_age(self) -> None:
        with pytest.raises(Zone4CapacityBackstopError):
            Zone4BackstopConfig(capacity_cap=1, max_age_seconds=0.0)

    def test_procedure_eviction_candidate_rejects_blank_item_id(self) -> None:
        with pytest.raises(Zone4CapacityBackstopError):
            ProcedureEvictionCandidate(
                item_id="   ",
                memory_score=0.5,
                is_compressed=False,
                age_seconds=0.0,
                last_used_at=_FIXED_TS,
            )

    def test_procedure_eviction_candidate_rejects_out_of_range_score(self) -> None:
        with pytest.raises(Zone4CapacityBackstopError):
            _candidate("bad-score", memory_score=1.5)

    def test_procedure_eviction_candidate_rejects_negative_age(self) -> None:
        with pytest.raises(Zone4CapacityBackstopError):
            _candidate("bad-age", age_seconds=-1.0)


class TestAC004CAP1DomainSelectionFunctions:
    """AC-004-CAP-1 at the domain layer: selection functions this sweep builds on."""

    def test_select_capacity_overflow_picks_lowest_scored_excess(self) -> None:
        candidates = [
            _candidate("a", memory_score=0.9),
            _candidate("b", memory_score=0.1),
            _candidate("c", memory_score=0.5),
        ]

        selected = select_capacity_overflow(candidates, cap=2)

        assert [c.item_id for c in selected] == ["b"]

    def test_select_capacity_overflow_returns_empty_when_at_cap_boundary(self) -> None:
        candidates = [_candidate("a"), _candidate("b")]

        selected = select_capacity_overflow(candidates, cap=2)

        assert selected == []

    def test_select_capacity_overflow_returns_empty_when_under_cap(self) -> None:
        candidates = [_candidate("a")]

        selected = select_capacity_overflow(candidates, cap=50_000)

        assert selected == []

    def test_select_max_age_exceeded_requires_compressed_state(self) -> None:
        candidates = [
            _candidate("active-old", is_compressed=False, age_seconds=999_999),
            _candidate("compressed-old", is_compressed=True, age_seconds=999_999),
        ]

        selected = select_max_age_exceeded(candidates, max_age_seconds=100.0)

        assert [c.item_id for c in selected] == ["compressed-old"]

    def test_select_max_age_exceeded_boundary_exactly_at_max_age_is_not_evicted(
        self,
    ) -> None:
        candidates = [_candidate("boundary", is_compressed=True, age_seconds=100.0)]

        selected = select_max_age_exceeded(candidates, max_age_seconds=100.0)

        assert selected == []

    def test_select_max_age_exceeded_one_second_over_boundary_is_evicted(self) -> None:
        candidates = [_candidate("just-over", is_compressed=True, age_seconds=100.001)]

        selected = select_max_age_exceeded(candidates, max_age_seconds=100.0)

        assert [c.item_id for c in selected] == ["just-over"]

    def test_plan_backstop_eviction_combines_both_triggers_lowest_score_first(
        self,
    ) -> None:
        candidates = [
            _candidate("high-score-over-cap", memory_score=0.9),
            _candidate("low-score-over-cap", memory_score=0.1),
            _candidate(
                "maxage-compressed", memory_score=0.99, is_compressed=True, age_seconds=999
            ),
        ]
        config = Zone4BackstopConfig(capacity_cap=2, max_age_seconds=100.0)

        decisions = plan_backstop_eviction(candidates, config)

        assert [d.item_id for d in decisions] == [
            "low-score-over-cap",
            "maxage-compressed",
        ]

    def test_plan_backstop_eviction_item_matching_both_triggers_tags_max_age(
        self,
    ) -> None:
        candidates = [
            _candidate("both", memory_score=0.01, is_compressed=True, age_seconds=999),
            _candidate("kept", memory_score=0.9),
        ]
        config = Zone4BackstopConfig(capacity_cap=1, max_age_seconds=100.0)

        decisions = plan_backstop_eviction(candidates, config)

        assert decisions == [
            EvictionDecision(item_id="both", reason=EvictionReason.MAX_AGE)
        ]

    def test_plan_backstop_eviction_tie_break_older_last_used_at_evicted_first(
        self,
    ) -> None:
        older = _candidate(
            "older", memory_score=0.5, last_used_at=_FIXED_TS - timedelta(days=1)
        )
        newer = _candidate("newer", memory_score=0.5, last_used_at=_FIXED_TS)
        config = Zone4BackstopConfig(capacity_cap=1, max_age_seconds=999999.0)

        decisions = plan_backstop_eviction([older, newer], config)

        assert [d.item_id for d in decisions] == ["older"]

    def test_plan_backstop_eviction_no_candidates_produces_no_decisions(self) -> None:
        config = _default_config()

        decisions = plan_backstop_eviction([], config)

        assert decisions == []


class TestMustNotDeviateNoArchivedOutcome:
    """Must-not-deviate item 3: no Archived transition/outcome anywhere (HLD 12F)."""

    def test_eviction_reason_has_exactly_two_members(self) -> None:
        assert set(EvictionReason) == {EvictionReason.CAPACITY, EvictionReason.MAX_AGE}

    def test_eviction_reason_has_no_archived_member(self) -> None:
        assert not hasattr(EvictionReason, "ARCHIVED")
        assert "archived" not in {r.value for r in EvictionReason}

    def test_sweep_exposes_no_archive_method(self) -> None:
        sweep, *_ = _make_sweep()
        assert not hasattr(sweep, "archive")

    def test_backstop_sweep_result_has_no_archived_field(self) -> None:
        result = BackstopSweepResult(tenant_id=_TENANT, evicted=(), failed=())
        assert not hasattr(result, "archived")


class TestAC004CAP1SweepCapacityAndMaxAgeTriggers:
    """AC-004-CAP-1 at the application layer: the sweep executes the plan end to end."""

    def test_capacity_overflow_evicts_lowest_scored_item(self) -> None:
        candidates = [
            _candidate("keep-high", memory_score=0.9),
            _candidate("evict-low", memory_score=0.1),
        ]
        sweep, _cfg, _src, eviction, retrieval_index, bus = _make_sweep(
            configs=[_default_config(cap=1)], candidates=candidates
        )

        result = sweep.run(_TENANT)

        assert result.evicted == (
            EvictionOutcome(item_id="evict-low", reason=EvictionReason.CAPACITY),
        )
        assert eviction.evicted == [(_TENANT, "evict-low")]
        assert retrieval_index.deleted == [(_TENANT, "evict-low")]
        assert len(bus.published) == 1
        event_type, payload = bus.published[0]
        assert event_type == "memory.evicted"
        assert payload["zone"] == "procedural"
        assert payload["reason"] == "capacity"
        assert payload["item_id"] == "evict-low"
        assert payload["tenant_id"] == _TENANT

    def test_max_age_exceeded_evicts_compressed_item(self) -> None:
        candidates = [
            _candidate(
                "stale-compressed",
                is_compressed=True,
                age_seconds=731 * 86400,
            )
        ]
        sweep, _cfg, _src, eviction, _retrieval_index, _bus = _make_sweep(
            candidates=candidates
        )

        result = sweep.run(_TENANT)

        assert [o.item_id for o in result.evicted] == ["stale-compressed"]
        assert result.evicted[0].reason == EvictionReason.MAX_AGE
        assert eviction.evicted == [(_TENANT, "stale-compressed")]

    def test_result_accounts_for_every_decision_evicted_or_failed(self) -> None:
        # Both items are MaxAge-eligible (compressed, past max_age), so both
        # are selected for eviction regardless of score -- a valid,
        # positive capacity_cap (must-not-deviate: cap/max_age are always
        # positive, never 0) leaves capacity out of play here.
        candidates = [
            _candidate(
                "good", memory_score=0.1, is_compressed=True, age_seconds=731 * 86400
            ),
            _candidate(
                "bad", memory_score=0.05, is_compressed=True, age_seconds=731 * 86400
            ),
        ]
        eviction = FakeEvictionPort(raise_for=frozenset({"bad"}))
        sweep, *_ = _make_sweep(candidates=candidates, eviction_port=eviction)

        result = sweep.run(_TENANT)

        # "bad" always fails; "good" always succeeds -- order depends on score.
        evicted_ids = {o.item_id for o in result.evicted}
        failed_ids = {f.item_id for f in result.failed}
        assert evicted_ids | failed_ids == {"good", "bad"}
        assert evicted_ids & failed_ids == set()
        assert "bad" in failed_ids
        assert "good" in evicted_ids

    def test_retrieval_index_failure_leaves_zone4_item_untouched(self) -> None:
        """Safer-failure-mode ordering: Zone 6 first, so a Zone-6 failure never orphans Zone 4."""
        candidates = [
            _candidate(
                "evict-me",
                memory_score=0.1,
                is_compressed=True,
                age_seconds=731 * 86400,
            )
        ]
        retrieval_index = FakeRetrievalIndexEviction(raise_for=frozenset({"evict-me"}))
        eviction = FakeEvictionPort()
        sweep, *_ = _make_sweep(
            candidates=candidates,
            eviction_port=eviction,
            retrieval_index_eviction=retrieval_index,
        )

        result = sweep.run(_TENANT)

        assert result.evicted == ()
        assert [f.item_id for f in result.failed] == ["evict-me"]
        assert eviction.evicted == []


class TestAC004CAP4DeploymentConfigNoCodeChange:
    """AC-004-CAP-4: cap/MaxAge are deployment config, read fresh, no code change to take effect."""

    def test_config_is_loaded_fresh_on_every_run_call(self) -> None:
        candidates = [_candidate("item-1", memory_score=0.5)]
        config_port = FakeConfigPort(
            [
                Zone4BackstopConfig(capacity_cap=1, max_age_seconds=1.0),
                Zone4BackstopConfig(capacity_cap=100, max_age_seconds=1.0),
            ]
        )
        sweep, *_ = _make_sweep(config_port=config_port, candidates=candidates)

        first = sweep.run(_TENANT)
        second = sweep.run(_TENANT)

        assert len(config_port.calls) == 2
        # First run: cap=1 with a single item -- no overflow, so nothing evicted
        # via capacity (item is not compressed, so no MaxAge trigger either).
        assert first.evicted == ()
        # Second run's tightened-then-loosened config is still read fresh --
        # no cached config value survives across calls.
        assert second.evicted == ()

    def test_changed_cap_takes_effect_on_next_run_without_a_code_change(self) -> None:
        candidates = [
            _candidate("a", memory_score=0.9),
            _candidate("b", memory_score=0.1),
        ]
        config_port = FakeConfigPort(
            [
                Zone4BackstopConfig(capacity_cap=2, max_age_seconds=999999.0),
                Zone4BackstopConfig(capacity_cap=1, max_age_seconds=999999.0),
            ]
        )
        sweep, *_ = _make_sweep(config_port=config_port, candidates=candidates)

        first = sweep.run(_TENANT)
        second = sweep.run(_TENANT)

        assert first.evicted == ()
        assert [o.item_id for o in second.evicted] == ["b"]

    def test_sweep_never_hardcodes_a_cap_or_max_age_literal(self) -> None:
        """Structural check: the sweep has no cap/max_age attribute of its own."""
        sweep, *_ = _make_sweep()
        public_attrs = [a for a in vars(sweep) if not a.startswith("_")]
        assert public_attrs == []


class TestMustNotDeviateWholeSweepPreconditionFailures:
    """`config_port.load`/`candidate_source.fetch_candidates` failures abort the whole run."""

    def test_config_load_failure_propagates_not_recorded_as_per_item_failure(
        self,
    ) -> None:
        sweep, *_ = _make_sweep(config_port=RaisingConfigPort())

        with pytest.raises(Zone4BackstopPortError):
            sweep.run(_TENANT)

    def test_fetch_candidates_failure_propagates_not_recorded_as_per_item_failure(
        self,
    ) -> None:
        sweep, *_ = _make_sweep(candidate_source=RaisingCandidateSource())

        with pytest.raises(Zone4BackstopPortError):
            sweep.run(_TENANT)


class TestRunInputValidation:
    def test_blank_tenant_id_raises_value_error(self) -> None:
        sweep, *_ = _make_sweep()

        with pytest.raises(ValueError, match="tenant_id"):
            sweep.run("   ")
