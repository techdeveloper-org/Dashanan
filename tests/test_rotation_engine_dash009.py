"""Test suite for DASH-STORY-009: RotationEngine (Active -> Compressed, HLD Rotation Engine).

Traces to FR-012 in SRS.md. FR-012 (verbatim): "The system SHALL compute a
composite Memory Score for every item from six weighted terms (Recency,
Frequency, Importance, UserAffinity, TaskRelevance, ProvenanceConfidence),
each normalized to [0,1], and SHALL use this score to drive zone lifecycle
transitions."

Covers, by AC ID:
  - AC-012: an item crossing CompressThreshold transitions to Compressed
    and stays retrievable; it SHALL NOT reach Archived without passing
    through Compressed.
  - AC-012-ROT-1: an item that would cross ArchiveThreshold instead stays
    in Compressed indefinitely -- no Archived attempt with no
    destination.

Also covers every must-not-deviate item verbatim from ar1_assignments.json
AR1-009 (see each `TestMustNotDeviate*` class below), plus boundary and
adverse cases per rules 33/40's roadmap (a stale re-scored item, a
concurrently-promoted item, per-item port-failure isolation, malformed
`tenant_id`).

Runtime assumptions (rule 33/40 test-roadmap conventions):
  - clock: `FakeClock`, fixed at `2026-01-01T00:00:00+00:00` unless a
    test states otherwise -- deterministic, no wall-clock dependency.
  - tenant_id: `"tenant-1"` for all requests unless a test states
    otherwise.
  - No fixture seed is required; nothing in this suite is randomized.

PII NOTE: per the dev_prompt's PII constraint, this suite carries only
scheduling and state-machine control metadata -- pseudonymized `item_id`
values, `RotationState` enum members, `MemoryScore` floats, timestamps --
never zone payload/fact content, anywhere below.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from dashanan.application.rotation_engine import (
    RotationCandidateSnapshot,
    RotationCompressionResult,
    RotationEngine,
    RotationEnginePortError,
    RotationSweepResult,
)
from dashanan.domain.rotation_state import RotationState
from dashanan.domain.rotation_zone_policy import RotationZonePolicy
from dashanan.domain.zone import ZoneId

_FIXED_TS = datetime(2026, 1, 1, tzinfo=UTC)
_TENANT = "tenant-1"
_EPISODIC_T_HALF_SECONDS = 172_800.0
"""HLD Section 12A: Zone 2 Episodic `t_half = 2 days`."""
_EPISODIC_MAX_AGE_SECONDS = 1.5552e7
"""HLD Section 12A: Zone 2 Episodic `MaxAge = 180 days`."""
_COMPRESS_THRESHOLD = 0.35
"""HLD Section 12A FINALIZED `CompressThreshold`."""


def _episodic_policy() -> RotationZonePolicy:
    return RotationZonePolicy.for_zone(
        t_half_seconds=_EPISODIC_T_HALF_SECONDS,
        compress_threshold=_COMPRESS_THRESHOLD,
        max_age_seconds=_EPISODIC_MAX_AGE_SECONDS,
    )


class FakeClock:
    """Deterministic, mutable Clock double: fixed instant, advanceable, injectable (testing-core DI)."""

    def __init__(self, fixed: datetime = _FIXED_TS) -> None:
        self._fixed = fixed

    def now(self) -> datetime:
        return self._fixed

    def advance_to(self, new_time: datetime) -> None:
        """Move this clock's `now()` forward (or backward) for the next call."""
        self._fixed = new_time


