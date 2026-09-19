"""QA pytest suite for DASH-STORY-005 (Provenance/Audit zone / Zone 7, FR-007).

QA subtask (backlog_draft.json 20% split), independent verification pass on
top of the Dev subtask's own suite (tests/test_smoke_provenance.py). That dev
suite is explicitly scoped to structural/wiring smoke checks (its own module
docstring: "The formal pytest suite covering AC-007 in full is the QA
subtask's responsibility"); this suite is the story's formal AC-by-AC proof,
matching the split DASH-STORY-003's test_smoke_episodic.py vs
test_qa_episodic_memory_zone2.py precedent.

Unlike the dev suite -- which asserts against literal SQL text via a
recording-only cursor double -- this suite drives `SqlProvenanceRepository`
against a FAKE that actually *executes* each of the three query shapes' real
filter/sort/limit semantics over an in-memory row store. This proves AC-007
behaviorally (a real chronological lineage scan, a real "most recent record"
selection, a real single-INSERT append) rather than only proving the SQL
text looks right.

Acceptance criterion under test, verbatim from
docs/phase-7-routing/implementation_execution_plan.json's DASH-STORY-005
dev_prompt (the qa_prompt cites the identical criterion) and from SRS.md's
AC-007 row:

  AC-007: "Given any fact is written to any zone, when the Provenance/Audit
  zone is queried for that fact's item_id, then a source attribution,
  confidence score, and edit history entry exists and is retrievable."

FR-007 under test, verbatim from SRS.md: "The system SHALL provide a
Provenance/Audit Memory zone recording, for every fact in every other zone,
its source attribution, a confidence score, and its edit history."

MUST-NOT-DEVIATE items under test (ar1_assignments.json, AR1-005), proven
here behaviorally rather than only structurally:
  1. No UPDATE grant exists on the Zone 7 table for any service role --
     corrections are new chained records, never an in-place update.
  2. SHA-256 prev_hash -> record_hash chain across every record.
  3. Confidence is COMPUTED from source_type and modifiers, never assigned
     once and left static (HLD 3.8) -- asserted against the RETURNED
     derivation of `compute_confidence`, never a self-computed literal.
  4. Append-only, O(1) amortized write.

Plus the architecture-fitness invariant (HLD Section 3.0, invariant 1):
no domain/** module may import infrastructure/**, scoped here to this
story's own new domain module (provenance_record.py) -- the repo-wide sweep
already lives in test_memory_orchestrator.py.

Runtime assumptions (rule 33/40/41 test-roadmap conventions, scoped to this
pure in-process library -- no HTTP/router/response-envelope layer exists in
this codebase, matching DASH-STORY-001/002/003's own precedent):
  - clock: fixed literal `datetime(2026, 1, 1, tzinfo=UTC)` base timestamps,
    with explicit `timedelta` offsets per test -- no injected Clock port is
    needed since this repository takes `write_timestamp` as data, not a
    live clock read (matching test_smoke_provenance.py's own note).
  - tenant_id: "tenant-1" for all requests unless a test states otherwise.
  - No fixture seed is required; nothing in this suite is randomized.

PII NOTE: per the dev_prompt's PII constraint, this suite carries only
schema-level illustration -- pseudonymized item_id/provenance_id values and
a placeholder retrieval_context_hash; no fact/payload content, real or
synthetic-realistic, appears anywhere below.
"""

from __future__ import annotations

import ast
import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from dashanan.domain.exceptions import ZoneRepositoryError
from dashanan.domain.provenance_record import (
    ConflictStatus,
    ProvenanceRecord,
    SourceType,
    compute_confidence,
    verify_chain,
)
from dashanan.domain.zone import ZoneId
from dashanan.infrastructure.sql_provenance_repository import (
    _APPEND_SQL,
    _FIND_BY_ITEM_SQL,
    _FIND_LATEST_BY_ITEM_SQL,
    SqlProvenanceRepository,
)

DOMAIN_DIR = Path(__file__).resolve().parents[1] / "src" / "dashanan" / "domain"
PROVENANCE_RECORD_FILE = DOMAIN_DIR / "provenance_record.py"

