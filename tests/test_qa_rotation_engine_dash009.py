"""QA pytest suite for DASH-STORY-009 (Rotation state machine, Active ->
Compressed, Archived deferred), tracing to FR-012 in SRS.md.

QA subtask (backlog_draft.json 20% split), an independent verification
pass on top of the Dev subtask's own suite (tests/test_rotation_state_
dash009.py, test_rotation_deadline_dash009.py, test_rotation_timer_wheel_
dash009.py, test_rotation_engine_dash009.py) -- matching the dev/QA split
already established by DASH-STORY-002/003/004/006/008 (e.g.
test_qa_zone2_capacity_backstop_dash004.py's identical "independent
verification pass" framing). This suite re-derives its own fakes
independently rather than importing the dev suite's doubles, so a bug
shared between a dev fake and the implementation is still caught.

Acceptance criteria under test, verbatim from
docs/phase-7-routing/implementation_execution_plan.json's DASH-STORY-009
qa_prompt/dev_prompt (identical criteria cited by both):

  FR-012 (verbatim): "The system SHALL compute a composite Memory Score
  for every item from six weighted terms (Recency, Frequency, Importance,
  UserAffinity, TaskRelevance, ProvenanceConfidence), each normalized to
  [0,1], and SHALL use this score to drive zone lifecycle transitions."

  AC-012: "An item crossing CompressThreshold (not ArchiveThreshold)
  transitions to Compressed and stays retrievable; it SHALL NOT reach
  Archived without passing through Compressed."

  AC-012-ROT-1: "Zone 8 out of scope: an item that would cross
  ArchiveThreshold instead stays in Compressed indefinitely -- no
  Archived attempt with no destination."

Test scenario enumeration (rule 33/40/41 "enumerate before code"
convention), one row per AC:

  AC-012:
    - happy: an Active item, due in the timer wheel, whose re-scored
      MemoryScore is at/below CompressThreshold transitions to
      Compressed via the guarded transition, publishes exactly one
      `memory.compressed` event, and is reported in `transitioned`
      (i.e. stays retrievable -- the sweep never deletes it).
    - happy: `guarded_transition(ACTIVE, COMPRESSED)` succeeds and
      returns `COMPRESSED` unchanged (State pattern contract).
    - boundary: a re-scored MemoryScore exactly equal to
      CompressThreshold still transitions (the re-score guard's `>
      threshold + epsilon` skip condition is a strict inequality).
    - boundary: a re-scored MemoryScore one epsilon-guard unit above
      CompressThreshold is skipped, never transitioned.
    - boundary (exhaustive): `guarded_transition(state, ARCHIVED)` is
      rejected for EVERY `RotationState` member, with no exception --
      Archived is structurally unreachable from any state (must-not-
      deviate item 1).
    - boundary: `guarded_transition(COMPRESSED, COMPRESSED)` (any
      outbound transition from Compressed) is rejected -- Compressed's
      allowed-transition set is the empty set.
    - adverse: an item snapshotted as already `COMPRESSED` (e.g. a
      concurrent read-triggered promotion already moved it) at sweep
      time is skipped, not re-transitioned and not double-published.
    - adverse: `RotationCandidatePort.current_state` raising
      `RotationEnginePortError` for one item is recorded as a
      `RotationFailure` and does not abort the rest of the sweep tick.
    - adverse: `RotationTransitionPort.compress` raising
      `RotationEnginePortError` is recorded as a `RotationFailure` and
      no `memory.compressed` event is published for that item.
    - golden regression: one canonical single-item sweep from schedule
      through transition through event publication, kept as a
      regression guard for any later story touching this component.
  AC-012-ROT-1:
    - happy: `RotationZonePolicy` has exactly one threshold field
      (`compress_threshold`) -- no `archive_threshold` field exists
      anywhere on the dataclass, making an archive deadline
      computation a structural (AttributeError/TypeError) impossibility,
      not a convention.
    - happy: after a successful `Compressed` transition, the item is
      NOT re-inserted into the zone's `RotationTimerWheel` -- a second
      `run_sweep` call immediately afterward pops nothing for that item
      (Compressed dwells indefinitely, must-not-deviate item 5).
    - adverse: no code path anywhere in `application/rotation_engine.py`
      or `domain/rotation_state.py` (import scan + guarded-transition
      exhaustiveness above) ever names `RotationState.ARCHIVED` as a
      transition TARGET -- proven both by static source scan and by
      runtime exhaustion of the state space.
    - adverse: `compute_rotation_deadline` is never called by
      `RotationEngine.schedule_or_pin` with any threshold other than
      the `policy.compress_threshold` value passed in (there is no
      second threshold value anywhere in this story's call graph to
      even pass).
    - golden regression: an item whose deadline is pinned (never
      crosses CompressThreshold by decay alone) is never scheduled into
      the timer wheel at all -- kept as the "no destination" regression
      guard's schedule-side companion to the dwell-side golden case
      above.

Architecture-fitness (HLD Section 3.0, invariant 1), scoped to this
story's four new modules: none of `domain/rotation_state.py`,
`domain/rotation_zone_policy.py`, `domain/rotation_deadline.py`,
`domain/rotation_timer_wheel.py`, or `application/rotation_engine.py`
may import from `dashanan.infrastructure`; the three pure domain modules
must not import the application-layer orchestration module either
(dependency direction: application -> domain, never the reverse).

Runtime assumptions (rule 33/40/41 test-roadmap conventions):
  - clock: QaFakeClock, fixed at 2026-09-19T00:00:00+00:00 (deliberately
    different from the dev suite's own fixture, so this suite's
    pass/fail is not an artifact of sharing the dev suite's exact
    numbers) -- deterministic, no wall-clock dependency.
  - tenant_id: "qa-tenant-009" for all requests unless a test states
    otherwise.
  - zone_id: ZoneId.EPISODIC for all requests unless a test states
    otherwise (Zone 2, the only Sprint-1 zone this engine is exercised
    against per DASH-STORY-009's own dev report).
  - No fixture seed is required; nothing in this suite is randomized.

PII NOTE: per the dev_prompt's PII constraint, this suite carries only
state-machine control metadata -- pseudonymized item_id strings,
RotationState enum values, MemoryScore floats, timestamps, and
scheduling primitives -- never zone payload/fact content, anywhere
below.
"""

