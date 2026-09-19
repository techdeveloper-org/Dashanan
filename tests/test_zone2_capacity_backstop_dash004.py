"""Test suite for DASH-STORY-004: Zone 2 capacity cap / MaxAge backstop (OAQ-4).

Traces to FR-002 in SRS.md. FR-002 (verbatim): "The system SHALL provide
an Episodic Memory zone that stores chronological interaction records
with recency-based decay, and SHALL retrieve them via keyset (cursor)
pagination or direct primary-key lookup."

Covers, by AC ID:
  - AC-002-CAP-1: capacity-cap and MaxAge-while-Compressed triggers,
    lowest-MemoryScore-first eviction ordering, boundary and combined-
    trigger cases.
  - AC-002-CAP-2: cap/MaxAge are deployment config, read fresh per sweep
    run, with no code change needed for an operator's config update to
    take effect on the next run.
  - AC-002-CAP-DPDP-1 (REDACTED, no PII examples): forced eviction
    satisfies, never bypasses, a pending DPDP erasure obligation, and
    never leaves an item partially erased (Zone 2 row gone but the
    cascade not run, or vice versa).

Also covers the must-not-deviate items verbatim from ar1_assignments.json
AR1-004 (see each TestMustNotDeviate* class below), and boundary/negative
cases per rules 33/40's roadmap (TTL/MaxAge exactly at the boundary,
cap exactly at threshold, per-item operational-failure isolation).

Runtime assumptions recorded per rule 33/40 test-roadmap conventions:
  - clock: FakeClock, fixed at 2026-01-01T00:00:00+00:00 unless a test
    states otherwise -- deterministic, no wall-clock dependency.
  - tenant_id: "tenant-1" for all requests unless a test states
    otherwise.
  - No fixture seed is required; nothing in this suite is randomized.

PII NOTE: per the dev_prompt's PII constraint, this suite carries only
eviction-ranking and config-surface mechanics -- pseudonymized item_id
values, MemoryScore floats, boolean/enum flags, timestamps -- never zone
payload/fact content, real or synthetic-realistic, and never a
DPDP-erasure example that could read as real PII content, anywhere below.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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
    """Zone2BackstopConfigPort double: returns configs in sequence, records calls."""

    def __init__(self, configs: list[Zone2BackstopConfig]) -> None:
        self._configs = list(configs)
        self.calls: list[str] = []
        self._call_index = 0

    def load(self, tenant_id: str) -> Zone2BackstopConfig:
        self.calls.append(tenant_id)
        config = self._configs[min(self._call_index, len(self._configs) - 1)]
        self._call_index += 1
        return config


class RaisingConfigPort:
    """Zone2BackstopConfigPort double that always raises."""

    def load(self, tenant_id: str) -> Zone2BackstopConfig:
        raise Zone2BackstopPortError("config store unreachable")


class FakeCandidateSource:
    """Zone2CandidateSource double: returns a fixed candidate list, records calls."""

    def __init__(self, candidates: list[EvictionCandidate]) -> None:
        self._candidates = candidates
        self.calls: list[tuple[str, datetime]] = []

    def fetch_candidates(
        self, tenant_id: str, as_of: datetime
    ) -> list[EvictionCandidate]:
        self.calls.append((tenant_id, as_of))
        return list(self._candidates)


class RaisingCandidateSource:
    """Zone2CandidateSource double that always raises."""

    def fetch_candidates(
        self, tenant_id: str, as_of: datetime
    ) -> list[EvictionCandidate]:
        raise Zone2BackstopPortError("episodic repository unreachable")


class FakeEvictionPort:
    """Zone2EvictionPort double: records evict() calls, can raise for specific items."""

    def __init__(self, raise_for: frozenset[str] = frozenset()) -> None:
        self.evicted: list[tuple[str, str]] = []
        self._raise_for = raise_for

    def evict(self, tenant_id: str, item_id: str) -> None:
        if item_id in self._raise_for:
            raise Zone2BackstopPortError(f"store unreachable for {item_id}")
        self.evicted.append((tenant_id, item_id))


class FakeRetrievalIndexEviction:
    """RetrievalIndexEvictionPort double: records delete_item() calls, can raise for specific items.

    P1 remediation (DSHN-58, attempt 1) test double -- exercises the new
    port `Zone2CapacityBackstopSweep._execute_one` now calls
    unconditionally for every eviction decision.
    """

    def __init__(self, raise_for: frozenset[str] = frozenset()) -> None:
        self.deleted: list[tuple[str, str]] = []
        self._raise_for = raise_for

    def delete_item(self, tenant_id: str, item_id: str) -> None:
        if item_id in self._raise_for:
            raise Zone2BackstopPortError(f"retrieval index unreachable for {item_id}")
        self.deleted.append((tenant_id, item_id))


class FakeDpdpPort:
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
            raise Zone2BackstopPortError(f"erasure cascade unreachable for {item_id}")
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


def _default_config(cap: int = 100_000, max_age_seconds: float = 180 * 86400) -> Zone2BackstopConfig:
    return Zone2BackstopConfig(capacity_cap=cap, max_age_seconds=max_age_seconds)


def _make_sweep(
    *,
    configs: list[Zone2BackstopConfig] | None = None,
    candidates: list[EvictionCandidate] | None = None,
    eviction_port: FakeEvictionPort | None = None,
    dpdp_port: FakeDpdpPort | None = None,
    retrieval_index_eviction: FakeRetrievalIndexEviction | None = None,
    event_bus: RecordingEventBus | None = None,
    clock: FakeClock | None = None,
) -> tuple[Zone2CapacityBackstopSweep, FakeConfigPort, FakeCandidateSource, FakeEvictionPort, FakeDpdpPort, RecordingEventBus]:
    config_port = FakeConfigPort(configs if configs is not None else [_default_config()])
    candidate_source = FakeCandidateSource(candidates if candidates is not None else [])
    eviction = eviction_port if eviction_port is not None else FakeEvictionPort()
    dpdp = dpdp_port if dpdp_port is not None else FakeDpdpPort()
    retrieval_index = (
        retrieval_index_eviction
        if retrieval_index_eviction is not None
        else FakeRetrievalIndexEviction()
    )
    bus = event_bus if event_bus is not None else RecordingEventBus()
    sweep = Zone2CapacityBackstopSweep(
        config_port=config_port,
        candidate_source=candidate_source,
        eviction_port=eviction,
        dpdp_port=dpdp,
        retrieval_index_eviction=retrieval_index,
        event_bus=bus,
        clock=clock if clock is not None else FakeClock(),
    )
    return sweep, config_port, candidate_source, eviction, dpdp, bus


class TestPackageWiring:
    """Baseline: the sweep and its ports construct and are importable."""

    def test_sweep_constructs_with_fake_ports(self) -> None:
        sweep, *_ = _make_sweep()
        assert sweep is not None

    def test_zone2backstopconfig_constructs_with_valid_values(self) -> None:
        config = Zone2BackstopConfig(capacity_cap=100_000, max_age_seconds=180 * 86400)
        assert config.capacity_cap == 100_000


class TestAC002CAP1DomainSelectionFunctions:
    """AC-002-CAP-1 at the domain layer: selection functions this sweep builds on."""

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

        selected = select_capacity_overflow(candidates, cap=100_000)

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
            _candidate("maxage-compressed", memory_score=0.99, is_compressed=True, age_seconds=999),
        ]
        config = Zone2BackstopConfig(capacity_cap=2, max_age_seconds=100.0)

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
        config = Zone2BackstopConfig(capacity_cap=1, max_age_seconds=100.0)

        decisions = plan_backstop_eviction(candidates, config)

        assert decisions == [EvictionDecision(item_id="both", reason=EvictionReason.MAX_AGE)]

    def test_plan_backstop_eviction_tie_break_older_written_at_evicted_first(
        self,
    ) -> None:
        older = _candidate(
            "older", memory_score=0.5, written_at=_FIXED_TS - timedelta(days=1)
        )
        newer = _candidate("newer", memory_score=0.5, written_at=_FIXED_TS)
        config = Zone2BackstopConfig(capacity_cap=1, max_age_seconds=999999.0)

        decisions = plan_backstop_eviction([older, newer], config)

        assert [d.item_id for d in decisions] == ["older"]


class TestMustNotDeviateItem3NoArchivedTransition:
    """Must-not-deviate item 3: no Archived transition attempt anywhere (HLD 12F)."""

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


class TestAC002CAP1SweepCapacityAndMaxAgeTriggers:
    """AC-002-CAP-1 at the application layer: the sweep executes the plan end to end."""

    def test_capacity_overflow_evicts_lowest_scored_item(self) -> None:
        candidates = [
            _candidate("keep-high", memory_score=0.9),
            _candidate("evict-low", memory_score=0.1),
        ]
        sweep, _cfg, _src, eviction, dpdp, bus = _make_sweep(
            configs=[_default_config(cap=1)], candidates=candidates
        )

        result = sweep.run(_TENANT)

        assert [o.item_id for o in result.evicted] == ["evict-low"]
        assert result.evicted[0].reason is EvictionReason.CAPACITY
        assert eviction.evicted == [(_TENANT, "evict-low")]
        assert bus.published[0][0] == "memory.evicted"
        assert bus.published[0][1]["reason"] == "capacity"

    def test_max_age_while_compressed_is_forcibly_evicted(self) -> None:
        candidates = [
            _candidate("aged-compressed", is_compressed=True, age_seconds=200 * 86400)
        ]
        sweep, *_r, eviction, dpdp, bus = _make_sweep(
            configs=[_default_config(cap=100_000, max_age_seconds=180 * 86400)],
            candidates=candidates,
        )

        result = sweep.run(_TENANT)

        assert [o.item_id for o in result.evicted] == ["aged-compressed"]
        assert result.evicted[0].reason is EvictionReason.MAX_AGE
        assert eviction.evicted == [(_TENANT, "aged-compressed")]

    def test_zone_under_cap_and_no_stale_items_evicts_nothing(self) -> None:
        candidates = [_candidate("fresh-and-under-cap", memory_score=0.5)]
        sweep, *_r, eviction, dpdp, bus = _make_sweep(candidates=candidates)

        result = sweep.run(_TENANT)

        assert result.evicted == ()
        assert result.failed == ()
        assert eviction.evicted == []
        assert bus.published == []

    def test_empty_zone_evicts_nothing(self) -> None:
        sweep, *_r, eviction, dpdp, bus = _make_sweep(candidates=[])

        result = sweep.run(_TENANT)

        assert result == BackstopSweepResult(tenant_id=_TENANT, evicted=(), failed=())
        assert bus.published == []


class TestAC002CAP2ConfigReadFreshPerSweepRun:
    """AC-002-CAP-2: cap/MaxAge are deployment config, used by the NEXT run with no code change."""

    def test_config_port_load_is_called_once_per_run(self) -> None:
        sweep, config_port, *_r = _make_sweep(candidates=[_candidate("x")])

        sweep.run(_TENANT)
        sweep.run(_TENANT)

        assert config_port.calls == [_TENANT, _TENANT]

    def test_operator_widened_max_age_between_runs_changes_next_run_outcome(self) -> None:
        candidates = [
            _candidate("only-item", is_compressed=True, age_seconds=100.0)
        ]
        narrow_max_age = _default_config(cap=100_000, max_age_seconds=50.0)
        widened_max_age = _default_config(cap=100_000, max_age_seconds=100_000.0)
        sweep, config_port, _src, eviction, _dpdp, _bus = _make_sweep(
            configs=[narrow_max_age, widened_max_age], candidates=candidates
        )

        first = sweep.run(_TENANT)
        second = sweep.run(_TENANT)

        assert [o.item_id for o in first.evicted] == ["only-item"]
        assert second.evicted == ()
        assert len(config_port.calls) == 2

    def test_no_config_value_is_ever_hardcoded_in_the_sweep_object(self) -> None:
        sweep, *_r = _make_sweep()
        assert not hasattr(sweep, "_capacity_cap")
        assert not hasattr(sweep, "_max_age_seconds")

    def test_whole_sweep_aborts_if_config_load_fails(self) -> None:
        sweep = Zone2CapacityBackstopSweep(
            config_port=RaisingConfigPort(),
            candidate_source=FakeCandidateSource([]),
            eviction_port=FakeEvictionPort(),
            dpdp_port=FakeDpdpPort(),
            retrieval_index_eviction=FakeRetrievalIndexEviction(),
            event_bus=RecordingEventBus(),
            clock=FakeClock(),
        )

        with pytest.raises(Zone2BackstopPortError):
            sweep.run(_TENANT)


class TestAC002CAPDPDP1SatisfyNeverBypassErasure:
    """AC-002-CAP-DPDP-1 (REDACTED, no PII examples): satisfy, never bypass, erasure."""

    def test_item_with_pending_erasure_routes_through_dpdp_cascade_only(self) -> None:
        candidates = [
            _candidate("subject-item", is_compressed=True, age_seconds=999.0)
        ]
        dpdp = FakeDpdpPort(pending=frozenset({"subject-item"}))
        eviction = FakeEvictionPort()
        sweep, *_r, ev_port, dpdp_port, bus = _make_sweep(
            configs=[_default_config(cap=100_000, max_age_seconds=100.0)],
            candidates=candidates,
            eviction_port=eviction,
            dpdp_port=dpdp,
        )

        result = sweep.run(_TENANT)

        assert result.evicted[0].via_dpdp_cascade is True
        assert dpdp.fulfilled == [(_TENANT, "subject-item")]
        assert eviction.evicted == [], (
            "an item with a pending erasure obligation must NEVER also go "
            "through the ordinary evict() path -- that would be the exact "
            "bypass AC-002-CAP-DPDP-1 forbids"
        )

    def test_item_without_pending_erasure_uses_ordinary_eviction(self) -> None:
        candidates = [
            _candidate("no-obligation-item", is_compressed=True, age_seconds=999.0)
        ]
        dpdp = FakeDpdpPort(pending=frozenset())
        eviction = FakeEvictionPort()
        sweep, *_r, ev_port, dpdp_port, bus = _make_sweep(
            configs=[_default_config(cap=100_000, max_age_seconds=100.0)],
            candidates=candidates,
            eviction_port=eviction,
            dpdp_port=dpdp,
        )

        result = sweep.run(_TENANT)

        assert result.evicted[0].via_dpdp_cascade is False
        assert dpdp.calls == [(_TENANT, "no-obligation-item")], (
            "the DPDP obligation check must still run for every eviction "
            "target, even when it turns out there is nothing pending"
        )
        assert eviction.evicted == [(_TENANT, "no-obligation-item")]

    def test_dpdp_cascade_failure_never_falls_back_to_ordinary_eviction(self) -> None:
        candidates = [
            _candidate("cascade-fails", is_compressed=True, age_seconds=999.0)
        ]
        dpdp = FakeDpdpPort(raise_for=frozenset({"cascade-fails"}))
        eviction = FakeEvictionPort()
        sweep, *_r, ev_port, dpdp_port, bus = _make_sweep(
            configs=[_default_config(cap=100_000, max_age_seconds=100.0)],
            candidates=candidates,
            eviction_port=eviction,
            dpdp_port=dpdp,
        )

        result = sweep.run(_TENANT)

        assert result.evicted == ()
        assert result.failed == (
            EvictionFailure(
                item_id="cascade-fails",
                reason=EvictionReason.MAX_AGE,
                error="erasure cascade unreachable for cascade-fails",
            ),
        )
        assert eviction.evicted == [], (
            "a DPDP cascade failure must never silently fall back to the "
            "ordinary evict() path -- that is a bypass, not a satisfy"
        )
        assert bus.published == [], "no memory.evicted event for a failed eviction"

    def test_mixed_batch_routes_each_item_independently(self) -> None:
        candidates = [
            _candidate("pending-a", is_compressed=True, age_seconds=999.0),
            _candidate("ordinary-b", is_compressed=True, age_seconds=999.0),
        ]
        dpdp = FakeDpdpPort(pending=frozenset({"pending-a"}))
        eviction = FakeEvictionPort()
        sweep, *_r, ev_port, dpdp_port, bus = _make_sweep(
            configs=[_default_config(cap=100_000, max_age_seconds=100.0)],
            candidates=candidates,
            eviction_port=eviction,
            dpdp_port=dpdp,
        )

        result = sweep.run(_TENANT)

        by_id = {o.item_id: o for o in result.evicted}
        assert by_id["pending-a"].via_dpdp_cascade is True
        assert by_id["ordinary-b"].via_dpdp_cascade is False
        assert eviction.evicted == [(_TENANT, "ordinary-b")]
        assert dpdp.fulfilled == [(_TENANT, "pending-a")]


class TestMustNotDeviateItem2EvictionOrderingAtSweepLayer:
    """Must-not-deviate item 2: the sweep preserves lowest-MemoryScore-first ordering."""

    def test_events_are_published_in_lowest_score_first_order(self) -> None:
        candidates = [
            _candidate("mid", memory_score=0.3, is_compressed=True, age_seconds=999.0),
            _candidate("lowest", memory_score=0.1, is_compressed=True, age_seconds=999.0),
            _candidate("higher", memory_score=0.5, is_compressed=True, age_seconds=999.0),
        ]
        sweep, *_r, eviction, dpdp, bus = _make_sweep(
            configs=[_default_config(cap=100_000, max_age_seconds=100.0)],
            candidates=candidates,
        )

        result = sweep.run(_TENANT)

        assert [o.item_id for o in result.evicted] == ["lowest", "mid", "higher"]
        assert [payload["item_id"] for _etype, payload in bus.published] == [
            "lowest",
            "mid",
            "higher",
        ]
        assert eviction.evicted == [
            (_TENANT, "lowest"),
            (_TENANT, "mid"),
            (_TENANT, "higher"),
        ]


class TestPerItemFailureIsolation:
    """A single item's operational failure must not block the rest of the sweep."""

    def test_one_failing_item_does_not_block_others(self) -> None:
        candidates = [
            _candidate("fails", is_compressed=True, age_seconds=999.0),
            _candidate("succeeds", is_compressed=True, age_seconds=999.0),
        ]
        eviction = FakeEvictionPort(raise_for=frozenset({"fails"}))
        sweep, *_r, ev_port, dpdp_port, bus = _make_sweep(
            configs=[_default_config(cap=100_000, max_age_seconds=100.0)],
            candidates=candidates,
            eviction_port=eviction,
        )

        result = sweep.run(_TENANT)

        assert [o.item_id for o in result.evicted] == ["succeeds"]
        assert [f.item_id for f in result.failed] == ["fails"]
        assert eviction.evicted == [(_TENANT, "succeeds")]
        assert len(bus.published) == 1
        assert bus.published[0][1]["item_id"] == "succeeds"

    def test_whole_sweep_aborts_if_candidate_fetch_fails(self) -> None:
        sweep = Zone2CapacityBackstopSweep(
            config_port=FakeConfigPort([_default_config()]),
            candidate_source=RaisingCandidateSource(),
            eviction_port=FakeEvictionPort(),
            dpdp_port=FakeDpdpPort(),
            retrieval_index_eviction=FakeRetrievalIndexEviction(),
            event_bus=RecordingEventBus(),
            clock=FakeClock(),
        )

        with pytest.raises(Zone2BackstopPortError):
            sweep.run(_TENANT)


