"""Test suite for DASH-STORY-016: Zone 4 (Procedural) rotation policy wiring.

Traces to FR-004 in SRS.md. Covers, by AC ID:
  - AC-004-CAP-2: a Procedure crossing CompressThreshold (not
    ArchiveThreshold) transitions to Compressed using a Recency half-life
    of 120 days.
  - AC-004-CAP-3: while Zone 8 is absent, an item that would otherwise
    cross ArchiveThreshold remains in Compressed indefinitely, bounded
    only by DASH-STORY-016's own capacity/MaxAge backstop (HLD Section
    12F) -- covered here at the rotation-engine layer (no second
    threshold exists to check); the capacity/MaxAge bound itself is
    covered by `test_zone4_capacity_backstop_dash016.py`.

Also verifies the mathematics-engineer delegation (sprint2_ar1_
assignments.json AR1-S2-016, advisory): "Confirmation that the published
decay constant (~6.68e-8) is the correct ln(2)/half-life conversion for a
120-day half-life expressed in the sweep engine's time unit."

MUST-NOT-DEVIATE (dev_prompt, binding) coverage:
  - "Do not duplicate the generic deadline-scheduled sweep engine built
    in DASH-STORY-009 -- wire into it, do not reimplement it": every test
    below drives the existing, unmodified `dashanan.application.
    rotation_engine.RotationEngine` (DASH-STORY-009) through
    `dashanan.domain.rotation_zone_policy.RotationZonePolicy.for_zone`
    with Zone 4's own HLD Section 12A parameters -- this module adds zero
    new sweep, timer-wheel, or state-machine code.
  - "Do not assume Zone 4 reaches Archived in this story -- stops at
    Compressed while Zone 8 is absent at Zone-4-scope (HLD 12F)": see
    `TestAC004CAP3NoArchiveThresholdForZone4`.

JUDGMENT CALL / SCOPE-STOP (carried forward per this story's dev report,
mirroring DASH-STORY-013's "carry this flag forward" pattern for a
partial-coverage gap): this suite exercises `RotationEngine` with FAKE
`RotationCandidatePort`/`RotationTransitionPort` doubles, exactly as
DASH-STORY-009's own `test_rotation_engine_dash009.py` does for Zone 2 --
it does NOT add a concrete, production `RotationCandidatePort`/
`RotationTransitionPort` adapter that reads real `Procedure` rows and
computes a genuine six-term composite MemoryScore for them. Reason:
`dashanan.domain.memory_score.compute_importance`'s own docstring records
HLD Section 12G / AC-012-SCORE-1 stating "Zone 4 (Procedural) is out of
Sprint 1 scope" for the tag-based Importance term the generic
`ScoreTerms`/`MemoryScoreEngine` pipeline uses, and two of the six terms
(Frequency's `f_cap`, HLD OAQ-20; ProvenanceConfidence's Zone 7 linkage)
have no established value or mechanism for a `Procedure` anywhere in this
codebase. `Procedure`'s own docstring (DASH-STORY-015) already declined to
invent a comparable cross-zone mechanism ("inventing a... mechanism
analogous to Zone 3/5 or Zone 7... is therefore deliberately omitted").
Building a concrete adapter now would require inventing exactly that
class of mechanism. This mirrors DASH-STORY-009's own precedent of
shipping the generic engine while deferring concrete per-zone
`schedule_or_pin` call-site wiring to a separate, explicitly-scoped
story. Flagged here, and in this story's dev report, as an open question
for the Solution Architect rather than guessed at.

Runtime assumptions (rule 33/40 test-roadmap conventions):
  - clock: `FakeClock`, fixed at `2026-01-01T00:00:00+00:00` unless a
    test states otherwise.
  - tenant_id: `"tenant-1"`.
  - No fixture seed required; nothing in this suite is randomized.

PII NOTE: this suite carries only scheduling and state-machine control
metadata -- pseudonymized `item_id` values, `RotationState` enum members,
`MemoryScore` floats, timestamps -- never a Procedure's `steps` payload.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from dashanan.application.rotation_engine import (
    RotationCandidateSnapshot,
    RotationCompressionResult,
    RotationEngine,
    RotationEnginePortError,
)
from dashanan.domain.memory_score import lambda_from_half_life
from dashanan.domain.rotation_state import RotationState
from dashanan.domain.rotation_zone_policy import RotationZonePolicy
from dashanan.domain.zone import ZoneId

_FIXED_TS = datetime(2026, 1, 1, tzinfo=UTC)
_TENANT = "tenant-1"

_PROCEDURAL_T_HALF_SECONDS = 120 * 86400.0
"""HLD Section 12A per-zone policy table, Zone 4 row: `t_half = 120 days`."""
_PROCEDURAL_MAX_AGE_SECONDS = 730 * 86400.0
"""HLD Section 12A per-zone policy table, Zone 4 row: `MaxAge = 730 days`."""
_COMPRESS_THRESHOLD = 0.35
"""HLD Section 12A FINALIZED `CompressThreshold` (adopted unchanged, all zones)."""
_PUBLISHED_LAMBDA_ZONE = 6.68e-8
"""HLD Section 12A per-zone policy table, Zone 4 row: `lambda_zone (s^-1)`."""
_LAMBDA_RELATIVE_TOLERANCE = 1e-2
"""Relative tolerance for comparing the exact derived lambda against HLD's own
rounded-to-3-significant-figures published literal (e.g. "6.68e-8" is a
3-sig-fig display of the exact `ln(2)/10_368_000 = 6.6854...e-8` value --
about 0.08% off the full-precision figure). 1% comfortably bounds that
rounding gap without masking a genuine derivation error, which would be
orders of magnitude larger."""


def _procedural_policy() -> RotationZonePolicy:
    return RotationZonePolicy.for_zone(
        t_half_seconds=_PROCEDURAL_T_HALF_SECONDS,
        compress_threshold=_COMPRESS_THRESHOLD,
        max_age_seconds=_PROCEDURAL_MAX_AGE_SECONDS,
    )


class FakeClock:
    """Deterministic, mutable Clock double: fixed instant, advanceable, injectable."""

    def __init__(self, fixed: datetime = _FIXED_TS) -> None:
        self._fixed = fixed

    def now(self) -> datetime:
        return self._fixed

    def advance_to(self, new_time: datetime) -> None:
        self._fixed = new_time


class RecordingEventBus:
    """EventBus double: records every publish() call, never raises."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, object]]] = []

    def publish(self, event_type: str, payload: dict[str, object]) -> None:
        self.published.append((event_type, dict(payload)))