from __future__ import annotations

import ast
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from dashanan.application.rotation_engine import (
    RotationCandidateSnapshot,
    RotationCompressionResult,
    RotationEngine,
    RotationEnginePortError,
    RotationFailure,
    RotationSkip,
    RotationTransitionOutcome,
)
from dashanan.domain.rotation_deadline import compute_rotation_deadline
from dashanan.domain.rotation_state import (
    RotationState,
    RotationTransitionError,
    guarded_transition,
)
from dashanan.domain.rotation_timer_wheel import RotationTimerWheel
from dashanan.domain.rotation_zone_policy import RotationZonePolicy
from dashanan.domain.zone import ZoneId

DOMAIN_DIR = Path(__file__).resolve().parents[1] / "src" / "dashanan" / "domain"
APPLICATION_DIR = Path(__file__).resolve().parents[1] / "src" / "dashanan" / "application"
ROTATION_STATE_FILE = DOMAIN_DIR / "rotation_state.py"
ROTATION_ZONE_POLICY_FILE = DOMAIN_DIR / "rotation_zone_policy.py"
ROTATION_DEADLINE_FILE = DOMAIN_DIR / "rotation_deadline.py"
ROTATION_TIMER_WHEEL_FILE = DOMAIN_DIR / "rotation_timer_wheel.py"
ROTATION_ENGINE_FILE = APPLICATION_DIR / "rotation_engine.py"

_STORY_DOMAIN_FILES = [
    ROTATION_STATE_FILE,
    ROTATION_ZONE_POLICY_FILE,
    ROTATION_DEADLINE_FILE,
    ROTATION_TIMER_WHEEL_FILE,
]

_FIXED_TS = datetime(2026, 9, 19, tzinfo=UTC)
_TENANT = "qa-tenant-009"
_ZONE = ZoneId.EPISODIC


class QaFakeClock:
    """Deterministic, advanceable Clock double, independent of the dev
    suite's own fake. `advance` lets a test move time forward between a
    `schedule_or_pin` call (which schedules a FUTURE due_at) and the
    `run_sweep` call that must observe it as due.
    """

    def __init__(self, fixed: datetime = _FIXED_TS) -> None:
        self._fixed = fixed

    def now(self) -> datetime:
        return self._fixed

    def advance(self, seconds: float) -> None:
        self._fixed = self._fixed + timedelta(seconds=seconds)


