"""Test suite for DASH-STORY-010: RotationDeadlineInvalidationService.

ADR-012 deadline-invalidation wiring on non-Recency term mutation, traced
to FR-012 in SRS.md. FR-012 (verbatim): "The system SHALL compute a
composite Memory Score for every item from six weighted terms (Recency,
Frequency, Importance, UserAffinity, TaskRelevance, ProvenanceConfidence),
each normalized to [0,1], and SHALL use this score to drive zone lifecycle
transitions."

Covers, by AC ID:
  - AC-012-INVAL-1: "Given a non-Recency MemoryScore term (Frequency,
    ProvenanceConfidence, or Importance) is mutated via access, conflict
    detection, or operator override, when the mutation occurs, then the
    item's stored rotation deadline is invalidated and re-inserted in
    O(log N) on all three mutation paths, so no rotation decision is ever
    made against a stale deadline."

Also covers every must-not-deviate item verbatim from ar1_assignments.json
AR1-010 (see each `TestMustNotDeviate*` class below), plus boundary and
adverse cases per rules 33/40's roadmap (a pinned item never reaching the
wheel, repeated mutation on the same item, all three entry points
independently exercised).

Runtime assumptions (rule 33/40 test-roadmap conventions):
  - clock: `FakeClock`, fixed at `2026-01-01T00:00:00+00:00` unless a test
    states otherwise -- deterministic, no wall-clock dependency (mirrors
    test_rotation_engine_dash009.py's identical convention).
  - tenant_id / item_id: `"item-1"`, `"item-2"` where a second item is
    needed to distinguish per-item wheel entries.
  - No fixture seed is required; nothing in this suite is randomized.

PII NOTE: per the dev_prompt's PII constraint, this suite carries only
scheduling and score-recomputation metadata -- pseudonymized `item_id`
values, `MutatedTerm` enum members, zone identifiers, timestamps -- never
zone payload/fact content, conflict text, or override rationale, anywhere
below.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from dashanan.application.rotation_deadline_invalidation import (
    _ENTRY_POINT_DESCRIPTION_BY_TERM,
    RotationDeadlineInvalidationService,
)
from dashanan.application.rotation_engine import (
    RotationCandidateSnapshot,
    RotationCompressionResult,
    RotationEngine,
    RotationEnginePortError,
)
from dashanan.domain.rotation_invalidation_trigger import MutatedTerm
from dashanan.domain.rotation_state import RotationState
from dashanan.domain.rotation_zone_policy import RotationZonePolicy
from dashanan.domain.zone import ZoneId

_FIXED_TS = datetime(2026, 1, 1, tzinfo=UTC)
_EPISODIC_T_HALF_SECONDS = 172_800.0
"""HLD Section 12A: Zone 2 Episodic `t_half = 2 days`."""
_EPISODIC_MAX_AGE_SECONDS = 1.5552e7
"""HLD Section 12A: Zone 2 Episodic `MaxAge = 180 days`."""
_COMPRESS_THRESHOLD = 0.35
"""HLD Section 12A FINALIZED `CompressThreshold`."""
_W_RECENCY = 1.0 / 6.0
"""`ScoreWeights.default().w_recency` (HLD Section 12A MVP: 1/6 each)."""


def _episodic_policy() -> RotationZonePolicy:
    return RotationZonePolicy.for_zone(
        t_half_seconds=_EPISODIC_T_HALF_SECONDS,
        compress_threshold=_COMPRESS_THRESHOLD,
        max_age_seconds=_EPISODIC_MAX_AGE_SECONDS,
    )


class FakeClock:
    """Deterministic Clock double: fixed instant (testing-core DI), mirrors
    test_rotation_engine_dash009.py's identical fixture."""

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


class FakeCandidatePort:
    """RotationCandidatePort double: unused by this story's entry points
    (schedule_or_pin never calls it) -- present only so `RotationEngine`'s
    constructor is satisfied, mirrors test_rotation_engine_dash009.py."""

    def current_state(
        self, tenant_id: str, zone_id: ZoneId, item_id: str
    ) -> RotationCandidateSnapshot:
        raise RotationEnginePortError(
            "FakeCandidatePort.current_state should never be called by "
            "schedule_or_pin -- this story never re-implements run_sweep"
        )


