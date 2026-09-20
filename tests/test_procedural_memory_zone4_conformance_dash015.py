"""Structural/architecture-conformance suite for DASH-STORY-015 (Zone 4, FR-004).

Dev subtask's own verification pass for the two ACs that are structural
guarantees rather than behavior:

  AC-004-2 (verbatim): "Zone 4's adapter port exposes only point-lookup,
  no similarity-search method; any similarity need routes exclusively
  through Zone 6. Enforced by a contract test asserting the Zone 4 adapter
  interface contains no similarity-search operation."

  AC-004-5 (verbatim): "A static dependency/import-graph audit against
  the Zone 4 module finds no import or call to any other zone's adapter
  interface, enforced as an automated architecture-conformance CI check
  (import-linter / dependency-cruiser)."

Judgment call (shared_file_request, not made): AC-004-5 names
import-linter/dependency-cruiser by example. Wiring either as a real CI
gate needs a `[tool.importlinter]` contract (or a dependency-cruiser
config) in `pyproject.toml`, and adding the `import-linter` package to
`requirements-dev.txt` -- both are shared project files outside this
story's own component scope (FILE-DISJOINTNESS), so this suite implements
the equivalent audit natively via the stdlib `ast` module instead of
editing them. Reported as a `shared_file_request` in this story's
implementation report: a maintainer who wants the formal import-linter
tool wired into `pyproject.toml`/CI should do that as its own change.
"""

from __future__ import annotations

import ast
import datetime
from pathlib import Path

import pytest

from dashanan.domain.procedural_memory_ports import ProcedureRepositoryPort
from dashanan.infrastructure.in_memory_procedural_memory_repository import (
    InMemoryProceduralMemoryRepository,
)

_SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "dashanan"

_ZONE_4_MODULE_FILES = (
    _SRC_ROOT / "domain" / "procedure.py",
    _SRC_ROOT / "domain" / "procedural_memory_ports.py",
    _SRC_ROOT / "infrastructure" / "in_memory_procedural_memory_repository.py",
)

_ALLOWED_DASHANAN_MODULES = frozenset(
    {
        # The Zone 4 module's own files.
        "dashanan.domain.procedure",
        "dashanan.domain.procedural_memory_ports",
        "dashanan.infrastructure.in_memory_procedural_memory_repository",
        # The shared domain kernel every zone is allowed to depend on
        # (HLD Section 3.11: "Zone N depends on nothing [else] -- zones
        # are leaves by design"; these four modules carry no per-zone
        # adapter logic, only cross-zone-shared contracts/value types).
        "dashanan.domain.ports",
        "dashanan.domain.exceptions",
        "dashanan.domain.memory_item",
        "dashanan.domain.zone",
    }
)

_FORBIDDEN_METHOD_NAME_SUBSTRINGS = ("search", "similar", "vector", "embed", "knn")
"""Substrings a similarity-search operation's name would plausibly contain
(mirrors `VectorIndexPort.search`/`LexicalIndexPort.search` naming in
`retrieval_index_ports.py`) -- AC-004-2's contract test asserts none of
these appear on Zone 4's own adapter port.
"""


def _imported_dashanan_modules(file_path: Path) -> set[str]:
    """Collect every `dashanan.*` module dotted-path this file imports.

    Static (AST-based) equivalent of an import-linter contract check:
    walks every `import`/`from ... import ...` statement without
    executing the module, so this is a true "static dependency/import-
    graph audit" (AC-004-5's own wording).
    """
    tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("dashanan"):
                    modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and (
            node.module and node.module.startswith("dashanan")
        ):
            modules.add(node.module)
    return modules