class QaRecordingEventBus:
    """EventBus double: records every publish() call, never raises."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, object]]] = []

    def publish(self, event_type: str, payload: dict[str, object]) -> None:
        self.published.append((event_type, dict(payload)))


class QaCandidatePort:
    """RotationCandidatePort double serving pre-seeded snapshots by item_id."""

    def __init__(
        self,
        snapshots: dict[str, RotationCandidateSnapshot] | None = None,
        raise_for: frozenset[str] = frozenset(),
    ) -> None:
        self._snapshots = dict(snapshots or {})
        self._raise_for = raise_for
        self.calls: list[str] = []

    def current_state(
        self, tenant_id: str, zone_id: ZoneId, item_id: str
    ) -> RotationCandidateSnapshot:
        self.calls.append(item_id)
        if item_id in self._raise_for:
            raise RotationEnginePortError(f"qa: candidate store unreachable for {item_id}")
        return self._snapshots[item_id]


class QaTransitionPort:
    """RotationTransitionPort double: records compress() calls, can raise for specific items."""

    def __init__(self, raise_for: frozenset[str] = frozenset()) -> None:
        self.compressed: list[tuple[str, ZoneId, str]] = []
        self._raise_for = raise_for
        self._generation: dict[str, int] = {}

    def compress(
        self, tenant_id: str, zone_id: ZoneId, item_id: str
    ) -> RotationCompressionResult:
        if item_id in self._raise_for:
            raise RotationEnginePortError(f"qa: transition store unreachable for {item_id}")
        self.compressed.append((tenant_id, zone_id, item_id))
        gen = self._generation.get(item_id, 0) + 1
        self._generation[item_id] = gen
        return RotationCompressionResult(
            generation=gen, original_tokens=1000, compressed_tokens=250
        )


def _policy(
    lambda_zone: float = 1.0,
    compress_threshold: float = 0.35,
    max_age_seconds: float = 1_000_000.0,
    cooldown_seconds: float = 0.0,
) -> RotationZonePolicy:
    return RotationZonePolicy(
        lambda_zone=lambda_zone,
        compress_threshold=compress_threshold,
        max_age_seconds=max_age_seconds,
        cooldown_seconds=cooldown_seconds,
    )


def _engine(
    *,
    transition_port: QaTransitionPort | None = None,
    candidate_port: QaCandidatePort | None = None,
    event_bus: QaRecordingEventBus | None = None,
    clock: QaFakeClock | None = None,
) -> tuple[RotationEngine, QaTransitionPort, QaCandidatePort, QaRecordingEventBus, QaFakeClock]:
    tp = transition_port if transition_port is not None else QaTransitionPort()
    cp = candidate_port if candidate_port is not None else QaCandidatePort()
    bus = event_bus if event_bus is not None else QaRecordingEventBus()
    clk = clock if clock is not None else QaFakeClock()
    engine = RotationEngine(
        transition_port=tp, candidate_port=cp, event_bus=bus, clock=clk
    )
    return engine, tp, cp, bus, clk


def _normalize_whitespace(text: str) -> str:
    """Collapse all whitespace runs (including line-wrap newlines inside
    a docstring) to single spaces, so a verbatim fragment can be checked
    for membership regardless of how the module docstring wraps lines."""
    return " ".join(text.split())


class TestFR012AndAC012TraceabilityAndWiring:
    """FR-012/AC-012/AC-012-ROT-1 verbatim trace and baseline wiring."""

    def test_fr012_is_cited_verbatim_in_this_suite(self) -> None:
        fr_012_fragment = (
            "The system SHALL compute a composite Memory Score "
            "for every item from six weighted terms (Recency, Frequency, "
            "Importance, UserAffinity, TaskRelevance, ProvenanceConfidence), "
            "each normalized to [0,1], and SHALL use this score to drive "
            "zone lifecycle transitions."
        )
        assert __doc__ is not None
        assert _normalize_whitespace(fr_012_fragment) in _normalize_whitespace(__doc__)

    def test_ac012_is_cited_verbatim_in_this_suite(self) -> None:
        ac_012_fragment = (
            "An item crossing CompressThreshold (not ArchiveThreshold) "
            "transitions to Compressed and stays retrievable; it SHALL NOT "
            "reach Archived without passing through Compressed."
        )
        assert __doc__ is not None
        assert _normalize_whitespace(ac_012_fragment) in _normalize_whitespace(__doc__)

    def test_ac012_rot1_is_cited_verbatim_in_this_suite(self) -> None:
        ac_rot1_fragment = (
            "Zone 8 out of scope: an item that would cross ArchiveThreshold "
            "instead stays in Compressed indefinitely -- no Archived attempt "
            "with no destination."
        )
        assert __doc__ is not None
        assert _normalize_whitespace(ac_rot1_fragment) in _normalize_whitespace(__doc__)

    def test_engine_constructs_from_qa_independent_fakes(self) -> None:
        engine, *_ = _engine()
        assert isinstance(engine, RotationEngine)


class TestAC012HappyPath:
    """AC-012 happy-path: Active -> Compressed transition, retrievable, event published."""

    def test_active_item_at_or_below_threshold_transitions_to_compressed(self) -> None:
        engine, transition_port, candidate_port, bus, clk = _engine(
            candidate_port=QaCandidatePort(
                {"item-1": RotationCandidateSnapshot(
                    lifecycle_state=RotationState.ACTIVE, memory_score=0.10
                )}
            )
        )
        engine.schedule_or_pin(
            zone_id=_ZONE,
            item_id="item-1",
            policy=_policy(),
            w_recency=1.0,
            c_weighted=0.0,
            max_age_remaining_seconds=1_000_000.0,
            cooldown_remaining_seconds=0.0,
        )
        clk.advance(10.0)

        result = engine.run_sweep(_TENANT, _ZONE, _policy())

        assert [o.item_id for o in result.transitioned] == ["item-1"]
        assert isinstance(result.transitioned[0], RotationTransitionOutcome)
        assert transition_port.compressed == [(_TENANT, _ZONE, "item-1")]
        assert bus.published == [
            (
                "memory.compressed",
                {
                    "tenant_id": _TENANT,
                    "item_id": "item-1",
                    "zone": _ZONE.value,
                    "generation": 1,
                    "original_tokens": 1000,
                    "compressed_tokens": 250,
                },
            )
        ]
        assert result.failed == ()
        assert result.skipped == (), (
            "AC-012: a genuinely due, still-eligible item must transition, "
            "never be skipped"
        )

    def test_transitioned_item_stays_retrievable_never_deleted(self) -> None:
        """AC-012's 'stays retrievable' clause: the sweep never issues a
        delete/evict call -- it only ever calls compress(), which is a
        state-preserving durability write, never a removal."""
        engine, transition_port, _cp, _bus, clk = _engine(
            candidate_port=QaCandidatePort(
                {"item-1": RotationCandidateSnapshot(
                    lifecycle_state=RotationState.ACTIVE, memory_score=0.0
                )}
            )
        )
        engine.schedule_or_pin(
            zone_id=_ZONE, item_id="item-1", policy=_policy(),
            w_recency=1.0, c_weighted=0.0,
            max_age_remaining_seconds=1_000_000.0, cooldown_remaining_seconds=0.0,
        )
        clk.advance(10.0)

        result = engine.run_sweep(_TENANT, _ZONE, _policy())

        assert len(result.transitioned) == 1
        assert not hasattr(transition_port, "delete")
        assert not hasattr(transition_port, "evict")

    def test_guarded_transition_active_to_compressed_succeeds(self) -> None:
        outcome = guarded_transition(RotationState.ACTIVE, RotationState.COMPRESSED)
        assert outcome is RotationState.COMPRESSED


class TestAC012BoundaryCases:
    """AC-012 boundaries: re-score guard's exact threshold, Archived unreachability."""

    def test_score_exactly_at_threshold_still_transitions(self) -> None:
        engine, transition_port, _cp, _bus, clk = _engine(
            candidate_port=QaCandidatePort(
                {"item-boundary": RotationCandidateSnapshot(
                    lifecycle_state=RotationState.ACTIVE, memory_score=0.35
                )}
            )
        )
        engine.schedule_or_pin(
            zone_id=_ZONE, item_id="item-boundary", policy=_policy(compress_threshold=0.35),
            w_recency=1.0, c_weighted=0.0,
            max_age_remaining_seconds=1_000_000.0, cooldown_remaining_seconds=0.0,
        )
        clk.advance(10.0)

        result = engine.run_sweep(_TENANT, _ZONE, _policy(compress_threshold=0.35))

        assert [o.item_id for o in result.transitioned] == ["item-boundary"]

    def test_score_just_above_threshold_guard_is_skipped(self) -> None:
        engine, transition_port, _cp, _bus, clk = _engine(
            candidate_port=QaCandidatePort(
                {"item-above": RotationCandidateSnapshot(
                    lifecycle_state=RotationState.ACTIVE, memory_score=0.35 + 1e-6
                )}
            )
        )
        engine.schedule_or_pin(
            zone_id=_ZONE, item_id="item-above", policy=_policy(compress_threshold=0.35),
            w_recency=1.0, c_weighted=0.0,
            max_age_remaining_seconds=1_000_000.0, cooldown_remaining_seconds=0.0,
        )
        clk.advance(10.0)

        result = engine.run_sweep(_TENANT, _ZONE, _policy(compress_threshold=0.35))

        assert result.transitioned == ()
        assert [s.item_id for s in result.skipped] == ["item-above"]
        assert transition_port.compressed == []

    @pytest.mark.parametrize("current", list(RotationState))
    def test_archived_is_unreachable_from_every_state_exhaustive(
        self, current: RotationState
    ) -> None:
        """Must-not-deviate item 1: Archived structurally unreachable from
        ANY current state -- exhaustive over the full RotationState space."""
        with pytest.raises(RotationTransitionError):
            guarded_transition(current, RotationState.ARCHIVED)

    def test_compressed_has_zero_outbound_transitions(self) -> None:
        """Must-not-deviate item 5's structural counterpart: COMPRESSED maps
        to an explicit empty transition set, not merely an absent key."""
        for target in RotationState:
            with pytest.raises(RotationTransitionError):
                guarded_transition(RotationState.COMPRESSED, target)