class FakeTransitionPort:
    """RotationTransitionPort double: unused by this story's entry points
    for the identical reason as FakeCandidatePort."""

    def compress(
        self, tenant_id: str, zone_id: ZoneId, item_id: str
    ) -> RotationCompressionResult:
        raise RotationEnginePortError(
            "FakeTransitionPort.compress should never be called by "
            "schedule_or_pin -- this story never re-implements run_sweep"
        )


class SpyRotationEngine:
    """Wraps a real `RotationEngine`, counting `schedule_or_pin` calls.

    Delegates every call to the wrapped real engine unchanged (never
    re-implements DASH-STORY-009's logic) -- purely an observation point
    for the O(log N) / "exactly one delegated call" must-not-deviate
    assertions below.
    """

    def __init__(self, engine: RotationEngine) -> None:
        self._engine = engine
        self.schedule_or_pin_calls: list[tuple[ZoneId, str]] = []
        self.run_sweep_calls = 0

    def schedule_or_pin(self, zone_id, item_id, **kwargs):
        self.schedule_or_pin_calls.append((zone_id, item_id))
        return self._engine.schedule_or_pin(zone_id=zone_id, item_id=item_id, **kwargs)

    def run_sweep(self, *args, **kwargs):
        self.run_sweep_calls += 1
        return self._engine.run_sweep(*args, **kwargs)


def _make_real_engine() -> RotationEngine:
    return RotationEngine(
        transition_port=FakeTransitionPort(),
        candidate_port=FakeCandidatePort(),
        event_bus=RecordingEventBus(),
        clock=FakeClock(),
    )


def _make_service() -> tuple[RotationDeadlineInvalidationService, RotationEngine]:
    engine = _make_real_engine()
    return RotationDeadlineInvalidationService(engine), engine


# A c_weighted value that, combined with `_W_RECENCY`, keeps
# `u = (theta - c_weighted) / w_recency` strictly between `u_floor` and
# `1.0` for the episodic policy above (`u = (0.35 - 0.25) / (1/6) = 0.6`)
# -- i.e. a genuinely SCHEDULED, future (never immediate, never pinned)
# case, per `dashanan.domain.rotation_deadline.compute_rotation_deadline`'s
# three branches. `delta_t* = -ln(0.6)/lambda_zone ~= 1.47 days`, well
# inside the episodic zone's 180-day MaxAge.
_SCHEDULING_C_WEIGHTED = 0.25
# A c_weighted value low enough that `u = (0.35 - 0.10) / (1/6) = 1.5 >=
# 1.0` -- the item has already crossed CompressThreshold at `dt=0`
# (`w_recency*1 + c_weighted = 0.2667 <= theta`), so `delta_t` clamps to
# `0` and the outcome is SCHEDULED immediately (`due_at == now`).
_IMMEDIATE_C_WEIGHTED = 0.10
# A c_weighted value at/above `theta` on its own (`0.40 > 0.35`), so even
# Recency=1 at `dt=0` cannot lift `u` above `u_floor` -- `u` is negative,
# the item's floor never crosses CompressThreshold by decay alone, and
# `compute_rotation_deadline` returns `None` (PINNED).
_PINNED_C_WEIGHTED = 0.40