_PLACEHOLDER_CONTEXT_HASH = hashlib.sha256(b"<PII_EXAMPLE_REDACTED>").hexdigest()
_FIXED_TS = datetime(2026, 1, 1, tzinfo=UTC)


class FakeProvenanceCursor:
    """A DB-API cursor double that actually EXECUTES each query shape's real
    filter/sort/lookup semantics against an in-memory row store, rather than
    only recording the SQL text (contrast with test_smoke_provenance.py's
    `RecordingCursor`).

    Rows are stored exactly as `SqlProvenanceRepository.append()` builds its
    INSERT params tuple -- `_SELECT_COLUMNS`' column order and the INSERT
    column order are identical in the real adapter, so a stored append row
    can be returned directly from a SELECT without any reshaping, exactly as
    a real driver round-trip would.
    """

    def __init__(self, store: "FakeProvenanceStore") -> None:
        self._store = store
        self.executed: list[tuple[str, tuple[object, ...]]] = []
        self._last_result: list[tuple[object, ...]] = []

    def execute(self, sql: str, params: Sequence[object]) -> None:
        params = tuple(params)
        self.executed.append((sql, params))

        if sql == _APPEND_SQL:
            self._store.insert(params)
            self._last_result = []
            return

        if sql == _FIND_BY_ITEM_SQL:
            tenant_id, item_id = params
            self._store.record_lineage_scan()
            candidates = [
                row
                for row in self._store.rows
                if row[0] == tenant_id and row[2] == item_id
            ]
            candidates.sort(key=lambda row: cast(datetime, row[6]))
            self._last_result = candidates
            return

        if sql == _FIND_LATEST_BY_ITEM_SQL:
            tenant_id, item_id = params
            self._store.record_latest_lookup()
            candidates = [
                row
                for row in self._store.rows
                if row[0] == tenant_id and row[2] == item_id
            ]
            candidates.sort(key=lambda row: cast(datetime, row[6]), reverse=True)
            self._last_result = candidates[:1]
            return

        raise AssertionError(f"FakeProvenanceCursor received an unrecognized query: {sql!r}")

    def fetchall(self) -> list[tuple[object, ...]]:
        return list(self._last_result)


class FakeProvenanceStore:
    """The in-memory, append-only backing store `FakeProvenanceCursor`
    operates over. Tracks `insert_calls` separately from the two read-path
    call counters so a test can prove `append()` never triggers a read and
    a read never mutates `rows` (must-not-deviate item 1/4's real-behavior
    proof).
    """

    def __init__(self) -> None:
        self.rows: list[tuple[object, ...]] = []
        self.insert_calls: int = 0
        self.lineage_scan_calls: int = 0
        self.latest_lookup_calls: int = 0

    def insert(self, row: tuple[object, ...]) -> None:
        self.rows.append(row)
        self.insert_calls += 1

    def record_lineage_scan(self) -> None:
        self.lineage_scan_calls += 1

    def record_latest_lookup(self) -> None:
        self.latest_lookup_calls += 1


class FakeProvenanceConnection:
    """DB-API connection double exposing one shared `FakeProvenanceCursor`."""

    def __init__(self) -> None:
        self.store = FakeProvenanceStore()
        self.cursor_obj = FakeProvenanceCursor(self.store)

    def cursor(self) -> FakeProvenanceCursor:
        return self.cursor_obj


class RaisingCursor:
    """A cursor double whose `execute` always raises, to prove `append`/
    `find_by_item_id` correctly wrap driver failures as `ZoneRepositoryError`
    without ever touching a real store.
    """

    def execute(self, sql: str, params: Sequence[object]) -> None:
        raise RuntimeError("connection refused")

    def fetchall(self) -> list[tuple[object, ...]]:
        return []


class RaisingConnection:
    def cursor(self) -> RaisingCursor:
        return RaisingCursor()


