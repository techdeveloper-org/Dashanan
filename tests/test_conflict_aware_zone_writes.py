"""Tests for DASH-STORY-022: wiring the FR-013 sweep into Zone 3/Zone 5 writes.

Covers `application.conflict_aware_zone_writes` (`ConflictAwareSemanticRepository`,
`ConflictAwareEntityMemoryRepository`) against the REAL `SqlSemanticRepository`
and `EntityMemoryRepository` write paths (AC-022-1's own "not a direct call to
ConflictDetectingProvenanceRepository" requirement), composed with the REAL
`ConflictDetectingProvenanceRepository` decorator wrapping an isolated
`FakeProvenanceRepository` double -- the same fake
`tests/test_conflict_detection_sweep.py` already uses to unit-test
`ConflictDetectingProvenanceRepository` itself, reused here rather than
re-implemented (DRY). This proves this story's own scope precisely: the new
call sites reach the real, unmodified sweep decorator (must-not-deviate item
1 -- the sweep's own decision logic is not re-derived here), composed the
same way `composition_root.build_provenance_repository` composes it
(must-not-deviate item 3), just backed by a fake DB-API-free double instead
of a real SQL connection.

Runtime assumptions recorded per rule 33/40 test-roadmap conventions:
  - clock: fixed literal `datetime(2026, 1, 1, tzinfo=UTC)`.
  - tenant_id: "tenant-1" for all records unless a test states otherwise.

PII NOTE: only pseudonymized item_id/provenance_id/subject/predicate/object
values and the literal `<PII_EXAMPLE_REDACTED>` placeholder appear below --
no fact/payload content.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from dashanan.application.conflict_aware_zone_writes import (
    ConflictAwareEntityMemoryRepository,
    ConflictAwareSemanticRepository,
    _zone3_edge_item_id,
    _zone5_attribute_item_id,
)
from dashanan.application.conflict_detection_sweep import (
    ConflictDetectingProvenanceRepository,
)
from dashanan.domain.entity_record import EntityAttributeRecord
from dashanan.domain.provenance_record import ConflictStatus, ProvenanceRecord, SourceType
from dashanan.domain.semantic_memory import GeneralFact, SemanticEdge
from dashanan.domain.write_gate import WriteAccepted, WriteRejected
from dashanan.domain.zone import ZoneId
from dashanan.infrastructure.entity_memory_repository import (
    ERROR_MISSING_ATTRIBUTE_PROVENANCE_ID,
    EntityMemoryRepository,
)
from tests.test_smoke_semantic import RecordingConnection, SqlSemanticRepository

_PLACEHOLDER_CONTEXT_HASH = hashlib.sha256(b"<PII_EXAMPLE_REDACTED>").hexdigest()
_FIXED_TS = datetime(2026, 1, 1, tzinfo=UTC)
_TENANT_ID = "tenant-1"


class FakeClock:
    """Deterministic, advanceable Clock double (testing-core DI)."""

    def __init__(self, fixed: datetime) -> None:
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


class FakeProvenanceRepository:
    """In-memory `ProvenanceRepositoryPort` double, keyed by (tenant_id, item_id).

    Reused verbatim from `tests/test_conflict_detection_sweep.py`'s own
    fake of the same name (DRY) -- this module does not re-derive the
    FR-013 sweep's own decision logic, only proves this story's new call
    sites reach it.
    """

    def __init__(self) -> None:
        self.appended: list[ProvenanceRecord] = []

    def find_by_item_id(self, tenant_id: str, item_id: str) -> list[ProvenanceRecord]:
        return [
            r for r in self.appended if r.tenant_id == tenant_id and r.item_id == item_id
        ]

    def find_latest_by_item_id(
        self, tenant_id: str, item_id: str
    ) -> ProvenanceRecord | None:
        matches = self.find_by_item_id(tenant_id, item_id)
        return matches[-1] if matches else None

    def append(self, record: ProvenanceRecord) -> None:
        self.appended.append(record)


def _swept_provenance_repository(
    clock: FakeClock,
) -> tuple[FakeProvenanceRepository, ConflictDetectingProvenanceRepository]:
    """Return `(fake, wrapped)`: the raw fake plus it wrapped in the REAL FR-013 sweep.

    AC-022-1 requires the write path to route through
    `ConflictDetectingProvenanceRepository.append`'s own conflict-detection
    logic -- injecting the bare `FakeProvenanceRepository` into a
    `ConflictAware*` writer would only prove the writer calls `.append`,
    not that the sweep itself runs. Every test below asserts on `fake.
    appended` (what the sweep durably wrote) while injecting `wrapped`
    (the real, unmodified decorator, must-not-deviate item 1) into the
    writer under test.
    """
    fake = FakeProvenanceRepository()
    wrapped = ConflictDetectingProvenanceRepository(wrapped=fake, clock=clock)
    return fake, wrapped


def _edge(
    edge_id: str = "edge-1",
    subject_ref: str = "entity-a",
    predicate: str = "related_to",
    object_ref: str = "entity-b",
) -> SemanticEdge:
    return SemanticEdge(
        tenant_id=_TENANT_ID,
        edge_id=edge_id,
        subject_ref=subject_ref,
        predicate=predicate,
        object_ref=object_ref,
    )


def _fact(fact_id: str = "fact-1", subject_scope: str = "global") -> GeneralFact:
    return GeneralFact(
        tenant_id=_TENANT_ID,
        fact_id=fact_id,
        statement="<PII_EXAMPLE_REDACTED>",
        subject_scope=subject_scope,
    )


class TestConflictAwareSemanticRepositoryInsertEdge:
    """AC-022-1/AC-022-2 for Zone 3's `SemanticEdge` write path."""

    def test_first_write_for_a_subject_predicate_object_appends_and_inserts_unchanged(
        self,
    ) -> None:
        semantic_connection = RecordingConnection()
        semantic_repository = SqlSemanticRepository(connection=semantic_connection)
        clock = FakeClock(_FIXED_TS)
        fake_provenance, provenance_repository = _swept_provenance_repository(clock)
        writer = ConflictAwareSemanticRepository(
            semantic_repository=semantic_repository,
            provenance_repository=provenance_repository,
            clock=clock,
        )
        edge = _edge()

        writer.insert_edge(
            edge,
            provenance_id="prov-1",
            source_type=SourceType.USER_STATED,
            retrieval_context_hash=_PLACEHOLDER_CONTEXT_HASH,
        )

        assert len(fake_provenance.appended) == 1
        record = fake_provenance.appended[0]
        assert record.item_id == _zone3_edge_item_id("entity-a", "related_to", "entity-b")
        assert record.source_zone is ZoneId.SEMANTIC
        assert record.conflict_status is ConflictStatus.NONE
        insert_sql, insert_params = semantic_connection.cursor_obj.executed[-1]
        assert insert_sql.strip().upper().startswith("INSERT INTO SEMANTIC_EDGES")
        assert insert_params[1] == "edge-1"

    def test_contradicting_edge_for_the_same_triple_disputes_both_records(self) -> None:
        """AC-022-1: the real Zone 3 write path routes through the FR-013 sweep."""
        semantic_connection = RecordingConnection()
        semantic_repository = SqlSemanticRepository(connection=semantic_connection)
        clock = FakeClock(_FIXED_TS)
        fake_provenance, provenance_repository = _swept_provenance_repository(clock)
        writer = ConflictAwareSemanticRepository(
            semantic_repository=semantic_repository,
            provenance_repository=provenance_repository,
            clock=clock,
        )
        writer.insert_edge(
            _edge(edge_id="edge-1"),
            provenance_id="prov-1",
            source_type=SourceType.USER_STATED,
            retrieval_context_hash=_PLACEHOLDER_CONTEXT_HASH,
        )
        clock.advance(300)

        # A second, UNLINKED claim about the same (subject, predicate,
        # object) -- prev_provenance_id omitted -- is the AC-022-1
        # contradiction case.
        writer.insert_edge(
            _edge(edge_id="edge-2"),
            provenance_id="prov-2",
            source_type=SourceType.USER_STATED,
            retrieval_context_hash=_PLACEHOLDER_CONTEXT_HASH,
        )

        # 1 (first write) + 1 (auto correction of prov-1) + 1 (downgraded prov-2) = 3.
        assert len(fake_provenance.appended) == 3
        correction, downgraded_incoming = fake_provenance.appended[1:]
        assert correction.conflict_status is ConflictStatus.DISPUTED
        assert downgraded_incoming.conflict_status is ConflictStatus.DISPUTED
        assert downgraded_incoming.provenance_id == "prov-2"
        assert correction.confidence < fake_provenance.appended[0].confidence

        # Both edges were still really inserted into Zone 3 -- the sweep
        # never blocks the zone-content write (AC-022-1's own text: both
        # records are marked disputed, neither is rejected/dropped).
        inserts = [
            sql
            for sql, _ in semantic_connection.cursor_obj.executed
            if sql.strip().upper().startswith("INSERT")
        ]
        assert len(inserts) == 2

    def test_a_properly_chained_correction_is_not_flagged_as_a_conflict(self) -> None:
        semantic_connection = RecordingConnection()
        semantic_repository = SqlSemanticRepository(connection=semantic_connection)
        clock = FakeClock(_FIXED_TS)
        fake_provenance, provenance_repository = _swept_provenance_repository(clock)
        writer = ConflictAwareSemanticRepository(
            semantic_repository=semantic_repository,
            provenance_repository=provenance_repository,
            clock=clock,
        )
        writer.insert_edge(
            _edge(edge_id="edge-1"),
            provenance_id="prov-1",
            source_type=SourceType.USER_STATED,
            retrieval_context_hash=_PLACEHOLDER_CONTEXT_HASH,
        )
        clock.advance(300)

        writer.insert_edge(
            _edge(edge_id="edge-2"),
            provenance_id="prov-2",
            source_type=SourceType.USER_STATED,
            retrieval_context_hash=_PLACEHOLDER_CONTEXT_HASH,
            prev_provenance_id="prov-1",
            prev_hash=fake_provenance.appended[0].record_hash,
        )

        assert len(fake_provenance.appended) == 2
        assert fake_provenance.appended[1].conflict_status is ConflictStatus.NONE

    def test_a_different_triple_is_never_flagged_as_conflicting(self) -> None:
        """Two edges with different (subject, predicate, object) are unrelated claims."""
        semantic_connection = RecordingConnection()
        semantic_repository = SqlSemanticRepository(connection=semantic_connection)
        clock = FakeClock(_FIXED_TS)
        fake_provenance, provenance_repository = _swept_provenance_repository(clock)
        writer = ConflictAwareSemanticRepository(
            semantic_repository=semantic_repository,
            provenance_repository=provenance_repository,
            clock=clock,
        )
        writer.insert_edge(
            _edge(edge_id="edge-1", object_ref="entity-b"),
            provenance_id="prov-1",
            source_type=SourceType.USER_STATED,
            retrieval_context_hash=_PLACEHOLDER_CONTEXT_HASH,
        )

        writer.insert_edge(
            _edge(edge_id="edge-2", object_ref="entity-c"),
            provenance_id="prov-2",
            source_type=SourceType.USER_STATED,
            retrieval_context_hash=_PLACEHOLDER_CONTEXT_HASH,
        )

        assert len(fake_provenance.appended) == 2
        assert all(
            r.conflict_status is ConflictStatus.NONE for r in fake_provenance.appended
        )