class RecordingEventBus:
    """EventBus double: records every publish() call, never raises."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, object]]] = []

    def publish(self, event_type: str, payload: dict[str, object]) -> None:
        self.published.append((event_type, dict(payload)))


class FakeCandidatePort:
    """RotationCandidatePort double: returns configured snapshots by `item_id`, records calls."""

    def __init__(
        self, snapshots: dict[str, RotationCandidateSnapshot] | None = None
    ) -> None:
        self._snapshots = dict(snapshots or {})
        self.calls: list[tuple[str, ZoneId, str]] = []

    def current_state(
        self, tenant_id: str, zone_id: ZoneId, item_id: str
    ) -> RotationCandidateSnapshot:
        self.calls.append((tenant_id, zone_id, item_id))
        return self._snapshots[item_id]


class RaisingCandidatePort:
    """RotationCandidatePort double that always raises `RotationEnginePortError`."""

    def current_state(
        self, tenant_id: str, zone_id: ZoneId, item_id: str
    ) -> RotationCandidateSnapshot:
        raise RotationEnginePortError("episodic repository unreachable")


class FakeTransitionPort:
    """RotationTransitionPort double: records compress() calls, can raise for specific items."""

    def __init__(self, raise_for: frozenset[str] = frozenset()) -> None:
        self.compressed: list[tuple[str, ZoneId, str]] = []
        self._raise_for = raise_for
        self._generation_by_item: dict[str, int] = {}

    def compress(
        self, tenant_id: str, zone_id: ZoneId, item_id: str
    ) -> RotationCompressionResult:
        if item_id in self._raise_for:
            raise RotationEnginePortError(f"store unreachable for {item_id}")
        self.compressed.append((tenant_id, zone_id, item_id))
        generation = self._generation_by_item.get(item_id, 0) + 1
        self._generation_by_item[item_id] = generation
        return RotationCompressionResult(
            generation=generation, original_tokens=100, compressed_tokens=20
        )


def _make_engine(
    candidate_port: object,
    transition_port: object,
    event_bus: object | None = None,
    clock: FakeClock | None = None,
) -> tuple[RotationEngine, RecordingEventBus, FakeClock]:
    bus = event_bus if event_bus is not None else RecordingEventBus()
    fake_clock = clock if clock is not None else FakeClock()
    engine = RotationEngine(
        transition_port=transition_port,
        candidate_port=candidate_port,
        event_bus=bus,
        clock=fake_clock,
    )
    return engine, bus, fake_clock


class TestAc012ActiveToCompressedTransition:
    """AC-012: an item crossing CompressThreshold transitions to Compressed and stays retrievable."""

    def test_due_active_item_below_threshold_transitions_to_compressed(self) -> None:
        candidate_port = FakeCandidatePort(
            {
                "item-1": RotationCandidateSnapshot(
                    lifecycle_state=RotationState.ACTIVE, memory_score=0.20
                )
            }
        )
        transition_port = FakeTransitionPort()
        engine, bus, clock = _make_engine(candidate_port, transition_port)
        engine.schedule_or_pin(
            zone_id=ZoneId.EPISODIC,
            item_id="item-1",
            policy=_episodic_policy(),
            w_recency=1.0 / 6.0,
            c_weighted=0.25,
            max_age_remaining_seconds=_EPISODIC_MAX_AGE_SECONDS,
            cooldown_remaining_seconds=0.0,
        )

        clock.advance_to(_FIXED_TS + timedelta(days=365))

        result = engine.run_sweep(_TENANT, ZoneId.EPISODIC, _episodic_policy())

        assert len(result.transitioned) == 1
        assert result.transitioned[0].item_id == "item-1"
        assert transition_port.compressed == [(_TENANT, ZoneId.EPISODIC, "item-1")]

    def test_transitioned_item_stays_retrievable_result_is_not_an_error(self) -> None:
        """"stays retrievable" -- the outcome is a normal Result-type success, never an exception."""
        candidate_port = FakeCandidatePort(
            {
                "item-1": RotationCandidateSnapshot(
                    lifecycle_state=RotationState.ACTIVE, memory_score=0.10
                )
            }
        )
        transition_port = FakeTransitionPort()
        engine, _, clock = _make_engine(candidate_port, transition_port)
        engine.schedule_or_pin(
            zone_id=ZoneId.EPISODIC,
            item_id="item-1",
            policy=_episodic_policy(),
            w_recency=1.0 / 6.0,
            c_weighted=0.20,
            max_age_remaining_seconds=_EPISODIC_MAX_AGE_SECONDS,
            cooldown_remaining_seconds=0.0,
        )
        clock.advance_to(_FIXED_TS + timedelta(days=365))

        result = engine.run_sweep(_TENANT, ZoneId.EPISODIC, _episodic_policy())

        assert isinstance(result, RotationSweepResult)
        assert result.failed == ()


class TestMustNotDeviateArchivedNeverAttempted:
    """Must-not-deviate items 1-2: "Guarded state machine... Archived structurally
    UNREACHABLE" and "No code path may attempt an Archived transition in
    Sprint 1; Zone 8 does not exist (AC-012-ROT-1, HLD 12F)".
    """

    def test_run_sweep_never_publishes_an_archived_event(self) -> None:
        candidate_port = FakeCandidatePort(
            {
                "item-1": RotationCandidateSnapshot(
                    lifecycle_state=RotationState.ACTIVE, memory_score=0.10
                )
            }
        )
        transition_port = FakeTransitionPort()
        engine, bus, clock = _make_engine(candidate_port, transition_port)
        engine.schedule_or_pin(
            zone_id=ZoneId.EPISODIC,
            item_id="item-1",
            policy=_episodic_policy(),
            w_recency=1.0 / 6.0,
            c_weighted=0.20,
            max_age_remaining_seconds=_EPISODIC_MAX_AGE_SECONDS,
            cooldown_remaining_seconds=0.0,
        )
        clock.advance_to(_FIXED_TS + timedelta(days=365))

        engine.run_sweep(_TENANT, ZoneId.EPISODIC, _episodic_policy())

        event_types = {event_type for event_type, _ in bus.published}
        assert "memory.archived" not in event_types
        assert event_types == {"memory.compressed"}

    def test_an_already_compressed_item_is_never_re_transitioned(self) -> None:
        """A due entry whose live state is already Compressed is skipped, not force-transitioned."""
        candidate_port = FakeCandidatePort(
            {
                "item-1": RotationCandidateSnapshot(
                    lifecycle_state=RotationState.COMPRESSED, memory_score=0.10
                )
            }
        )
        transition_port = FakeTransitionPort()
        engine, bus, clock = _make_engine(candidate_port, transition_port)
        engine.schedule_or_pin(
            zone_id=ZoneId.EPISODIC,
            item_id="item-1",
            policy=_episodic_policy(),
            w_recency=1.0 / 6.0,
            c_weighted=0.20,
            max_age_remaining_seconds=_EPISODIC_MAX_AGE_SECONDS,
            cooldown_remaining_seconds=0.0,
        )
        clock.advance_to(_FIXED_TS + timedelta(days=365))

        result = engine.run_sweep(_TENANT, ZoneId.EPISODIC, _episodic_policy())

        assert result.transitioned == ()
        assert len(result.skipped) == 1
        assert transition_port.compressed == []
        assert bus.published == []

    def test_rotation_zone_policy_has_no_archive_threshold_field(self) -> None:
        """Structural enforcement: no field anywhere this engine reads carries an ArchiveThreshold."""
        policy = _episodic_policy()
        assert not hasattr(policy, "archive_threshold")


class TestAc012Rot1ArchiveThresholdItemStaysCompressed:
    """AC-012-ROT-1: an item that would cross ArchiveThreshold stays in Compressed indefinitely."""

    def test_item_far_below_archive_threshold_still_only_reaches_compressed(
        self,
    ) -> None:
        """A score of 0.05 (well under both CompressThreshold=0.35 and a
        hypothetical ArchiveThreshold=0.25) still only ever reaches
        Compressed -- this engine has no second threshold to check.
        """
        candidate_port = FakeCandidatePort(
            {
                "item-1": RotationCandidateSnapshot(
                    lifecycle_state=RotationState.ACTIVE, memory_score=0.05
                )
            }
        )
        transition_port = FakeTransitionPort()
        engine, bus, clock = _make_engine(candidate_port, transition_port)
        engine.schedule_or_pin(
            zone_id=ZoneId.EPISODIC,
            item_id="item-1",
            policy=_episodic_policy(),
            w_recency=1.0 / 6.0,
            c_weighted=0.10,
            max_age_remaining_seconds=_EPISODIC_MAX_AGE_SECONDS,
            cooldown_remaining_seconds=0.0,
        )
        clock.advance_to(_FIXED_TS + timedelta(days=365))

        result = engine.run_sweep(_TENANT, ZoneId.EPISODIC, _episodic_policy())

        assert len(result.transitioned) == 1
        published_states = {
            payload["item_id"]: event_type for event_type, payload in bus.published
        }
        assert published_states == {"item-1": "memory.compressed"}


class TestMustNotDeviateOKLogNSweep:
    """Must-not-deviate item 3: "O(k log N) sweep, never an O(N) scan"."""

    def test_sweep_only_processes_due_items_not_the_full_scheduled_population(
        self,
    ) -> None:
        snapshots = {
            f"future-{i}": RotationCandidateSnapshot(
                lifecycle_state=RotationState.ACTIVE, memory_score=0.10
            )
            for i in range(200)
        }
        snapshots["due-1"] = RotationCandidateSnapshot(
            lifecycle_state=RotationState.ACTIVE, memory_score=0.10
        )
        candidate_port = FakeCandidatePort(snapshots)
        transition_port = FakeTransitionPort()
        engine, _, clock = _make_engine(candidate_port, transition_port)
        policy = _episodic_policy()

        for i in range(200):
            engine.schedule_or_pin(
                zone_id=ZoneId.EPISODIC,
                item_id=f"future-{i}",
                policy=policy,
                w_recency=1.0 / 6.0,
                c_weighted=0.349,
                max_age_remaining_seconds=_EPISODIC_MAX_AGE_SECONDS,
                cooldown_remaining_seconds=0.0,
            )
        engine.schedule_or_pin(
            zone_id=ZoneId.EPISODIC,
            item_id="due-1",
            policy=policy,
            w_recency=1.0 / 6.0,
            c_weighted=0.10,
            max_age_remaining_seconds=1.0,
            cooldown_remaining_seconds=0.0,
        )
        clock.advance_to(_FIXED_TS + timedelta(seconds=2))

        result = engine.run_sweep(_TENANT, ZoneId.EPISODIC, policy)

        assert len(candidate_port.calls) == 1
        assert candidate_port.calls[0][2] == "due-1"
        assert len(result.transitioned) == 1


class TestMustNotDeviatePublishEventNeverDirectReactor:
    """Must-not-deviate item 4: "Publish zone-transition events -- do not call
    reactors directly (Observer, HLD Section 6)".
    """

    def test_successful_transition_publishes_memory_compressed_via_event_bus(
        self,
    ) -> None:
        candidate_port = FakeCandidatePort(
            {
                "item-1": RotationCandidateSnapshot(
                    lifecycle_state=RotationState.ACTIVE, memory_score=0.10
                )
            }
        )
        transition_port = FakeTransitionPort()
        engine, bus, clock = _make_engine(candidate_port, transition_port)
        engine.schedule_or_pin(
            zone_id=ZoneId.EPISODIC,
            item_id="item-1",
            policy=_episodic_policy(),
            w_recency=1.0 / 6.0,
            c_weighted=0.20,
            max_age_remaining_seconds=_EPISODIC_MAX_AGE_SECONDS,
            cooldown_remaining_seconds=0.0,
        )
        clock.advance_to(_FIXED_TS + timedelta(days=365))

        engine.run_sweep(_TENANT, ZoneId.EPISODIC, _episodic_policy())

        assert len(bus.published) == 1
        event_type, payload = bus.published[0]
        assert event_type == "memory.compressed"
        assert payload["item_id"] == "item-1"
        assert payload["tenant_id"] == _TENANT
        assert payload["zone"] == ZoneId.EPISODIC.value
        assert payload["generation"] == 1

    def test_engine_holds_no_reference_to_any_concrete_reactor(self) -> None:
        """Structural check: RotationEngine's only observer-facing collaborator is the EventBus port."""
        candidate_port = FakeCandidatePort({})
        transition_port = FakeTransitionPort()
        engine, _, clock = _make_engine(candidate_port, transition_port)
        public_attrs = [a for a in vars(engine) if not a.startswith("_")]
        assert public_attrs == []