def _record(
    tenant_id: str = "tenant-1",
    provenance_id: str = "prov-1",
    item_id: str = "item-1",
    source_type: SourceType = SourceType.USER_STATED,
    write_timestamp: datetime = _FIXED_TS,
    actor: str = "dashanan-orchestrator",
    change: str = "initial write",
    prev_hash: str | None = None,
    prev_provenance_id: str | None = None,
    conflict_status: ConflictStatus = ConflictStatus.NONE,
    failed_faithfulness_check: bool = False,
    compression_generation: int = 0,
    source_confidences: Sequence[float] | None = None,
) -> ProvenanceRecord:
    return ProvenanceRecord.create(
        tenant_id=tenant_id,
        provenance_id=provenance_id,
        item_id=item_id,
        source_zone=ZoneId.EPISODIC,
        source_type=source_type,
        write_timestamp=write_timestamp,
        actor=actor,
        change=change,
        retrieval_context_hash=_PLACEHOLDER_CONTEXT_HASH,
        prev_hash=prev_hash,
        prev_provenance_id=prev_provenance_id,
        conflict_status=conflict_status,
        failed_faithfulness_check=failed_faithfulness_check,
        compression_generation=compression_generation,
        source_confidences=source_confidences,
    )


@pytest.fixture
def connection() -> FakeProvenanceConnection:
    return FakeProvenanceConnection()


@pytest.fixture
def repo(connection: FakeProvenanceConnection) -> SqlProvenanceRepository:
    return SqlProvenanceRepository(connection, verify_privileges=False)


class TestAC007RetrievabilityRealBehavior:
    """AC-007 (verbatim): "Given any fact is written to any zone, when the
    Provenance/Audit zone is queried for that fact's item_id, then a source
    attribution, confidence score, and edit history entry exists and is
    retrievable." Proven here end-to-end through the real `append` write
    path and the real `find_by_item_id`/`find_latest_by_item_id` read paths
    over a store that actually filters/sorts, not only a recorded SQL
    string.
    """

    def test_golden_write_then_query_by_item_id_returns_source_confidence_and_history(
        self, repo: SqlProvenanceRepository
    ) -> None:
        """Golden regression case for AC-007: the exact given/when/then the
        AC states, driven end-to-end. Any later story touching Zone 7 must
        keep this test green.
        """
        written = _record(
            item_id="item-golden",
            source_type=SourceType.TOOL_OUTPUT,
            actor="ingestion-pipeline",
            change="initial write",
        )

        repo.append(written)
        found = repo.find_by_item_id(tenant_id="tenant-1", item_id="item-golden")

        assert len(found) == 1
        retrieved = found[0]
        # "a source attribution ... exists and is retrievable"
        assert retrieved.source_type == SourceType.TOOL_OUTPUT
        assert retrieved.source_zone == ZoneId.EPISODIC
        # "a confidence score ... exists and is retrievable"
        assert retrieved.confidence == pytest.approx(compute_confidence(SourceType.TOOL_OUTPUT))
        # "an edit history entry exists and is retrievable"
        assert len(retrieved.update_history) == 1
        assert retrieved.update_history[0].actor == "ingestion-pipeline"
        assert retrieved.update_history[0].change == "initial write"

    def test_fact_written_to_any_zone_is_locatable_by_its_item_id(
        self, repo: SqlProvenanceRepository
    ) -> None:
        """"Any fact is written to any zone" -- proves the query key is
        purely `item_id`, independent of which `source_zone` the fact
        originated from (episodic, semantic, entity, ...).
        """
        for zone, item_id in (
            (ZoneId.EPISODIC, "item-from-episodic"),
            (ZoneId.SEMANTIC, "item-from-semantic"),
            (ZoneId.ENTITY, "item-from-entity"),
        ):
            repo.append(
                ProvenanceRecord.create(
                    tenant_id="tenant-1",
                    provenance_id=f"prov-{item_id}",
                    item_id=item_id,
                    source_zone=zone,
                    source_type=SourceType.SYSTEM_DERIVED,
                    write_timestamp=_FIXED_TS,
                    actor="orchestrator",
                    change="initial write",
                    retrieval_context_hash=_PLACEHOLDER_CONTEXT_HASH,
                )
            )

        for zone, item_id in (
            (ZoneId.EPISODIC, "item-from-episodic"),
            (ZoneId.SEMANTIC, "item-from-semantic"),
            (ZoneId.ENTITY, "item-from-entity"),
        ):
            latest = repo.find_latest_by_item_id(tenant_id="tenant-1", item_id=item_id)
            assert latest is not None
            assert latest.source_zone == zone

    def test_find_by_item_id_returns_the_full_chronological_lineage_real_sort(
        self, repo: SqlProvenanceRepository
    ) -> None:
        base = _FIXED_TS
        # Appended deliberately OUT of chronological order.
        third = _record(provenance_id="prov-3", write_timestamp=base + timedelta(days=5))
        first = _record(provenance_id="prov-1", write_timestamp=base)
        second = _record(provenance_id="prov-2", write_timestamp=base + timedelta(days=2))
        repo.append(third)
        repo.append(first)
        repo.append(second)

        lineage = repo.find_by_item_id(tenant_id="tenant-1", item_id="item-1")

        assert [r.provenance_id for r in lineage] == ["prov-1", "prov-2", "prov-3"]

    def test_find_latest_by_item_id_returns_the_most_recently_written_record(
        self, repo: SqlProvenanceRepository
    ) -> None:
        base = _FIXED_TS
        repo.append(_record(provenance_id="prov-1", write_timestamp=base))
        repo.append(
            _record(
                provenance_id="prov-2",
                write_timestamp=base + timedelta(days=1),
                source_type=SourceType.TOOL_OUTPUT,
            )
        )

        latest = repo.find_latest_by_item_id(tenant_id="tenant-1", item_id="item-1")

        assert latest is not None
        assert latest.provenance_id == "prov-2"
        assert latest.source_type == SourceType.TOOL_OUTPUT

    def test_find_by_item_id_never_returns_records_for_a_different_item(
        self, repo: SqlProvenanceRepository
    ) -> None:
        repo.append(_record(provenance_id="prov-a", item_id="item-A"))
        repo.append(_record(provenance_id="prov-b", item_id="item-B"))

        found_a = repo.find_by_item_id(tenant_id="tenant-1", item_id="item-A")

        assert [r.provenance_id for r in found_a] == ["prov-a"]

    def test_tenant_isolation_holds_under_a_real_mixed_tenant_store(
        self, repo: SqlProvenanceRepository
    ) -> None:
        repo.append(_record(tenant_id="tenant-A", provenance_id="prov-a"))
        repo.append(_record(tenant_id="tenant-B", provenance_id="prov-b"))

        found_a = repo.find_by_item_id(tenant_id="tenant-A", item_id="item-1")
        found_b = repo.find_by_item_id(tenant_id="tenant-B", item_id="item-1")

        assert [r.provenance_id for r in found_a] == ["prov-a"]
        assert [r.provenance_id for r in found_b] == ["prov-b"]


