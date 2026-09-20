"""Smoke assertions for DASH-STORY-012, inline per the dev subtask scope.

The formal pytest suite covering AC-003 / AC-003-SCHEMA-1 / AC-003-GF-1 /
AC-003-OWN-1 in full is the QA subtask's responsibility (see the story's
`qa_prompt`). These checks confirm the package is importable, wired
correctly, and that the must-not-deviate structural properties (exactly
two owned entity types, object_ref never blank at the domain layer, the
DB-level CHECK constraint's existence and wording, no cross-zone table
collision) hold, ahead of that formal suite landing -- the exact scope
split `tests/test_smoke_provenance.py` used for DASH-STORY-005.

Runtime assumptions recorded per rule 33/40 test-roadmap conventions:
  - clock: none needed -- SemanticEdge/GeneralFact carry no timestamp
    field in HLD Section 3.4's literal shape, so no fixed instant is
    required anywhere in this suite.
  - tenant_id: "tenant-1" for all requests unless a test states otherwise.
  - No fixture seed is required; nothing in this suite is randomized.

PII NOTE: per the dev_prompt's PII constraint, every illustration below
uses pseudonymized `<ENTITY_A>` / `<ENTITY_B>` placeholders or the literal
`<PII_EXAMPLE_REDACTED>` marker -- never HLD's own worked example's
realistic name ("Sachin", "Mumbai").
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from dashanan.domain.exceptions import ZoneRepositoryError
from dashanan.domain.semantic_memory import GeneralFact, SemanticEdge, SemanticState
from dashanan.infrastructure.sql_semantic_repository import (
    _FIND_EDGES_BY_OBJECT_SQL,
    _FIND_EDGES_BY_SUBJECT_SQL,
    _FIND_FACT_BY_ID_SQL,
    _INSERT_EDGE_SQL,
    _INSERT_FACT_SQL,
    SqlSemanticRepository,
)

_PLACEHOLDER_STATEMENT = "<PII_EXAMPLE_REDACTED>"
_SCHEMA_SQL_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "dashanan"
    / "infrastructure"
    / "semantic_schema.sql"
)


class RecordingCursor:
    """DB-API cursor double: records every execute() call, returns canned rows."""

    def __init__(self, rows: list[tuple[object, ...]] | None = None) -> None:
        self.executed: list[tuple[str, tuple[object, ...]]] = []
        self._rows = rows or []
        self._raise: Exception | None = None

    def execute(self, sql: str, params: Sequence[object]) -> None:
        self.executed.append((sql, tuple(params)))
        if self._raise is not None:
            raise self._raise

    def fetchall(self) -> list[tuple[object, ...]]:
        return list(self._rows)


class RecordingConnection:
    """DB-API connection double exposing one shared RecordingCursor."""

    def __init__(self, rows: list[tuple[object, ...]] | None = None) -> None:
        self.cursor_obj = RecordingCursor(rows)

    def cursor(self) -> RecordingCursor:
        return self.cursor_obj


def _edge(
    tenant_id: str = "tenant-1",
    edge_id: str = "edge-1",
    subject_ref: str = "entity-a",
    predicate: str = "related_to",
    object_ref: str = "entity-b",
) -> SemanticEdge:
    return SemanticEdge(
        tenant_id=tenant_id,
        edge_id=edge_id,
        subject_ref=subject_ref,
        predicate=predicate,
        object_ref=object_ref,
    )


def _fact(
    tenant_id: str = "tenant-1",
    fact_id: str = "fact-1",
    statement: str = _PLACEHOLDER_STATEMENT,
    subject_scope: str = "global",
) -> GeneralFact:
    return GeneralFact(
        tenant_id=tenant_id,
        fact_id=fact_id,
        statement=statement,
        subject_scope=subject_scope,
    )


def _edge_row(edge: SemanticEdge) -> tuple[object, ...]:
    return (
        edge.tenant_id,
        edge.edge_id,
        edge.subject_ref,
        edge.predicate,
        edge.object_ref,
        dict(edge.qualifiers),
        edge.state.value,
        dict(edge.score_terms),
    )


def _fact_row(fact: GeneralFact) -> tuple[object, ...]:
    return (
        fact.tenant_id,
        fact.fact_id,
        fact.statement,
        fact.subject_scope,
        fact.state.value,
        dict(fact.score_terms),
    )


class TestPackageWiring:
    """Baseline: the adapter constructs and is importable, before any AC-level suite."""

    def test_repository_constructs_with_fake_connection(self) -> None:
        repo = SqlSemanticRepository(RecordingConnection())
        assert repo is not None


class TestMustNotDeviateOnlyTwoOwnedEntityTypes:
    """Must-not-deviate item 1: SemanticEdge and GeneralFact are the only owned types."""

    def test_domain_module_exposes_exactly_two_entity_dataclasses(self) -> None:
        import dashanan.domain.semantic_memory as module

        entity_names = {
            name
            for name in vars(module)
            if isinstance(getattr(module, name), type)
            and getattr(module, name).__module__ == module.__name__
            and name not in {"SemanticState"}
        }
        assert entity_names == {"SemanticEdge", "GeneralFact"}


class TestAC003Schema1ObjectRefCheckConstraint:
    """AC-003-SCHEMA-1: object_ref IS NULL is rejected by a DB-level CHECK constraint."""

    def test_schema_declares_the_named_check_constraint(self) -> None:
        sql_text = _SCHEMA_SQL_PATH.read_text(encoding="utf-8")
        assert "chk_semantic_edges_object_ref_not_null" in sql_text
        assert "CHECK (object_ref IS NOT NULL)" in sql_text

    def test_object_ref_column_is_not_declared_not_null_directly(self) -> None:
        """The rejection must come from the named CHECK, not a plain column
        constraint -- confirms AC-003-SCHEMA-1's literal "CHECK constraint"
        wording is what actually fires, not an unrelated NOT NULL clause."""
        sql_text = _SCHEMA_SQL_PATH.read_text(encoding="utf-8")
        object_ref_line = next(
            line for line in sql_text.splitlines() if line.strip().startswith("object_ref")
        )
        assert "NOT NULL" not in object_ref_line

    def test_domain_layer_rejects_blank_object_ref_before_reaching_the_database(
        self,
    ) -> None:
        """Defense-in-depth (this story's dev report, judgment-call list):
        the domain guard fires first for an in-process caller, but the DB
        CHECK constraint above remains the binding enforcement."""
        with pytest.raises(ValueError, match="object_ref"):
            SemanticEdge(
                tenant_id="tenant-1",
                edge_id="edge-1",
                subject_ref="entity-a",
                predicate="related_to",
                object_ref="",
            )

    def test_insert_edge_does_not_pre_validate_beyond_domain_construction(
        self,
    ) -> None:
        """The adapter itself adds no second application-level gate --
        must-not-deviate item 2 names the DB constraint as the sanctioned
        enforcement, so `insert_edge` simply forwards a valid domain
        object's fields to the parameterized INSERT."""
        connection = RecordingConnection()
        repo = SqlSemanticRepository(connection)
        edge = _edge()

        repo.insert_edge(edge)

        assert len(connection.cursor_obj.executed) == 1
        sql_used, params_used = connection.cursor_obj.executed[0]
        assert sql_used == _INSERT_EDGE_SQL
        assert params_used[4] == "entity-b"  # object_ref position

    def test_insert_edge_wraps_a_check_violation_as_zone_repository_error(self) -> None:
        connection = RecordingConnection()
        connection.cursor_obj._raise = RuntimeError(
            "new row for relation \"semantic_edges\" violates check "
            "constraint \"chk_semantic_edges_object_ref_not_null\""
        )
        repo = SqlSemanticRepository(connection)

        with pytest.raises(ZoneRepositoryError):
            repo.insert_edge(_edge())


class TestAC003GF1GeneralFactRouting:
    """AC-003-GF-1: |subjects|==0 is stored as GeneralFact(tenant_id, fact_id,
    statement, subject_scope, state, score_terms)."""

    def test_general_fact_carries_exactly_the_hld_shape_fields(self) -> None:
        fact = _fact()
        assert fact.tenant_id == "tenant-1"
        assert fact.fact_id == "fact-1"
        assert fact.statement == _PLACEHOLDER_STATEMENT
        assert fact.subject_scope == "global"
        assert fact.state == SemanticState.ACTIVE
        assert fact.score_terms == {}

    def test_general_fact_has_no_subject_ref_or_object_ref_field(self) -> None:
        """A GeneralFact structurally cannot reference a subject entity --
        the |subjects|==0 precondition is enforced by the dataclass shape
        itself, not just by convention."""
        import dataclasses

        field_names = {f.name for f in dataclasses.fields(GeneralFact)}
        assert "subject_ref" not in field_names
        assert "object_ref" not in field_names

    def test_insert_general_fact_issues_a_single_insert_statement(self) -> None:
        connection = RecordingConnection()
        repo = SqlSemanticRepository(connection)

        repo.insert_general_fact(_fact())

        assert len(connection.cursor_obj.executed) == 1
        sql_used, params_used = connection.cursor_obj.executed[0]
        assert sql_used == _INSERT_FACT_SQL
        assert params_used[0] == "tenant-1"

    def test_find_fact_by_id_returns_none_when_no_row_matches(self) -> None:
        connection = RecordingConnection(rows=[])
        repo = SqlSemanticRepository(connection)

        assert repo.find_fact_by_id(tenant_id="tenant-1", fact_id="fact-1") is None

    def test_find_fact_by_id_returns_the_matching_fact(self) -> None:
        fact = _fact()
        connection = RecordingConnection(rows=[_fact_row(fact)])
        repo = SqlSemanticRepository(connection)

        found = repo.find_fact_by_id(tenant_id="tenant-1", fact_id="fact-1")

        sql_used, params_used = connection.cursor_obj.executed[0]
        assert sql_used == _FIND_FACT_BY_ID_SQL
        assert params_used == ("tenant-1", "fact-1")
        assert found is not None
        assert found.statement == _PLACEHOLDER_STATEMENT


class TestAC003OwnAndHardRule1NoSharedTable:
    """AC-003-OWN-1 / HLD Hard Rule 1: no other zone holds a table for these types."""

    def test_schema_creates_exactly_two_tables(self) -> None:
        sql_text = _SCHEMA_SQL_PATH.read_text(encoding="utf-8")
        created_tables = {
            line.split()[2]
            for line in sql_text.splitlines()
            if line.strip().upper().startswith("CREATE TABLE")
        }
        assert created_tables == {"semantic_edges", "general_facts"}

    def test_no_other_zone_schema_file_creates_these_table_names(self) -> None:
        other_schema_files = [
            p
            for p in _SCHEMA_SQL_PATH.parent.glob("*_schema.sql")
            if p.name != "semantic_schema.sql"
        ]
        assert other_schema_files, "expected sibling zone schema files to exist"
        for other in other_schema_files:
            text = other.read_text(encoding="utf-8")
            assert "CREATE TABLE semantic_edges" not in text
            assert "CREATE TABLE general_facts" not in text


class TestForwardAndReverseTraversal:
    """HLD Section 5 DSA row: two-sided B-tree, forward and reverse edge lookup."""

    def test_find_edges_by_subject_uses_the_subject_indexed_query(self) -> None:
        edge = _edge()
        connection = RecordingConnection(rows=[_edge_row(edge)])
        repo = SqlSemanticRepository(connection)

        found = repo.find_edges_by_subject(tenant_id="tenant-1", subject_ref="entity-a")

        sql_used, params_used = connection.cursor_obj.executed[0]
        assert sql_used == _FIND_EDGES_BY_SUBJECT_SQL
        assert params_used == ("tenant-1", "entity-a")
        assert len(found) == 1
        assert found[0].predicate == "related_to"

    def test_find_edges_by_object_uses_the_object_indexed_query(self) -> None:
        edge = _edge()
        connection = RecordingConnection(rows=[_edge_row(edge)])
        repo = SqlSemanticRepository(connection)

        found = repo.find_edges_by_object(tenant_id="tenant-1", object_ref="entity-b")

        sql_used, params_used = connection.cursor_obj.executed[0]
        assert sql_used == _FIND_EDGES_BY_OBJECT_SQL
        assert params_used == ("tenant-1", "entity-b")
        assert len(found) == 1

    def test_schema_indexes_both_subject_ref_and_object_ref(self) -> None:
        sql_text = _SCHEMA_SQL_PATH.read_text(encoding="utf-8")
        assert "idx_semantic_edges_subject" in sql_text
        assert "idx_semantic_edges_object" in sql_text


class TestMustNotDeviateTenantIdRequired:
    """Structural extension of HLD 3.0 invariant 2 to this story's own repository."""

    def test_find_edges_by_subject_rejects_blank_tenant_id_before_querying(
        self,
    ) -> None:
        connection = RecordingConnection()
        repo = SqlSemanticRepository(connection)

        with pytest.raises(ValueError, match="tenant_id"):
            repo.find_edges_by_subject(tenant_id="", subject_ref="entity-a")
        assert connection.cursor_obj.executed == []

    def test_insert_edge_rejects_blank_tenant_id_before_querying(self) -> None:
        connection = RecordingConnection()
        repo = SqlSemanticRepository(connection)
        edge = _edge(tenant_id="tenant-1")
        object.__setattr__(edge, "tenant_id", "")

        with pytest.raises(ValueError, match="tenant_id"):
            repo.insert_edge(edge)
        assert connection.cursor_obj.executed == []

    def test_insert_general_fact_rejects_blank_tenant_id_before_querying(self) -> None:
        connection = RecordingConnection()
        repo = SqlSemanticRepository(connection)
        fact = _fact(tenant_id="tenant-1")
        object.__setattr__(fact, "tenant_id", "")

        with pytest.raises(ValueError, match="tenant_id"):
            repo.insert_general_fact(fact)
        assert connection.cursor_obj.executed == []


class TestBoundaryAndNegativeCases:
    """Boundary/negative cases per rule 33/40 test-roadmap conventions."""

    def test_semantic_edge_rejects_blank_subject_ref(self) -> None:
        with pytest.raises(ValueError, match="subject_ref"):
            SemanticEdge(
                tenant_id="tenant-1",
                edge_id="edge-1",
                subject_ref=" ",
                predicate="related_to",
                object_ref="entity-b",
            )

    def test_semantic_edge_rejects_blank_predicate(self) -> None:
        with pytest.raises(ValueError, match="predicate"):
            SemanticEdge(
                tenant_id="tenant-1",
                edge_id="edge-1",
                subject_ref="entity-a",
                predicate="",
                object_ref="entity-b",
            )

    def test_general_fact_rejects_blank_statement(self) -> None:
        with pytest.raises(ValueError, match="statement"):
            GeneralFact(
                tenant_id="tenant-1",
                fact_id="fact-1",
                statement="   ",
                subject_scope="global",
            )

    def test_general_fact_rejects_blank_subject_scope(self) -> None:
        with pytest.raises(ValueError, match="subject_scope"):
            GeneralFact(
                tenant_id="tenant-1",
                fact_id="fact-1",
                statement=_PLACEHOLDER_STATEMENT,
                subject_scope="",
            )

    def test_find_edges_by_subject_wraps_underlying_failure_as_zone_repository_error(
        self,
    ) -> None:
        connection = RecordingConnection()
        connection.cursor_obj._raise = RuntimeError("connection refused")
        repo = SqlSemanticRepository(connection)

        with pytest.raises(ZoneRepositoryError):
            repo.find_edges_by_subject(tenant_id="tenant-1", subject_ref="entity-a")

    def test_find_edges_by_subject_rejects_blank_subject_ref(self) -> None:
        repo = SqlSemanticRepository(RecordingConnection())
        with pytest.raises(ValueError, match="subject_ref"):
            repo.find_edges_by_subject(tenant_id="tenant-1", subject_ref="")
