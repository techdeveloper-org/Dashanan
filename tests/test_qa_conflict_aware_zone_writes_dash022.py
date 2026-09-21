"""QA regression guards for DASH-STORY-022 (FR-013 Zone 3/Zone 5 conflict-detection wiring).

This module is the QA subtask (20% of story points, sprint3_ar1_assignments.json)
for DASH-STORY-022, tracing to FR-013 (SRS.md). It is deliberately narrow and
additive to `tests/test_conflict_aware_zone_writes.py` (the DEV subtask's own
unit-test suite, which already covers AC-022-1/AC-022-2's full positive/
negative/boundary matrix for both zones) and
`tests/integration/test_zone3_zone5_conflict_sweep_wired.py` (the real-SQL-adapter
integration suite). This module closes two gaps neither of those already covers:

1. A dedicated, explicitly AC-015-labeled golden regression guard per zone,
   asserting the FULL text of AC-015's own contract -- "a contradicting true
   fact triggers the conflict-detection sweep to downgrade BOTH records
   rather than silently overwriting the true fact" -- including the specific
   claim (not otherwise asserted anywhere else) that the original true-fact
   record itself is never mutated/deleted, only ever appended-alongside by a
   new DISPUTED correction record chained via `prev_provenance_id`.
2. `dependency_integrity_check` (sprint3_ar1_assignments.json, must-not-deviate):
   an AST-level assertion that `application.conflict_aware_zone_writes`'s own
   import surface is exactly the small, expected set this story owns -- no
   accidental import of a collaborator outside this story's two call sites
   (Zone 3's `SqlSemanticRepository`, Zone 5's `EntityMemoryRepository`) or of
   `domain.conflict_detection`/`ConflictDetectingProvenanceRepository`'s own
   internals (both locked, must-not-deviate item 1: this story calls them,
   it does not reach into them).

Runtime assumptions recorded per rule 33/40 test-roadmap conventions:
  - clock: fixed literal `datetime(2026, 1, 1, tzinfo=UTC)`, advanced +300
    seconds between the first (true-fact) write and the second
    (contradicting) write in every golden test below.
  - tenant_id: "tenant-1" for every test below.
  - fixture seed: none -- this story's own code path (item_id construction,
    provenance-record composition) uses no randomness; `uuid4()` is called
    only inside the already-locked `ConflictDetectingProvenanceRepository`
    for the correction record's own `provenance_id`, which no assertion
    below depends on.

PII NOTE: only pseudonymized item_id/provenance_id/subject/predicate/object
values and the literal `<PII_EXAMPLE_REDACTED>` placeholder appear below --
no fact/payload content, mirroring `test_conflict_aware_zone_writes.py`'s
identical posture.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
from datetime import UTC, datetime

from dashanan.application import conflict_aware_zone_writes as conflict_aware_zone_writes_module
from dashanan.application.conflict_aware_zone_writes import (
    ConflictAwareEntityMemoryRepository,
    ConflictAwareSemanticRepository,
    _zone3_edge_item_id,
    _zone5_attribute_item_id,
)
from dashanan.application.conflict_detection_sweep import (
    ConflictDetectingProvenanceRepository,
)
from dashanan.domain.provenance_record import ConflictStatus, SourceType
from dashanan.domain.semantic_memory import SemanticEdge
from dashanan.infrastructure.entity_memory_repository import EntityMemoryRepository
from tests.test_conflict_aware_zone_writes import (
    FakeClock,
    FakeProvenanceRepository,
    RecordingEventBus,
)
from tests.test_smoke_semantic import RecordingConnection, SqlSemanticRepository

_PLACEHOLDER_CONTEXT_HASH = hashlib.sha256(b"<PII_EXAMPLE_REDACTED>").hexdigest()
_FIXED_TS = datetime(2026, 1, 1, tzinfo=UTC)
_TENANT_ID = "tenant-1"


class TestAC015GoldenRegressionGuardZone3:
    """AC-015 (SRS.md, verbatim, this story's own in-scope clause):

    "...a contradicting true fact triggers the conflict-detection sweep to
    downgrade both records rather than silently overwriting the true fact
    (HLD threat T-1)."

    One golden guard for Zone 3's `SemanticEdge` write path, wired through
    the REAL `SqlSemanticRepository` (not a direct
    `ConflictDetectingProvenanceRepository` call), per this story's own
    "testable" text in sprint3_ar1_assignments.json.
    """

    def test_contradicting_semantic_edge_downgrades_both_records_not_overwrite(
        self,
    ) -> None:
        """The true-fact record is never mutated/deleted -- only a new,
        chained DISPUTED correction is appended alongside it, and the
        incoming contradiction is itself appended as DISPUTED too. Neither
        write silently wins."""
        semantic_connection = RecordingConnection()
        semantic_repository = SqlSemanticRepository(connection=semantic_connection)
        clock = FakeClock(_FIXED_TS)
        fake_provenance = FakeProvenanceRepository()
        provenance_repository = ConflictDetectingProvenanceRepository(
            wrapped=fake_provenance, clock=clock
        )
        writer = ConflictAwareSemanticRepository(
            semantic_repository=semantic_repository,
            provenance_repository=provenance_repository,
            clock=clock,
        )
        true_fact_edge = SemanticEdge(
            tenant_id=_TENANT_ID,
            edge_id="edge-true-fact",
            subject_ref="entity-a",
            predicate="related_to",
            object_ref="entity-b",
        )
        writer.insert_edge(
            true_fact_edge,
            provenance_id="prov-true-fact",
            source_type=SourceType.USER_STATED,
            retrieval_context_hash=_PLACEHOLDER_CONTEXT_HASH,
        )
        clock.advance(300)

        contradicting_edge = SemanticEdge(
            tenant_id=_TENANT_ID,
            edge_id="edge-contradiction",
            subject_ref="entity-a",
            predicate="related_to",
            object_ref="entity-b",
        )
        writer.insert_edge(
            contradicting_edge,
            provenance_id="prov-contradiction",
            source_type=SourceType.USER_STATED,
            retrieval_context_hash=_PLACEHOLDER_CONTEXT_HASH,
        )

        item_id = _zone3_edge_item_id("entity-a", "related_to", "entity-b")
        lineage = fake_provenance.find_by_item_id(_TENANT_ID, item_id)
        assert len(lineage) == 3
        original_true_fact, correction, downgraded_incoming = lineage

        # The true fact is never overwritten: its own row is still present,
        # unmodified, still carrying conflict_status=NONE exactly as
        # originally appended.
        assert original_true_fact.provenance_id == "prov-true-fact"
        assert original_true_fact.conflict_status is ConflictStatus.NONE

        # "downgrade both records": the correction chains FROM (does not
        # replace) the true fact, and both the correction and the incoming
        # contradiction are DISPUTED.
        assert correction.update_history[0].prev_provenance_id == "prov-true-fact"
        assert correction.conflict_status is ConflictStatus.DISPUTED
        assert downgraded_incoming.provenance_id == "prov-contradiction"
        assert downgraded_incoming.conflict_status is ConflictStatus.DISPUTED
        assert correction.confidence < original_true_fact.confidence
        assert downgraded_incoming.confidence < 1.0

        # The sweep never blocks the zone-content write: both edges really
        # reached the real Zone 3 SQL adapter.
        inserts = [
            sql
            for sql, _ in semantic_connection.cursor_obj.executed
            if sql.strip().upper().startswith("INSERT")
        ]
        assert len(inserts) == 2


class TestAC015GoldenRegressionGuardZone5:
    """AC-015's identical guard for Zone 5's `write_attribute` write path."""

    def test_contradicting_attribute_write_downgrades_both_records_not_overwrite(
        self,
    ) -> None:
        clock = FakeClock(_FIXED_TS)
        fake_provenance = FakeProvenanceRepository()
        provenance_repository = ConflictDetectingProvenanceRepository(
            wrapped=fake_provenance, clock=clock
        )
        entity_memory_repository = EntityMemoryRepository(
            clock=clock, event_bus=RecordingEventBus()
        )
        writer = ConflictAwareEntityMemoryRepository(
            entity_memory_repository=entity_memory_repository,
            provenance_repository=provenance_repository,
            clock=clock,
        )

        writer.write_attribute(
            _TENANT_ID,
            "entity-1",
            "display_name",
            "<PII_EXAMPLE_REDACTED>",
            "prov-true-fact",
            source_type=SourceType.USER_STATED,
            retrieval_context_hash=_PLACEHOLDER_CONTEXT_HASH,
        )
        clock.advance(300)

        result = writer.write_attribute(
            _TENANT_ID,
            "entity-1",
            "display_name",
            "<PII_EXAMPLE_REDACTED>",
            "prov-contradiction",
            source_type=SourceType.USER_STATED,
            retrieval_context_hash=_PLACEHOLDER_CONTEXT_HASH,
        )

        item_id = _zone5_attribute_item_id("entity-1", "display_name")
        lineage = fake_provenance.find_by_item_id(_TENANT_ID, item_id)
        assert len(lineage) == 3
        original_true_fact, correction, downgraded_incoming = lineage

        assert original_true_fact.provenance_id == "prov-true-fact"
        assert original_true_fact.conflict_status is ConflictStatus.NONE
        assert correction.update_history[0].prev_provenance_id == "prov-true-fact"
        assert correction.conflict_status is ConflictStatus.DISPUTED
        assert downgraded_incoming.provenance_id == "prov-contradiction"
        assert downgraded_incoming.conflict_status is ConflictStatus.DISPUTED
        assert correction.confidence < original_true_fact.confidence

        # AC-015: the write is still really persisted (dispute lives on the
        # Zone 7 record, not a block on the Zone 5 attribute write itself).
        entity = writer.get_entity(_TENANT_ID, "entity-1")
        assert entity is not None
        assert entity.attributes[0].provenance_id == "prov-contradiction"
        assert result.__class__.__name__ == "WriteAccepted"


class TestDependencyIntegrityNoMutationOutsideStoryOwnership:
    """dependency_integrity_check (sprint3_ar1_assignments.json, must-not-deviate):

    `application.conflict_aware_zone_writes`'s own actual `import` targets
    are exactly the small, expected set this story owns -- no accidental
    reach into `domain.conflict_detection`'s locked algorithm internals, no
    accidental import of an unrelated zone's adapter, and no second,
    parallel `ConflictDetectingProvenanceRepository` construction path
    (must-not-deviate item 3 -- this module only ever receives one via
    constructor injection, never imports the class itself)."""

    def test_import_surface_is_exactly_the_expected_small_set(self) -> None:
        source = inspect.getsource(conflict_aware_zone_writes_module)
        tree = ast.parse(source)
        imported_modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imported_modules.add(alias.name)

        allowed_prefixes = (
            "__future__",
            "logging",
            "collections.abc",
            "datetime",
            "typing",
            "dashanan.domain.entity_record",
            "dashanan.domain.ports",
            "dashanan.domain.provenance_record",
            "dashanan.domain.semantic_memory",
            "dashanan.domain.write_gate",
            "dashanan.domain.zone",
            "dashanan.infrastructure.entity_memory_repository",
            "dashanan.infrastructure.sql_semantic_repository",
        )
        for module_name in imported_modules:
            assert any(
                module_name == prefix or module_name.startswith(prefix + ".")
                for prefix in allowed_prefixes
            ), f"Unexpected import '{module_name}' outside DASH-STORY-022 ownership"

        # Explicitly confirm the two must-not-deviate exclusions: this
        # module never imports domain.conflict_detection (the locked
        # algorithm, must-not-deviate item 1) and never imports
        # ConflictDetectingProvenanceRepository/composition_root itself
        # (must-not-deviate item 3 -- it is injected, never constructed
        # here).
        assert "dashanan.domain.conflict_detection" not in imported_modules
        assert "dashanan.application.conflict_detection_sweep" not in imported_modules
        assert "dashanan.infrastructure.composition_root" not in imported_modules

    def test_writer_classes_never_construct_their_own_provenance_repository(self) -> None:
        """Structural proof of must-not-deviate item 3: both writer classes
        accept `provenance_repository` purely as a constructor parameter --
        neither class body contains a call that constructs one."""
        for cls in (ConflictAwareSemanticRepository, ConflictAwareEntityMemoryRepository):
            source = inspect.getsource(cls)
            assert "ConflictDetectingProvenanceRepository(" not in source
            assert "SqlProvenanceRepository(" not in source