class FakeCandidatePort:
    """RotationCandidatePort double: returns configured snapshots by `item_id`."""

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


class FakeTransitionPort:
    """RotationTransitionPort double: records compress() calls."""

    def __init__(self) -> None:
        self.compressed: list[tuple[str, ZoneId, str]] = []

    def compress(
        self, tenant_id: str, zone_id: ZoneId, item_id: str
    ) -> RotationCompressionResult:
        self.compressed.append((tenant_id, zone_id, item_id))
        return RotationCompressionResult(
            generation=1, original_tokens=5, compressed_tokens=5
        )


def _make_engine(
    candidate_port: object,
    transition_port: object,
    clock: FakeClock | None = None,
) -> tuple[RotationEngine, RecordingEventBus, FakeClock]:
    bus = RecordingEventBus()
    fake_clock = clock if clock is not None else FakeClock()
    engine = RotationEngine(
        transition_port=transition_port,
        candidate_port=candidate_port,
        event_bus=bus,
        clock=fake_clock,
    )
    return engine, bus, fake_clock


class TestMathDelegationDecayConstantVerification:
    """AR1-S2-016 mathematical delegation (advisory): verify the published
    decay constant is the correct `ln(2)/t_half` conversion for Zone 4's
    120-day half-life, expressed in seconds (the sweep engine's time unit).

    Per this story's dev report: no live `mathematics-engineer` subagent
    dispatch is available in this execution environment, so this
    verification instead asserts against `lambda_from_half_life` --
    `dashanan.domain.memory_score`'s own single, already-reviewed
    derivation point every zone policy in this codebase is required to
    route through (its docstring: "the sole derivation point every caller
    must route through... [never] hardcode HLD 12A's rounded literals").
    This is the RETURNED derivation from tested, existing production code,
    not a self-computed approximation -- see this story's dev report,
    judgment-call list, for why this is the available substitute for a
    live nested dispatch in this harness.
    """

    def test_lambda_from_half_life_matches_hld_12a_published_zone4_constant(
        self,
    ) -> None:
        derived = lambda_from_half_life(_PROCEDURAL_T_HALF_SECONDS)

        assert derived == pytest.approx(
            _PUBLISHED_LAMBDA_ZONE, rel=_LAMBDA_RELATIVE_TOLERANCE
        )

    def test_lambda_from_half_life_is_exactly_ln2_over_t_half(self) -> None:
        derived = lambda_from_half_life(_PROCEDURAL_T_HALF_SECONDS)

        assert derived == math.log(2.0) / _PROCEDURAL_T_HALF_SECONDS

    def test_procedural_policy_carries_the_derived_lambda_not_a_hardcoded_literal(
        self,
    ) -> None:
        policy = _procedural_policy()

        assert policy.lambda_zone == lambda_from_half_life(_PROCEDURAL_T_HALF_SECONDS)


