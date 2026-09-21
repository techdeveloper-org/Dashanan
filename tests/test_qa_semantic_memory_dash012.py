"""QA pytest suite for DASH-STORY-012 (Zone 3 Semantic Memory, FR-003).

QA subtask (20% of story points, backlog_draft.json) -- independent
verification pass on top of the Dev subtask's own suite
(tests/test_smoke_semantic.py). This suite enumerates every AC named in
the qa_prompt as a Given/When/Then test scenario (happy path, boundary,
adverse), states the assertion proving/disproving it, then implements it.

FR-003 (verbatim, SRS.md): "The system SHALL provide a Semantic Memory
zone holding distilled, deduplicated cross-entity relationships and
general facts not owned by any single entity record, per the locked
Zone 3/Zone 5 ownership ADR."

ACCEPTANCE CRITERIA UNDER TEST:
  AC-003: Given a candidate fact that spans two or more entities, When it
    is classified per the Zone 3/Zone 5 ownership ADR (HLD Section 3.4,
    ADR-006), Then it is stored in Semantic Memory, not duplicated into
    any single Entity record.
  AC-003-SCHEMA-1: Given a write attempts to persist a SemanticEdge with
    object_ref IS NULL, When submitted to Zone 3's store, Then the
    database-level CHECK constraint rejects it, making the R-005
    non-duplication regression structurally impossible.
  AC-003-GF-1: Given a candidate fact with zero subject entity references
    (|subjects|==0), When classified, Then it is stored as a Zone 3
    GeneralFact(tenant_id, fact_id, statement, subject_scope, state,
    score_terms).
  AC-003-OWN-1: Given a SemanticEdge or GeneralFact record exists in Zone
    3, When any other zone's table is inspected, Then no other zone
    holds a table for these record types, per HLD Section 3.10 Hard
    Rule 1.

TEST SCENARIO ENUMERATION (per qa_prompt's "think step by step" order):
  1. AC-003 happy path: a two-entity fact is stored as a SemanticEdge,
     never as an attribute write on any Entity/Zone-5 shape (this
     codebase has no Entity-write API in this suite's scope -- the
     assertion is structural: SemanticEdge is the only shape a
     two-entity fact can take, and it carries no Entity-record fields).
  2. AC-003 adverse: constructing the routing target with a missing
     object_ref must fail before ever reaching a store -- proven via
     AC-003-SCHEMA-1's own domain-layer guard.
  3. AC-003-SCHEMA-1 happy path: the schema declares the named CHECK
     constraint with the exact rejecting predicate.
  4. AC-003-SCHEMA-1 boundary: an empty-string object_ref (not None) is
     also rejected by the domain guard -- the boundary between "blank"
     and "present" is a stripped, non-empty string.
  5. AC-003-SCHEMA-1 adverse: a real CHECK-constraint violation raised by
     the underlying driver is converted to a typed ZoneRepositoryError,
     never leaked as a raw driver exception (error-handling-patterns).
  6. AC-003-GF-1 happy path: a |subjects|==0 fact is stored as a
     GeneralFact carrying exactly the six HLD-named fields.
  7. AC-003-GF-1 boundary: GeneralFact's dataclass shape structurally
     excludes subject_ref/object_ref -- the |subjects|==0 invariant is
     enforced by the type itself, not by convention.
  8. AC-003-GF-1 adverse: GeneralFact construction rejects a blank
     statement or subject_scope before any insert is attempted.
  9. AC-003-OWN-1 happy path: semantic_schema.sql creates exactly the two
     Zone-3-owned tables (semantic_edges, general_facts).
  10. AC-003-OWN-1 adverse: no sibling zone schema file (episodic,
      provenance, zone8-consolidation) declares a table with either
      name -- the cross-zone collision this AC forbids.
  11. Must-not-deviate cross-check: SqlSemanticRepository issues exactly
      the four SQL statements this story's scope allows (find x2,
      insert x2) -- no update/delete method exists yet.
  12. Golden regression guard: one pinned test per AC ID that must never
      be deleted by a later story touching this component.

Runtime assumptions (recorded per rule 33/40 test-roadmap conventions):
  - clock: none required -- SemanticEdge/GeneralFact carry no timestamp
    field in HLD Section 3.4's literal shape.
  - tenant_id: "tenant-1" for every request unless a test states
    otherwise.
  - No fixture seed is required; nothing in this suite is randomized.

PII NOTE: every illustration below uses pseudonymized `<ENTITY_A>` /
`<ENTITY_B>` placeholders or the literal `<PII_EXAMPLE_REDACTED>` marker,
never HLD's own worked example's realistic name ("Sachin", "Mumbai").
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from pathlib import Path

import pytest

from dashanan.domain.exceptions import ZoneRepositoryError
from dashanan.domain.semantic_memory import GeneralFact, SemanticEdge, SemanticState
from dashanan.infrastructure.sql_semantic_repository import (
    _INSERT_EDGE_SQL,
    _INSERT_FACT_SQL,
    SqlSemanticRepository,
)

_PLACEHOLDER_STATEMENT = "<PII_EXAMPLE_REDACTED>"
_INFRA_DIR = (
    Path(__file__).resolve().parents[1] / "src" / "dashanan" / "infrastructure"
)
_SCHEMA_SQL_PATH = _INFRA_DIR / "semantic_schema.sql"


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
    subject_ref: str = "<ENTITY_A>",
    predicate: str = "related_to",
    object_ref: str = "<ENTITY_B>",
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


class TestAC003TwoEntityFactIsSemanticEdgeNeverEntityOwned:
    """AC-003: a fact spanning two or more entities is Semantic-Memory-owned.

    Golden regression guard for AC-003.
    """

    def test_two_entity_fact_is_representable_only_as_semantic_edge(self) -> None:
        """Happy path: |subjects|>=1 and |objects|>=1 maps onto SemanticEdge,
        whose shape carries subject_ref AND object_ref -- the two-entity
        span AC-003 describes -- and no Entity-record field (no `attributes`,
        no `entity_id` primary key shape) that would let this same fact be
        duplicated into a single Entity record."""
        edge = _edge(subject_ref="<ENTITY_A>", object_ref="<ENTITY_B>")

        field_names = {f.name for f in dataclasses.fields(SemanticEdge)}
        assert {"subject_ref", "object_ref"} <= field_names
        assert "attributes" not in field_names, (
            "SemanticEdge must not carry an Entity-record-shaped "
            "'attributes' field -- that would allow the R-005 "
            "duplication AC-003 forbids"
        )
        assert edge.subject_ref == "<ENTITY_A>"
        assert edge.object_ref == "<ENTITY_B>"

    def test_two_entity_fact_never_routes_to_general_fact_shape(self) -> None:
        """Adverse/boundary: a two-entity fact (|subjects|>=1, |objects|>=1)
        must never be constructible as a GeneralFact, since GeneralFact's
        shape has no object_ref -- it structurally cannot represent a
        cross-entity edge, so the routing decision cannot silently produce
        the wrong owned-entity type for a two-entity fact."""
        field_names = {f.name for f in dataclasses.fields(GeneralFact)}
        assert "object_ref" not in field_names
        assert "subject_ref" not in field_names


class TestAC003Schema1ObjectRefCheckConstraint:
    """AC-003-SCHEMA-1: object_ref IS NULL is rejected by a DB-level CHECK.

    Golden regression guard for AC-003-SCHEMA-1.
    """

    def test_schema_declares_named_check_constraint_with_exact_predicate(
        self,
    ) -> None:
        """Happy path: the schema file names the constraint and the exact
        rejecting predicate AC-003-SCHEMA-1 requires."""
        sql_text = _SCHEMA_SQL_PATH.read_text(encoding="utf-8")
        assert "CONSTRAINT chk_semantic_edges_object_ref_not_null" in sql_text
        assert "CHECK (object_ref IS NOT NULL)" in sql_text

    def test_object_ref_column_has_no_column_level_not_null(self) -> None:
        """Boundary: the rejection must come from the named CHECK, not a
        plain column-level NOT NULL -- otherwise the literal wording of
        AC-003-SCHEMA-1 ("the database-level CHECK constraint rejects it")
        would not describe what actually fires."""
        sql_text = _SCHEMA_SQL_PATH.read_text(encoding="utf-8")
        object_ref_line = next(
            line
            for line in sql_text.splitlines()
            if line.strip().startswith("object_ref")
        )
        assert "NOT NULL" not in object_ref_line

    def test_semantic_edge_rejects_none_object_ref_before_reaching_store(
        self,
    ) -> None:
        """Adverse: constructing a SemanticEdge with a missing object_ref
        fails at the domain layer -- defense-in-depth ahead of the DB
        constraint, never a substitute for it (must-not-deviate item 2)."""
        with pytest.raises(ValueError, match="object_ref"):
            SemanticEdge(
                tenant_id="tenant-1",
                edge_id="edge-1",
                subject_ref="<ENTITY_A>",
                predicate="related_to",
                object_ref="",
            )

    def test_semantic_edge_rejects_whitespace_only_object_ref(self) -> None:
        """Boundary: a whitespace-only object_ref ("   ") is the same as
        blank -- the domain guard must strip before checking, not treat
        whitespace as a present value."""
        with pytest.raises(ValueError, match="object_ref"):
            SemanticEdge(
                tenant_id="tenant-1",
                edge_id="edge-1",
                subject_ref="<ENTITY_A>",
                predicate="related_to",
                object_ref="   ",
            )

    def test_insert_edge_forwards_a_valid_object_ref_unmodified(self) -> None:
        """Happy path: a valid SemanticEdge's object_ref reaches the
        parameterized INSERT unchanged -- the adapter adds no second gate
        beyond domain construction (must-not-deviate item 2)."""
        connection = RecordingConnection()
        repo = SqlSemanticRepository(connection)
        edge = _edge(object_ref="<ENTITY_B>")

        repo.insert_edge(edge)

        sql_used, params_used = connection.cursor_obj.executed[0]
        assert sql_used == _INSERT_EDGE_SQL
        assert params_used[4] == "<ENTITY_B>"

    def test_insert_edge_wraps_a_real_check_violation_as_zone_repository_error(
        self,
    ) -> None:
        """Adverse: if a CHECK violation somehow reaches the driver (e.g. a
        future caller bypassing the domain guard via a raw row), the
        adapter converts it to the typed ZoneRepositoryError, never a raw
        driver exception (error-handling-patterns: no bare propagation)."""
        connection = RecordingConnection()
        connection.cursor_obj._raise = RuntimeError(
            'new row for relation "semantic_edges" violates check '
            'constraint "chk_semantic_edges_object_ref_not_null"'
        )
        repo = SqlSemanticRepository(connection)

        with pytest.raises(ZoneRepositoryError) as exc_info:
            repo.insert_edge(_edge())
        assert exc_info.value.zone == "semantic"


class TestAC003GF1GeneralFactRouting:
    """AC-003-GF-1: |subjects|==0 is stored as the six-field GeneralFact shape.

    Golden regression guard for AC-003-GF-1.
    """

    def test_zero_subject_fact_carries_exactly_the_hld_named_fields(self) -> None:
        """Happy path: GeneralFact(tenant_id, fact_id, statement,
        subject_scope, state, score_terms) -- the exact six fields HLD
        Section 3.4 names, no more, no fewer."""
        fact = _fact(
            tenant_id="tenant-1",
            fact_id="fact-1",
            statement=_PLACEHOLDER_STATEMENT,
            subject_scope="global",
        )

        field_names = {f.name for f in dataclasses.fields(GeneralFact)}
        assert field_names == {
            "tenant_id",
            "fact_id",
            "statement",
            "subject_scope",
            "state",
            "score_terms",
        }
        assert fact.tenant_id == "tenant-1"
        assert fact.fact_id == "fact-1"
        assert fact.statement == _PLACEHOLDER_STATEMENT
        assert fact.subject_scope == "global"
        assert fact.state == SemanticState.ACTIVE
        assert fact.score_terms == {}

    def test_general_fact_structurally_cannot_reference_a_subject_entity(
        self,
    ) -> None:
        """Boundary: the |subjects|==0 precondition is enforced by the
        dataclass shape itself -- no subject_ref/object_ref field exists
        to be populated, not merely "left blank by convention"."""
        field_names = {f.name for f in dataclasses.fields(GeneralFact)}
        assert "subject_ref" not in field_names
        assert "object_ref" not in field_names

    def test_general_fact_rejects_blank_statement(self) -> None:
        """Adverse: a blank statement is rejected before ever reaching
        insert_general_fact."""
        with pytest.raises(ValueError, match="statement"):
            GeneralFact(
                tenant_id="tenant-1",
                fact_id="fact-1",
                statement="   ",
                subject_scope="global",
            )

    def test_general_fact_rejects_blank_subject_scope(self) -> None:
        """Adverse: a blank subject_scope is rejected before insert."""
        with pytest.raises(ValueError, match="subject_scope"):
            GeneralFact(
                tenant_id="tenant-1",
                fact_id="fact-1",
                statement=_PLACEHOLDER_STATEMENT,
                subject_scope="",
            )

    def test_insert_general_fact_issues_exactly_one_parameterized_insert(
        self,
    ) -> None:
        """Happy path: insert_general_fact issues exactly the expected SQL
        with tenant_id first, never a string-concatenated statement
        (application-security-core: parameterized queries only)."""
        connection = RecordingConnection()
        repo = SqlSemanticRepository(connection)

        repo.insert_general_fact(_fact())

        assert len(connection.cursor_obj.executed) == 1
        sql_used, params_used = connection.cursor_obj.executed[0]
        assert sql_used == _INSERT_FACT_SQL
        assert params_used[0] == "tenant-1"
        assert "%s" in sql_used
        assert _PLACEHOLDER_STATEMENT not in sql_used, (
            "insert_general_fact must never string-concatenate the "
            "statement into the SQL text"
        )

    def test_find_fact_by_id_round_trips_all_six_fields(self) -> None:
        """Happy path: a fact written and read back preserves every field,
        proving the adapter's row-mapping does not silently drop a
        HLD-named field."""
        fact = _fact(fact_id="fact-42", subject_scope="regional")
        row = (
            fact.tenant_id,
            fact.fact_id,
            fact.statement,
            fact.subject_scope,
            fact.state.value,
            dict(fact.score_terms),
        )
        connection = RecordingConnection(rows=[row])
        repo = SqlSemanticRepository(connection)

        found = repo.find_fact_by_id(tenant_id="tenant-1", fact_id="fact-42")

        assert found is not None
        assert found.tenant_id == fact.tenant_id
        assert found.fact_id == fact.fact_id
        assert found.statement == fact.statement
        assert found.subject_scope == fact.subject_scope
        assert found.state == fact.state
        assert found.score_terms == fact.score_terms


class TestAC003Own1NoOtherZoneHoldsTheseTables:
    """AC-003-OWN-1 / HLD Hard Rule 1: no other zone shares Zone 3's tables.

    Golden regression guard for AC-003-OWN-1.
    """

    def test_semantic_schema_creates_exactly_the_two_zone3_owned_tables(
        self,
    ) -> None:
        """Happy path: semantic_edges and general_facts are the only two
        tables this file creates (must-not-deviate item 1: no third owned
        entity type)."""
        sql_text = _SCHEMA_SQL_PATH.read_text(encoding="utf-8")
        created_tables = {
            line.split()[2]
            for line in sql_text.splitlines()
            if line.strip().upper().startswith("CREATE TABLE")
        }
        assert created_tables == {"semantic_edges", "general_facts"}

    def test_no_sibling_zone_schema_file_declares_either_table_name(
        self,
    ) -> None:
        """Adverse: iterate every OTHER *_schema.sql file in the
        infrastructure directory and confirm none of them declares
        CREATE TABLE semantic_edges or CREATE TABLE general_facts -- the
        exact cross-zone table collision AC-003-OWN-1 forbids."""
        sibling_schema_files = sorted(
            p
            for p in _INFRA_DIR.glob("*_schema.sql")
            if p.name != "semantic_schema.sql"
        )
        assert sibling_schema_files, (
            "expected at least one sibling zone schema file to exist "
            "for this cross-zone check to be meaningful"
        )
        for sibling in sibling_schema_files:
            text = sibling.read_text(encoding="utf-8")
            assert "CREATE TABLE semantic_edges" not in text, (
                f"{sibling.name} must not declare Zone 3's semantic_edges "
                "table (HLD Section 3.10 Hard Rule 1)"
            )
            assert "CREATE TABLE general_facts" not in text, (
                f"{sibling.name} must not declare Zone 3's general_facts "
                "table (HLD Section 3.10 Hard Rule 1)"
            )

    def test_semantic_role_grants_are_scoped_to_only_these_two_tables(
        self,
    ) -> None:
        """Boundary: dashanan_semantic_role's GRANT statement names only
        the two Zone-3-owned tables -- it must not be widened to grant
        access to another zone's table (ADR-006 'separate roles per
        zone')."""
        sql_text = _SCHEMA_SQL_PATH.read_text(encoding="utf-8")
        grant_line = next(
            line
            for line in sql_text.splitlines()
            if line.strip().startswith("GRANT SELECT, INSERT, UPDATE")
        )
        assert "semantic_edges" in grant_line
        assert "general_facts" in grant_line
        assert "episodic_entries" not in grant_line
        assert "provenance_records" not in grant_line

    def test_semantic_role_is_never_granted_delete(self) -> None:
        """Adverse: DELETE must never appear in dashanan_semantic_role's
        grant -- a future DPDP erasure cascade runs under a separate,
        narrowly-scoped compliance role, not this application role."""
        sql_text = _SCHEMA_SQL_PATH.read_text(encoding="utf-8")
        grant_line = next(
            line
            for line in sql_text.splitlines()
            if "dashanan_semantic_role" in line and "GRANT" in line
        )
        assert "DELETE" not in grant_line