class TestMustNotDeviateCompressedDwellsIndefinitely:
    """Must-not-deviate item 5: "Compressed dwell is indefinite, bounded only by DASH-STORY-004's backstop"."""

    def test_transitioned_item_is_not_rescheduled_into_the_timer_wheel(self) -> None:
        candidate_port = FakeCandidatePort(
            {
                "item-1": RotationCandidateSnapshot(
                    lifecycle_state=RotationState.ACTIVE, memory_score=0.10
                )
            }
        )
        transition_port = FakeTransitionPort()
        engine, _, clock = _make_engine(candidate_port, transition_port)
        policy = _episodic_policy()
        engine.schedule_or_pin(
            zone_id=ZoneId.EPISODIC,
            item_id="item-1",
            policy=policy,
            w_recency=1.0 / 6.0,
            c_weighted=0.20,
            max_age_remaining_seconds=_EPISODIC_MAX_AGE_SECONDS,
            cooldown_remaining_seconds=0.0,
        )
        clock.advance_to(_FIXED_TS + timedelta(days=365))

        first_sweep = engine.run_sweep(_TENANT, ZoneId.EPISODIC, policy)
        second_sweep = engine.run_sweep(_TENANT, ZoneId.EPISODIC, policy)

        assert len(first_sweep.transitioned) == 1
        assert second_sweep.transitioned == ()
        assert second_sweep.skipped == ()
        assert second_sweep.failed == ()