class TestAc012Inval1AllThreeEntryPoints:
    """AC-012-INVAL-1: every one of the three named mutation paths
    invalidates and re-inserts the item's stored rotation deadline."""

    def test_on_access_schedules_a_deadline(self) -> None:
        service, _ = _make_service()

        outcome = service.on_access(
            zone_id=ZoneId.EPISODIC,
            item_id="item-1",
            policy=_episodic_policy(),
            w_recency=_W_RECENCY,
            c_weighted=_SCHEDULING_C_WEIGHTED,
            max_age_remaining_seconds=_EPISODIC_MAX_AGE_SECONDS,
            cooldown_remaining_seconds=0.0,
        )

        assert outcome.scheduled is True
        assert outcome.item_id == "item-1"
        assert outcome.due_at is not None

    def test_on_conflict_detected_schedules_a_deadline(self) -> None:
        service, _ = _make_service()

        outcome = service.on_conflict_detected(
            zone_id=ZoneId.EPISODIC,
            item_id="item-1",
            policy=_episodic_policy(),
            w_recency=_W_RECENCY,
            c_weighted=_SCHEDULING_C_WEIGHTED,
            max_age_remaining_seconds=_EPISODIC_MAX_AGE_SECONDS,
            cooldown_remaining_seconds=0.0,
        )

        assert outcome.scheduled is True
        assert outcome.item_id == "item-1"
        assert outcome.due_at is not None

    def test_on_operator_override_schedules_a_deadline(self) -> None:
        service, _ = _make_service()

        outcome = service.on_operator_override(
            zone_id=ZoneId.EPISODIC,
            item_id="item-1",
            policy=_episodic_policy(),
            w_recency=_W_RECENCY,
            c_weighted=_SCHEDULING_C_WEIGHTED,
            max_age_remaining_seconds=_EPISODIC_MAX_AGE_SECONDS,
            cooldown_remaining_seconds=0.0,
        )

        assert outcome.scheduled is True
        assert outcome.item_id == "item-1"
        assert outcome.due_at is not None

    def test_on_access_immediate_boundary_still_schedules(self) -> None:
        """`u >= 1.0` boundary: delta_t clamps to 0, outcome is still SCHEDULED."""
        service, _ = _make_service()

        outcome = service.on_access(
            zone_id=ZoneId.EPISODIC,
            item_id="item-1",
            policy=_episodic_policy(),
            w_recency=_W_RECENCY,
            c_weighted=_IMMEDIATE_C_WEIGHTED,
            max_age_remaining_seconds=_EPISODIC_MAX_AGE_SECONDS,
            cooldown_remaining_seconds=0.0,
        )

        assert outcome.scheduled is True
        assert outcome.due_at == _FIXED_TS

    def test_on_access_pinned_boundary_never_schedules(self) -> None:
        """`u <= u_floor` boundary: item is PINNED, never inserted into the wheel."""
        service, _ = _make_service()

        outcome = service.on_access(
            zone_id=ZoneId.EPISODIC,
            item_id="item-1",
            policy=_episodic_policy(),
            w_recency=_W_RECENCY,
            c_weighted=_PINNED_C_WEIGHTED,
            max_age_remaining_seconds=_EPISODIC_MAX_AGE_SECONDS,
            cooldown_remaining_seconds=0.0,
        )

        assert outcome.scheduled is False
        assert outcome.due_at is None


class TestMustNotDeviateAllThreeMutationPathsWired:
    """Must-not-deviate: 'ALL THREE mutation paths must be wired, not a
    subset (HLD 12C: "must be wired into every term-mutation path")'."""

    def test_entry_point_description_map_covers_every_mutated_term(self) -> None:
        assert frozenset(_ENTRY_POINT_DESCRIPTION_BY_TERM) == frozenset(MutatedTerm)

    def test_entry_point_description_map_matches_hld_12c_wording(self) -> None:
        assert _ENTRY_POINT_DESCRIPTION_BY_TERM[MutatedTerm.FREQUENCY] == "access"
        assert (
            _ENTRY_POINT_DESCRIPTION_BY_TERM[MutatedTerm.PROVENANCE_CONFIDENCE]
            == "conflict detection"
        )
        assert (
            _ENTRY_POINT_DESCRIPTION_BY_TERM[MutatedTerm.IMPORTANCE]
            == "operator override"
        )

    def test_service_exposes_a_public_method_per_mutated_term(self) -> None:
        service, _ = _make_service()
        method_by_term = {
            MutatedTerm.FREQUENCY: service.on_access,
            MutatedTerm.PROVENANCE_CONFIDENCE: service.on_conflict_detected,
            MutatedTerm.IMPORTANCE: service.on_operator_override,
        }
        assert frozenset(method_by_term) == frozenset(MutatedTerm)
        for method in method_by_term.values():
            assert callable(method)

    @pytest.mark.parametrize(
        "entry_point_name",
        ["on_access", "on_conflict_detected", "on_operator_override"],
    )
    def test_every_entry_point_independently_reaches_the_wheel(
        self, entry_point_name: str
    ) -> None:
        service, engine = _make_service()
        entry_point = getattr(service, entry_point_name)

        entry_point(
            zone_id=ZoneId.EPISODIC,
            item_id="item-1",
            policy=_episodic_policy(),
            w_recency=_W_RECENCY,
            c_weighted=_SCHEDULING_C_WEIGHTED,
            max_age_remaining_seconds=_EPISODIC_MAX_AGE_SECONDS,
            cooldown_remaining_seconds=0.0,
        )

        wheel = engine._wheel_for(ZoneId.EPISODIC)
        assert wheel.size == 1