class TestAC007CorrectionsAsNewChainedRecordsRealBehavior:
    """AC-007 read alongside must-not-deviate item 1: "corrections are new
    chained records, never an in-place update" -- proven over a REAL
    multi-record lineage where a correction is appended, never mutates the
    original row, and both remain retrievable via AC-007's item_id query.
    """

    def test_correction_appends_a_new_record_the_original_row_is_unchanged(
        self, repo: SqlProvenanceRepository, connection: FakeProvenanceConnection
    ) -> None:
        original = _record(
            provenance_id="prov-1",
            write_timestamp=_FIXED_TS,
            source_type=SourceType.LLM_INFERRED,
        )
        repo.append(original)
        original_row_snapshot = connection.store.rows[0]

        correction = _record(
            provenance_id="prov-2",
            write_timestamp=_FIXED_TS + timedelta(hours=1),
            source_type=SourceType.USER_STATED,
            actor="human-reviewer",
            change="correction: re-confirmed by user",
            prev_hash=original.record_hash,
            prev_provenance_id=original.provenance_id,
        )
        repo.append(correction)

        # The original row's stored tuple is byte-for-byte unchanged --
        # append() never rewrites an existing row (no UPDATE path exists).
        assert connection.store.rows[0] == original_row_snapshot
        assert len(connection.store.rows) == 2

        lineage = repo.find_by_item_id(tenant_id="tenant-1", item_id="item-1")
        assert [r.provenance_id for r in lineage] == ["prov-1", "prov-2"]
        assert lineage[1].update_history[0].prev_provenance_id == "prov-1"
        assert verify_chain(lineage) is True

    def test_three_generation_correction_chain_verifies_end_to_end(
        self, repo: SqlProvenanceRepository
    ) -> None:
        gen1 = _record(provenance_id="prov-1", write_timestamp=_FIXED_TS)
        repo.append(gen1)
        gen2 = _record(
            provenance_id="prov-2",
            write_timestamp=_FIXED_TS + timedelta(hours=1),
            prev_hash=gen1.record_hash,
            prev_provenance_id=gen1.provenance_id,
            conflict_status=ConflictStatus.DISPUTED,
        )
        repo.append(gen2)
        gen3 = _record(
            provenance_id="prov-3",
            write_timestamp=_FIXED_TS + timedelta(hours=2),
            prev_hash=gen2.record_hash,
            prev_provenance_id=gen2.provenance_id,
            conflict_status=ConflictStatus.RESOLVED_CONFIRMED,
        )
        repo.append(gen3)

        lineage = repo.find_by_item_id(tenant_id="tenant-1", item_id="item-1")

        assert len(lineage) == 3
        assert verify_chain(lineage) is True
        # Confidence differs across generations because it is recomputed,
        # never carried forward as a static value (must-not-deviate item 3).
        assert lineage[1].confidence == pytest.approx(compute_confidence(SourceType.USER_STATED, conflict_status=ConflictStatus.DISPUTED))
        assert lineage[0].confidence != lineage[1].confidence