class TestBoundaryAndNegativeCases:
    """Boundary/negative cases per rule 33/40 test-roadmap conventions."""

    def test_run_rejects_blank_tenant_id(self) -> None:
        sweep, *_r = _make_sweep()

        with pytest.raises(ValueError, match="tenant_id"):
            sweep.run("   ")

    def test_zone2backstopconfig_rejects_zero_capacity_cap(self) -> None:
        with pytest.raises(Zone2CapacityBackstopError, match="capacity_cap"):
            Zone2BackstopConfig(capacity_cap=0, max_age_seconds=100.0)

    def test_zone2backstopconfig_rejects_negative_capacity_cap(self) -> None:
        with pytest.raises(Zone2CapacityBackstopError, match="capacity_cap"):
            Zone2BackstopConfig(capacity_cap=-1, max_age_seconds=100.0)

    def test_zone2backstopconfig_rejects_zero_max_age(self) -> None:
        with pytest.raises(Zone2CapacityBackstopError, match="max_age_seconds"):
            Zone2BackstopConfig(capacity_cap=1, max_age_seconds=0.0)

    def test_eviction_candidate_rejects_blank_item_id(self) -> None:
        with pytest.raises(Zone2CapacityBackstopError, match="item_id"):
            _candidate("   ")

    def test_eviction_candidate_rejects_out_of_range_score(self) -> None:
        with pytest.raises(Zone2CapacityBackstopError, match="memory_score"):
            _candidate("x", memory_score=1.5)

    def test_eviction_candidate_rejects_negative_age(self) -> None:
        with pytest.raises(Zone2CapacityBackstopError, match="age_seconds"):
            _candidate("x", age_seconds=-1.0)

    def test_capacity_overflow_by_exactly_one_evicts_exactly_one(self) -> None:
        candidates = [_candidate("a", memory_score=0.9), _candidate("b", memory_score=0.1)]

        selected = select_capacity_overflow(candidates, cap=1)

        assert len(selected) == 1
        assert selected[0].item_id == "b"


