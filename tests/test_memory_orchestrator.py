"""Formal pytest suite for DASH-STORY-001 (Memory Orchestrator core facade, FR-009).

QA subtask (20% split). Covers AC-009, AC-009-SUPP-1, AC-009-R1-1, the
documented boundary/negative cases, and the HLD 3.0 invariant 1
architecture-fitness check (no domain module imports infrastructure).

Runtime assumptions recorded per rule 33/40 test-roadmap conventions:
  - clock: FakeClock, fixed at 2026-01-01T00:00:00+00:00 unless a test
    states otherwise -- deterministic, no wall-clock dependency.
  - tenant_id: "tenant-1" for all requests unless a test states otherwise.
  - No fixture seed is required; nothing in this suite is randomized.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dashanan.application.context_assembly_builder import ContextAssemblyBuilder
from dashanan.application.context_assembly_request import ContextAssemblyRequest
from dashanan.application.memory_orchestrator import MemoryOrchestrator
from dashanan.domain.exceptions import ZoneRepositoryError
from dashanan.domain.memory_item import MemoryItem
from dashanan.domain.ports import ZoneQuery
from dashanan.domain.zone import ZoneId

DOMAIN_DIR = (
    Path(__file__).resolve().parents[1] / "src" / "dashanan" / "domain"
)


class FakeClock:
    """Deterministic Clock double: fixed instant, injectable (testing-core DI)."""

    def __init__(self, fixed: datetime) -> None:
        self._fixed = fixed

    def now(self) -> datetime:
        return self._fixed


class RecordingEventBus:
    """EventBus double that records every publish call for assertion."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, object]]] = []

    def publish(self, event_type: str, payload: dict[str, object]) -> None:
        self.published.append((event_type, payload))


class StubZoneRepository:
    """ZoneRepository double returning a fixed item list or raising on demand."""

    def __init__(
        self,
        items: list[MemoryItem] | None = None,
        raises: ZoneRepositoryError | None = None,
    ) -> None:
        self._items = items or []
        self._raises = raises
        self.received_queries: list[ZoneQuery] = []

    def fetch(self, query: ZoneQuery) -> list[MemoryItem]:
        self.received_queries.append(query)
        if self._raises is not None:
            raise self._raises
        return list(self._items)


@pytest.fixture
def fixed_clock() -> FakeClock:
    return FakeClock(datetime(2026, 1, 1, tzinfo=UTC))


@pytest.fixture
def event_bus() -> RecordingEventBus:
    return RecordingEventBus()


def _request(**overrides: object) -> ContextAssemblyRequest:
    defaults: dict[str, object] = {
        "tenant_id": "tenant-1",
        "session_id": "session-1",
        "task": "recall the last order",
        "token_budget": 1000,
    }
    defaults.update(overrides)
    return ContextAssemblyRequest(**defaults)  # type: ignore[arg-type]