class TestMustNotDeviateComputedConfidenceAgainstReturnedDerivation:
    """Must-not-deviate item 3, asserted against `compute_confidence`'s own
    RETURNED derivation per the qa_prompt's instruction ("assert against the
    RETURNED derivation, never a self-computed approximation") -- never a
    hand-rederived literal in this test file.
    """

    @pytest.mark.parametrize(
        "source_type",
        [
            SourceType.USER_STATED,
            SourceType.TOOL_OUTPUT,
            SourceType.IMPORTED,
            SourceType.SYSTEM_DERIVED,
            SourceType.LLM_INFERRED,
        ],
    )
    def test_record_confidence_equals_compute_confidence_for_every_fixed_source_type(
        self, source_type: SourceType
    ) -> None:
        record = _record(source_type=source_type)
        assert record.confidence == pytest.approx(compute_confidence(source_type))

    def test_record_confidence_equals_compute_confidence_for_summarized_from_n(self) -> None:
        confidences = [0.9, 0.6, 0.3]
        record = _record(
            source_type=SourceType.SUMMARIZED_FROM_N,
            source_confidences=confidences,
        )
        assert record.confidence == pytest.approx(
            compute_confidence(
                SourceType.SUMMARIZED_FROM_N, source_confidences=confidences
            )
        )

    def test_record_confidence_equals_compute_confidence_with_all_modifiers_combined(
        self,
    ) -> None:
        record = _record(
            source_type=SourceType.TOOL_OUTPUT,
            conflict_status=ConflictStatus.DISPUTED,
            failed_faithfulness_check=True,
            compression_generation=1,
        )
        assert record.confidence == pytest.approx(
            compute_confidence(
                SourceType.TOOL_OUTPUT,
                conflict_status=ConflictStatus.DISPUTED,
                failed_faithfulness_check=True,
                compression_generation=1,
            )
        )

    def test_confidence_clamps_to_zero_not_negative_at_the_lower_boundary(self) -> None:
        """Boundary: a low SUMMARIZED_FROM_N base (mean of low source
        confidences x 0.6) combined with both negative modifiers drives the
        raw formula below 0 -- HLD Section 12G's clamp must floor at
        exactly 0.0, never go negative.
        """
        low_source_confidences = [0.1, 0.1]
        value = compute_confidence(
            SourceType.SUMMARIZED_FROM_N,
            conflict_status=ConflictStatus.DISPUTED,
            failed_faithfulness_check=True,
            source_confidences=low_source_confidences,
        )
        assert value == pytest.approx(0.0)
        assert value >= 0.0
        record = _record(
            source_type=SourceType.SUMMARIZED_FROM_N,
            conflict_status=ConflictStatus.DISPUTED,
            failed_faithfulness_check=True,
            source_confidences=low_source_confidences,
        )
        assert record.confidence == pytest.approx(value)

    def test_confidence_clamps_to_one_not_above_at_the_upper_boundary(self) -> None:
        """Boundary: USER_STATED's base is exactly 1.0 with zero modifiers
        -- must stay at the inclusive upper boundary, never exceed it.
        """
        value = compute_confidence(SourceType.USER_STATED)
        assert value == pytest.approx(1.0)
        assert value <= 1.0


