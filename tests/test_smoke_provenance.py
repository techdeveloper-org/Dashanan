"""Smoke assertions for DASH-STORY-005, inline per the dev subtask scope.

The formal pytest suite covering AC-007 in full is the QA subtask's
responsibility (see the story's `qa_prompt`). These checks confirm the
package is importable, wired correctly, and that the must-not-deviate
structural properties (no UPDATE grant / append-only, SHA-256 hash chain,
computed-not-static confidence, single-INSERT append) hold, ahead of that
formal suite landing -- the exact scope split `tests/test_smoke_episodic.py`
used for DASH-STORY-003.

Runtime assumptions recorded per rule 33/40 test-roadmap conventions:
  - clock: fixed literal `datetime(2026, 1, 1, tzinfo=UTC)` timestamps,
    passed explicitly per test -- no injected Clock port is needed since
    this repository takes `write_timestamp` as data, not a live clock read.
  - tenant_id: "tenant-1" for all requests unless a test states otherwise.
  - No fixture seed is required; nothing in this suite is randomized.

PII NOTE: per the dev_prompt's PII constraint, this suite carries only
schema-level illustration -- pseudonymized item_id/provenance_id values and
placeholder source_type/retrieval_context_hash content; no fact/payload
content, real or synthetic-realistic, appears anywhere below.
"""

from __future__ import annotations

import hashlib
import inspect
import re
from collections.abc import Sequence
from datetime import UTC, datetime

import pytest

from dashanan.domain.exceptions import ZoneRepositoryError
from dashanan.domain.provenance_record import (
    ConflictStatus,
    ProvenanceRecord,
    ProvenanceUpdateEntry,
    SourceType,
    compute_confidence,
    compute_record_hash,
    verify_chain,
)
from dashanan.domain.zone import ZoneId
from dashanan.infrastructure.sql_provenance_repository import (
    _APPEND_SQL,
    _FIND_BY_ITEM_SQL,
    _FIND_LATEST_BY_ITEM_SQL,
    SqlProvenanceRepository,
)

_PLACEHOLDER_CONTEXT_HASH = hashlib.sha256(b"<PII_EXAMPLE_REDACTED>").hexdigest()
_FIXED_TS = datetime(2026, 1, 1, tzinfo=UTC)


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


def _record(
    tenant_id: str = "tenant-1",
    provenance_id: str = "prov-1",
    item_id: str = "item-1",
    source_type: SourceType = SourceType.USER_STATED,
    write_timestamp: datetime = _FIXED_TS,
    prev_hash: str | None = None,
    prev_provenance_id: str | None = None,
) -> ProvenanceRecord:
    return ProvenanceRecord.create(
        tenant_id=tenant_id,
        provenance_id=provenance_id,
        item_id=item_id,
        source_zone=ZoneId.EPISODIC,
        source_type=source_type,
        write_timestamp=write_timestamp,
        actor="dashanan-orchestrator",
        change="initial write",
        retrieval_context_hash=_PLACEHOLDER_CONTEXT_HASH,
        prev_hash=prev_hash,
        prev_provenance_id=prev_provenance_id,
    )


def _row_for(record: ProvenanceRecord) -> tuple[object, ...]:
    import json

    return (
        record.tenant_id,
        record.provenance_id,
        record.item_id,
        record.source_zone.value,
        record.source_type.value,
        list(record.source_refs),
        record.write_timestamp,
        json.dumps(
            [
                {
                    "ts": e.ts.isoformat(),
                    "actor": e.actor,
                    "change": e.change,
                    "prev_provenance_id": e.prev_provenance_id,
                }
                for e in record.update_history
            ]
        ),
        record.retrieval_context_hash,
        record.conflict_status.value,
        record.invalidation_flag,
        record.confidence,
        record.prev_hash,
        record.record_hash,
    )


class TestPackageWiring:
    """Baseline: the adapter constructs and is importable, before any AC-level suite."""

    def test_repository_constructs_with_fake_connection(self) -> None:
        repo = SqlProvenanceRepository(RecordingConnection(), verify_privileges=False)
        assert repo is not None