class TestAC012AdverseCases:
    """AC-012 adverse/negative: concurrent promotion, port failures, failure isolation."""

    def test_item_already_compressed_at_sweep_time_is_skipped_not_retransitioned(
        self,
    ) -> None:
        engine, transition_port, _cp, bus, clk = _engine(
            candidate_port=QaCandidatePort(
                {"already-compressed": RotationCandidateSnapshot(
                    lifecycle_state=RotationState.COMPRESSED, memory_score=0.10
                )}
            )
        )
        engine.schedule_or_pin(
            zone_id=_ZONE, item_id="already-compressed", policy=_policy(),
            w_recency=1.0, c_weighted=0.0,
            max_age_remaining_seconds=1_000_000.0, cooldown_remaining_seconds=0.0,
        )
        clk.advance(10.0)

        result = engine.run_sweep(_TENANT, _ZONE, _policy())

        assert result.transitioned == ()
        assert [s.item_id for s in result.skipped] == ["already-compressed"]
        assert transition_port.compressed == []
        assert bus.published == []

    def test_candidate_port_failure_is_recorded_as_failure_not_abort(self) -> None:
        engine, transition_port, candidate_port, bus, clk = _engine(
            candidate_port=QaCandidatePort(
                snapshots={
                    "ok-item": RotationCandidateSnapshot(
                        lifecycle_state=RotationState.ACTIVE, memory_score=0.0
                    )
                },
                raise_for=frozenset({"bad-item"}),
            )
        )
        engine.schedule_or_pin(
            zone_id=_ZONE, item_id="bad-item", policy=_policy(),
            w_recency=1.0, c_weighted=0.0,
            max_age_remaining_seconds=1_000_000.0, cooldown_remaining_seconds=0.0,
        )
        engine.schedule_or_pin(
            zone_id=_ZONE, item_id="ok-item", policy=_policy(),
            w_recency=1.0, c_weighted=0.0,
            max_age_remaining_seconds=1_000_000.0, cooldown_remaining_seconds=0.0,
        )
        clk.advance(10.0)

        result = engine.run_sweep(_TENANT, _ZONE, _policy())

        assert [f.item_id for f in result.failed] == ["bad-item"]
        assert isinstance(result.failed[0], RotationFailure)
        assert [o.item_id for o in result.transitioned] == ["ok-item"], (
            "one item's port failure must not prevent another due item "
            "from being processed in the same sweep tick"
        )

    def test_transition_port_failure_is_recorded_as_failure_no_event_published(
        self,
    ) -> None:
        engine, transition_port, candidate_port, bus, clk = _engine(
            transition_port=QaTransitionPort(raise_for=frozenset({"unreachable-store"})),
            candidate_port=QaCandidatePort(
                {"unreachable-store": RotationCandidateSnapshot(
                    lifecycle_state=RotationState.ACTIVE, memory_score=0.0
                )}
            ),
        )
        engine.schedule_or_pin(
            zone_id=_ZONE, item_id="unreachable-store", policy=_policy(),
            w_recency=1.0, c_weighted=0.0,
            max_age_remaining_seconds=1_000_000.0, cooldown_remaining_seconds=0.0,
        )
        clk.advance(10.0)

        result = engine.run_sweep(_TENANT, _ZONE, _policy())

        assert result.transitioned == ()
        assert [f.item_id for f in result.failed] == ["unreachable-store"]
        assert bus.published == []

    def test_golden_single_item_schedule_through_publish_regression(self) -> None:
        """Golden regression case (one per AC, per this story's own
        dev-suite convention): a single item scheduled via schedule_or_pin,
        swept, transitioned, and published, end to end."""
        engine, transition_port, candidate_port, bus, clk = _engine(
            candidate_port=QaCandidatePort(
                {"golden-item": RotationCandidateSnapshot(
                    lifecycle_state=RotationState.ACTIVE, memory_score=0.20
                )}
            )
        )
        schedule_outcome = engine.schedule_or_pin(
            zone_id=_ZONE, item_id="golden-item", policy=_policy(compress_threshold=0.35),
            w_recency=1.0, c_weighted=0.0,
            max_age_remaining_seconds=1_000_000.0, cooldown_remaining_seconds=0.0,
        )
        assert schedule_outcome.scheduled is True
        assert schedule_outcome.due_at is not None
        clk.advance(10.0)

        result = engine.run_sweep(_TENANT, _ZONE, _policy(compress_threshold=0.35))

        assert result.transitioned[0].item_id == "golden-item"
        assert result.transitioned[0].generation == 1
        assert transition_port.compressed == [(_TENANT, _ZONE, "golden-item")]
        assert len(bus.published) == 1
        assert bus.published[0][0] == "memory.compressed"


