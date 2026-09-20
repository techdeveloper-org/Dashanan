"""Pytest suite for DASH-STORY-015 (Procedural Memory zone / Zone 4, FR-004).

Dev subtask (60% split, backlog_draft.json). Covers AC-004-1, AC-004-3,
AC-004-4 plus the story's must-not-deviate list to the extent testable at
this layer: O(1) point lookup, the Importance ratio, and the write-commit
projection event. A dedicated QA subtask (20% split) enumerates the full
AC boundary/adverse matrix separately, including AC-004-2's contract test
and AC-004-5's import-graph audit (see
`test_procedural_memory_zone4_conformance_dash015.py`, this Dev subtask's
own structural verification pass for those two).

FR-004 (verbatim, SRS.md): "The system SHALL provide a Procedural Memory
zone holding learned task procedures, tool-use patterns, and successful
action sequences." (HLD Section 3.5 Zone 4).

Runtime assumptions (rule 33/40 test-roadmap conventions):
  - clock: FakeClock, fixed at 2026-01-01T00:00:00+00:00 unless a test
    states otherwise -- deterministic, no wall-clock dependency.
  - event_bus: RecordingEventBus, records every publish() call.
  - tenant_id: "tenant-1" unless a test states otherwise.
  - No fixture seed is required; nothing in this suite is randomized.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from dashanan.domain.exceptions import ZoneRepositoryError
from dashanan.domain.ports import ZoneQuery
from dashanan.domain.procedure import Procedure, hash_task_signature
from dashanan.domain.zone import ZoneId
from dashanan.infrastructure.in_memory_procedural_memory_repository import (
    PROJECTION_EVENT_TYPE,
    InMemoryProceduralMemoryRepository,
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


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def event_bus() -> RecordingEventBus:
    return RecordingEventBus()


@pytest.fixture
def repo(
    event_bus: RecordingEventBus, clock: FakeClock
) -> InMemoryProceduralMemoryRepository:
    return InMemoryProceduralMemoryRepository(event_bus=event_bus, clock=clock)


def _zone_query(**overrides: object) -> ZoneQuery:
    defaults: dict[str, object] = {
        "tenant_id": _TENANT,
        "task": "deploy the staging branch",
        "query_embedding": None,
        "max_items": 10,
        "min_provenance_conf": 0.0,
    }
    defaults.update(overrides)
    return ZoneQuery(**defaults)  # type: ignore[arg-type]


class TestAC004Story:
    """AC-004 (verbatim, DASH-STORY-015 dev_prompt):

    "Given a host records a successful tool-use sequence, when Procedural
    Memory is queried for that task type, then the stored procedure is
    retrievable and distinguishable from one-off episodic events."
    """

    def test_recorded_procedure_is_retrievable_by_its_task(
        self, repo: InMemoryProceduralMemoryRepository
    ) -> None:
        repo.record_execution(
            _TENANT,
            "deploy the staging branch",
            steps=("git pull", "run migrations", "restart service"),
            succeeded=True,
        )

        task_signature_hash = hash_task_signature("deploy the staging branch")
        found = repo.get_by_task_signature_hash(_TENANT, task_signature_hash)

        assert found is not None
        assert found.steps == ("git pull", "run migrations", "restart service")


class TestAC0041O1PointLookup:
    """AC-004-1 (verbatim): "Zone 4 returns a Procedure by exact
    task_signature_hash via O(1) point lookup (HashMap keyed by
    task_signature_hash)."
    """

    def test_get_by_task_signature_hash_returns_exact_match(
        self, repo: InMemoryProceduralMemoryRepository
    ) -> None:
        procedure = Procedure(
            tenant_id=_TENANT,
            task_signature_hash="a" * 64,
            steps=("step-1",),
            success_count=1,
            failure_count=0,
            last_used_at=_FIXED_TS,
        )
        repo.commit(procedure)

        assert repo.get_by_task_signature_hash(_TENANT, "a" * 64) == procedure

    def test_get_by_task_signature_hash_returns_none_for_unknown_key(
        self, repo: InMemoryProceduralMemoryRepository
    ) -> None:
        assert repo.get_by_task_signature_hash(_TENANT, "unknown-hash") is None

    def test_storage_is_a_dict_keyed_by_tenant_and_hash(
        self, repo: InMemoryProceduralMemoryRepository
    ) -> None:
        """Structural proof of "HashMap keyed by task_signature_hash": the
        adapter's own storage attribute is a `dict`, not a list needing a
        scan.
        """
        assert isinstance(repo._procedures, dict)

    def test_lookup_is_isolated_per_tenant(
        self, repo: InMemoryProceduralMemoryRepository
    ) -> None:
        procedure = Procedure(
            tenant_id="tenant-a",
            task_signature_hash="b" * 64,
            steps=("step-1",),
            success_count=1,
            failure_count=0,
            last_used_at=_FIXED_TS,
        )
        repo.commit(procedure)

        assert repo.get_by_task_signature_hash("tenant-b", "b" * 64) is None

    def test_get_rejects_blank_tenant_id(
        self, repo: InMemoryProceduralMemoryRepository
    ) -> None:
        with pytest.raises(ValueError, match="tenant_id must not be blank"):
            repo.get_by_task_signature_hash("   ", "a" * 64)

    def test_get_rejects_blank_task_signature_hash(
        self, repo: InMemoryProceduralMemoryRepository
    ) -> None:
        with pytest.raises(ValueError, match="task_signature_hash must not be blank"):
            repo.get_by_task_signature_hash(_TENANT, "")

    def test_hash_task_signature_is_deterministic_and_exact(self) -> None:
        first = hash_task_signature("Deploy The Staging Branch")
        second = hash_task_signature("  deploy the staging branch  ")
        different = hash_task_signature("deploy the production branch")

        assert first == second
        assert first != different
        assert len(first) == 64


class TestAC0043ImportanceRatio:
    """AC-004-3 (verbatim): "Importance = success_count/
    (success_count+failure_count), naturally bounded [0,1], no clamp
    function required."
    """

    @pytest.mark.parametrize(
        "success_count,failure_count,expected",
        [
            (1, 0, 1.0),
            (0, 1, 0.0),
            (3, 1, 0.75),
            (7, 3, 0.7),
        ],
    )
    def test_importance_matches_the_formula(
        self, success_count: int, failure_count: int, expected: float
    ) -> None:
        procedure = Procedure(
            tenant_id=_TENANT,
            task_signature_hash="c" * 64,
            steps=("step-1",),
            success_count=success_count,
            failure_count=failure_count,
            last_used_at=_FIXED_TS,
        )

        assert procedure.importance() == pytest.approx(expected)

    def test_importance_is_naturally_bounded_zero_to_one(self) -> None:
        for success_count, failure_count in [(0, 0), (1, 0), (0, 5), (100, 1)]:
            procedure = Procedure(
                tenant_id=_TENANT,
                task_signature_hash="d" * 64,
                steps=("step-1",),
                success_count=success_count,
                failure_count=failure_count,
                last_used_at=_FIXED_TS,
            )
            assert 0.0 <= procedure.importance() <= 1.0

    def test_importance_with_no_recorded_outcomes_is_zero_not_a_crash(self) -> None:
        """0/0 is undefined, not out-of-range -- must not raise ZeroDivisionError."""
        procedure = Procedure(
            tenant_id=_TENANT,
            task_signature_hash="e" * 64,
            steps=("step-1",),
            success_count=0,
            failure_count=0,
            last_used_at=_FIXED_TS,
        )

        assert procedure.importance() == 0.0

    def test_record_execution_increments_success_count_and_importance(
        self, repo: InMemoryProceduralMemoryRepository
    ) -> None:
        repo.record_execution(_TENANT, "run the release checklist", steps=("a",), succeeded=True)
        repo.record_execution(_TENANT, "run the release checklist", steps=("a",), succeeded=True)
        procedure = repo.record_execution(
            _TENANT, "run the release checklist", steps=("a",), succeeded=False
        )

        assert procedure.success_count == 2
        assert procedure.failure_count == 1
        assert procedure.importance() == pytest.approx(2 / 3)

    def test_procedure_rejects_negative_success_count(self) -> None:
        with pytest.raises(ValueError, match="success_count must not be negative"):
            Procedure(
                tenant_id=_TENANT,
                task_signature_hash="f" * 64,
                steps=("step-1",),
                success_count=-1,
                failure_count=0,
                last_used_at=_FIXED_TS,
            )


class TestAC0044ProjectionEventOnCommit:
    """AC-004-4 (verbatim): "On write commit, Zone 4 publishes a projection
    event so the item becomes discoverable via Zone 6."
    """

    def test_commit_publishes_exactly_one_projection_event(
        self,
        repo: InMemoryProceduralMemoryRepository,
        event_bus: RecordingEventBus,
    ) -> None:
        procedure = Procedure(
            tenant_id=_TENANT,
            task_signature_hash="a" * 64,
            steps=("step-1",),
            success_count=1,
            failure_count=0,
            last_used_at=_FIXED_TS,
        )

        repo.commit(procedure)

        assert len(event_bus.published) == 1
        event_type, payload = event_bus.published[0]
        assert event_type == PROJECTION_EVENT_TYPE
        assert payload["tenant_id"] == _TENANT
        assert payload["item_id"] == "a" * 64
        assert payload["zone"] == ZoneId.PROCEDURAL.value
        assert payload["target_zone"] == ZoneId.RETRIEVAL_INDEX.value

    def test_committed_item_is_immediately_readable_before_publish_returns(
        self,
        repo: InMemoryProceduralMemoryRepository,
        event_bus: RecordingEventBus,
    ) -> None:
        """The upsert must land before the event is published, so a
        consumer reacting to the event can read the committed value.
        """

        class AssertingEventBus:
            def __init__(self) -> None:
                self.repo: InMemoryProceduralMemoryRepository | None = None
                self.observed_present = False

            def publish(self, event_type: str, payload: dict[str, object]) -> None:
                assert self.repo is not None
                self.observed_present = (
                    self.repo.get_by_task_signature_hash(_TENANT, "b" * 64)
                    is not None
                )

        asserting_bus = AssertingEventBus()
        repo_with_asserting_bus = InMemoryProceduralMemoryRepository(
            event_bus=asserting_bus, clock=FakeClock()
        )
        asserting_bus.repo = repo_with_asserting_bus
        procedure = Procedure(
            tenant_id=_TENANT,
            task_signature_hash="b" * 64,
            steps=("step-1",),
            success_count=1,
            failure_count=0,
            last_used_at=_FIXED_TS,
        )

        repo_with_asserting_bus.commit(procedure)

        assert asserting_bus.observed_present is True

    def test_record_execution_also_publishes_a_projection_event(
        self,
        repo: InMemoryProceduralMemoryRepository,
        event_bus: RecordingEventBus,
    ) -> None:
        repo.record_execution(_TENANT, "run the tests", steps=("pytest",), succeeded=True)

        assert len(event_bus.published) == 1
        assert event_bus.published[0][0] == PROJECTION_EVENT_TYPE


class TestMustNotDeviateNoReadsFromAnotherZone:
    """must_not_deviate: "Do not add a READS relationship from Zone 4 to
    any other zone -- treat as reads-nothing until the HLD is amended."
    """

    def test_repository_never_holds_a_reference_to_another_zones_repository(
        self, repo: InMemoryProceduralMemoryRepository
    ) -> None:
        """Structural proof: the adapter's own `__dict__` carries only its
        EventBus and Clock ports and its internal storage -- no
        `ZoneRepository` for any other zone is ever injected or reachable.
        """
        attribute_values = vars(repo).values()
        assert not any(
            hasattr(value, "fetch") for value in attribute_values
        ), "InMemoryProceduralMemoryRepository must not hold another zone's ZoneRepository"


class TestFetchSatisfiesZoneRepositoryContract:
    """`fetch()` conformance, so this adapter can register with the
    Orchestrator (`MemoryOrchestrator.__init__`'s `zone_repositories`)
    exactly like every other zone's Shape A adapter.
    """

    def test_fetch_returns_the_matching_procedure_as_a_memory_item(
        self, repo: InMemoryProceduralMemoryRepository
    ) -> None:
        repo.record_execution(
            _TENANT,
            "deploy the staging branch",
            steps=("git pull", "restart service"),
            succeeded=True,
        )

        results = repo.fetch(_zone_query())

        assert len(results) == 1
        assert results[0].source_zone == ZoneId.PROCEDURAL
        assert results[0].score == pytest.approx(1.0)

    def test_fetch_returns_empty_list_when_no_procedure_matches(
        self, repo: InMemoryProceduralMemoryRepository
    ) -> None:
        assert repo.fetch(_zone_query(task="never seen before")) == []

    def test_fetch_returns_empty_list_when_task_is_none(
        self, repo: InMemoryProceduralMemoryRepository
    ) -> None:
        assert repo.fetch(_zone_query(task=None)) == []

    def test_fetch_never_performs_a_similarity_scan(
        self, repo: InMemoryProceduralMemoryRepository
    ) -> None:
        """`query.query_embedding` is deliberately ignored: Zone 4's own
        `fetch` never consults a vector, only the exact-match hash key
        (must-not-deviate item 1).
        """
        repo.record_execution(
            _TENANT, "deploy the staging branch", steps=("a",), succeeded=True
        )

        results = repo.fetch(
            _zone_query(task="deploy the staging branch", query_embedding=[0.1, 0.2])
        )

        assert len(results) == 1

    def test_fetch_rejects_blank_tenant_id(
        self, repo: InMemoryProceduralMemoryRepository
    ) -> None:
        with pytest.raises(ZoneRepositoryError):
            repo.fetch(_zone_query(tenant_id="   "))