class TestAC009NoZoneLevelRouting:
    """AC-009: omitting `zones` routes across every registered zone."""

    def test_no_zones_specified_queries_every_registered_zone(
        self, fixed_clock: FakeClock, event_bus: RecordingEventBus
    ) -> None:
        """Omitting `zones` targets the full ZoneId enum (AC-009); zones with no
        registered adapter still degrade per AC-009-SUPP-1 -- the two ACs
        compose rather than conflict. All eight zones are registered here so
        this test isolates AC-009's routing claim without SUPP-1 interference.
        """
        repos = {
            zone: StubZoneRepository(
                items=[MemoryItem(f"{zone.value}-1", zone, "payload", token_count=10, score=1.0)]
            )
            for zone in ZoneId
        }
        orchestrator = MemoryOrchestrator(
            zone_repositories=repos,
            event_bus=event_bus,
            clock=fixed_clock,
        )
        request = _request(zones=None)

        result = orchestrator.assemble_context(request)

        for zone, repo in repos.items():
            assert repo.received_queries, f"{zone.value} zone must have been queried"
        returned_ids = {item.item_id for item in result.items}
        assert returned_ids == {f"{zone.value}-1" for zone in ZoneId}
        assert result.degraded is False
        assert result.zones_unavailable == []

    def test_no_zones_specified_still_degrades_gracefully_for_partial_registration(
        self, fixed_clock: FakeClock, event_bus: RecordingEventBus
    ) -> None:
        """A realistic deployment: only some zones registered. Omitting `zones`
        still targets all eight (AC-009); the unregistered six degrade
        (AC-009-SUPP-1) rather than being silently excluded from routing.
        """
        working_repo = StubZoneRepository(
            items=[MemoryItem("w1", ZoneId.WORKING, "payload", token_count=10, score=1.0)]
        )
        semantic_repo = StubZoneRepository(
            items=[MemoryItem("s1", ZoneId.SEMANTIC, "payload", token_count=10, score=1.0)]
        )
        orchestrator = MemoryOrchestrator(
            zone_repositories={
                ZoneId.WORKING: working_repo,
                ZoneId.SEMANTIC: semantic_repo,
            },
            event_bus=event_bus,
            clock=fixed_clock,
        )
        request = _request(zones=None)

        result = orchestrator.assemble_context(request)

        assert working_repo.received_queries, "WORKING zone must have been queried"
        assert semantic_repo.received_queries, "SEMANTIC zone must have been queried"
        returned_ids = {item.item_id for item in result.items}
        assert returned_ids == {"w1", "s1"}
        assert result.degraded is True
        assert set(result.zones_unavailable) == set(ZoneId) - {ZoneId.WORKING, ZoneId.SEMANTIC}

    def test_caller_never_supplies_zone_level_knowledge(
        self, fixed_clock: FakeClock, event_bus: RecordingEventBus
    ) -> None:
        """The request DTO itself proves the caller need not name a zone."""
        orchestrator = MemoryOrchestrator(
            zone_repositories={}, event_bus=event_bus, clock=fixed_clock
        )
        request = _request()
        assert request.zones is None

        result = orchestrator.assemble_context(request)

        assert result.token_budget == 1000


class TestAC009Supp1ZoneUnavailable:
    """AC-009-SUPP-1: an unregistered/failing zone degrades, never raises."""

    def test_unregistered_zone_marked_unavailable_not_raised(
        self, fixed_clock: FakeClock, event_bus: RecordingEventBus
    ) -> None:
        orchestrator = MemoryOrchestrator(
            zone_repositories={}, event_bus=event_bus, clock=fixed_clock
        )
        request = _request(zones=[ZoneId.EPISODIC])

        result = orchestrator.assemble_context(request)

        assert result.degraded is True
        assert result.zones_unavailable == [ZoneId.EPISODIC]
        assert result.items == []

    def test_registered_zone_that_raises_zone_repository_error_degrades(
        self, fixed_clock: FakeClock, event_bus: RecordingEventBus
    ) -> None:
        failing_repo = StubZoneRepository(
            raises=ZoneRepositoryError(zone="working", reason="connection refused")
        )
        orchestrator = MemoryOrchestrator(
            zone_repositories={ZoneId.WORKING: failing_repo},
            event_bus=event_bus,
            clock=fixed_clock,
        )
        request = _request(zones=[ZoneId.WORKING])

        result = orchestrator.assemble_context(request)

        assert result.degraded is True
        assert result.zones_unavailable == [ZoneId.WORKING]
        assert result.items == []

    def test_mixed_available_and_unavailable_zones_returns_partial_success(
        self, fixed_clock: FakeClock, event_bus: RecordingEventBus
    ) -> None:
        working_repo = StubZoneRepository(
            items=[MemoryItem("w1", ZoneId.WORKING, "payload", token_count=10, score=1.0)]
        )
        orchestrator = MemoryOrchestrator(
            zone_repositories={ZoneId.WORKING: working_repo},
            event_bus=event_bus,
            clock=fixed_clock,
        )
        request = _request(zones=[ZoneId.WORKING, ZoneId.EPISODIC])

        result = orchestrator.assemble_context(request)

        assert result.degraded is True
        assert result.zones_unavailable == [ZoneId.EPISODIC]
        assert [item.item_id for item in result.items] == ["w1"]

    def test_unavailable_zone_response_still_publishes_lifecycle_event(
        self, fixed_clock: FakeClock, event_bus: RecordingEventBus
    ) -> None:
        orchestrator = MemoryOrchestrator(
            zone_repositories={}, event_bus=event_bus, clock=fixed_clock
        )
        request = _request(zones=[ZoneId.PROVENANCE])

        orchestrator.assemble_context(request)

        assert len(event_bus.published) == 1
        event_type, payload = event_bus.published[0]
        assert event_type == "context.assembled"
        assert payload["degraded"] is True
        assert payload["zones_unavailable"] == [ZoneId.PROVENANCE.value]