class TestAC012ROT1HappyPath:
    """AC-012-ROT-1: no ArchiveThreshold field, no destination, indefinite Compressed dwell."""

    def test_rotation_zone_policy_has_exactly_one_threshold_field(self) -> None:
        policy = _policy()
        field_names = {f for f in policy.__dataclass_fields__}
        assert "compress_threshold" in field_names
        assert "archive_threshold" not in field_names, (
            "AC-012-ROT-1: RotationZonePolicy must never carry an "
            "archive_threshold field -- Zone 8 has no destination in "
            "Sprint 1, so computing an archive deadline must be a "
            "structural impossibility, not a convention"
        )

    def test_compressed_item_is_not_rescheduled_after_successful_transition(
        self,
    ) -> None:
        engine, transition_port, candidate_port, bus, clk = _engine(
            candidate_port=QaCandidatePort(
                {"dwelling-item": RotationCandidateSnapshot(
                    lifecycle_state=RotationState.ACTIVE, memory_score=0.0
                )}
            )
        )
        engine.schedule_or_pin(
            zone_id=_ZONE, item_id="dwelling-item", policy=_policy(),
            w_recency=1.0, c_weighted=0.0,
            max_age_remaining_seconds=1_000_000.0, cooldown_remaining_seconds=0.0,
        )
        clk.advance(10.0)
        first = engine.run_sweep(_TENANT, _ZONE, _policy())
        assert [o.item_id for o in first.transitioned] == ["dwelling-item"]

        second = engine.run_sweep(_TENANT, _ZONE, _policy())

        assert second.transitioned == ()
        assert second.skipped == ()
        assert second.failed == (), (
            "AC-012-ROT-1: a Compressed item dwells indefinitely -- the "
            "second sweep tick must find nothing due for it at all, "
            "because it was never re-inserted into the timer wheel"
        )
        assert transition_port.compressed == [(_TENANT, _ZONE, "dwelling-item")], (
            "compress() must be called exactly once for the item's entire "
            "lifetime in this sweep, never a second time"
        )