class TestMustNotDeviateComputedConfidence:
    """Must-not-deviate item 3: confidence is COMPUTED, never assigned once and left static."""

    def test_base_values_match_hld_section_3_8_table(self) -> None:
        assert compute_confidence(SourceType.USER_STATED) == pytest.approx(1.0)
        assert compute_confidence(SourceType.TOOL_OUTPUT) == pytest.approx(0.9)
        assert compute_confidence(SourceType.IMPORTED) == pytest.approx(0.8)
        assert compute_confidence(SourceType.SYSTEM_DERIVED) == pytest.approx(0.7)
        assert compute_confidence(SourceType.LLM_INFERRED) == pytest.approx(0.6)

    def test_summarized_from_n_uses_mean_of_source_confidences(self) -> None:
        value = compute_confidence(
            SourceType.SUMMARIZED_FROM_N, source_confidences=[1.0, 0.5]
        )
        assert value == pytest.approx(0.6 * 0.75)

    def test_summarized_from_n_requires_non_empty_source_confidences(self) -> None:
        with pytest.raises(ValueError, match="source_confidences"):
            compute_confidence(SourceType.SUMMARIZED_FROM_N)

    def test_disputed_modifier_subtracts_0_3(self) -> None:
        value = compute_confidence(
            SourceType.TOOL_OUTPUT, conflict_status=ConflictStatus.DISPUTED
        )
        assert value == pytest.approx(0.9 - 0.3)

    def test_failed_faithfulness_modifier_subtracts_0_2(self) -> None:
        value = compute_confidence(
            SourceType.TOOL_OUTPUT, failed_faithfulness_check=True
        )
        assert value == pytest.approx(0.9 - 0.2)

    def test_compression_generation_multiplies_by_0_9_per_generation(self) -> None:
        value = compute_confidence(SourceType.USER_STATED, compression_generation=2)
        assert value == pytest.approx(1.0 * 0.9 * 0.9)

    def test_compression_generation_rejects_negative(self) -> None:
        with pytest.raises(ValueError, match="compression_generation"):
            compute_confidence(SourceType.USER_STATED, compression_generation=-1)

    def test_result_is_clamped_to_0_1_range(self) -> None:
        value = compute_confidence(
            SourceType.LLM_INFERRED,
            conflict_status=ConflictStatus.DISPUTED,
            failed_faithfulness_check=True,
        )
        assert 0.0 <= value <= 1.0
        assert value == pytest.approx(0.1)  # 0.6 - 0.3 - 0.2 = 0.1, still within range

    def test_create_stores_the_computed_confidence_not_a_caller_literal(self) -> None:
        """ProvenanceRecord.create takes no `confidence` parameter at all --
        the only way must-not-deviate item 3 can be structurally guaranteed."""
        record = _record(source_type=SourceType.TOOL_OUTPUT)
        assert record.confidence == pytest.approx(
            compute_confidence(SourceType.TOOL_OUTPUT)
        )
        assert "confidence" not in inspect.signature(ProvenanceRecord.create).parameters


class TestMustNotDeviateHashChain:
    """Must-not-deviate item 2: SHA-256 prev_hash -> record_hash chain."""

    def test_record_hash_is_64_char_lowercase_hex(self) -> None:
        record = _record()
        assert len(record.record_hash) == 64
        assert all(c in "0123456789abcdef" for c in record.record_hash)

    def test_first_record_in_a_chain_has_no_prev_hash(self) -> None:
        record = _record(prev_hash=None)
        assert record.prev_hash is None

    def test_second_record_chains_to_the_first_via_prev_hash(self) -> None:
        first = _record(provenance_id="prov-1", prev_hash=None)
        second = _record(
            provenance_id="prov-2",
            prev_hash=first.record_hash,
            prev_provenance_id=first.provenance_id,
        )
        assert second.prev_hash == first.record_hash
        assert verify_chain([first, second]) is True

    def test_changing_any_field_changes_the_record_hash(self) -> None:
        base_kwargs = dict(
            tenant_id="tenant-1",
            provenance_id="prov-1",
            item_id="item-1",
            source_zone="episodic",
            source_type="user_stated",
            source_refs=(),
            write_timestamp=_FIXED_TS,
            retrieval_context_hash=_PLACEHOLDER_CONTEXT_HASH,
            conflict_status="none",
            invalidation_flag=False,
            confidence=1.0,
            prev_hash=None,
        )
        original = compute_record_hash(**base_kwargs)
        tampered = compute_record_hash(**{**base_kwargs, "confidence": 0.5})
        assert original != tampered

    def test_verify_chain_detects_a_tampered_confidence(self) -> None:
        """Simulates an attacker editing confidence in place without
        recomputing record_hash -- HLD threat T-2's exact scenario."""
        import dataclasses

        first = _record(provenance_id="prov-1", prev_hash=None)
        second = _record(
            provenance_id="prov-2",
            prev_hash=first.record_hash,
            prev_provenance_id=first.provenance_id,
        )
        tampered_second = dataclasses.replace(second, confidence=0.01)

        assert verify_chain([first, tampered_second]) is False

    def test_verify_chain_detects_a_broken_prev_hash_link(self) -> None:
        first = _record(provenance_id="prov-1", prev_hash=None)
        second = _record(provenance_id="prov-2", prev_hash="0" * 64)

        assert verify_chain([first, second]) is False

    def test_verify_chain_accepts_a_single_genesis_record(self) -> None:
        assert verify_chain([_record(prev_hash=None)]) is True

    def test_verify_chain_accepts_an_empty_sequence(self) -> None:
        assert verify_chain([]) is True


