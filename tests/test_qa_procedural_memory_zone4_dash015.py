"""QA pytest suite for DASH-STORY-015 (Procedural Memory zone / Zone 4, FR-004).

QA subtask (backlog_draft.json 20% split). Independent verification pass on
top of the Dev subtask's own two suites
(tests/test_procedural_memory_repository_dash015.py,
tests/test_procedural_memory_zone4_conformance_dash015.py): re-proves every
AC-004-* from a fresh angle and covers the boundary/adversarial matrix the
Dev suites do not already exercise -- the `Procedure` value-object's full
`__post_init__` boundary matrix (blank tenant_id, blank task_signature_hash,
empty steps, negative failure_count), frozen-dataclass immutability,
`score_terms` default-factory independence across instances, `commit`'s
pure-upsert (not increment-in-place) semantics, `record_execution`'s
first-use zero-state creation path and cross-tenant isolation, concurrent
commits under the adapter's own `threading.Lock`, the projection event's
full payload shape (including a round-trippable `committed_at`), and an
independent re-check of AC-004-2/AC-004-5's structural contracts via the
`typing.Protocol` machinery directly rather than the Dev suite's own
name-substring/AST approach.

FR-004 (verbatim, SRS.md, cited exactly as the qa_prompt requires): "The
system SHALL provide a Procedural Memory zone holding learned task
procedures, tool-use patterns, and successful action sequences." Traced by
DASH-STORY-015.

Acceptance criteria under test (verbatim, backlog_draft.json):
  AC-004-1: "Zone 4 returns a Procedure by exact task_signature_hash via
    O(1) point lookup (HashMap keyed by task_signature_hash)."
  AC-004-2: "Zone 4's adapter port exposes only point-lookup, no
    similarity-search method; any similarity need routes exclusively
    through Zone 6. Enforced by a contract test asserting the Zone 4
    adapter interface contains no similarity-search operation."
  AC-004-3: "Importance = success_count/(success_count+failure_count),
    naturally bounded [0,1], no clamp function required."
  AC-004-4: "On write commit, Zone 4 publishes a projection event so the
    item becomes discoverable via Zone 6."
  AC-004-5: "A static dependency/import-graph audit against the Zone 4
    module finds no import or call to any other zone's adapter interface,
    enforced as an automated architecture-conformance CI check
    (import-linter / dependency-cruiser)."

Must-not-deviate items re-checked independently (sprint2_ar1_assignments.json):
  - No Zone-4-owned vector/similarity search path (HLD 3.5).
  - No READS relationship from Zone 4 to any other zone.
  - No invented conflict-detection/provenance-linkage mechanism.

Runtime assumptions (matching test_qa_entity_memory_zone5_dash017.py's own
documented convention -- no HTTP/router/response-envelope layer exists in
this codebase; this is a domain-only Clean Architecture library):
  - clock: FakeClock, fixed at 2026-01-01T00:00:00+00:00 unless a test
    states otherwise.
  - tenant_id: "tenant-1" unless a test states otherwise.
  - No fixture seed required beyond the fixed clock; the concurrency test
    uses `threading` with a fixed thread count (20) and asserts on the
    final stored-item count, not on timing.
"""

from __future__ import annotations

import dataclasses
import threading
from datetime import UTC, datetime, timedelta
from typing import Protocol, get_type_hints

import pytest

from dashanan.domain.exceptions import ZoneRepositoryError
from dashanan.domain.ports import ZoneQuery
from dashanan.domain.procedural_memory_ports import ProcedureRepositoryPort
from dashanan.domain.procedure import Procedure, ProcedureState, hash_task_signature
from dashanan.domain.zone import ZoneId
from dashanan.infrastructure.in_memory_procedural_memory_repository import (
    PROJECTION_EVENT_TYPE,
    InMemoryProceduralMemoryRepository,
)
from dashanan.infrastructure.noop_event_bus import NoOpEventBus