class TestMustNotDeviateRepositoryScope:
    """Cross-check: the adapter exposes exactly this story's sanctioned surface."""

    def test_no_update_or_delete_method_exists_on_the_adapter(self) -> None:
        """State-machine transitions and score-term recomputation are the
        Rotation Engine's / Scoring Service's own write paths -- adding an
        update/delete here would be scope creep beyond this schema-only
        story.

        UPDATED by DASH-STORY-025 (DSHN-70, AC-025-1): `delete_edges_by_
        subject` is the one sanctioned exception to this class's DASH-012
        no-update/no-delete scope -- AC-025-1's own literal DPDP erasure
        requirement authorizes exactly this one subject-keyed delete
        method, never a generic `update`/`delete`. The bare `"update"`/
        `"delete"` exclusion checks below remain unchanged and still pass.
        """
        public_methods = {
            name
            for name in vars(SqlSemanticRepository)
            if not name.startswith("_")
        }
        assert "update" not in public_methods
        assert "delete" not in public_methods
        assert public_methods == {
            "find_edges_by_subject",
            "find_edges_by_object",
            "find_fact_by_id",
            "insert_edge",
            "insert_general_fact",
            "delete_edges_by_subject",
        }

    def test_every_public_method_validates_tenant_id_first(self) -> None:
        """HLD 3.0 invariant 2, extended to this story's own adapter: every
        public method rejects a blank tenant_id before issuing any query."""
        connection = RecordingConnection()
        repo = SqlSemanticRepository(connection)

        with pytest.raises(ValueError, match="tenant_id"):
            repo.find_edges_by_object(tenant_id="", object_ref="<ENTITY_B>")
        assert connection.cursor_obj.executed == []