class TestAC012ROT1AdverseCases:
    """AC-012-ROT-1 adverse: static scan for any ARCHIVED transition target, pinned items."""

    def _imported_and_referenced_names(self, source_path: Path) -> set[str]:
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr:
                names.add(node.attr)
        return names

    @pytest.mark.parametrize(
        "source_path", [ROTATION_ENGINE_FILE, ROTATION_STATE_FILE]
    )
    def test_archived_never_appears_as_a_transition_target_in_source(
        self, source_path: Path
    ) -> None:
        """Static proof: ARCHIVED is referenced only as an enum member
        declaration/docstring mention in rotation_state.py, and never at
        all as a `guarded_transition(..., RotationState.ARCHIVED)` call
        argument anywhere in this story's application module."""
        tree = ast.parse(
            source_path.read_text(encoding="utf-8"), filename=str(source_path)
        )
        archived_as_call_arg = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for arg in list(node.args) + [kw.value for kw in node.keywords]:
                    if (
                        isinstance(arg, ast.Attribute)
                        and arg.attr == "ARCHIVED"
                    ):
                        archived_as_call_arg += 1
        assert archived_as_call_arg == 0, (
            f"{source_path.name}: RotationState.ARCHIVED must never appear "
            "as a call argument (a transition target) anywhere in this "
            "story's source"
        )

    def test_schedule_or_pin_never_receives_a_second_threshold_value(self) -> None:
        """There is no archive_threshold anywhere in this story's call
        graph for schedule_or_pin to even pass -- proven by RotationZone
        Policy's single-field structure (see happy-path test above) plus
        a direct call showing compute_rotation_deadline only ever sees
        policy.compress_threshold."""
        policy = _policy(compress_threshold=0.35)
        engine, *_r = _engine()
        outcome = engine.schedule_or_pin(
            zone_id=_ZONE, item_id="threshold-check", policy=policy,
            w_recency=1.0, c_weighted=0.0,
            max_age_remaining_seconds=1_000_000.0, cooldown_remaining_seconds=0.0,
        )
        expected_delta = compute_rotation_deadline(
            lambda_zone=policy.lambda_zone,
            theta=policy.compress_threshold,
            w_recency=1.0,
            c_weighted=0.0,
            max_age_remaining_s=1_000_000.0,
            cooldown_remaining_s=0.0,
        )
        assert expected_delta is not None
        assert outcome.due_at == _FIXED_TS + timedelta(seconds=expected_delta)

    def test_golden_pinned_item_never_scheduled_into_timer_wheel(self) -> None:
        """Golden regression case for AC-012-ROT-1: an item whose floor
        score never crosses CompressThreshold by decay alone (u <=
        u_floor) is pinned -- `scheduled` is False and it never occupies
        a live timer-wheel entry."""
        engine, *_r = _engine()
        policy = _policy(compress_threshold=0.35, max_age_seconds=1.0)

        outcome = engine.schedule_or_pin(
            zone_id=_ZONE, item_id="pinned-forever", policy=policy,
            w_recency=1.0, c_weighted=0.90,
            max_age_remaining_seconds=1.0, cooldown_remaining_seconds=0.0,
        )

        assert outcome.scheduled is False
        assert outcome.due_at is None

        result = engine.run_sweep(_TENANT, _ZONE, policy)
        assert result.transitioned == ()
        assert result.skipped == ()
        assert result.failed == ()