_FIXED_TS = datetime(2026, 1, 1, tzinfo=UTC)
_TENANT = "tenant-1"


class FakeClock:
    """Deterministic, advanceable Clock double (testing-core DI)."""

    def __init__(self, fixed: datetime = _FIXED_TS) -> None:
        self._now = fixed

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


class RecordingEventBus:
    """`EventBus` double that captures every published event for inspection."""

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


def _procedure(**overrides: object) -> Procedure:
    defaults: dict[str, object] = {
        "tenant_id": _TENANT,
        "task_signature_hash": "a" * 64,
        "steps": ("step-1",),
        "success_count": 1,
        "failure_count": 0,
        "last_used_at": _FIXED_TS,
    }
    defaults.update(overrides)
    return Procedure(**defaults)  # type: ignore[arg-type]


class TestProcedureBoundaryMatrix:
    """AC-004-1/AC-004-3's underlying value object: the full
    `__post_init__` boundary matrix, closing the cells the Dev suite's
    `TestAC0043ImportanceRatio.test_procedure_rejects_negative_success_count`
    left unexercised.
    """

    def test_rejects_blank_tenant_id(self) -> None:
        with pytest.raises(ValueError, match="Procedure.tenant_id must not be blank"):
            _procedure(tenant_id="   ")

    def test_rejects_blank_task_signature_hash(self) -> None:
        with pytest.raises(
            ValueError, match="Procedure.task_signature_hash must not be blank"
        ):
            _procedure(task_signature_hash="")

    def test_rejects_empty_steps(self) -> None:
        with pytest.raises(ValueError, match="Procedure.steps must not be empty"):
            _procedure(steps=())

    def test_rejects_negative_failure_count(self) -> None:
        """The Dev suite only proves this for `success_count`; the sibling
        `failure_count` branch is a distinct `if` in `__post_init__` and
        needs its own proof for full statement coverage.
        """
        with pytest.raises(
            ValueError, match="failure_count must not be negative"
        ):
            _procedure(failure_count=-1)

    def test_accepts_zero_success_and_failure_count(self) -> None:
        procedure = _procedure(success_count=0, failure_count=0)
        assert procedure.success_count == 0
        assert procedure.failure_count == 0

    def test_default_state_is_active(self) -> None:
        procedure = _procedure()
        assert procedure.state == ProcedureState.ACTIVE

    def test_default_score_terms_are_independent_across_instances(self) -> None:
        """`score_terms: Mapping[str, float] = field(default_factory=dict)`
        must give each instance its own dict -- a `field(default={})`
        typo would silently share one mutable dict across every
        `Procedure` created without an explicit `score_terms` argument.
        """
        first = _procedure(task_signature_hash="a" * 64)
        second = _procedure(task_signature_hash="b" * 64)

        assert first.score_terms is not second.score_terms
        assert dict(first.score_terms) == {}
        assert dict(second.score_terms) == {}


class TestProcedureImmutability:
    """Frozen-dataclass contract: mutation must go through
    `dataclasses.replace`, matching the codebase's Value-Object convention
    this story's own `procedure.py` docstring cites (`WorkingItem`,
    `Episode`).
    """

    def test_direct_attribute_assignment_raises(self) -> None:
        procedure = _procedure()
        with pytest.raises(dataclasses.FrozenInstanceError):
            procedure.success_count = 99  # type: ignore[misc]

    def test_replace_produces_a_new_instance_leaving_original_untouched(self) -> None:
        original = _procedure(success_count=1, failure_count=0)

        replaced = dataclasses.replace(original, success_count=2)

        assert replaced is not original
        assert original.success_count == 1
        assert replaced.success_count == 2
        assert replaced.importance() == pytest.approx(1.0)
        assert original.importance() == pytest.approx(1.0)

    def test_procedure_is_a_frozen_dataclass(self) -> None:
        assert dataclasses.is_dataclass(Procedure)
        params = Procedure.__dataclass_params__  # type: ignore[attr-defined]
        assert params.frozen is True