class TestBoundaryAndAdversarialMatrix:
    """The boundary/adversarial coverage the dev smoke suite does not
    already exercise, per this story's own AR1-005/QA-split convention
    (rule 33/40/41: TTL-exactly-at-expiry-equivalent boundaries, malformed
    source_type, replay).
    """

    def test_malformed_source_type_string_is_rejected_at_the_repository_boundary(
        self, repo: SqlProvenanceRepository, connection: FakeProvenanceConnection
    ) -> None:
        """Adversarial: a row with a `source_type` string outside the fixed
        enum (e.g. corrupted data, or a future value a reader does not yet
        know) must raise rather than silently constructing an invalid
        domain object.
        """
        good = _record(provenance_id="prov-1")
        repo.append(good)
        tenant_id, provenance_id, item_id, source_zone, _source_type, *rest = (
            connection.store.rows[0]
        )
        corrupted_row = (
            tenant_id,
            provenance_id,
            item_id,
            source_zone,
            "not_a_real_source_type",
            *rest,
        )
        connection.store.rows[0] = corrupted_row

        with pytest.raises(ValueError):
            repo.find_by_item_id(tenant_id="tenant-1", item_id="item-1")

    def test_replay_of_the_same_write_timestamp_does_not_lose_either_record(
        self, repo: SqlProvenanceRepository
    ) -> None:
        """Adversarial "replay": two records for the same item_id land with
        an identical write_timestamp (e.g. a retried write after a network
        timeout produced a duplicate-looking append) -- both must remain
        retrievable, not silently deduplicated or overwritten.
        """
        first = _record(provenance_id="prov-1", write_timestamp=_FIXED_TS)
        second = _record(
            provenance_id="prov-2",
            write_timestamp=_FIXED_TS,
            prev_hash=first.record_hash,
            prev_provenance_id=first.provenance_id,
        )
        repo.append(first)
        repo.append(second)

        lineage = repo.find_by_item_id(tenant_id="tenant-1", item_id="item-1")

        assert len(lineage) == 2
        assert {r.provenance_id for r in lineage} == {"prov-1", "prov-2"}

    def test_find_by_item_id_returns_empty_list_for_an_item_with_no_provenance(
        self, repo: SqlProvenanceRepository
    ) -> None:
        assert repo.find_by_item_id(tenant_id="tenant-1", item_id="never-written") == []

    def test_find_latest_by_item_id_returns_none_for_an_item_with_no_provenance(
        self, repo: SqlProvenanceRepository
    ) -> None:
        assert (
            repo.find_latest_by_item_id(tenant_id="tenant-1", item_id="never-written")
            is None
        )

    def test_find_by_item_id_wraps_a_driver_failure_as_zone_repository_error(
        self,
    ) -> None:
        repo = SqlProvenanceRepository(RaisingConnection(), verify_privileges=False)
        with pytest.raises(ZoneRepositoryError):
            repo.find_by_item_id(tenant_id="tenant-1", item_id="item-1")

    def test_append_wraps_a_driver_failure_as_zone_repository_error(self) -> None:
        repo = SqlProvenanceRepository(RaisingConnection(), verify_privileges=False)
        with pytest.raises(ZoneRepositoryError):
            repo.append(_record())

    def test_source_refs_round_trip_through_the_store_for_summarized_from_n(
        self, repo: SqlProvenanceRepository
    ) -> None:
        record = ProvenanceRecord.create(
            tenant_id="tenant-1",
            provenance_id="prov-1",
            item_id="item-1",
            source_zone=ZoneId.SEMANTIC,
            source_type=SourceType.SUMMARIZED_FROM_N,
            write_timestamp=_FIXED_TS,
            actor="consolidation-worker",
            change="initial write",
            retrieval_context_hash=_PLACEHOLDER_CONTEXT_HASH,
            source_refs=("item-a", "item-b", "item-c"),
            source_confidences=[0.8, 0.6, 0.4],
        )
        repo.append(record)

        found = repo.find_latest_by_item_id(tenant_id="tenant-1", item_id="item-1")

        assert found is not None
        assert found.source_refs == ("item-a", "item-b", "item-c")