class TestMustNotDeviateAppendOnly:
    """Must-not-deviate item 1: no UPDATE grant / append-only, no mutation methods."""

    def test_repository_exposes_no_update_or_delete_method(self) -> None:
        repo = SqlProvenanceRepository(RecordingConnection(), verify_privileges=False)
        assert not hasattr(repo, "update")
        assert not hasattr(repo, "delete")

    def test_no_repository_query_string_contains_update_or_delete(self) -> None:
        # \b (word boundary) correctly does NOT match inside the
        # "update_history" column name, since "_" is a \w char with no
        # boundary before "HISTORY" -- this checks for the SQL keywords
        # UPDATE/DELETE as standalone tokens, not that substring.
        for sql in (_FIND_BY_ITEM_SQL, _FIND_LATEST_BY_ITEM_SQL, _APPEND_SQL):
            assert re.search(r"\bUPDATE\b", sql, re.IGNORECASE) is None
            assert re.search(r"\bDELETE\b", sql, re.IGNORECASE) is None

    def test_domain_entity_exposes_no_mutating_method(self) -> None:
        record = _record()
        assert not hasattr(record, "update")
        assert not hasattr(record, "correct")
        assert not hasattr(record, "set_confidence")


class TestMustNotDeviateAppendOnlyO1Write:
    """Must-not-deviate item 4: append-only, O(1) amortized write."""

    def test_append_issues_a_single_insert_statement(self) -> None:
        connection = RecordingConnection()
        repo = SqlProvenanceRepository(connection, verify_privileges=False)
        record = _record()

        repo.append(record)

        assert len(connection.cursor_obj.executed) == 1
        sql_used, params_used = connection.cursor_obj.executed[0]
        assert sql_used.strip().upper().startswith("INSERT INTO PROVENANCE_RECORDS")
        assert params_used[0] == "tenant-1"


class TestMustNotDeviateTenantIdRequired:
    """Structural extension of HLD 3.0 invariant 2 to this story's own repository."""

    def test_find_by_item_id_rejects_blank_tenant_id_before_querying(self) -> None:
        connection = RecordingConnection()
        repo = SqlProvenanceRepository(connection, verify_privileges=False)

        with pytest.raises(ValueError, match="tenant_id"):
            repo.find_by_item_id(tenant_id="", item_id="item-1")
        assert connection.cursor_obj.executed == []

    def test_find_latest_by_item_id_rejects_blank_tenant_id_before_querying(
        self,
    ) -> None:
        connection = RecordingConnection()
        repo = SqlProvenanceRepository(connection, verify_privileges=False)

        with pytest.raises(ValueError, match="tenant_id"):
            repo.find_latest_by_item_id(tenant_id="", item_id="item-1")
        assert connection.cursor_obj.executed == []

    def test_append_rejects_blank_tenant_id_before_querying(self) -> None:
        connection = RecordingConnection()
        repo = SqlProvenanceRepository(connection, verify_privileges=False)
        record = _record(tenant_id="tenant-1")
        object.__setattr__(record, "tenant_id", "")

        with pytest.raises(ValueError, match="tenant_id"):
            repo.append(record)
        assert connection.cursor_obj.executed == []


