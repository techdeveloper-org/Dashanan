"""QA suite for DASH-STORY-010: ADR-012 deadline-invalidation wiring on non-Recency term mutation.

Independent QA verification of `dashanan.application.rotation_deadline_invalidation.
RotationDeadlineInvalidationService` and `dashanan.domain.rotation_invalidation_trigger.
MutatedTerm`, traced to FR-012 in SRS.md and covering AC-012-INVAL-1 as read from
docs/phase-7-routing/implementation_execution_plan.json's DASH-STORY-010 dev_prompt.

FR-012 (verbatim): "The system SHALL compute a composite Memory Score for every item
from six weighted terms (Recency, Frequency, Importance, UserAffinity, TaskRelevance,
ProvenanceConfidence), each normalized to [0,1], and SHALL use this score to drive zone
lifecycle transitions."

AC-012-INVAL-1 under test (verbatim): "Given a non-Recency MemoryScore term (Frequency,
ProvenanceConfidence, or Importance) is mutated via access, conflict detection, or
operator override, when the mutation occurs, then the item's stored rotation deadline is
invalidated and re-inserted in O(log N) on all three mutation paths, so no rotation
decision is ever made against a stale deadline."

This suite is independent of tests/test_rotation_deadline_invalidation_dash010.py and
tests/test_rotation_invalidation_trigger_dash010.py (the Dev sub-task's own tests): it
constructs its own fixtures, exercises the real `RotationEngine` + `RotationTimerWheel`
(DASH-STORY-009, unmodified) rather than doubles wherever the AC concerns end-to-end
scheduling behavior, and adds an architecture-fitness check (HLD Section 3.0 invariant 1:
no `dashanan.domain.*` module may import `dashanan.infrastructure.*`).

Runtime conventions:
  - clock: `_FakeClock`, fixed at `2026-06-01T00:00:00+00:00`.
  - tenant/zone: Zone 2 Episodic policy (HLD Section 12A: t_half=2 days,
    CompressThreshold=0.35, MaxAge=180 days), the same zone DASH-STORY-009's own suite
    uses, so the boundary math below is independently re-derivable from
    `compute_rotation_deadline`'s closed form.

PII NOTE: fixtures use only pseudonymized item_id strings, MutatedTerm enum members, and
scheduling scalars -- never item content, conflict text, or override rationale.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from dashanan.application.rotation_deadline_invalidation import (
    RotationDeadlineInvalidationService,
)
from dashanan.application.rotation_engine import (
    RotationCandidateSnapshot,
    RotationCompressionResult,
    RotationEngine,
    RotationEnginePortError,
    RotationScheduleOutcome,
)
from dashanan.domain.rotation_invalidation_trigger import MutatedTerm
from dashanan.domain.zone import ZoneId
from dashanan.domain.rotation_zone_policy import RotationZonePolicy

_FIXED_TS = datetime(2026, 6, 1, tzinfo=UTC)
_T_HALF_SECONDS = 172_800.0
_MAX_AGE_SECONDS = 1.5552e7
_COMPRESS_THRESHOLD = 0.35
_W_RECENCY = 1.0 / 6.0


def _episodic_policy() -> RotationZonePolicy:
    return RotationZonePolicy.for_zone(
        t_half_seconds=_T_HALF_SECONDS,
        compress_threshold=_COMPRESS_THRESHOLD,
        max_age_seconds=_MAX_AGE_SECONDS,
    )


class _FakeClock:
    """Deterministic Clock double, fixed at `_FIXED_TS`."""

    def __init__(self, fixed: datetime = _FIXED_TS) -> None:
        self._fixed = fixed

    def now(self) -> datetime:
        return self._fixed


class _RecordingEventBus:
    """EventBus double: records every publish() call."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, object]]] = []

    def publish(self, event_type: str, payload: dict[str, object]) -> None:
        self.published.append((event_type, dict(payload)))


class _UnusedCandidatePort:
    """RotationCandidatePort double that must never be called by schedule_or_pin."""

    def current_state(
        self, tenant_id: str, zone_id: ZoneId, item_id: str
    ) -> RotationCandidateSnapshot:
        raise RotationEnginePortError(
            "current_state must never be called by schedule_or_pin -- "
            "this story never re-implements run_sweep"
        )


class _UnusedTransitionPort:
    """RotationTransitionPort double that must never be called by schedule_or_pin."""

    def compress(
        self, tenant_id: str, zone_id: ZoneId, item_id: str
    ) -> RotationCompressionResult:
        raise RotationEnginePortError(
            "compress must never be called by schedule_or_pin -- "
            "this story never re-implements run_sweep"
        )