class TestAC0041IndependentReVerification:
    """AC-004-1 re-proved from a fresh angle: many procedures under one
    tenant, verifying the point lookup returns only the exact key's own
    procedure regardless of population size, plus the O(1) contract's own
    structural guarantee re-checked via the port's return type.
    """

    def test_point_lookup_unaffected_by_population_size(
        self, repo: InMemoryProceduralMemoryRepository
    ) -> None:
        for i in range(50):
            repo.commit(
                _procedure(
                    task_signature_hash=f"{i:064x}",
                    steps=(f"step-{i}",),
                )
            )

        found = repo.get_by_task_signature_hash(_TENANT, f"{27:064x}")

        assert found is not None
        assert found.steps == ("step-27",)

    def test_commit_is_a_pure_upsert_not_an_increment(
        self, repo: InMemoryProceduralMemoryRepository
    ) -> None:
        """`commit`'s own docstring: "This method is a pure upsert, not an
        increment-in-place." Committing a second, unrelated `Procedure` at
        the same key must fully replace the first, not merge counters.
        """
        repo.commit(
            _procedure(
                task_signature_hash="c" * 64, success_count=10, failure_count=10
            )
        )
        repo.commit(
            _procedure(
                task_signature_hash="c" * 64,
                success_count=1,
                failure_count=0,
                steps=("replacement-step",),
            )
        )

        found = repo.get_by_task_signature_hash(_TENANT, "c" * 64)

        assert found is not None
        assert found.success_count == 1
        assert found.failure_count == 0
        assert found.steps == ("replacement-step",)

    def test_get_by_task_signature_hash_return_type_is_procedure_or_none(
        self, repo: InMemoryProceduralMemoryRepository
    ) -> None:
        """AC-004-1's O(1) point-lookup contract, re-checked at the type
        boundary rather than only the value boundary the Dev suite covers.
        """
        repo.commit(_procedure(task_signature_hash="d" * 64))

        hit = repo.get_by_task_signature_hash(_TENANT, "d" * 64)
        miss = repo.get_by_task_signature_hash(_TENANT, "e" * 64)

        assert isinstance(hit, Procedure)
        assert miss is None


class TestAC0043IndependentReVerification:
    """AC-004-3 re-proved via `record_execution`'s first-use path and
    cross-tenant isolation, which the Dev suite's own
    `test_record_execution_increments_success_count_and_importance` does
    not exercise (it starts from an already-empty single-tenant state and
    only checks the final ratio, not the intermediate zero-state or
    tenant isolation).
    """

    def test_record_execution_creates_zero_state_procedure_on_first_use(
        self, repo: InMemoryProceduralMemoryRepository
    ) -> None:
        procedure = repo.record_execution(
            _TENANT, "first ever task", steps=("only-step",), succeeded=True
        )

        assert procedure.success_count == 1
        assert procedure.failure_count == 0
        assert procedure.importance() == pytest.approx(1.0)

    def test_record_execution_is_isolated_per_tenant(
        self, repo: InMemoryProceduralMemoryRepository
    ) -> None:
        repo.record_execution(
            "tenant-a", "shared task text", steps=("a",), succeeded=True
        )
        repo.record_execution(
            "tenant-a", "shared task text", steps=("a",), succeeded=True
        )
        repo.record_execution(
            "tenant-b", "shared task text", steps=("a",), succeeded=False
        )

        task_signature_hash = hash_task_signature("shared task text")
        tenant_a = repo.get_by_task_signature_hash("tenant-a", task_signature_hash)
        tenant_b = repo.get_by_task_signature_hash("tenant-b", task_signature_hash)

        assert tenant_a is not None and tenant_a.success_count == 2
        assert tenant_b is not None and tenant_b.success_count == 0
        assert tenant_b.failure_count == 1

    def test_record_execution_rejects_blank_tenant_id(
        self, repo: InMemoryProceduralMemoryRepository
    ) -> None:
        with pytest.raises(ValueError, match="tenant_id must not be blank"):
            repo.record_execution("   ", "a task", steps=("a",), succeeded=True)

    def test_hash_task_signature_rejects_blank_task(self) -> None:
        with pytest.raises(ValueError, match="non-blank task"):
            hash_task_signature("   ")