class TestMustNotDeviateAppendOnlyRealBehavior:
    """Real (not only structural) proof of must-not-deviate items 1 and 4:
    every write is exactly one INSERT and reads never touch the insert
    counter, over a store that genuinely grows across many appends.
    """

    def test_append_is_the_only_operation_that_grows_the_store(
        self, repo: SqlProvenanceRepository, connection: FakeProvenanceConnection
    ) -> None:
        for i in range(20):
            repo.append(
                _record(
                    provenance_id=f"prov-{i}",
                    item_id=f"item-{i}",
                    write_timestamp=_FIXED_TS + timedelta(minutes=i),
                )
            )
        assert connection.store.insert_calls == 20
        assert len(connection.store.rows) == 20

        repo.find_by_item_id(tenant_id="tenant-1", item_id="item-0")
        repo.find_latest_by_item_id(tenant_id="tenant-1", item_id="item-0")

        assert connection.store.insert_calls == 20, (
            "reads must never call insert -- must-not-deviate item 1 "
            "(no UPDATE grant / append-only) proven behaviorally"
        )
        assert len(connection.store.rows) == 20

    def test_each_append_issues_exactly_one_insert_regardless_of_store_size(
        self, repo: SqlProvenanceRepository, connection: FakeProvenanceConnection
    ) -> None:
        """O(1) amortized write (must-not-deviate item 4): the number of
        `execute` calls per `append()` stays exactly 1 whether the store
        already holds 0 or 199 prior records.
        """
        for i in range(199):
            repo.append(_record(provenance_id=f"prov-{i}", item_id=f"item-{i}"))

        before = len(connection.cursor_obj.executed)
        repo.append(_record(provenance_id="prov-199", item_id="item-199"))
        after = len(connection.cursor_obj.executed)

        assert after - before == 1


class TestArchitectureFitnessScopedToStory:
    """HLD Section 3.0 invariant 1, scoped to this story's own new domain
    module: `domain/provenance_record.py` may not import from
    `dashanan.infrastructure`. (The repo-wide sweep already lives in
    test_memory_orchestrator.py; this is the QA subtask's own independent,
    story-scoped proof, matching DASH-STORY-002/003's precedent.)
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

    def test_domain_module_exists(self) -> None:
        assert PROVENANCE_RECORD_FILE.is_file(), f"{PROVENANCE_RECORD_FILE} not found"

    def test_domain_module_does_not_import_infrastructure(self) -> None:
        imported = self._imported_module_names(PROVENANCE_RECORD_FILE)
        bad = [
            name
            for name in imported
            if name == "dashanan.infrastructure"
            or name.startswith("dashanan.infrastructure.")
        ]
        assert not bad, (
            "HLD 3.0 invariant 1 violated -- "
            f"{PROVENANCE_RECORD_FILE.name} imports infrastructure: {bad}"
        )

    def test_provenance_record_is_a_frozen_immutable_value_object(self) -> None:
        import dataclasses

        assert dataclasses.is_dataclass(ProvenanceRecord)
        params = ProvenanceRecord.__dataclass_params__
        assert params.frozen is True

    def test_repository_does_not_implement_the_frozen_zone_repository_protocol(
        self,
    ) -> None:
        """This story's own documented judgment call (dev report): Zone 7
        is deliberately NOT wired to the frozen `ZoneRepository` Protocol in
        `domain/ports.py` (HLD 3.10: Zone 7 "READS: nothing"). Confirm the
        adapter exposes its own bespoke `find_by_item_id`/
        `find_latest_by_item_id`/`append` surface, not a generic `fetch`.
        """
        assert not hasattr(SqlProvenanceRepository, "fetch")