class TestAC009R1UniqueIdentifiers:
    """AC-009-R1-1: assembly_id and trace_id are unique and non-empty every call."""

    def test_success_response_carries_unique_nonempty_ids(
        self, fixed_clock: FakeClock, event_bus: RecordingEventBus
    ) -> None:
        working_repo = StubZoneRepository(
            items=[MemoryItem("w1", ZoneId.WORKING, "payload", token_count=10, score=1.0)]
        )
        orchestrator = MemoryOrchestrator(
            zone_repositories={ZoneId.WORKING: working_repo},
            event_bus=event_bus,
            clock=fixed_clock,
        )
        request = _request(zones=[ZoneId.WORKING])

        first = orchestrator.assemble_context(request)
        second = orchestrator.assemble_context(request)

        for result in (first, second):
            assert result.assembly_id != ""
            assert result.trace_id != ""
        assert first.assembly_id != second.assembly_id
        assert first.trace_id != second.trace_id

    def test_degraded_response_also_carries_unique_nonempty_ids(
        self, fixed_clock: FakeClock, event_bus: RecordingEventBus
    ) -> None:
        """Explicit AC-009-SUPP-1 x AC-009-R1-1 interaction: degraded is not exempt."""
        orchestrator = MemoryOrchestrator(
            zone_repositories={}, event_bus=event_bus, clock=fixed_clock
        )
        request = _request(zones=[ZoneId.CONSOLIDATION])

        first = orchestrator.assemble_context(request)
        second = orchestrator.assemble_context(request)

        assert first.degraded is True and second.degraded is True
        assert first.assembly_id and first.trace_id
        assert second.assembly_id and second.trace_id
        assert first.assembly_id != second.assembly_id
        assert first.trace_id != second.trace_id

    def test_repeated_calls_never_collide_across_many_iterations(
        self, fixed_clock: FakeClock, event_bus: RecordingEventBus
    ) -> None:
        orchestrator = MemoryOrchestrator(
            zone_repositories={}, event_bus=event_bus, clock=fixed_clock
        )
        request = _request()

        assembly_ids = set()
        trace_ids = set()
        for _ in range(50):
            result = orchestrator.assemble_context(request)
            assembly_ids.add(result.assembly_id)
            trace_ids.add(result.trace_id)

        assert len(assembly_ids) == 50, "assembly_id collided across repeated calls"
        assert len(trace_ids) == 50, "trace_id collided across repeated calls"