class TestMustNotDeviateOLogNReinsert:
    """Must-not-deviate: 'O(log N) re-insert bound on every deadline update'."""

    @pytest.mark.parametrize(
        "entry_point_name",
        ["on_access", "on_conflict_detected", "on_operator_override"],
    )
    def test_each_entry_point_makes_exactly_one_schedule_or_pin_call(
        self, entry_point_name: str
    ) -> None:
        engine = _make_real_engine()
        spy = SpyRotationEngine(engine)
        service = RotationDeadlineInvalidationService(spy)
        entry_point = getattr(service, entry_point_name)

        entry_point(
            zone_id=ZoneId.EPISODIC,
            item_id="item-1",
            policy=_episodic_policy(),
            w_recency=_W_RECENCY,
            c_weighted=_SCHEDULING_C_WEIGHTED,
            max_age_remaining_seconds=_EPISODIC_MAX_AGE_SECONDS,
            cooldown_remaining_seconds=0.0,
        )

        assert len(spy.schedule_or_pin_calls) == 1
        assert spy.schedule_or_pin_calls[0] == (ZoneId.EPISODIC, "item-1")

    def test_never_calls_run_sweep(self) -> None:
        """Must-not-deviate: 'Do not re-implement the sweep itself -- that
        is DASH-STORY-009's scope, not yours'."""
        engine = _make_real_engine()
        spy = SpyRotationEngine(engine)
        service = RotationDeadlineInvalidationService(spy)

        service.on_access(
            zone_id=ZoneId.EPISODIC,
            item_id="item-1",
            policy=_episodic_policy(),
            w_recency=_W_RECENCY,
            c_weighted=_SCHEDULING_C_WEIGHTED,
            max_age_remaining_seconds=_EPISODIC_MAX_AGE_SECONDS,
            cooldown_remaining_seconds=0.0,
        )
        service.on_conflict_detected(
            zone_id=ZoneId.EPISODIC,
            item_id="item-1",
            policy=_episodic_policy(),
            w_recency=_W_RECENCY,
            c_weighted=_SCHEDULING_C_WEIGHTED,
            max_age_remaining_seconds=_EPISODIC_MAX_AGE_SECONDS,
            cooldown_remaining_seconds=0.0,
        )
        service.on_operator_override(
            zone_id=ZoneId.EPISODIC,
            item_id="item-1",
            policy=_episodic_policy(),
            w_recency=_W_RECENCY,
            c_weighted=_SCHEDULING_C_WEIGHTED,
            max_age_remaining_seconds=_EPISODIC_MAX_AGE_SECONDS,
            cooldown_remaining_seconds=0.0,
        )

        assert spy.run_sweep_calls == 0

    def test_source_never_calls_run_sweep_or_pop_due(self) -> None:
        """Structural guard (must-not-deviate item 4), independent of the
        spy above: the module's CODE never invokes the sweep internals it
        must not re-implement. (Docstrings above may still name them in
        prose, e.g. to explain what this module deliberately does not do
        -- this check looks for an actual call/attribute-access pattern,
        not a prose mention.)"""
        import inspect

        from dashanan.application import rotation_deadline_invalidation as module

        source = inspect.getsource(module)
        assert ".run_sweep(" not in source
        assert ".pop_due(" not in source
        assert ".guarded_transition(" not in source
        assert "import guarded_transition" not in source


