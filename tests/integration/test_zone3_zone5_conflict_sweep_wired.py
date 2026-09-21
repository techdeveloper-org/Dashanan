"""FR-013 conflict-detection sweep, wired against the REAL Zone 3/Zone 5 adapters.

DASH-STORY-022. Mirrors `tests/integration/test_conflict_detection_sweep_wired.py`'s
own "close the gap: run the sweep against real, non-mock collaborators, not
only an isolated fake" pattern, extended to this story's two new call sites:
every collaborator here is the real, non-mock production class --
`SqlSemanticRepository` (Zone 3) and `EntityMemoryRepository` (Zone 5) issuing
their own real writes, `SqlProvenanceRepository` issuing real parameterized
SQL, wrapped by the real `ConflictDetectingProvenanceRepository` decorator --
composed exactly the way `infrastructure.composition_root.
build_conflict_aware_semantic_repository`/`build_conflict_aware_entity_memory_repository`
compose them (must-not-deviate item 3: through `build_provenance_repository`,
never a second, parallel wrapper).

PII NOTE: only pseudonymized item_id/provenance_id values and placeholder
retrieval_context_hash/statement content appear below, mirroring
`test_conflict_detection_sweep_wired.py`'s identical posture.
"""

from __future__ import annotations

import hashlib

from dashanan.domain.provenance_record import ConflictStatus, SourceType
from dashanan.domain.semantic_memory import GeneralFact, SemanticEdge
from dashanan.domain.write_gate import WriteAccepted
from dashanan.infrastructure.composition_root import (
    build_conflict_aware_entity_memory_repository,
    build_conflict_aware_semantic_repository,
)
from dashanan.infrastructure.entity_memory_repository import EntityMemoryRepository

from .conftest import DEFAULT_TENANT_ID, SeedableClock
from tests.test_smoke_episodic import RecordingConnection as ProvenanceRecordingConnection
from tests.test_smoke_semantic import RecordingConnection as SemanticRecordingConnection

_PLACEHOLDER_CONTEXT_HASH = hashlib.sha256(b"<PII_EXAMPLE_REDACTED>").hexdigest()