class TestPinnedItemsAreNeverScheduled:
    """F5 (mathematics-engineer delegation): pinned is a hot path -- must never enter the wheel."""

    def test_pinned_item_produces_no_due_entry_ever(self) -> None:
        """Sprint 1 default profile (mathematics-engineer W2): C=2.30 -> pinned."""
        candidate_port = FakeCandidatePort({})
        transition_port = FakeTransitionPort()
        engine, bus, clock = _make_engine(candidate_port, transition_port)
        policy = _episodic_policy()

        outcome = engine.schedule_or_pin(
            zone_id=ZoneId.EPISODIC,
            item_id="pinned-item",
            policy=policy,
            w_recency=1.0 / 6.0,
            c_weighted=(0.30 + 0.50 + 0.80 + 0.50 + 0.20) / 6.0,
            max_age_remaining_seconds=_EPISODIC_MAX_AGE_SECONDS,
            cooldown_remaining_seconds=0.0,
        )
        assert outcome.scheduled is False
        assert outcome.due_at is None

        clock.advance_to(_FIXED_TS + timedelta(days=365))
        result = engine.run_sweep(_TENANT, ZoneId.EPISODIC, policy)

        assert result.transitioned == ()
        assert candidate_port.calls == []
        assert bus.published == []


class TestReScoreGuardDefendsAgainstStaleDeadlines:
    """The sweep's "re-score" step (Template Method) rejects a deadline stale for
    any reason not yet covered by DASH-STORY-010's invalidation wiring.
    """

    def test_item_re_scored_above_threshold_at_sweep_time_is_skipped_not_transitioned(
        self,
    ) -> None:
        candidate_port = FakeCandidatePort(
            {
                "item-1": RotationCandidateSnapshot(
                    lifecycle_state=RotationState.ACTIVE, memory_score=0.90
                )
            }
        )
        transition_port = FakeTransitionPort()
        engine, bus, clock = _make_engine(candidate_port, transition_port)
        engine.schedule_or_pin(
            zone_id=ZoneId.EPISODIC,
            item_id="item-1",
            policy=_episodic_policy(),
            w_recency=1.0 / 6.0,
            c_weighted=0.20,
            max_age_remaining_seconds=_EPISODIC_MAX_AGE_SECONDS,
            cooldown_remaining_seconds=0.0,
        )
        clock.advance_to(_FIXED_TS + timedelta(days=365))

        result = engine.run_sweep(_TENANT, ZoneId.EPISODIC, _episodic_policy())

        assert result.transitioned == ()
        assert len(result.skipped) == 1
        assert transition_port.compressed == []
        assert bus.published == []

    def test_concurrently_promoted_item_active_no_longer_true_is_skipped(
        self,
    ) -> None:
        """ADR-012 mechanism (b), read-triggered promotion, already moved the item out of Active."""
        candidate_port = FakeCandidatePort(
            {
                "item-1": RotationCandidateSnapshot(
                    lifecycle_state=RotationState.COMPRESSED, memory_score=0.10
                )
            }
        )
        transition_port = FakeTransitionPort()
        engine, _, clock = _make_engine(candidate_port, transition_port)
        engine.schedule_or_pin(
            zone_id=ZoneId.EPISODIC,
            item_id="item-1",
            policy=_episodic_policy(),
            w_recency=1.0 / 6.0,
            c_weighted=0.20,
            max_age_remaining_seconds=_EPISODIC_MAX_AGE_SECONDS,
            cooldown_remaining_seconds=0.0,
        )
        clock.advance_to(_FIXED_TS + timedelta(days=365))

        result = engine.run_sweep(_TENANT, ZoneId.EPISODIC, _episodic_policy())

        assert result.transitioned == ()
        assert len(result.skipped) == 1
        assert "compressed" in result.skipped[0].reason