class TestMustNotDeviateNoStaleDeadlineWindow:
    """Must-not-deviate: 'No rotation decision may ever act on a stale
    deadline (AC-012-INVAL-1)'."""

    def test_repeated_mutation_on_same_item_leaves_exactly_one_live_entry(
        self,
    ) -> None:
        """Two mutations on the same item_id must never leave two live
        wheel entries -- the second call's invalidate-and-reinsert must
        orphan the first (RotationTimerWheel's lazy-deletion contract),
        never merely append alongside it."""
        service, engine = _make_service()

        service.on_access(
            zone_id=ZoneId.EPISODIC,
            item_id="item-1",
            policy=_episodic_policy(),
            w_recency=_W_RECENCY,
            c_weighted=_SCHEDULING_C_WEIGHTED,
            max_age_remaining_seconds=_EPISODIC_MAX_AGE_SECONDS,
            cooldown_remaining_seconds=0.0,
        )
        first_wheel_size = engine._wheel_for(ZoneId.EPISODIC).size
        assert first_wheel_size == 1

        service.on_conflict_detected(
            zone_id=ZoneId.EPISODIC,
            item_id="item-1",
            policy=_episodic_policy(),
            w_recency=_W_RECENCY,
            c_weighted=_IMMEDIATE_C_WEIGHTED,
            max_age_remaining_seconds=_EPISODIC_MAX_AGE_SECONDS,
            cooldown_remaining_seconds=0.0,
        )

        wheel = engine._wheel_for(ZoneId.EPISODIC)
        assert wheel.size == 1, (
            "a second mutation on the same item_id must invalidate-and-"
            "reinsert, never leave a second, stale live entry"
        )

    def test_repeated_mutation_pop_due_returns_only_the_latest_deadline(
        self,
    ) -> None:
        """Popping the wheel after two mutations on the same item must
        return exactly one entry, carrying the LATEST due_at -- proof
        that no rotation decision (the sweep's pop_due) can ever observe
        the stale, pre-mutation deadline."""
        service, engine = _make_service()

        service.on_access(
            zone_id=ZoneId.EPISODIC,
            item_id="item-1",
            policy=_episodic_policy(),
            w_recency=_W_RECENCY,
            c_weighted=_SCHEDULING_C_WEIGHTED,
            max_age_remaining_seconds=_EPISODIC_MAX_AGE_SECONDS,
            cooldown_remaining_seconds=0.0,
        )
        second_outcome = service.on_operator_override(
            zone_id=ZoneId.EPISODIC,
            item_id="item-1",
            policy=_episodic_policy(),
            w_recency=_W_RECENCY,
            c_weighted=_IMMEDIATE_C_WEIGHTED,
            max_age_remaining_seconds=_EPISODIC_MAX_AGE_SECONDS,
            cooldown_remaining_seconds=0.0,
        )

        wheel = engine._wheel_for(ZoneId.EPISODIC)
        due = wheel.pop_due(second_outcome.due_at)

        assert len(due) == 1
        assert due[0].item_id == "item-1"
        assert due[0].due_at == second_outcome.due_at

    def test_pin_after_schedule_removes_the_item_from_the_wheel(self) -> None:
        """A mutation that pins an already-scheduled item must invalidate
        its live entry -- the sweep must never observe a deadline that is
        stale because the item's score has since become unreachable."""
        service, engine = _make_service()

        service.on_access(
            zone_id=ZoneId.EPISODIC,
            item_id="item-1",
            policy=_episodic_policy(),
            w_recency=_W_RECENCY,
            c_weighted=_SCHEDULING_C_WEIGHTED,
            max_age_remaining_seconds=_EPISODIC_MAX_AGE_SECONDS,
            cooldown_remaining_seconds=0.0,
        )
        assert engine._wheel_for(ZoneId.EPISODIC).size == 1

        service.on_conflict_detected(
            zone_id=ZoneId.EPISODIC,
            item_id="item-1",
            policy=_episodic_policy(),
            w_recency=_W_RECENCY,
            c_weighted=_PINNED_C_WEIGHTED,
            max_age_remaining_seconds=_EPISODIC_MAX_AGE_SECONDS,
            cooldown_remaining_seconds=0.0,
        )

        assert engine._wheel_for(ZoneId.EPISODIC).size == 0

    def test_two_different_items_each_keep_their_own_live_entry(self) -> None:
        """Adverse case: mutating one item must never invalidate another
        item's live deadline (per-item scoping, not zone-wide)."""
        service, engine = _make_service()

        service.on_access(
            zone_id=ZoneId.EPISODIC,
            item_id="item-1",
            policy=_episodic_policy(),
            w_recency=_W_RECENCY,
            c_weighted=_SCHEDULING_C_WEIGHTED,
            max_age_remaining_seconds=_EPISODIC_MAX_AGE_SECONDS,
            cooldown_remaining_seconds=0.0,
        )
        service.on_access(
            zone_id=ZoneId.EPISODIC,
            item_id="item-2",
            policy=_episodic_policy(),
            w_recency=_W_RECENCY,
            c_weighted=_SCHEDULING_C_WEIGHTED,
            max_age_remaining_seconds=_EPISODIC_MAX_AGE_SECONDS,
            cooldown_remaining_seconds=0.0,
        )

        assert engine._wheel_for(ZoneId.EPISODIC).size == 2