class TestAC007Retrievability:
    """AC-007: for that fact's item_id, a source attribution, confidence
    score, and edit history entry exists and is retrievable."""

    def test_find_by_item_id_returns_the_full_chronological_lineage(self) -> None:
        first = _record(provenance_id="prov-1", write_timestamp=_FIXED_TS)
        rows = [_row_for(first)]
        connection = RecordingConnection(rows=rows)
        repo = SqlProvenanceRepository(connection, verify_privileges=False)

        found = repo.find_by_item_id(tenant_id="tenant-1", item_id="item-1")

        sql_used, params_used = connection.cursor_obj.executed[0]
        assert sql_used == _FIND_BY_ITEM_SQL
        assert params_used == ("tenant-1", "item-1")
        assert len(found) == 1
        assert found[0].source_type == SourceType.USER_STATED
        assert found[0].confidence == pytest.approx(1.0)
        assert len(found[0].update_history) == 1

    def test_find_latest_by_item_id_returns_none_when_no_row_matches(self) -> None:
        connection = RecordingConnection(rows=[])
        repo = SqlProvenanceRepository(connection, verify_privileges=False)

        assert (
            repo.find_latest_by_item_id(tenant_id="tenant-1", item_id="item-1")
            is None
        )


class TestBoundaryAndNegativeCases:
    """Boundary/negative cases per rule 33/40 test-roadmap conventions."""

    def test_provenance_record_rejects_confidence_above_one(self) -> None:
        with pytest.raises(ValueError, match="confidence"):
            ProvenanceRecord(
                tenant_id="tenant-1",
                provenance_id="prov-1",
                item_id="item-1",
                source_zone=ZoneId.EPISODIC,
                source_type=SourceType.USER_STATED,
                source_refs=(),
                write_timestamp=_FIXED_TS,
                update_history=(
                    ProvenanceUpdateEntry(
                        ts=_FIXED_TS, actor="x", change="initial write"
                    ),
                ),
                retrieval_context_hash=_PLACEHOLDER_CONTEXT_HASH,
                conflict_status=ConflictStatus.NONE,
                invalidation_flag=False,
                confidence=1.5,
                prev_hash=None,
                record_hash="a" * 64,
            )

    def test_provenance_record_rejects_malformed_record_hash(self) -> None:
        with pytest.raises(ValueError, match="record_hash"):
            ProvenanceRecord(
                tenant_id="tenant-1",
                provenance_id="prov-1",
                item_id="item-1",
                source_zone=ZoneId.EPISODIC,
                source_type=SourceType.USER_STATED,
                source_refs=(),
                write_timestamp=_FIXED_TS,
                update_history=(
                    ProvenanceUpdateEntry(
                        ts=_FIXED_TS, actor="x", change="initial write"
                    ),
                ),
                retrieval_context_hash=_PLACEHOLDER_CONTEXT_HASH,
                conflict_status=ConflictStatus.NONE,
                invalidation_flag=False,
                confidence=1.0,
                prev_hash=None,
                record_hash="not-a-hash",
            )

    def test_provenance_record_rejects_empty_update_history(self) -> None:
        with pytest.raises(ValueError, match="update_history"):
            ProvenanceRecord(
                tenant_id="tenant-1",
                provenance_id="prov-1",
                item_id="item-1",
                source_zone=ZoneId.EPISODIC,
                source_type=SourceType.USER_STATED,
                source_refs=(),
                write_timestamp=_FIXED_TS,
                update_history=(),
                retrieval_context_hash=_PLACEHOLDER_CONTEXT_HASH,
                conflict_status=ConflictStatus.NONE,
                invalidation_flag=False,
                confidence=1.0,
                prev_hash=None,
                record_hash="a" * 64,
            )

    def test_provenance_record_rejects_blank_item_id(self) -> None:
        with pytest.raises(ValueError, match="item_id"):
            _record(item_id="")

    def test_provenance_update_entry_rejects_blank_actor(self) -> None:
        with pytest.raises(ValueError, match="actor"):
            ProvenanceUpdateEntry(ts=_FIXED_TS, actor="  ", change="initial write")

    def test_find_by_item_id_rejects_blank_item_id(self) -> None:
        repo = SqlProvenanceRepository(RecordingConnection(), verify_privileges=False)
        with pytest.raises(ValueError, match="item_id"):
            repo.find_by_item_id(tenant_id="tenant-1", item_id="")

    def test_find_by_item_id_wraps_underlying_failure_as_zone_repository_error(
        self,
    ) -> None:
        connection = RecordingConnection()
        connection.cursor_obj._raise = RuntimeError("connection refused")
        repo = SqlProvenanceRepository(connection, verify_privileges=False)

        with pytest.raises(ZoneRepositoryError):
            repo.find_by_item_id(tenant_id="tenant-1", item_id="item-1")