class TestZone3ConflictSweepRunsAgainstTheRealSemanticAdapter:
    """AC-022-1/AC-022-2: `SqlSemanticRepository.insert_edge` through the real sweep."""

    def test_non_conflicting_edge_write_issues_the_sweeps_select_then_two_real_inserts(
        self, clock: SeedableClock
    ) -> None:
        semantic_connection = SemanticRecordingConnection()
        provenance_connection = ProvenanceRecordingConnection()
        writer = build_conflict_aware_semantic_repository(
            semantic_connection,
            provenance_connection,
            clock,
            verify_privileges=False,
        )
        edge = SemanticEdge(
            tenant_id=DEFAULT_TENANT_ID,
            edge_id="edge-1",
            subject_ref="entity-a",
            predicate="related_to",
            object_ref="entity-b",
        )

        writer.insert_edge(
            edge,
            provenance_id="prov-1",
            source_type=SourceType.USER_STATED,
            retrieval_context_hash=_PLACEHOLDER_CONTEXT_HASH,
        )

        provenance_executed = provenance_connection.cursor_obj.executed
        assert len(provenance_executed) == 2, (
            "expected exactly one real SELECT (the sweep's own "
            "find_by_item_id) followed by one real provenance INSERT"
        )
        select_sql, _ = provenance_executed[0]
        provenance_insert_sql, _ = provenance_executed[1]
        assert select_sql.strip().upper().startswith("SELECT")
        assert provenance_insert_sql.strip().upper().startswith(
            "INSERT INTO PROVENANCE_RECORDS"
        )

        semantic_executed = semantic_connection.cursor_obj.executed
        assert len(semantic_executed) == 1
        semantic_insert_sql, semantic_insert_params = semantic_executed[0]
        assert semantic_insert_sql.strip().upper().startswith("INSERT INTO SEMANTIC_EDGES")
        assert semantic_insert_params[1] == "edge-1"

    def test_contradicting_edge_write_issues_real_sql_for_both_downgrade_writes(
        self, clock: SeedableClock
    ) -> None:
        semantic_connection = SemanticRecordingConnection()
        provenance_connection = ProvenanceRecordingConnection()
        writer = build_conflict_aware_semantic_repository(
            semantic_connection,
            provenance_connection,
            clock,
            verify_privileges=False,
        )
        first = SemanticEdge(
            tenant_id=DEFAULT_TENANT_ID,
            edge_id="edge-1",
            subject_ref="entity-a",
            predicate="related_to",
            object_ref="entity-b",
        )
        writer.insert_edge(
            first,
            provenance_id="wired-prov-1",
            source_type=SourceType.USER_STATED,
            retrieval_context_hash=_PLACEHOLDER_CONTEXT_HASH,
        )

        # Simulate the just-appended provenance row being SELECT-able for
        # the next find_by_item_id call -- the fake transport has no live
        # backing store, mirrors `test_conflict_detection_sweep_wired.py`'s
        # identical seeding pattern.
        provenance_cursor = provenance_connection.cursor_obj
        _, first_insert_params = provenance_cursor.executed[-1]
        provenance_cursor._rows = [first_insert_params]

        clock.advance(300)
        second = SemanticEdge(
            tenant_id=DEFAULT_TENANT_ID,
            edge_id="edge-2",
            subject_ref="entity-a",
            predicate="related_to",
            object_ref="entity-b",
        )
        executed_before = len(provenance_cursor.executed)
        writer.insert_edge(
            second,
            provenance_id="wired-prov-2",
            source_type=SourceType.USER_STATED,
            retrieval_context_hash=_PLACEHOLDER_CONTEXT_HASH,
        )

        new_statements = provenance_cursor.executed[executed_before:]
        # find_by_item_id (SELECT) + correction append (INSERT) +
        # downgraded-incoming append (INSERT) = 3 real SQL statements.
        assert len(new_statements) == 3
        select_sql, _ = new_statements[0]
        correction_sql, correction_params = new_statements[1]
        downgraded_sql, downgraded_params = new_statements[2]
        assert select_sql.strip().upper().startswith("SELECT")
        assert correction_sql.strip().upper().startswith("INSERT INTO PROVENANCE_RECORDS")
        assert downgraded_sql.strip().upper().startswith("INSERT INTO PROVENANCE_RECORDS")
        # `_APPEND_SQL`'s param order: (..., conflict_status, ...) is index 9.
        assert correction_params[9] == ConflictStatus.DISPUTED.value
        assert downgraded_params[9] == ConflictStatus.DISPUTED.value
        assert downgraded_params[1] == "wired-prov-2"

        # Both edges were still really inserted into Zone 3.
        semantic_inserts = [
            sql
            for sql, _ in semantic_connection.cursor_obj.executed
            if sql.strip().upper().startswith("INSERT")
        ]
        assert len(semantic_inserts) == 2


class TestZone3ConflictSweepRunsForGeneralFact:
    """AC-022-1: `SqlSemanticRepository.insert_general_fact` through the real sweep."""

    def test_non_conflicting_fact_write_issues_one_select_then_two_real_inserts(
        self, clock: SeedableClock
    ) -> None:
        semantic_connection = SemanticRecordingConnection()
        provenance_connection = ProvenanceRecordingConnection()
        writer = build_conflict_aware_semantic_repository(
            semantic_connection,
            provenance_connection,
            clock,
            verify_privileges=False,
        )
        fact = GeneralFact(
            tenant_id=DEFAULT_TENANT_ID,
            fact_id="fact-1",
            statement="<PII_EXAMPLE_REDACTED>",
            subject_scope="global",
        )

        writer.insert_general_fact(
            fact,
            provenance_id="prov-1",
            source_type=SourceType.IMPORTED,
            retrieval_context_hash=_PLACEHOLDER_CONTEXT_HASH,
        )

        assert len(provenance_connection.cursor_obj.executed) == 2
        semantic_insert_sql, _ = semantic_connection.cursor_obj.executed[0]
        assert semantic_insert_sql.strip().upper().startswith("INSERT INTO GENERAL_FACTS")