class TestPerItemFailureIsolation:
    """A single port failure isolates to one item -- the rest of the tick still runs."""

    def test_candidate_port_failure_is_recorded_not_raised(self) -> None:
        transition_port = FakeTransitionPort()
        engine, _, clock = _make_engine(RaisingCandidatePort(), transition_port)
        engine.schedule_or_pin(
            zone_id=ZoneId.EPISODIC,
            item_id="item-1",
            policy=_episodic_policy(),
            w_recency=1.0 / 6.0,
            c_weighted=0.20,
            max_age_remaining_seconds=_EPISODIC_MAX_AGE_SECONDS,
            cooldown_remaining_seconds=0.0,
        )
        clock.advance_to(_FIXED_TS + timedelta(days=365))

        result = engine.run_sweep(_TENANT, ZoneId.EPISODIC, _episodic_policy())

        assert result.transitioned == ()
        assert len(result.failed) == 1
        assert result.failed[0].item_id == "item-1"

    def test_one_failing_item_does_not_block_a_sibling_items_transition(self) -> None:
        candidate_port = FakeCandidatePort(
            {
                "good": RotationCandidateSnapshot(
                    lifecycle_state=RotationState.ACTIVE, memory_score=0.10
                ),
                "bad": RotationCandidateSnapshot(
                    lifecycle_state=RotationState.ACTIVE, memory_score=0.10
                ),
            }
        )
        transition_port = FakeTransitionPort(raise_for=frozenset({"bad"}))
        engine, _, clock = _make_engine(candidate_port, transition_port)
        policy = _episodic_policy()
        for item_id in ("good", "bad"):
            engine.schedule_or_pin(
                zone_id=ZoneId.EPISODIC,
                item_id=item_id,
                policy=policy,
                w_recency=1.0 / 6.0,
                c_weighted=0.20,
                max_age_remaining_seconds=_EPISODIC_MAX_AGE_SECONDS,
                cooldown_remaining_seconds=0.0,
            )
        clock.advance_to(_FIXED_TS + timedelta(days=365))

        result = engine.run_sweep(_TENANT, ZoneId.EPISODIC, policy)

        assert [t.item_id for t in result.transitioned] == ["good"]
        assert [f.item_id for f in result.failed] == ["bad"]