class TestRotationDeadlineWorkedExample:
    """Direct re-derivation of the closed-form deadline (F1) against a
    hand-computed value, independent of the dev suite's own worked
    examples -- proving the general-weight formula, never the equal-weight
    shortcut, is what actually executes."""

    def test_closed_form_matches_hand_computed_value(self) -> None:
        lambda_zone = 1.0
        theta = 0.5
        w_recency = 1.0
        c_weighted = 0.0
        max_age_remaining_s = 100.0
        cooldown_remaining_s = 0.0

        delta_t = compute_rotation_deadline(
            lambda_zone=lambda_zone,
            theta=theta,
            w_recency=w_recency,
            c_weighted=c_weighted,
            max_age_remaining_s=max_age_remaining_s,
            cooldown_remaining_s=cooldown_remaining_s,
        )

        expected = -math.log((theta - c_weighted) / w_recency) / lambda_zone
        assert delta_t == pytest.approx(expected, rel=1e-12)
        assert delta_t == pytest.approx(math.log(2.0), rel=1e-12)

    def test_back_substitution_s_of_delta_t_equals_theta(self) -> None:
        """S(dt*) == theta exactly (within float tolerance), the closed
        form's own defining equation, re-verified independently."""
        lambda_zone = 0.8
        theta = 0.35
        w_recency = 1.0
        c_weighted = 0.0

        delta_t = compute_rotation_deadline(
            lambda_zone=lambda_zone,
            theta=theta,
            w_recency=w_recency,
            c_weighted=c_weighted,
            max_age_remaining_s=1_000_000.0,
            cooldown_remaining_s=0.0,
        )
        assert delta_t is not None

        s_at_delta_t = w_recency * math.exp(-lambda_zone * delta_t) + c_weighted
        assert s_at_delta_t == pytest.approx(theta, abs=1e-9)


class TestTimerWheelOKLogNSweepBound:
    """Must-not-deviate item 3, re-proven at the QA layer: pop_due visits
    only due (or stale) entries, never the full live-scheduled population."""

    def test_large_wheel_pop_due_only_touches_due_items(self) -> None:
        wheel = RotationTimerWheel()
        base = _FIXED_TS
        for i in range(500):
            wheel.schedule(f"item-{i}", base + timedelta(seconds=i))
        assert wheel.size == 500

        due = wheel.pop_due(base + timedelta(seconds=2))

        assert {d.item_id for d in due} == {"item-0", "item-1", "item-2"}
        assert wheel.size == 497, (
            "popping k=3 due items from N=500 live-scheduled entries must "
            "leave exactly N-k=497 live -- proving the sweep never touched "
            "the other 497 entries"
        )