class _CountingRotationEngine(RotationEngine):
    """RotationEngine subclass that counts schedule_or_pin/run_sweep invocations.

    Used to independently verify the O(log N) must-not-deviate item without
    reimplementing a spy-wrapper pattern identical to the Dev sub-task's own suite.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.schedule_or_pin_calls = 0
        self.run_sweep_calls = 0

    def schedule_or_pin(self, *args: object, **kwargs: object) -> RotationScheduleOutcome:
        self.schedule_or_pin_calls += 1
        return super().schedule_or_pin(*args, **kwargs)  # type: ignore[arg-type]

    def run_sweep(self, *args: object, **kwargs: object):
        self.run_sweep_calls += 1
        return super().run_sweep(*args, **kwargs)  # type: ignore[arg-type]


@pytest.fixture
def engine() -> _CountingRotationEngine:
    return _CountingRotationEngine(
        transition_port=_UnusedTransitionPort(),
        candidate_port=_UnusedCandidatePort(),
        event_bus=_RecordingEventBus(),
        clock=_FakeClock(),
    )


@pytest.fixture
def service(engine: _CountingRotationEngine) -> RotationDeadlineInvalidationService:
    return RotationDeadlineInvalidationService(engine)


def _c_weighted(
    frequency: float = 0.0,
    provenance_confidence: float = 0.0,
    importance: float = 0.0,
    user_affinity: float = 0.0,
    task_relevance: float = 0.0,
) -> float:
    """The weighted sum of every non-Recency term at equal 1/6 weight (Sprint 1 default).

    Mirrors `dashanan.domain.rotation_deadline.weighted_non_recency_sum`'s equal-weight
    default so this suite's boundary values are independently derivable from
    `compute_rotation_deadline`'s closed form, not copy-pasted from the dev report.
    """
    return _W_RECENCY * (
        frequency + provenance_confidence + importance + user_affinity + task_relevance
    )


# Boundary c_weighted values, derived algebraically from
# u = (theta - c_weighted) / w_recency (dashanan.domain.rotation_deadline.
# compute_rotation_deadline) for this suite's fixed Episodic policy
# (theta=0.35, w_recency=1/6):
#   SCHEDULED (0 < u < 1): theta - w_recency < c_weighted < theta
#   IMMEDIATE (u >= 1.0):  c_weighted <= theta - w_recency
#   PINNED    (u <= u_floor, u_floor ~ 1.33e-14 given MaxAge=180d dwarfs t_half=2d):
#             c_weighted >= theta (u <= 0)
_C_WEIGHTED_SCHEDULED_FUTURE = 0.25
"""u = (0.35 - 0.25) / (1/6) = 0.6 -> strictly inside (0, 1) -> SCHEDULE branch."""
_C_WEIGHTED_IMMEDIATE = 0.10
"""u = (0.35 - 0.10) / (1/6) = 1.5 -> >= 1.0 -> IMMEDIATE branch, clamped to dt=0."""
_C_WEIGHTED_PINNED = 0.90
"""u = (0.35 - 0.90) / (1/6) = -3.9 -> <= u_floor -> PINNED branch (never scheduled)."""


def _schedule_kwargs(zone_id: ZoneId, item_id: str, c_weighted: float) -> dict[str, object]:
    return {
        "zone_id": zone_id,
        "item_id": item_id,
        "policy": _episodic_policy(),
        "w_recency": _W_RECENCY,
        "c_weighted": c_weighted,
        "max_age_remaining_seconds": _MAX_AGE_SECONDS,
        "cooldown_remaining_seconds": 0.0,
    }


class TestACInvalOnAccessHappyPath:
    """AC-012-INVAL-1, Frequency-on-access (`on_access`)."""

    def test_scheduled_future_deadline_is_returned_and_positive(
        self, service: RotationDeadlineInvalidationService
    ) -> None:
        outcome = service.on_access(
            **_schedule_kwargs(ZoneId.EPISODIC, "item-access-1", _C_WEIGHTED_SCHEDULED_FUTURE)
        )
        assert outcome.scheduled is True
        assert outcome.due_at is not None
        assert outcome.due_at > _FIXED_TS

    def test_immediate_deadline_clamps_to_now_when_u_at_or_above_one(
        self, service: RotationDeadlineInvalidationService
    ) -> None:
        outcome = service.on_access(
            **_schedule_kwargs(ZoneId.EPISODIC, "item-access-2", _C_WEIGHTED_IMMEDIATE)
        )
        assert outcome.scheduled is True
        assert outcome.due_at == _FIXED_TS

    def test_pinned_item_is_not_scheduled(
        self, service: RotationDeadlineInvalidationService
    ) -> None:
        outcome = service.on_access(
            **_schedule_kwargs(ZoneId.EPISODIC, "item-access-3", _C_WEIGHTED_PINNED)
        )
        assert outcome.scheduled is False
        assert outcome.due_at is None


class TestACInvalOnConflictDetectedHappyPath:
    """AC-012-INVAL-1, ProvenanceConfidence-on-conflict (`on_conflict_detected`)."""

    def test_scheduled_future_deadline(
        self, service: RotationDeadlineInvalidationService
    ) -> None:
        outcome = service.on_conflict_detected(
            **_schedule_kwargs(ZoneId.EPISODIC, "item-conflict-1", _C_WEIGHTED_SCHEDULED_FUTURE)
        )
        assert outcome.scheduled is True
        assert outcome.due_at is not None
        assert outcome.due_at > _FIXED_TS

    def test_pinned_item_is_not_scheduled(
        self, service: RotationDeadlineInvalidationService
    ) -> None:
        outcome = service.on_conflict_detected(
            **_schedule_kwargs(ZoneId.EPISODIC, "item-conflict-2", _C_WEIGHTED_PINNED)
        )
        assert outcome.scheduled is False
        assert outcome.due_at is None


class TestACInvalOnOperatorOverrideHappyPath:
    """AC-012-INVAL-1, Importance-on-operator-override (`on_operator_override`)."""

    def test_scheduled_future_deadline(
        self, service: RotationDeadlineInvalidationService
    ) -> None:
        outcome = service.on_operator_override(
            **_schedule_kwargs(ZoneId.EPISODIC, "item-override-1", _C_WEIGHTED_SCHEDULED_FUTURE)
        )
        assert outcome.scheduled is True
        assert outcome.due_at is not None
        assert outcome.due_at > _FIXED_TS

    def test_pinned_item_is_not_scheduled(
        self, service: RotationDeadlineInvalidationService
    ) -> None:
        outcome = service.on_operator_override(
            **_schedule_kwargs(ZoneId.EPISODIC, "item-override-2", _C_WEIGHTED_PINNED)
        )
        assert outcome.scheduled is False
        assert outcome.due_at is None


class TestACInvalAllThreeMutationPathsWired:
    """Must-not-deviate: ALL THREE mutation paths must be wired, never a subset."""

    @pytest.mark.parametrize(
        "entry_point_name,term",
        [
            ("on_access", MutatedTerm.FREQUENCY),
            ("on_conflict_detected", MutatedTerm.PROVENANCE_CONFIDENCE),
            ("on_operator_override", MutatedTerm.IMPORTANCE),
        ],
    )
    def test_each_mutated_term_has_a_dedicated_entry_point(
        self,
        service: RotationDeadlineInvalidationService,
        entry_point_name: str,
        term: MutatedTerm,
    ) -> None:
        assert hasattr(service, entry_point_name)
        entry_point = getattr(service, entry_point_name)
        c_weighted = _c_weighted(**{term.value: 0.05})
        outcome = entry_point(
            **_schedule_kwargs(ZoneId.EPISODIC, f"item-wired-{term.value}", c_weighted)
        )
        assert isinstance(outcome, RotationScheduleOutcome)

    def test_mutated_term_enum_has_exactly_three_members(self) -> None:
        assert {m.value for m in MutatedTerm} == {
            "frequency",
            "provenance_confidence",
            "importance",
        }
        assert len(list(MutatedTerm)) == 3

    def test_no_recency_member_exists_in_mutated_term(self) -> None:
        assert not any(m.value == "recency" for m in MutatedTerm)


class TestACInvalOLogNReinsertBound:
    """Must-not-deviate: O(log N) re-insert bound on every deadline update."""

    @pytest.mark.parametrize(
        "entry_point_name",
        ["on_access", "on_conflict_detected", "on_operator_override"],
    )
    def test_each_entry_point_calls_schedule_or_pin_exactly_once(
        self,
        service: RotationDeadlineInvalidationService,
        engine: _CountingRotationEngine,
        entry_point_name: str,
    ) -> None:
        entry_point = getattr(service, entry_point_name)
        c_weighted = _c_weighted(frequency=0.05)
        entry_point(**_schedule_kwargs(ZoneId.EPISODIC, "item-count-1", c_weighted))
        assert engine.schedule_or_pin_calls == 1
        assert engine.run_sweep_calls == 0

    def test_repeated_mutation_calls_schedule_or_pin_once_per_call_not_cumulatively(
        self,
        service: RotationDeadlineInvalidationService,
        engine: _CountingRotationEngine,
    ) -> None:
        for i in range(5):
            service.on_access(
                **_schedule_kwargs(ZoneId.EPISODIC, "item-repeat-1", _c_weighted(frequency=0.05 + i * 0.01))
            )
        assert engine.schedule_or_pin_calls == 5

    def test_service_source_never_calls_run_sweep_pop_due_or_guarded_transition(self) -> None:
        module_path = Path(
            "src/dashanan/application/rotation_deadline_invalidation.py"
        )
        source = module_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        forbidden_calls = {"run_sweep", "pop_due", "guarded_transition"}
        found: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in forbidden_calls:
                found.add(node.attr)
            if isinstance(node, ast.Name) and node.id in forbidden_calls:
                found.add(node.id)
        assert not found, (
            f"rotation_deadline_invalidation.py must not call {forbidden_calls}; "
            f"found calls to: {found}"
        )


class TestACInvalNoStaleDeadlineWindow:
    """Must-not-deviate: no rotation decision may ever act on a stale deadline (real engine + wheel)."""

    def test_repeated_mutation_on_same_item_leaves_exactly_one_live_wheel_entry(
        self, service: RotationDeadlineInvalidationService, engine: _CountingRotationEngine
    ) -> None:
        service.on_access(
            **_schedule_kwargs(ZoneId.EPISODIC, "item-stale-1", _c_weighted(frequency=0.05))
        )
        service.on_conflict_detected(
            **_schedule_kwargs(ZoneId.EPISODIC, "item-stale-1", _c_weighted(provenance_confidence=0.06))
        )
        service.on_operator_override(
            **_schedule_kwargs(ZoneId.EPISODIC, "item-stale-1", _c_weighted(importance=0.07))
        )
        wheel = engine._wheel_for(ZoneId.EPISODIC)  # noqa test-internal introspection
        assert wheel.size == 1

    def test_pop_due_returns_only_the_latest_deadline_for_repeatedly_mutated_item(
        self, service: RotationDeadlineInvalidationService, engine: _CountingRotationEngine
    ) -> None:
        first = service.on_access(
            **_schedule_kwargs(ZoneId.EPISODIC, "item-stale-2", _c_weighted(frequency=0.05))
        )
        second = service.on_access(
            **_schedule_kwargs(ZoneId.EPISODIC, "item-stale-2", _c_weighted(frequency=0.1))
        )
        assert first.due_at is not None and second.due_at is not None
        wheel = engine._wheel_for(ZoneId.EPISODIC)  # noqa test-internal introspection
        due = wheel.pop_due(second.due_at + timedelta(seconds=1))
        matching = [entry for entry in due if entry.item_id == "item-stale-2"]
        assert len(matching) == 1
        assert matching[0].due_at == second.due_at

    def test_pin_after_schedule_removes_the_item_from_the_wheel(
        self, service: RotationDeadlineInvalidationService, engine: _CountingRotationEngine
    ) -> None:
        service.on_access(
            **_schedule_kwargs(ZoneId.EPISODIC, "item-stale-3", _c_weighted(frequency=0.05))
        )
        wheel = engine._wheel_for(ZoneId.EPISODIC)  # noqa test-internal introspection
        assert wheel.size == 1
        service.on_operator_override(
            **_schedule_kwargs(
                ZoneId.EPISODIC,
                "item-stale-3",
                _c_weighted(importance=1.0, frequency=1.0, provenance_confidence=1.0),
            )
        )
        assert wheel.size == 0

    def test_two_different_items_each_keep_their_own_live_entry(
        self, service: RotationDeadlineInvalidationService, engine: _CountingRotationEngine
    ) -> None:
        service.on_access(
            **_schedule_kwargs(ZoneId.EPISODIC, "item-stale-4a", _c_weighted(frequency=0.05))
        )
        service.on_access(
            **_schedule_kwargs(ZoneId.EPISODIC, "item-stale-4b", _c_weighted(frequency=0.05))
        )
        wheel = engine._wheel_for(ZoneId.EPISODIC)  # noqa test-internal introspection
        assert wheel.size == 2


class TestACInvalAdverseCases:
    """Adverse/negative cases per rule-33/40 roadmap adapted to this domain-only module."""

    def test_blank_item_id_raises_value_error_from_underlying_engine(
        self, service: RotationDeadlineInvalidationService
    ) -> None:
        with pytest.raises(ValueError):
            service.on_access(**_schedule_kwargs(ZoneId.EPISODIC, "", _c_weighted(frequency=0.05)))

    def test_out_of_range_memory_score_term_raises_value_error(
        self, service: RotationDeadlineInvalidationService
    ) -> None:
        # A c_weighted so far outside a valid composite score that compute_rotation_deadline's
        # own guard (u < 0 is impossible for any valid [0,1]-bounded term set) cannot silently
        # accept it -- this exercises the negative/adverse boundary rather than the happy path.
        with pytest.raises(ValueError):
            service.on_access(
                **_schedule_kwargs(ZoneId.EPISODIC, "item-adverse-1", c_weighted=float("nan"))
            )

    def test_replay_of_identical_mutation_is_idempotent_in_outcome_shape(
        self, service: RotationDeadlineInvalidationService
    ) -> None:
        kwargs = _schedule_kwargs(ZoneId.EPISODIC, "item-replay-1", _c_weighted(frequency=0.05))
        first = service.on_access(**kwargs)
        second = service.on_access(**kwargs)
        assert first.scheduled == second.scheduled
        assert first.due_at == second.due_at


class TestACInvalNoUnexpectedPortOrEventBusInvocation:
    """Must-not-deviate: no reactor/port call outside RotationEngine.schedule_or_pin."""

    def test_no_event_is_published_by_any_entry_point(
        self, service: RotationDeadlineInvalidationService, engine: _CountingRotationEngine
    ) -> None:
        service.on_access(
            **_schedule_kwargs(ZoneId.EPISODIC, "item-noevent-1", _c_weighted(frequency=0.05))
        )
        service.on_conflict_detected(
            **_schedule_kwargs(ZoneId.EPISODIC, "item-noevent-2", _c_weighted(provenance_confidence=0.05))
        )
        service.on_operator_override(
            **_schedule_kwargs(ZoneId.EPISODIC, "item-noevent-3", _c_weighted(importance=0.05))
        )
        event_bus = engine._event_bus  # noqa test-internal introspection
        assert event_bus.published == []

    def test_candidate_and_transition_ports_are_never_invoked(
        self, service: RotationDeadlineInvalidationService
    ) -> None:
        service.on_access(
            **_schedule_kwargs(ZoneId.EPISODIC, "item-noport-1", _c_weighted(frequency=0.05))
        )
        # _UnusedCandidatePort / _UnusedTransitionPort raise RotationEnginePortError if
        # ever called; reaching this line without raising proves neither port fired.


class TestArchitectureFitnessNoDomainImportingInfrastructure:
    """HLD Section 3.0 invariant 1: no dashanan.domain.* module imports dashanan.infrastructure.*."""

    def test_rotation_invalidation_trigger_domain_module_has_no_infrastructure_import(self) -> None:
        module_path = Path("src/dashanan/domain/rotation_invalidation_trigger.py")
        source = module_path.read_text(encoding="utf-8")
        assert "dashanan.infrastructure" not in source
        assert "from dashanan import infrastructure" not in source

    def test_every_domain_module_is_free_of_infrastructure_imports(self) -> None:
        domain_dir = Path("src/dashanan/domain")
        offenders: list[str] = []
        for py_file in domain_dir.glob("*.py"):
            if py_file.name == "__init__.py":
                continue
            source = py_file.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(py_file))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.startswith("dashanan.infrastructure"):
                            offenders.append(f"{py_file.name}: import {alias.name}")
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if module.startswith("dashanan.infrastructure"):
                        offenders.append(f"{py_file.name}: from {module} import ...")
        assert offenders == [], (
            "HLD Section 3.0 invariant 1 violated -- domain module(s) importing "
            f"infrastructure: {offenders}"
        )

    def test_rotation_deadline_invalidation_application_module_does_not_import_infrastructure(
        self,
    ) -> None:
        module_path = Path("src/dashanan/application/rotation_deadline_invalidation.py")
        source = module_path.read_text(encoding="utf-8")
        assert "dashanan.infrastructure" not in source