class TestMustNotDeviateNoOwnershipViolation:
    """Must-not-deviate / dependency_integrity_check: no mutation outside
    this story's ownership -- `RotationCandidatePort`/`RotationTransitionPort`
    (DASH-STORY-009's ports) are never invoked by this story's wiring."""

    def test_candidate_port_never_invoked(self) -> None:
        service, _ = _make_service()

        service.on_access(
            zone_id=ZoneId.EPISODIC,
            item_id="item-1",
            policy=_episodic_policy(),
            w_recency=_W_RECENCY,
            c_weighted=_SCHEDULING_C_WEIGHTED,
            max_age_remaining_seconds=_EPISODIC_MAX_AGE_SECONDS,
            cooldown_remaining_seconds=0.0,
        )
        # FakeCandidatePort.current_state raises if ever called -- reaching
        # this line without a raised RotationEnginePortError is the proof.

    def test_transition_port_never_invoked(self) -> None:
        service, _ = _make_service()

        service.on_conflict_detected(
            zone_id=ZoneId.EPISODIC,
            item_id="item-1",
            policy=_episodic_policy(),
            w_recency=_W_RECENCY,
            c_weighted=_SCHEDULING_C_WEIGHTED,
            max_age_remaining_seconds=_EPISODIC_MAX_AGE_SECONDS,
            cooldown_remaining_seconds=0.0,
        )
        # FakeTransitionPort.compress raises if ever called -- reaching
        # this line without a raised RotationEnginePortError is the proof.

    def test_no_event_is_published_by_a_mutation_call(self) -> None:
        """schedule_or_pin (must-not-deviate item 4's own scope) never
        publishes `memory.compressed` -- only `run_sweep` does; this
        story's wiring must not accidentally trigger a zone-transition
        event as a side effect of a term mutation."""
        engine = _make_real_engine()
        service = RotationDeadlineInvalidationService(engine)
        bus: RecordingEventBus = engine._event_bus  # type: ignore[assignment]

        service.on_operator_override(
            zone_id=ZoneId.EPISODIC,
            item_id="item-1",
            policy=_episodic_policy(),
            w_recency=_W_RECENCY,
            c_weighted=_SCHEDULING_C_WEIGHTED,
            max_age_remaining_seconds=_EPISODIC_MAX_AGE_SECONDS,
            cooldown_remaining_seconds=0.0,
        )

        assert bus.published == []