class TestConflictAwareSemanticRepositoryInsertGeneralFact:
    """AC-022-1 for Zone 3's `GeneralFact` write path (keyed on `fact_id`)."""

    def test_first_write_for_a_fact_id_appends_and_inserts_unchanged(self) -> None:
        semantic_connection = RecordingConnection()
        semantic_repository = SqlSemanticRepository(connection=semantic_connection)
        clock = FakeClock(_FIXED_TS)
        fake_provenance, provenance_repository = _swept_provenance_repository(clock)
        writer = ConflictAwareSemanticRepository(
            semantic_repository=semantic_repository,
            provenance_repository=provenance_repository,
            clock=clock,
        )

        writer.insert_general_fact(
            _fact(),
            provenance_id="prov-1",
            source_type=SourceType.IMPORTED,
            retrieval_context_hash=_PLACEHOLDER_CONTEXT_HASH,
        )

        assert len(fake_provenance.appended) == 1
        assert fake_provenance.appended[0].item_id == "fact-1"  # fact_id keying is unchanged
        insert_sql, _ = semantic_connection.cursor_obj.executed[-1]
        assert insert_sql.strip().upper().startswith("INSERT INTO GENERAL_FACTS")

    def test_contradicting_write_for_the_same_fact_id_disputes_both_records(self) -> None:
        semantic_connection = RecordingConnection()
        semantic_repository = SqlSemanticRepository(connection=semantic_connection)
        clock = FakeClock(_FIXED_TS)
        fake_provenance, provenance_repository = _swept_provenance_repository(clock)
        writer = ConflictAwareSemanticRepository(
            semantic_repository=semantic_repository,
            provenance_repository=provenance_repository,
            clock=clock,
        )
        writer.insert_general_fact(
            _fact(fact_id="fact-1"),
            provenance_id="prov-1",
            source_type=SourceType.IMPORTED,
            retrieval_context_hash=_PLACEHOLDER_CONTEXT_HASH,
        )
        clock.advance(300)

        writer.insert_general_fact(
            _fact(fact_id="fact-1"),
            provenance_id="prov-2",
            source_type=SourceType.IMPORTED,
            retrieval_context_hash=_PLACEHOLDER_CONTEXT_HASH,
        )

        assert len(fake_provenance.appended) == 3
        assert fake_provenance.appended[1].conflict_status is ConflictStatus.DISPUTED
        assert fake_provenance.appended[2].conflict_status is ConflictStatus.DISPUTED