class TestRetrievalIndexEvictionClosesPartialErasureFinding:
    """P1 remediation (DSHN-58, attempt 1): every eviction also cleans up Zone 6.

    Prior to this remediation, `Zone2EvictionPort.evict`'s Zone-2-only
    contract meant the ORDINARY (non-DPDP) eviction path never touched
    Zone 6 -- routine capacity/MaxAge evictions, the common case, left
    Zone 6's own payload copy of the item fully retrievable via search
    indefinitely. These tests exercise `RetrievalIndexEvictionPort` being
    called for both the ordinary and DPDP-cascade branches.
    """

    def test_ordinary_eviction_also_removes_item_from_retrieval_index(self) -> None:
        candidates = [
            _candidate("keep-high", memory_score=0.9),
            _candidate("ordinary-item", memory_score=0.1),
        ]
        retrieval_index = FakeRetrievalIndexEviction()
        sweep, *_r, eviction, dpdp, bus = _make_sweep(
            configs=[_default_config(cap=1)],
            candidates=candidates,
            retrieval_index_eviction=retrieval_index,
        )

        result = sweep.run(_TENANT)

        assert [o.item_id for o in result.evicted] == ["ordinary-item"]
        assert retrieval_index.deleted == [(_TENANT, "ordinary-item")], (
            "an ordinary (non-DPDP) eviction must ALSO remove the item from "
            "Zone 6's retrieval index -- Zone2EvictionPort.evict alone is "
            "Zone-2-only and previously left Zone 6 retrievable indefinitely"
        )

    def test_dpdp_cascade_eviction_also_calls_retrieval_index_eviction(self) -> None:
        candidates = [
            _candidate("pending-item", is_compressed=True, age_seconds=999.0)
        ]
        dpdp = FakeDpdpPort(pending=frozenset({"pending-item"}))
        retrieval_index = FakeRetrievalIndexEviction()
        sweep, *_r, eviction, dpdp_port, bus = _make_sweep(
            configs=[_default_config(cap=100_000, max_age_seconds=100.0)],
            candidates=candidates,
            dpdp_port=dpdp,
            retrieval_index_eviction=retrieval_index,
        )

        result = sweep.run(_TENANT)

        assert result.evicted[0].via_dpdp_cascade is True
        assert retrieval_index.deleted == [(_TENANT, "pending-item")], (
            "the DPDP branch must also trigger RetrievalIndexEvictionPort "
            "as a defense-in-depth cleanup, even though a real "
            "DpdpErasureCascadePort adapter already performs its own "
            "Zone-6 leg internally (idempotent, so this is a safe no-op)"
        )

    def test_retrieval_index_failure_is_isolated_as_a_per_item_failure(self) -> None:
        candidates = [
            _candidate("keep-highest", memory_score=0.9),
            _candidate("index-fails", memory_score=0.1),
            _candidate("index-succeeds", memory_score=0.2),
        ]
        retrieval_index = FakeRetrievalIndexEviction(raise_for=frozenset({"index-fails"}))
        sweep, *_r, eviction, dpdp, bus = _make_sweep(
            configs=[_default_config(cap=1)],
            candidates=candidates,
            retrieval_index_eviction=retrieval_index,
        )

        result = sweep.run(_TENANT)

        assert [o.item_id for o in result.evicted] == ["index-succeeds"]
        assert [f.item_id for f in result.failed] == ["index-fails"]
        assert retrieval_index.deleted == [(_TENANT, "index-succeeds")]
        assert eviction.evicted == [(_TENANT, "index-succeeds")], (
            "Zone 6 removal must run BEFORE Zone2EvictionPort.evict on the "
            "ordinary path -- if Zone 6 removal fails, Zone 2 must be left "
            "untouched so the failure is a clean, fully-retryable per-item "
            "failure rather than a Zone-2-gone-but-reported-failed state"
        )