class TestRunSweepInputValidation:
    def test_blank_tenant_id_raises_value_error(self) -> None:
        engine, _, clock = _make_engine(FakeCandidatePort({}), FakeTransitionPort())
        with pytest.raises(ValueError, match="tenant_id"):
            engine.run_sweep("   ", ZoneId.EPISODIC, _episodic_policy())


class TestZonesAreIndependent:
    """HLD Section 5: "Per-zone" timer wheel -- a sweep of one zone never touches another's schedule."""

    def test_sweeping_one_zone_does_not_pop_another_zones_due_item(self) -> None:
        candidate_port = FakeCandidatePort(
            {
                "item-1": RotationCandidateSnapshot(
                    lifecycle_state=RotationState.ACTIVE, memory_score=0.10
                )
            }
        )
        transition_port = FakeTransitionPort()
        engine, _, clock = _make_engine(candidate_port, transition_port)
        engine.schedule_or_pin(
            zone_id=ZoneId.WORKING,
            item_id="item-1",
            policy=_episodic_policy(),
            w_recency=1.0 / 6.0,
            c_weighted=0.20,
            max_age_remaining_seconds=_EPISODIC_MAX_AGE_SECONDS,
            cooldown_remaining_seconds=0.0,
        )
        clock.advance_to(_FIXED_TS + timedelta(days=365))

        result = engine.run_sweep(_TENANT, ZoneId.EPISODIC, _episodic_policy())

        assert result.transitioned == ()