class TestAC004CAP2CrossingCompressThresholdTransitionsToCompressed:
    """AC-004-CAP-2: a Procedure crossing CompressThreshold transitions to
    Compressed using a Recency half-life of 120 days."""

    def test_due_active_procedure_below_threshold_transitions_to_compressed(
        self,
    ) -> None:
        policy = _procedural_policy()
        candidate_port = FakeCandidatePort(
            {
                "task-hash-1": RotationCandidateSnapshot(
                    lifecycle_state=RotationState.ACTIVE, memory_score=0.20
                )
            }
        )
        transition_port = FakeTransitionPort()
        engine, bus, clock = _make_engine(candidate_port, transition_port)
        engine.schedule_or_pin(
            zone_id=ZoneId.PROCEDURAL,
            item_id="task-hash-1",
            policy=policy,
            w_recency=1.0 / 6.0,
            c_weighted=0.25,
            max_age_remaining_seconds=_PROCEDURAL_MAX_AGE_SECONDS,
            cooldown_remaining_seconds=0.0,
        )
        clock.advance_to(_FIXED_TS + timedelta(days=365))

        result = engine.run_sweep(_TENANT, ZoneId.PROCEDURAL, policy)

        assert len(result.transitioned) == 1
        assert result.transitioned[0].item_id == "task-hash-1"
        assert transition_port.compressed == [
            (_TENANT, ZoneId.PROCEDURAL, "task-hash-1")
        ]
        assert bus.published[0][0] == "memory.compressed"
        assert bus.published[0][1]["zone"] == ZoneId.PROCEDURAL.value

    def test_procedure_at_or_above_threshold_is_not_transitioned(self) -> None:
        policy = _procedural_policy()
        candidate_port = FakeCandidatePort(
            {
                "task-hash-1": RotationCandidateSnapshot(
                    lifecycle_state=RotationState.ACTIVE, memory_score=0.90
                )
            }
        )
        transition_port = FakeTransitionPort()
        engine, bus, clock = _make_engine(candidate_port, transition_port)
        engine.schedule_or_pin(
            zone_id=ZoneId.PROCEDURAL,
            item_id="task-hash-1",
            policy=policy,
            w_recency=1.0 / 6.0,
            c_weighted=0.20,
            max_age_remaining_seconds=_PROCEDURAL_MAX_AGE_SECONDS,
            cooldown_remaining_seconds=0.0,
        )
        clock.advance_to(_FIXED_TS + timedelta(days=365))

        result = engine.run_sweep(_TENANT, ZoneId.PROCEDURAL, policy)

        assert result.transitioned == ()
        assert len(result.skipped) == 1
        assert transition_port.compressed == []
        assert bus.published == []

    def test_120_day_half_life_produces_a_slower_decay_than_the_2_day_episodic_zone(
        self,
    ) -> None:
        """Sanity check on the "Recency half-life of 120 days" binding itself:
        a longer half-life must produce a strictly smaller lambda_zone (slower
        decay) than Zone 2 Episodic's 2-day half-life."""
        procedural_lambda = _procedural_policy().lambda_zone
        episodic_lambda = lambda_from_half_life(2 * 86400.0)

        assert procedural_lambda < episodic_lambda