class TestConflictAwareEntityMemoryRepositoryWriteAttribute:
    """AC-022-1/AC-022-2 for Zone 5's `write_attribute` write path."""

    def _writer(
        self,
        clock: FakeClock,
        provenance_repository: ConflictDetectingProvenanceRepository,
    ) -> ConflictAwareEntityMemoryRepository:
        entity_memory_repository = EntityMemoryRepository(
            clock=clock, event_bus=RecordingEventBus()
        )
        return ConflictAwareEntityMemoryRepository(
            entity_memory_repository=entity_memory_repository,
            provenance_repository=provenance_repository,
            clock=clock,
        )

    def test_first_write_for_an_entity_attribute_appends_and_writes_unchanged(self) -> None:
        clock = FakeClock(_FIXED_TS)
        fake_provenance, provenance_repository = _swept_provenance_repository(clock)
        writer = self._writer(clock, provenance_repository)

        result = writer.write_attribute(
            _TENANT_ID,
            "entity-1",
            "display_name",
            "<PII_EXAMPLE_REDACTED>",
            "prov-1",
            source_type=SourceType.USER_STATED,
            retrieval_context_hash=_PLACEHOLDER_CONTEXT_HASH,
        )

        assert isinstance(result, WriteAccepted)
        assert len(fake_provenance.appended) == 1
        record = fake_provenance.appended[0]
        assert record.item_id == _zone5_attribute_item_id("entity-1", "display_name")
        assert record.source_zone is ZoneId.ENTITY
        entity = writer.get_entity(_TENANT_ID, "entity-1")
        assert entity is not None
        assert entity.attributes == (
            EntityAttributeRecord(
                tenant_id=_TENANT_ID,
                entity_id="entity-1",
                attribute_name="display_name",
                value="<PII_EXAMPLE_REDACTED>",
                provenance_id="prov-1",
                updated_at=_FIXED_TS,
            ),
        )

    def test_contradicting_write_for_the_same_entity_attribute_disputes_both_records(
        self,
    ) -> None:
        """AC-022-1: the real Zone 5 write path routes through the FR-013 sweep."""
        clock = FakeClock(_FIXED_TS)
        fake_provenance, provenance_repository = _swept_provenance_repository(clock)
        writer = self._writer(clock, provenance_repository)
        writer.write_attribute(
            _TENANT_ID,
            "entity-1",
            "display_name",
            "<PII_EXAMPLE_REDACTED>",
            "prov-1",
            source_type=SourceType.USER_STATED,
            retrieval_context_hash=_PLACEHOLDER_CONTEXT_HASH,
        )
        clock.advance(300)

        result = writer.write_attribute(
            _TENANT_ID,
            "entity-1",
            "display_name",
            "<PII_EXAMPLE_REDACTED>",
            "prov-2",
            source_type=SourceType.USER_STATED,
            retrieval_context_hash=_PLACEHOLDER_CONTEXT_HASH,
        )

        assert isinstance(result, WriteAccepted)
        assert len(fake_provenance.appended) == 3
        correction, downgraded_incoming = fake_provenance.appended[1:]
        assert correction.conflict_status is ConflictStatus.DISPUTED
        assert downgraded_incoming.conflict_status is ConflictStatus.DISPUTED
        # AC-022-1: even though the write is disputed, the attribute is
        # still really persisted (dispute lives on the Zone 7 record, not
        # a block on the Zone 5 write itself).
        entity = writer.get_entity(_TENANT_ID, "entity-1")
        assert entity is not None
        assert entity.attributes[0].provenance_id == "prov-2"

    def test_a_different_attribute_on_the_same_entity_is_never_flagged_as_conflicting(
        self,
    ) -> None:
        clock = FakeClock(_FIXED_TS)
        fake_provenance, provenance_repository = _swept_provenance_repository(clock)
        writer = self._writer(clock, provenance_repository)
        writer.write_attribute(
            _TENANT_ID,
            "entity-1",
            "display_name",
            "<PII_EXAMPLE_REDACTED>",
            "prov-1",
            source_type=SourceType.USER_STATED,
            retrieval_context_hash=_PLACEHOLDER_CONTEXT_HASH,
        )

        writer.write_attribute(
            _TENANT_ID,
            "entity-1",
            "role",
            "<PII_EXAMPLE_REDACTED>",
            "prov-2",
            source_type=SourceType.USER_STATED,
            retrieval_context_hash=_PLACEHOLDER_CONTEXT_HASH,
        )

        assert len(fake_provenance.appended) == 2
        assert all(
            r.conflict_status is ConflictStatus.NONE for r in fake_provenance.appended
        )

    def test_blank_provenance_id_is_rejected_without_running_the_sweep(self) -> None:
        """AC-005-5's own rejection is reused unchanged -- the sweep never runs."""
        clock = FakeClock(_FIXED_TS)
        fake_provenance, provenance_repository = _swept_provenance_repository(clock)
        writer = self._writer(clock, provenance_repository)

        result = writer.write_attribute(
            _TENANT_ID,
            "entity-1",
            "display_name",
            "<PII_EXAMPLE_REDACTED>",
            "",
            source_type=SourceType.USER_STATED,
            retrieval_context_hash=_PLACEHOLDER_CONTEXT_HASH,
        )

        assert isinstance(result, WriteRejected)
        assert result.error_code == ERROR_MISSING_ATTRIBUTE_PROVENANCE_ID
        assert fake_provenance.appended == []