class TestAC0044IndependentReVerification:
    """AC-004-4 re-proved with the full projection-event payload shape,
    including the `committed_at` field's round-trippability, which the
    Dev suite's own `test_commit_publishes_exactly_one_projection_event`
    checks for presence of the other four keys but not this one's format.
    """

    def test_projection_event_committed_at_is_a_valid_iso_timestamp(
        self,
        repo: InMemoryProceduralMemoryRepository,
        event_bus: RecordingEventBus,
        clock: FakeClock,
    ) -> None:
        clock.advance(seconds=3600)
        repo.commit(_procedure(task_signature_hash="a" * 64))

        _, payload = event_bus.published[0]
        parsed = datetime.fromisoformat(str(payload["committed_at"]))

        assert parsed == _FIXED_TS + timedelta(seconds=3600)

    def test_two_sequential_commits_publish_two_independent_events(
        self,
        repo: InMemoryProceduralMemoryRepository,
        event_bus: RecordingEventBus,
    ) -> None:
        repo.commit(_procedure(task_signature_hash="a" * 64))
        repo.commit(_procedure(task_signature_hash="b" * 64))

        assert len(event_bus.published) == 2
        item_ids = {payload["item_id"] for _, payload in event_bus.published}
        assert item_ids == {"a" * 64, "b" * 64}

    def test_commit_succeeds_regardless_of_concrete_event_bus_implementation(
        self, clock: FakeClock
    ) -> None:
        """Mirrors `test_qa_entity_memory_zone5_dash017.py`'s own
        "write succeeds regardless of EventBus implementation" pattern:
        the commit's own upsert must not be coupled to which concrete
        `EventBus` is wired in.
        """
        repo_with_noop_bus = InMemoryProceduralMemoryRepository(
            event_bus=NoOpEventBus(), clock=clock
        )

        repo_with_noop_bus.commit(_procedure(task_signature_hash="a" * 64))

        assert (
            repo_with_noop_bus.get_by_task_signature_hash(_TENANT, "a" * 64)
            is not None
        )