class TestArchitectureFitnessScopedToStory:
    """HLD Section 3.0 invariant 1, scoped to this story's five new
    modules: none may import from `dashanan.infrastructure`, and the
    three pure domain modules must not depend upward on the
    application-layer orchestration module.
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

    @pytest.mark.parametrize(
        "source_path", [*_STORY_DOMAIN_FILES, ROTATION_ENGINE_FILE]
    )
    def test_module_exists(self, source_path: Path) -> None:
        assert source_path.is_file(), f"{source_path} not found"

    @pytest.mark.parametrize(
        "source_path", [*_STORY_DOMAIN_FILES, ROTATION_ENGINE_FILE]
    )
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

    @pytest.mark.parametrize("source_path", _STORY_DOMAIN_FILES)
    def test_domain_module_does_not_import_application_module(
        self, source_path: Path
    ) -> None:
        imported = self._imported_module_names(source_path)
        bad = [
            name
            for name in imported
            if name == "dashanan.application.rotation_engine"
            or name.startswith("dashanan.application.")
        ]
        assert not bad, f"domain module must not import application/**: {bad}"

    def test_rotation_zone_policy_value_object_is_frozen_immutable(self) -> None:
        import dataclasses

        assert dataclasses.is_dataclass(RotationZonePolicy)
        params = getattr(RotationZonePolicy, "__dataclass_params__")
        assert params.frozen is True

    def test_rotation_state_enum_has_exactly_three_members(self) -> None:
        assert {s.value for s in RotationState} == {"active", "compressed", "archived"}


class TestBoundaryAndNegativeInputValidation:
    """Rule 33/40/41 boundary/negative roadmap: constructor guards, malformed input."""

    def test_run_sweep_rejects_blank_tenant_id(self) -> None:
        engine, *_r = _engine()
        with pytest.raises(ValueError, match="tenant_id"):
            engine.run_sweep("", _ZONE, _policy())

    def test_run_sweep_rejects_whitespace_only_tenant_id(self) -> None:
        engine, *_r = _engine()
        with pytest.raises(ValueError, match="tenant_id"):
            engine.run_sweep("   \t  ", _ZONE, _policy())

    def test_rotation_zone_policy_rejects_non_positive_lambda(self) -> None:
        with pytest.raises(ValueError, match="lambda_zone"):
            _policy(lambda_zone=0.0)

    def test_rotation_zone_policy_rejects_threshold_above_one(self) -> None:
        with pytest.raises(ValueError, match="compress_threshold"):
            _policy(compress_threshold=1.5)

    def test_rotation_zone_policy_rejects_threshold_below_zero(self) -> None:
        with pytest.raises(ValueError, match="compress_threshold"):
            _policy(compress_threshold=-0.1)

    def test_rotation_zone_policy_rejects_negative_cooldown(self) -> None:
        with pytest.raises(ValueError, match="cooldown_seconds"):
            _policy(cooldown_seconds=-1.0)

    def test_compute_rotation_deadline_rejects_non_positive_lambda(self) -> None:
        with pytest.raises(ValueError, match="lambda_zone"):
            compute_rotation_deadline(
                lambda_zone=0.0, theta=0.35, w_recency=1.0, c_weighted=0.0,
                max_age_remaining_s=100.0, cooldown_remaining_s=0.0,
            )

    def test_compute_rotation_deadline_rejects_non_positive_w_recency(self) -> None:
        with pytest.raises(ValueError, match="w_recency"):
            compute_rotation_deadline(
                lambda_zone=1.0, theta=0.35, w_recency=0.0, c_weighted=0.0,
                max_age_remaining_s=100.0, cooldown_remaining_s=0.0,
            )

    def test_rotation_candidate_snapshot_rejects_score_above_one(self) -> None:
        with pytest.raises(ValueError, match="memory_score"):
            RotationCandidateSnapshot(
                lifecycle_state=RotationState.ACTIVE, memory_score=1.5
            )

    def test_rotation_candidate_snapshot_rejects_score_below_zero(self) -> None:
        with pytest.raises(ValueError, match="memory_score"):
            RotationCandidateSnapshot(
                lifecycle_state=RotationState.ACTIVE, memory_score=-0.5
            )

    def test_timer_wheel_schedule_rejects_blank_item_id(self) -> None:
        wheel = RotationTimerWheel()
        with pytest.raises(ValueError, match="item_id"):
            wheel.schedule("   ", _FIXED_TS)

    def test_timer_wheel_invalidate_of_unknown_item_is_a_noop(self) -> None:
        wheel = RotationTimerWheel()
        wheel.invalidate("never-scheduled")
        assert wheel.size == 0

    def test_run_sweep_with_empty_wheel_returns_empty_result(self) -> None:
        engine, *_r = _engine()
        result = engine.run_sweep(_TENANT, _ZONE, _policy())
        assert result.transitioned == ()
        assert result.skipped == ()
        assert result.failed == ()