class TestAC0042NoSimilaritySearchOnTheAdapterPort:
    """AC-004-2's own contract test."""

    def test_procedure_repository_port_has_no_similarity_search_method(self) -> None:
        method_names = [
            name
            for name in vars(ProcedureRepositoryPort)
            if not name.startswith("_")
        ]

        assert method_names, "ProcedureRepositoryPort must declare at least one method"
        for name in method_names:
            lowered = name.lower()
            assert not any(
                forbidden in lowered for forbidden in _FORBIDDEN_METHOD_NAME_SUBSTRINGS
            ), (
                f"ProcedureRepositoryPort.{name} looks like a similarity-search "
                "operation; AC-004-2 requires Zone 4's adapter port to expose "
                "point-lookup only -- any similarity need must route through "
                "Zone 6 instead"
            )

    def test_procedure_repository_port_exposes_exactly_point_lookup_and_commit(
        self,
    ) -> None:
        method_names = {
            name for name in vars(ProcedureRepositoryPort) if not name.startswith("_")
        }

        assert method_names == {"get_by_task_signature_hash", "commit"}

    def test_concrete_adapter_satisfies_the_port_via_structural_typing(self) -> None:
        repo = InMemoryProceduralMemoryRepository(
            event_bus=_NoOpEventBus(), clock=_FixedClock()
        )

        assert isinstance(repo, ProcedureRepositoryPort)


class TestAC0045ImportGraphAudit:
    """AC-004-5's own static import-graph audit."""

    @pytest.mark.parametrize("file_path", _ZONE_4_MODULE_FILES, ids=lambda p: p.name)
    def test_zone4_module_file_imports_no_other_zones_adapter_interface(
        self, file_path: Path
    ) -> None:
        imported = _imported_dashanan_modules(file_path)
        disallowed = imported - _ALLOWED_DASHANAN_MODULES

        assert not disallowed, (
            f"{file_path.name} imports {sorted(disallowed)}, which is outside "
            "the shared domain kernel; HLD Section 3.11 ('Zone N depends on "
            "nothing -- zones are leaves by design') and must-not-deviate "
            "item 2 forbid the Zone 4 module from importing any other zone's "
            "adapter interface"
        )

    def test_zone4_module_never_imports_another_zones_infrastructure_adapter(
        self,
    ) -> None:
        """Belt-and-suspenders check against the concrete other-zone adapter
        modules that exist in this codebase today, named explicitly so a
        future addition to `_ALLOWED_DASHANAN_MODULES` cannot silently
        re-open this door.
        """
        known_other_zone_or_cross_cutting_modules = {
            "dashanan.infrastructure.working_memory_lru_repository",
            "dashanan.infrastructure.sql_episodic_repository",
            "dashanan.infrastructure.sql_provenance_repository",
            "dashanan.infrastructure.sql_write_journal_repository",
            "dashanan.infrastructure.hybrid_retrieval_index_repository",
            "dashanan.infrastructure.in_memory_vector_index",
            "dashanan.infrastructure.in_memory_lexical_index",
            "dashanan.infrastructure.dpdp_erasure_cascade",
            "dashanan.domain.retrieval_index_ports",
            "dashanan.application.memory_orchestrator",
            "dashanan.application.zone2_capacity_backstop_sweep",
            "dashanan.application.provenance_write_gate",
            "dashanan.application.rotation_engine",
            "dashanan.application.conflict_detection_sweep",
        }

        for file_path in _ZONE_4_MODULE_FILES:
            imported = _imported_dashanan_modules(file_path)
            overlap = imported & known_other_zone_or_cross_cutting_modules
            assert not overlap, (
                f"{file_path.name} imports {sorted(overlap)} -- a direct "
                "dependency on another zone's adapter or a cross-cutting "
                "application service, forbidden for a Zone 4 leaf module"
            )


class _NoOpEventBus:
    """Minimal EventBus double, local to this file to avoid a cross-test import."""

    def publish(self, event_type: str, payload: dict[str, object]) -> None:
        return None


class _FixedClock:
    """Minimal Clock double, local to this file to avoid a cross-test import."""

    def now(self) -> datetime.datetime:
        return datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