class TestConcurrentCommitsUnderTheAdapterSOwnLock:
    """Adversarial case the Dev suite does not cover at all: many threads
    calling `commit` concurrently, proving the adapter's own
    `threading.Lock` (documented in its class docstring) actually
    prevents a lost update against the shared `_procedures` dict.
    """

    def test_concurrent_commits_to_distinct_keys_all_land(
        self, repo: InMemoryProceduralMemoryRepository
    ) -> None:
        thread_count = 20
        barrier = threading.Barrier(thread_count)

        def _commit_one(index: int) -> None:
            barrier.wait()
            repo.commit(
                _procedure(
                    task_signature_hash=f"{index:064x}", steps=(f"step-{index}",)
                )
            )

        threads = [
            threading.Thread(target=_commit_one, args=(i,)) for i in range(thread_count)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        for i in range(thread_count):
            found = repo.get_by_task_signature_hash(_TENANT, f"{i:064x}")
            assert found is not None, f"commit for index {i} was lost under concurrency"
            assert found.steps == (f"step-{i}",)


class TestAC0042IndependentReVerificationViaProtocolMachinery:
    """AC-004-2 re-proved via `typing.Protocol` introspection directly,
    an independent mechanism from the Dev suite's own
    name-substring-on-`vars()` approach in
    `test_procedural_memory_zone4_conformance_dash015.py`.
    """

    def test_procedure_repository_port_is_a_runtime_checkable_protocol(self) -> None:
        assert issubclass(ProcedureRepositoryPort, Protocol)  # type: ignore[arg-type]
        assert getattr(ProcedureRepositoryPort, "_is_runtime_protocol", False) is True

    def test_get_by_task_signature_hash_signature_returns_optional_procedure(
        self,
    ) -> None:
        hints = get_type_hints(ProcedureRepositoryPort.get_by_task_signature_hash)
        assert hints["return"] == Procedure | None

    def test_commit_signature_accepts_a_single_procedure_argument(self) -> None:
        hints = get_type_hints(ProcedureRepositoryPort.commit)
        assert hints["procedure"] is Procedure

    def test_an_object_missing_commit_does_not_satisfy_the_protocol(self) -> None:
        class _PointLookupOnly:
            def get_by_task_signature_hash(
                self, tenant_id: str, task_signature_hash: str
            ) -> Procedure | None:
                return None

        assert not isinstance(_PointLookupOnly(), ProcedureRepositoryPort)


class TestFetchSatisfiesZoneRepositoryContractFreshAngle:
    """`fetch()` re-proved with the boundary the Dev suite's own
    `TestFetchSatisfiesZoneRepositoryContract` does not cover: the
    returned `MemoryItem.token_count`/payload shape, and a second,
    unrelated procedure never leaking into a different task's result.
    """

    def test_fetch_payload_joins_steps_with_arrow_separator(
        self, repo: InMemoryProceduralMemoryRepository
    ) -> None:
        repo.record_execution(
            _TENANT,
            "multi step task",
            steps=("clone repo", "run build", "deploy"),
            succeeded=True,
        )

        results = repo.fetch(
            ZoneQuery(
                tenant_id=_TENANT,
                task="multi step task",
                query_embedding=None,
                max_items=10,
                min_provenance_conf=0.0,
            )
        )

        assert len(results) == 1
        assert results[0].payload == "clone repo -> run build -> deploy"
        assert results[0].token_count == 3

    def test_fetch_for_one_task_never_returns_a_different_tasks_procedure(
        self, repo: InMemoryProceduralMemoryRepository
    ) -> None:
        repo.record_execution(_TENANT, "task alpha", steps=("a",), succeeded=True)
        repo.record_execution(_TENANT, "task beta", steps=("b",), succeeded=True)

        results = repo.fetch(
            ZoneQuery(
                tenant_id=_TENANT,
                task="task alpha",
                query_embedding=None,
                max_items=10,
                min_provenance_conf=0.0,
            )
        )

        assert len(results) == 1
        assert results[0].item_id == hash_task_signature("task alpha")

    def test_fetch_rejects_blank_tenant_id_with_zone_repository_error(
        self, repo: InMemoryProceduralMemoryRepository
    ) -> None:
        with pytest.raises(ZoneRepositoryError) as exc_info:
            repo.fetch(
                ZoneQuery(
                    tenant_id="",
                    task="anything",
                    query_embedding=None,
                    max_items=10,
                    min_provenance_conf=0.0,
                )
            )
        assert exc_info.value.zone == ZoneId.PROCEDURAL.value


class TestMustNotDeviateNoInventedConflictOrProvenanceLinkage:
    """must_not_deviate: "Do not invent a conflict-detection or
    provenance-linkage mechanism analogous to Zone 3/5 or Zone 7" --
    re-checked structurally rather than by code inspection: `Procedure`
    must carry no provenance/conflict-shaped field, and `commit` must
    accept only a single `Procedure`, never a pair to compare.
    """

    def test_procedure_has_no_provenance_or_conflict_field(self) -> None:
        field_names = {f.name for f in dataclasses.fields(Procedure)}
        forbidden = {"provenance_id", "conflict_status", "provenance_conf"}
        assert not (field_names & forbidden), (
            f"Procedure must not carry a provenance/conflict field; found: "
            f"{field_names & forbidden}"
        )

    def test_commit_signature_takes_exactly_one_procedure_parameter(self) -> None:
        hints = get_type_hints(InMemoryProceduralMemoryRepository.commit)
        non_return_params = {k: v for k, v in hints.items() if k != "return"}
        assert non_return_params == {"procedure": Procedure}