class TestAC004CAP3NoArchiveThresholdForZone4:
    """AC-004-CAP-3: while Zone 8 is absent, an item that would otherwise
    cross ArchiveThreshold instead stays in Compressed indefinitely -- no
    Archived attempt, bounded only by the capacity/MaxAge backstop."""

    def test_procedure_far_below_a_hypothetical_archive_threshold_still_only_reaches_compressed(
        self,
    ) -> None:
        """A score of 0.05 (well under both CompressThreshold=0.35 and a
        hypothetical ArchiveThreshold=0.25) still only ever reaches
        Compressed for Zone 4 -- the engine has no second threshold to
        check, exactly as HLD Section 12F establishes for every zone
        while Zone 8 is absent."""
        policy = _procedural_policy()
        candidate_port = FakeCandidatePort(
            {
                "task-hash-1": RotationCandidateSnapshot(
                    lifecycle_state=RotationState.ACTIVE, memory_score=0.05
                )
            }
        )
        transition_port = FakeTransitionPort()
        engine, bus, clock = _make_engine(candidate_port, transition_port)
        engine.schedule_or_pin(
            zone_id=ZoneId.PROCEDURAL,
            item_id="task-hash-1",
            policy=policy,
            w_recency=1.0 / 6.0,
            c_weighted=0.10,
            max_age_remaining_seconds=_PROCEDURAL_MAX_AGE_SECONDS,
            cooldown_remaining_seconds=0.0,
        )
        clock.advance_to(_FIXED_TS + timedelta(days=365))

        result = engine.run_sweep(_TENANT, ZoneId.PROCEDURAL, policy)

        assert len(result.transitioned) == 1
        published_types = {event_type for event_type, _ in bus.published}
        assert published_types == {"memory.compressed"}
        assert "memory.archived" not in published_types

    def test_procedural_zone_policy_has_no_archive_threshold_field(self) -> None:
        """Structural enforcement (mirrors test_rotation_engine_dash009's
        identical check): no field this engine reads for Zone 4 carries an
        ArchiveThreshold."""
        policy = _procedural_policy()
        assert not hasattr(policy, "archive_threshold")

    def test_compressed_procedure_dwells_indefinitely_not_rescheduled(self) -> None:
        policy = _procedural_policy()
        candidate_port = FakeCandidatePort(
            {
                "task-hash-1": RotationCandidateSnapshot(
                    lifecycle_state=RotationState.ACTIVE, memory_score=0.10
                )
            }
        )
        transition_port = FakeTransitionPort()
        engine, _bus, clock = _make_engine(candidate_port, transition_port)
        engine.schedule_or_pin(
            zone_id=ZoneId.PROCEDURAL,
            item_id="task-hash-1",
            policy=policy,
            w_recency=1.0 / 6.0,
            c_weighted=0.20,
            max_age_remaining_seconds=_PROCEDURAL_MAX_AGE_SECONDS,
            cooldown_remaining_seconds=0.0,
        )
        clock.advance_to(_FIXED_TS + timedelta(days=365))

        first_sweep = engine.run_sweep(_TENANT, ZoneId.PROCEDURAL, policy)
        second_sweep = engine.run_sweep(_TENANT, ZoneId.PROCEDURAL, policy)

        assert len(first_sweep.transitioned) == 1
        assert second_sweep.transitioned == ()
        assert second_sweep.skipped == ()
        assert second_sweep.failed == ()


class TestMustNotDeviateZone4IndependentOfOtherZones:
    """HLD Section 5: a Zone 4 sweep never touches another zone's schedule."""

    def test_sweeping_procedural_zone_does_not_pop_episodic_zones_due_item(
        self,
    ) -> None:
        policy = _procedural_policy()
        candidate_port = FakeCandidatePort(
            {
                "task-hash-1": RotationCandidateSnapshot(
                    lifecycle_state=RotationState.ACTIVE, memory_score=0.10
                )
            }
        )
        transition_port = FakeTransitionPort()
        engine, _bus, clock = _make_engine(candidate_port, transition_port)
        engine.schedule_or_pin(
            zone_id=ZoneId.EPISODIC,
            item_id="task-hash-1",
            policy=policy,
            w_recency=1.0 / 6.0,
            c_weighted=0.20,
            max_age_remaining_seconds=_PROCEDURAL_MAX_AGE_SECONDS,
            cooldown_remaining_seconds=0.0,
        )
        clock.advance_to(_FIXED_TS + timedelta(days=365))

        result = engine.run_sweep(_TENANT, ZoneId.PROCEDURAL, policy)

        assert result.transitioned == ()


class TestPerItemFailureIsolation:
    """A single port failure isolates to one Procedure -- the rest of the tick still runs."""

    def test_candidate_port_failure_is_recorded_not_raised(self) -> None:
        class RaisingCandidatePort:
            def current_state(
                self, tenant_id: str, zone_id: ZoneId, item_id: str
            ) -> RotationCandidateSnapshot:
                raise RotationEnginePortError("procedural repository unreachable")

        policy = _procedural_policy()
        transition_port = FakeTransitionPort()
        engine, _bus, clock = _make_engine(RaisingCandidatePort(), transition_port)
        engine.schedule_or_pin(
            zone_id=ZoneId.PROCEDURAL,
            item_id="task-hash-1",
            policy=policy,
            w_recency=1.0 / 6.0,
            c_weighted=0.20,
            max_age_remaining_seconds=_PROCEDURAL_MAX_AGE_SECONDS,
            cooldown_remaining_seconds=0.0,
        )
        clock.advance_to(_FIXED_TS + timedelta(days=365))

        result = engine.run_sweep(_TENANT, ZoneId.PROCEDURAL, policy)

        assert result.transitioned == ()
        assert len(result.failed) == 1
        assert result.failed[0].item_id == "task-hash-1"