class TestItemIdEncodingCollisionResistance:
    """Confirmed HIGH finding: a bare colon join lets two distinct component
    tuples collide into the same item_id, so FR-013's conflict sweep would
    wrongly detect (or wrongly fail to distinguish) unrelated writes as the
    same tracked item. Both helpers now length-prefix each component so no
    component's own content can be misread as the delimiter.
    """

    def test_zone3_edge_item_id_does_not_collide_on_ambiguous_colon_split(self) -> None:
        """subject_ref='A:B', predicate='C' must differ from subject_ref='A', predicate='B:C'."""
        collided_a = _zone3_edge_item_id("A:B", "C", "object-x")
        collided_b = _zone3_edge_item_id("A", "B:C", "object-x")

        assert collided_a != collided_b

    def test_zone5_attribute_item_id_does_not_collide_on_ambiguous_colon_split(self) -> None:
        """entity_id='A:B', attribute_name='C' must differ from entity_id='A', attribute_name='B:C'."""
        collided_a = _zone5_attribute_item_id("A:B", "C")
        collided_b = _zone5_attribute_item_id("A", "B:C")

        assert collided_a != collided_b

    def test_zone3_edge_item_id_is_deterministic_for_real_inputs(self) -> None:
        """Same (subject, predicate, object) always produces the same item_id."""
        first = _zone3_edge_item_id("entity-a", "related_to", "entity-b")
        second = _zone3_edge_item_id("entity-a", "related_to", "entity-b")

        assert first == second
        assert first == "8:entity-a:10:related_to:8:entity-b"

    def test_zone5_attribute_item_id_is_deterministic_for_real_inputs(self) -> None:
        """Same (entity_id, attribute_name) always produces the same item_id."""
        first = _zone5_attribute_item_id("entity-1", "display_name")
        second = _zone5_attribute_item_id("entity-1", "display_name")

        assert first == second
        assert first == "8:entity-1:12:display_name"

    def test_the_collision_example_no_longer_produces_the_same_item_id_end_to_end(
        self,
    ) -> None:
        """The exact collision scenario from the confirmed finding, driven through
        `ConflictAwareSemanticRepository.insert_edge` end to end: two edges that
        would have collided under the old bare-colon join must now sweep as two
        distinct, unrelated item_ids -- neither is flagged as disputed.
        """
        semantic_connection = RecordingConnection()
        semantic_repository = SqlSemanticRepository(connection=semantic_connection)
        clock = FakeClock(_FIXED_TS)
        fake_provenance, provenance_repository = _swept_provenance_repository(clock)
        writer = ConflictAwareSemanticRepository(
            semantic_repository=semantic_repository,
            provenance_repository=provenance_repository,
            clock=clock,
        )

        writer.insert_edge(
            _edge(edge_id="edge-1", subject_ref="A:B", predicate="C", object_ref="obj"),
            provenance_id="prov-1",
            source_type=SourceType.USER_STATED,
            retrieval_context_hash=_PLACEHOLDER_CONTEXT_HASH,
        )
        writer.insert_edge(
            _edge(edge_id="edge-2", subject_ref="A", predicate="B:C", object_ref="obj"),
            provenance_id="prov-2",
            source_type=SourceType.USER_STATED,
            retrieval_context_hash=_PLACEHOLDER_CONTEXT_HASH,
        )

        assert len(fake_provenance.appended) == 2
        first_item_id, second_item_id = (r.item_id for r in fake_provenance.appended)
        assert first_item_id != second_item_id
        assert all(
            r.conflict_status is ConflictStatus.NONE for r in fake_provenance.appended
        )