class TestBoundaryAndNegativeCases:
    """Boundary/negative cases named or implied by qa_prompt.prompt and the code."""

    def test_zero_token_budget_rejected_at_request_construction(self) -> None:
        with pytest.raises(ValueError, match="token_budget must be positive"):
            _request(token_budget=0)

    def test_negative_token_budget_rejected_at_request_construction(self) -> None:
        with pytest.raises(ValueError, match="token_budget must be positive"):
            _request(token_budget=-5)

    def test_request_requires_task_or_query_embedding(self) -> None:
        with pytest.raises(ValueError, match="requires 'task' or 'query_embedding'"):
            _request(task=None, query_embedding=None)

    def test_empty_zone_repositories_dict_degrades_every_default_zone(
        self, fixed_clock: FakeClock, event_bus: RecordingEventBus
    ) -> None:
        orchestrator = MemoryOrchestrator(
            zone_repositories={}, event_bus=event_bus, clock=fixed_clock
        )
        request = _request(zones=None)

        result = orchestrator.assemble_context(request)

        assert result.degraded is True
        assert set(result.zones_unavailable) == set(ZoneId)
        assert result.items == []

    def test_zero_token_count_item_rejected_by_memory_item_invariant(self) -> None:
        with pytest.raises(ValueError, match="token_count must be positive"):
            MemoryItem("x", ZoneId.WORKING, "payload", token_count=0, score=1.0)

    def test_blank_item_id_rejected_by_memory_item_invariant(self) -> None:
        with pytest.raises(ValueError, match="item_id must not be blank"):
            MemoryItem("   ", ZoneId.WORKING, "payload", token_count=5, score=1.0)

    def test_orchestrator_never_raises_for_every_zone_missing(
        self, fixed_clock: FakeClock, event_bus: RecordingEventBus
    ) -> None:
        """Every one of the eight zones unregistered -- still a typed result, not an exception."""
        orchestrator = MemoryOrchestrator(
            zone_repositories={}, event_bus=event_bus, clock=fixed_clock
        )
        request = _request(zones=list(ZoneId))

        result = orchestrator.assemble_context(request)

        assert result.degraded is True
        assert len(result.zones_unavailable) == len(ZoneId)


class TestOrchestratorZoneQueryDelegation:
    """The Orchestrator strips routing-only fields before delegating to a zone."""

    def test_zone_query_carries_tenant_task_and_budget_inputs(
        self, fixed_clock: FakeClock, event_bus: RecordingEventBus
    ) -> None:
        repo = StubZoneRepository(items=[])
        orchestrator = MemoryOrchestrator(
            zone_repositories={ZoneId.WORKING: repo},
            event_bus=event_bus,
            clock=fixed_clock,
        )
        request = _request(
            zones=[ZoneId.WORKING],
            tenant_id="tenant-42",
            task="what happened",
            max_items=7,
            min_provenance_conf=0.5,
        )

        orchestrator.assemble_context(request)

        assert len(repo.received_queries) == 1
        query = repo.received_queries[0]
        assert query.tenant_id == "tenant-42"
        assert query.task == "what happened"
        assert query.max_items == 7
        assert query.min_provenance_conf == 0.5


class TestArchitectureFitnessInvariant1:
    """HLD 3.0 invariant 1: no module under domain/ imports infrastructure/.

    A real AST-based import-graph check, not a hand-wave grep: parses each
    domain/*.py module's import statements (both `import x` and
    `from x import y` forms) and asserts none resolve to a
    `dashanan.infrastructure` module or submodule.
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

    def test_domain_modules_exist_to_check(self) -> None:
        domain_files = sorted(DOMAIN_DIR.glob("*.py"))
        assert domain_files, f"No domain modules found under {DOMAIN_DIR}"

    def test_no_domain_module_imports_infrastructure(self) -> None:
        violations: dict[str, list[str]] = {}
        for domain_file in sorted(DOMAIN_DIR.glob("*.py")):
            imported = self._imported_module_names(domain_file)
            bad = [
                name
                for name in imported
                if name == "dashanan.infrastructure"
                or name.startswith("dashanan.infrastructure.")
            ]
            if bad:
                violations[domain_file.name] = bad

        assert not violations, (
            "HLD 3.0 invariant 1 violated -- domain module(s) import "
            f"infrastructure: {violations}"
        )


class TestContextAssemblyBuilderContract:
    """Builder-level checks that back the Orchestrator's documented contract."""

    def test_build_without_identifiers_raises_value_error(self) -> None:
        builder = ContextAssemblyBuilder().with_token_budget(100)
        with pytest.raises(ValueError, match="with_identifiers"):
            builder.build()

    def test_mark_zone_unavailable_is_idempotent_per_zone(self) -> None:
        builder = (
            ContextAssemblyBuilder()
            .with_token_budget(100)
            .with_identifiers("a1", "t1")
            .mark_zone_unavailable(ZoneId.WORKING)
            .mark_zone_unavailable(ZoneId.WORKING)
        )
        result = builder.build()
        assert result.zones_unavailable == [ZoneId.WORKING]