class TestZone5ConflictSweepRunsAgainstTheRealEntityMemoryAdapter:
    """AC-022-1/AC-022-2: `EntityMemoryRepository.write_attribute` through the real sweep."""

    def test_non_conflicting_attribute_write_issues_the_sweeps_select_then_one_insert(
        self, clock: SeedableClock, event_bus
    ) -> None:
        entity_memory_repository = EntityMemoryRepository(clock=clock, event_bus=event_bus)
        provenance_connection = ProvenanceRecordingConnection()
        writer = build_conflict_aware_entity_memory_repository(
            entity_memory_repository,
            provenance_connection,
            clock,
            verify_privileges=False,
        )

        result = writer.write_attribute(
            DEFAULT_TENANT_ID,
            "entity-1",
            "display_name",
            "<PII_EXAMPLE_REDACTED>",
            "prov-1",
            source_type=SourceType.USER_STATED,
            retrieval_context_hash=_PLACEHOLDER_CONTEXT_HASH,
        )

        assert isinstance(result, WriteAccepted)
        provenance_executed = provenance_connection.cursor_obj.executed
        assert len(provenance_executed) == 2
        select_sql, _ = provenance_executed[0]
        insert_sql, _ = provenance_executed[1]
        assert select_sql.strip().upper().startswith("SELECT")
        assert insert_sql.strip().upper().startswith("INSERT INTO PROVENANCE_RECORDS")
        entity = writer.get_entity(DEFAULT_TENANT_ID, "entity-1")
        assert entity is not None
        assert entity.attributes[0].value == "<PII_EXAMPLE_REDACTED>"

    def test_contradicting_attribute_write_disputes_both_records_for_real(
        self, clock: SeedableClock, event_bus
    ) -> None:
        entity_memory_repository = EntityMemoryRepository(clock=clock, event_bus=event_bus)
        provenance_connection = ProvenanceRecordingConnection()
        writer = build_conflict_aware_entity_memory_repository(
            entity_memory_repository,
            provenance_connection,
            clock,
            verify_privileges=False,
        )
        writer.write_attribute(
            DEFAULT_TENANT_ID,
            "entity-1",
            "display_name",
            "<PII_EXAMPLE_REDACTED>",
            "wired-prov-1",
            source_type=SourceType.USER_STATED,
            retrieval_context_hash=_PLACEHOLDER_CONTEXT_HASH,
        )

        provenance_cursor = provenance_connection.cursor_obj
        _, first_insert_params = provenance_cursor.executed[-1]
        provenance_cursor._rows = [first_insert_params]

        clock.advance(300)
        executed_before = len(provenance_cursor.executed)
        result = writer.write_attribute(
            DEFAULT_TENANT_ID,
            "entity-1",
            "display_name",
            "<PII_EXAMPLE_REDACTED>",
            "wired-prov-2",
            source_type=SourceType.USER_STATED,
            retrieval_context_hash=_PLACEHOLDER_CONTEXT_HASH,
        )

        assert isinstance(result, WriteAccepted)
        new_statements = provenance_cursor.executed[executed_before:]
        assert len(new_statements) == 3
        correction_sql, correction_params = new_statements[1]
        downgraded_sql, downgraded_params = new_statements[2]
        assert correction_sql.strip().upper().startswith("INSERT INTO PROVENANCE_RECORDS")
        assert downgraded_sql.strip().upper().startswith("INSERT INTO PROVENANCE_RECORDS")
        assert correction_params[9] == ConflictStatus.DISPUTED.value
        assert downgraded_params[9] == ConflictStatus.DISPUTED.value

        # AC-022-1: the attribute write itself still succeeds; the Zone 5
        # record is not blocked by the dispute.
        entity = writer.get_entity(DEFAULT_TENANT_ID, "entity-1")
        assert entity is not None
        assert entity.attributes[0].provenance_id == "wired-prov-2"
