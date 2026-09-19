"""Tests for the FR-013 conflict-detection sweep (DSHN-60 HIGH remediation).

Covers both the pure decision logic (`domain.conflict_detection`) and its
application-layer wiring (`application.conflict_detection_sweep.
ConflictDetectingProvenanceRepository`), proving HLD Threat T-1's
"strongest control" now runs for real on every write rather than existing
only as a passive, never-computed `ConflictStatus` field.

Runtime assumptions recorded per rule 33/40 test-roadmap conventions:
  - clock: fixed literal `datetime(2026, 1, 1, tzinfo=UTC)` timestamps.
  - tenant_id: "tenant-1" for all records unless a test states otherwise.

PII NOTE: only pseudonymized item_id/provenance_id values and placeholder
retrieval_context_hash content appear below -- no fact/payload content.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from dashanan.application.conflict_detection_sweep import (
    ConflictDetectingProvenanceRepository,
)
from dashanan.domain.conflict_detection import (
    ConflictDetectionResult,
    detect_conflict,
    latest_active_record,
)
from dashanan.domain.provenance_record import ConflictStatus, ProvenanceRecord, SourceType
from dashanan.domain.zone import ZoneId

_PLACEHOLDER_CONTEXT_HASH = hashlib.sha256(b"<PII_EXAMPLE_REDACTED>").hexdigest()
_FIXED_TS = datetime(2026, 1, 1, tzinfo=UTC)
_LATER_TS = datetime(2026, 1, 1, 0, 5, tzinfo=UTC)


class FakeClock:
    """Deterministic Clock double: fixed instant, injectable (testing-core DI)."""

    def __init__(self, fixed: datetime) -> None:
        self._fixed = fixed

    def now(self) -> datetime:
        return self._fixed


class FakeProvenanceRepository:
    """In-memory `ProvenanceRepositoryPort` double, keyed by (tenant_id, item_id)."""

    def __init__(self) -> None:
        self.appended: list[ProvenanceRecord] = []

    def find_by_item_id(self, tenant_id: str, item_id: str) -> list[ProvenanceRecord]:
        return [
            r
            for r in self.appended
            if r.tenant_id == tenant_id and r.item_id == item_id
        ]

    def append(self, record: ProvenanceRecord) -> None:
        self.appended.append(record)


def _record(
    provenance_id: str,
    item_id: str = "item-1",
    prev_provenance_id: str | None = None,
    prev_hash: str | None = None,
    invalidation_flag: bool = False,
    write_timestamp: datetime = _FIXED_TS,
    tenant_id: str = "tenant-1",
) -> ProvenanceRecord:
    return ProvenanceRecord.create(
        tenant_id=tenant_id,
        provenance_id=provenance_id,
        item_id=item_id,
        source_zone=ZoneId.EPISODIC,
        source_type=SourceType.USER_STATED,
        write_timestamp=write_timestamp,
        actor="dashanan-orchestrator",
        change="initial write" if prev_provenance_id is None else "correction",
        retrieval_context_hash=_PLACEHOLDER_CONTEXT_HASH,
        prev_provenance_id=prev_provenance_id,
        prev_hash=prev_hash,
        invalidation_flag=invalidation_flag,
    )


class TestLatestActiveRecord:
    def test_returns_none_for_empty_history(self) -> None:
        assert latest_active_record([]) is None

    def test_returns_the_single_record_when_only_one_exists(self) -> None:
        record = _record("prov-1")
        assert latest_active_record([record]) is record

    def test_returns_the_chronologically_last_non_invalidated_record(self) -> None:
        first = _record("prov-1")
        second = _record(
            "prov-2", prev_provenance_id="prov-1", write_timestamp=_LATER_TS
        )
        assert latest_active_record([first, second]) is second

    def test_skips_invalidated_records_and_falls_back_to_an_earlier_active_one(
        self,
    ) -> None:
        first = _record("prov-1")
        second = _record(
            "prov-2",
            prev_provenance_id="prov-1",
            write_timestamp=_LATER_TS,
            invalidation_flag=True,
        )
        assert latest_active_record([first, second]) is first

    def test_returns_none_when_every_record_is_invalidated(self) -> None:
        first = _record("prov-1", invalidation_flag=True)
        assert latest_active_record([first]) is None


class TestDetectConflict:
    def test_no_existing_records_is_never_a_conflict(self) -> None:
        result = detect_conflict([], incoming_prev_provenance_id=None)
        assert result == ConflictDetectionResult(
            conflicting=False, disputed_provenance_id=None
        )

    def test_incoming_write_chaining_from_the_active_record_is_not_a_conflict(
        self,
    ) -> None:
        existing = _record("prov-1")
        result = detect_conflict([existing], incoming_prev_provenance_id="prov-1")
        assert result.conflicting is False

    def test_incoming_write_with_no_prev_id_against_an_active_record_is_a_conflict(
        self,
    ) -> None:
        """The exact T-1 scenario: a second, unlinked claim about item_id arrives."""
        existing = _record("prov-1")
        result = detect_conflict([existing], incoming_prev_provenance_id=None)
        assert result == ConflictDetectionResult(
            conflicting=True, disputed_provenance_id="prov-1"
        )

    def test_incoming_write_chaining_from_a_different_provenance_id_is_a_conflict(
        self,
    ) -> None:
        existing = _record("prov-1")
        result = detect_conflict(
            [existing], incoming_prev_provenance_id="some-other-prov-id"
        )
        assert result == ConflictDetectionResult(
            conflicting=True, disputed_provenance_id="prov-1"
        )

    def test_incoming_write_against_only_invalidated_records_is_not_a_conflict(
        self,
    ) -> None:
        existing = _record("prov-1", invalidation_flag=True)
        result = detect_conflict([existing], incoming_prev_provenance_id=None)
        assert result.conflicting is False

    def test_chain_of_two_records_incoming_correctly_chains_from_the_latest(
        self,
    ) -> None:
        first = _record("prov-1")
        second = _record(
            "prov-2", prev_provenance_id="prov-1", write_timestamp=_LATER_TS
        )
        result = detect_conflict([first, second], incoming_prev_provenance_id="prov-2")
        assert result.conflicting is False

    def test_chain_of_two_records_incoming_chains_from_the_stale_first_record(
        self,
    ) -> None:
        """A write that acknowledges only the FIRST record, ignoring the
        second, more recent one, is itself a contradiction."""
        first = _record("prov-1")
        second = _record(
            "prov-2", prev_provenance_id="prov-1", write_timestamp=_LATER_TS
        )
        result = detect_conflict([first, second], incoming_prev_provenance_id="prov-1")
        assert result == ConflictDetectionResult(
            conflicting=True, disputed_provenance_id="prov-2"
        )


class TestConflictDetectingProvenanceRepositoryAppend:
    def test_first_write_for_an_item_id_appends_only_itself(self) -> None:
        repo = FakeProvenanceRepository()
        sweep = ConflictDetectingProvenanceRepository(repo, FakeClock(_FIXED_TS))

        record = _record("prov-1")
        sweep.append(record)

        assert repo.appended == [record]

    def test_a_proper_chained_correction_appends_only_itself(self) -> None:
        repo = FakeProvenanceRepository()
        sweep = ConflictDetectingProvenanceRepository(repo, FakeClock(_FIXED_TS))
        first = _record("prov-1")
        repo.append(first)

        correction = _record(
            "prov-2", prev_provenance_id="prov-1", write_timestamp=_LATER_TS
        )
        sweep.append(correction)

        assert repo.appended == [first, correction]

    def test_an_unlinked_second_write_triggers_a_disputed_correction_first(
        self,
    ) -> None:
        repo = FakeProvenanceRepository()
        sweep = ConflictDetectingProvenanceRepository(repo, FakeClock(_LATER_TS))
        first = _record("prov-1")
        repo.append(first)

        conflicting_write = _record(
            "prov-2", prev_provenance_id=None, write_timestamp=_LATER_TS
        )
        sweep.append(conflicting_write)

        assert len(repo.appended) == 3
        auto_correction = repo.appended[1]
        assert auto_correction.conflict_status is ConflictStatus.DISPUTED
        assert auto_correction.update_history[0].prev_provenance_id == "prov-1"
        assert auto_correction.prev_hash == first.record_hash
        assert auto_correction.item_id == "item-1"

        # AC-015: BOTH records are downgraded -- the incoming write is also
        # re-appended as DISPUTED (with a recomputed confidence/hash), not
        # appended as the caller's original, un-downgraded object.
        downgraded_incoming = repo.appended[2]
        assert downgraded_incoming is not conflicting_write
        assert downgraded_incoming.provenance_id == conflicting_write.provenance_id
        assert downgraded_incoming.conflict_status is ConflictStatus.DISPUTED

    def test_disputed_correction_confidence_reflects_the_disputed_modifier(
        self,
    ) -> None:
        """`compute_confidence` applies HLD Section 3.8's -0.3 DISPUTED
        modifier -- the correction record's confidence must not equal the
        original record's confidence (must-not-deviate item 3: confidence
        is always computed, never copied statically)."""
        repo = FakeProvenanceRepository()
        sweep = ConflictDetectingProvenanceRepository(repo, FakeClock(_LATER_TS))
        first = _record("prov-1")
        repo.append(first)

        incoming = _record("prov-2", prev_provenance_id=None, write_timestamp=_LATER_TS)
        sweep.append(incoming)

        auto_correction = repo.appended[1]
        downgraded_incoming = repo.appended[2]
        assert auto_correction.confidence < first.confidence
        assert downgraded_incoming.confidence < incoming.confidence

    def test_find_by_item_id_delegates_unchanged(self) -> None:
        repo = FakeProvenanceRepository()
        sweep = ConflictDetectingProvenanceRepository(repo, FakeClock(_FIXED_TS))
        record = _record("prov-1")
        repo.append(record)

        assert sweep.find_by_item_id("tenant-1", "item-1") == [record]
