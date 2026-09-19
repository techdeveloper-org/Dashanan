"""Smoke assertions for DASH-STORY-006, inline per the dev subtask scope.

The formal pytest suite covering AC-010/AC-016 in full is the QA
subtask's responsibility (see the story's `qa_prompt`). These checks
confirm the package is importable, wired correctly, and that the
must-not-deviate structural properties (422-before-persistence,
WAL/outbox durability barrier, structural unbypassability, cross-zone
scope, HLD Threat S-2 binding) hold, ahead of that formal suite landing
-- the exact scope split `tests/test_smoke_provenance.py` used for
DASH-STORY-005.

Runtime assumptions recorded per rule 33/40 test-roadmap conventions:
  - clock: FakeClock, fixed at 2026-01-01T00:00:00+00:00 unless a test
    states otherwise -- deterministic, no wall-clock dependency.
  - tenant_id: "tenant-1" for all requests unless a test states
    otherwise.
  - No fixture seed is required; nothing in this suite is randomized.

PII NOTE: per the dev_prompt's PII constraint, this suite carries only
write-path control metadata -- pseudonymized item_id/caller_identity
values and a placeholder retrieval_context_hash; no zone payload/fact
content, real or synthetic-realistic, appears anywhere below.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from dashanan.application.provenance_write_gate import (
    ERROR_FORGED_USER_TURN_MARKER,
    ERROR_MISSING_CALLER_BINDING,
    ERROR_MISSING_USER_TURN_MARKER,
    ERROR_UNRESOLVABLE_SOURCE_TYPE,
    ProvenanceWriteGate,
)
from dashanan.domain.provenance_record import SourceType
from dashanan.domain.write_gate import (
    ProvenanceJournalEntry,
    UserTurnAttestation,
    WriteAccepted,
    WriteRejected,
    WriteRequest,
    is_hex_sha256,
    resolve_source_type,
    sign_user_turn_attestation,
)
from dashanan.domain.zone import ZoneId
from dashanan.infrastructure.sql_write_journal_repository import (
    _APPEND_SQL,
    SqlWriteJournalRepository,
)

_PLACEHOLDER_CONTEXT_HASH = hashlib.sha256(b"<PII_EXAMPLE_REDACTED>").hexdigest()
_FIXED_TS = datetime(2026, 1, 1, tzinfo=UTC)
# Test-only HMAC key for UserTurnAttestation signing/verification -- never a
# real secret; production keys come from a secrets manager (see
# ProvenanceWriteGate.__init__'s user_turn_signing_key docstring).
_TEST_SIGNING_KEY = b"smoke-suite-test-only-user-turn-signing-key"


class FakeClock:
    """Deterministic Clock double: fixed instant, injectable (testing-core DI)."""

    def __init__(self, fixed: datetime) -> None:
        self._fixed = fixed

    def now(self) -> datetime:
        return self._fixed


class RecordingJournal:
    """ProvenanceJournalPort double: records every append() call, can raise on demand."""

    def __init__(self, raises: Exception | None = None) -> None:
        self.entries: list[ProvenanceJournalEntry] = []
        self._raises = raises

    def append(self, entry: ProvenanceJournalEntry) -> None:
        if self._raises is not None:
            raise self._raises
        self.entries.append(entry)

    def find_by_idempotency_key(
        self, tenant_id: str, idempotency_key: str
    ) -> ProvenanceJournalEntry | None:
        for entry in self.entries:
            if (
                entry.tenant_id == tenant_id
                and entry.idempotency_key == idempotency_key
            ):
                return entry
        return None


class OrderTracker:
    """Records call order across the journal double and persist_fact together."""

    def __init__(self, journal: RecordingJournal) -> None:
        self._journal = journal
        self.calls: list[str] = []
        self._original_append = journal.append
        journal.append = self._tracked_append  # type: ignore[method-assign]

    def _tracked_append(self, entry: ProvenanceJournalEntry) -> None:
        self.calls.append("journal.append")
        self._original_append(entry)

    def persist_fact(self) -> None:
        self.calls.append("persist_fact")


class RecordingCursor:
    """DB-API cursor double: records every execute() call, can raise on demand."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[object, ...]]] = []
        self.raise_on_execute: Exception | None = None
        # Configured by find_by_idempotency_key tests to stand in for the
        # row a real SELECT would return; None means "no matching row".
        self.queued_fetchone_result: Sequence[object] | None = None

    def execute(self, sql: str, params: Sequence[object]) -> None:
        self.executed.append((sql, tuple(params)))
        if self.raise_on_execute is not None:
            raise self.raise_on_execute

    def fetchone(self) -> Sequence[object] | None:
        return self.queued_fetchone_result


class RecordingConnection:
    """DB-API connection double exposing one shared RecordingCursor."""

    def __init__(self) -> None:
        self.cursor_obj = RecordingCursor()
        self.commit_count = 0
        self.raise_on_commit: Exception | None = None

    def cursor(self) -> RecordingCursor:
        return self.cursor_obj

    def commit(self) -> None:
        self.commit_count += 1
        if self.raise_on_commit is not None:
            raise self.raise_on_commit


def _request(
    *,
    tenant_id: str = "tenant-1",
    item_id: str = "item-1",
    source_zone: ZoneId = ZoneId.EPISODIC,
    source_type_raw: str = "tool_output",
    caller_identity: str = "dashanan-orchestrator",
    retrieval_context_hash: str = _PLACEHOLDER_CONTEXT_HASH,
    idempotency_key: str | None = None,
    user_turn_marker: bool = False,
    user_turn_attestation: UserTurnAttestation | None = None,
) -> WriteRequest:
    return WriteRequest(
        tenant_id=tenant_id,
        item_id=item_id,
        source_zone=source_zone,
        source_type_raw=source_type_raw,
        caller_identity=caller_identity,
        retrieval_context_hash=retrieval_context_hash,
        # A fresh nonce per call by default -- each helper invocation is a
        # distinct logical write unless a test explicitly reuses the same
        # key to exercise replay behavior.
        idempotency_key=idempotency_key or str(uuid4()),
        user_turn_marker=user_turn_marker,
        user_turn_attestation=user_turn_attestation,
    )


def _valid_user_turn_attestation(
    *,
    tenant_id: str = "tenant-1",
    item_id: str = "item-1",
    caller_identity: str = "dashanan-orchestrator",
    retrieval_context_hash: str = _PLACEHOLDER_CONTEXT_HASH,
    issued_at: datetime = _FIXED_TS,
) -> UserTurnAttestation:
    """Build a genuine attestation the way only the host (key-holder) can."""
    return sign_user_turn_attestation(
        secret=_TEST_SIGNING_KEY,
        tenant_id=tenant_id,
        item_id=item_id,
        caller_identity=caller_identity,
        retrieval_context_hash=retrieval_context_hash,
        issued_at=issued_at,
    )


def _noop() -> None:
    return None


class TestPackageWiring:
    """Baseline: the gate and adapter construct and are importable."""

    def test_gate_constructs_with_fake_journal_and_clock(self) -> None:
        gate = ProvenanceWriteGate(
            journal=RecordingJournal(),
            clock=FakeClock(_FIXED_TS),
            user_turn_signing_key=_TEST_SIGNING_KEY,
        )
        assert gate is not None

    def test_repository_constructs_with_fake_connection(self) -> None:
        repo = SqlWriteJournalRepository(RecordingConnection())
        assert repo is not None


class TestAC010UnresolvableSourceType:
    """AC-010: an unresolvable source_type is rejected (422) before persistence."""

    def test_blank_source_type_is_rejected_with_422(self) -> None:
        journal = RecordingJournal()
        gate = ProvenanceWriteGate(
            journal=journal,
            clock=FakeClock(_FIXED_TS),
            user_turn_signing_key=_TEST_SIGNING_KEY,
        )
        persisted = []

        result = gate.submit_write(
            _request(source_type_raw=""), lambda: persisted.append(True)
        )

        assert isinstance(result, WriteRejected)
        assert result.http_status == 422
        assert result.error_code == ERROR_UNRESOLVABLE_SOURCE_TYPE
        assert journal.entries == []
        assert persisted == []

    def test_unknown_source_type_value_is_rejected_with_422(self) -> None:
        journal = RecordingJournal()
        gate = ProvenanceWriteGate(
            journal=journal,
            clock=FakeClock(_FIXED_TS),
            user_turn_signing_key=_TEST_SIGNING_KEY,
        )
        persisted = []

        result = gate.submit_write(
            _request(source_type_raw="not_a_real_source_type"),
            lambda: persisted.append(True),
        )

        assert isinstance(result, WriteRejected)
        assert result.error_code == ERROR_UNRESOLVABLE_SOURCE_TYPE
        assert journal.entries == []
        assert persisted == []

    def test_resolvable_source_type_is_not_rejected_for_this_reason(self) -> None:
        assert resolve_source_type("tool_output") is SourceType.TOOL_OUTPUT
        assert resolve_source_type("") is None
        assert resolve_source_type(None) is None
        assert resolve_source_type("bogus") is None

    def test_non_string_source_type_resolves_to_none_without_raising(self) -> None:
        """MEDIUM finding: resolve_source_type had no type guard -- a
        non-string source_type_raw raised an unhandled exception instead
        of resolving to None (which the gate turns into a 422)."""
        assert resolve_source_type(123) is None
        assert resolve_source_type(["user_stated"]) is None
        assert resolve_source_type({"source_type": "user_stated"}) is None

    def test_non_string_source_type_is_rejected_with_422_not_a_crash(self) -> None:
        journal = RecordingJournal()
        gate = ProvenanceWriteGate(
            journal=journal,
            clock=FakeClock(_FIXED_TS),
            user_turn_signing_key=_TEST_SIGNING_KEY,
        )
        persisted = []
        request = _request()
        # Bypass the frozen dataclass's own str-typed field to simulate a
        # malformed caller -- the type hint is not runtime-enforced.
        object.__setattr__(request, "source_type_raw", 123)

        result = gate.submit_write(request, lambda: persisted.append(True))

        assert isinstance(result, WriteRejected)
        assert result.http_status == 422
        assert result.error_code == ERROR_UNRESOLVABLE_SOURCE_TYPE
        assert persisted == []


class TestAC016BypassRejectedBeforePersistence:
    """AC-016: a write that bypasses provenance recording (S-2 binding
    failure) is rejected before persistence, same enforcement path as
    AC-010."""

    def test_user_stated_without_user_turn_marker_is_rejected_with_422(self) -> None:
        journal = RecordingJournal()
        gate = ProvenanceWriteGate(
            journal=journal,
            clock=FakeClock(_FIXED_TS),
            user_turn_signing_key=_TEST_SIGNING_KEY,
        )
        persisted = []

        result = gate.submit_write(
            _request(source_type_raw="user_stated", user_turn_marker=False),
            lambda: persisted.append(True),
        )

        assert isinstance(result, WriteRejected)
        assert result.http_status == 422
        assert result.error_code == ERROR_MISSING_USER_TURN_MARKER
        assert journal.entries == []
        assert persisted == []

    def test_blank_caller_identity_is_rejected_before_persistence(self) -> None:
        journal = RecordingJournal()
        gate = ProvenanceWriteGate(
            journal=journal,
            clock=FakeClock(_FIXED_TS),
            user_turn_signing_key=_TEST_SIGNING_KEY,
        )
        persisted = []

        result = gate.submit_write(
            _request(caller_identity="   "), lambda: persisted.append(True)
        )

        assert isinstance(result, WriteRejected)
        assert result.error_code == ERROR_MISSING_CALLER_BINDING
        assert journal.entries == []
        assert persisted == []

    def test_malformed_retrieval_context_hash_is_rejected_before_persistence(
        self,
    ) -> None:
        journal = RecordingJournal()
        gate = ProvenanceWriteGate(
            journal=journal,
            clock=FakeClock(_FIXED_TS),
            user_turn_signing_key=_TEST_SIGNING_KEY,
        )
        persisted = []

        result = gate.submit_write(
            _request(retrieval_context_hash="not-a-sha256-hash"),
            lambda: persisted.append(True),
        )

        assert isinstance(result, WriteRejected)
        assert result.error_code == ERROR_MISSING_CALLER_BINDING
        assert journal.entries == []
        assert persisted == []


class TestHldThreatS2Binding:
    """HLD Threat S-2: source_type binds to caller identity + retrieval_context hash."""

    def test_accepted_user_stated_write_journals_the_marker_true(self) -> None:
        journal = RecordingJournal()
        gate = ProvenanceWriteGate(
            journal=journal,
            clock=FakeClock(_FIXED_TS),
            user_turn_signing_key=_TEST_SIGNING_KEY,
        )

        result = gate.submit_write(
            _request(
                source_type_raw="user_stated",
                user_turn_marker=True,
                user_turn_attestation=_valid_user_turn_attestation(),
            ),
            _noop,
        )

        assert isinstance(result, WriteAccepted)
        assert len(journal.entries) == 1
        assert journal.entries[0].source_type is SourceType.USER_STATED
        assert journal.entries[0].user_turn_marker is True

    def test_user_stated_with_marker_but_no_attestation_is_rejected_as_forged(
        self,
    ) -> None:
        """The exact CRITICAL finding this remediation closes: a caller
        setting source_type_raw='user_stated' and user_turn_marker=True
        together, with no genuine host-issued attestation, must not be
        accepted -- it previously forged a maximum-confidence (1.0)
        'user_stated' provenance attribution."""
        journal = RecordingJournal()
        gate = ProvenanceWriteGate(
            journal=journal,
            clock=FakeClock(_FIXED_TS),
            user_turn_signing_key=_TEST_SIGNING_KEY,
        )
        persisted = []

        result = gate.submit_write(
            _request(source_type_raw="user_stated", user_turn_marker=True),
            lambda: persisted.append(True),
        )

        assert isinstance(result, WriteRejected)
        assert result.http_status == 422
        assert result.error_code == ERROR_FORGED_USER_TURN_MARKER
        assert journal.entries == []
        assert persisted == []

    def test_user_stated_with_tampered_attestation_signature_is_rejected(self) -> None:
        journal = RecordingJournal()
        gate = ProvenanceWriteGate(
            journal=journal,
            clock=FakeClock(_FIXED_TS),
            user_turn_signing_key=_TEST_SIGNING_KEY,
        )
        genuine = _valid_user_turn_attestation()
        tampered = UserTurnAttestation(
            issued_at=genuine.issued_at, signature="0" * len(genuine.signature)
        )

        result = gate.submit_write(
            _request(
                source_type_raw="user_stated",
                user_turn_marker=True,
                user_turn_attestation=tampered,
            ),
            _noop,
        )

        assert isinstance(result, WriteRejected)
        assert result.error_code == ERROR_FORGED_USER_TURN_MARKER
        assert journal.entries == []

    def test_user_stated_attestation_signed_with_wrong_key_is_rejected(self) -> None:
        """A caller without the host's signing key cannot forge a valid
        attestation even if it knows the attestation's shape."""
        journal = RecordingJournal()
        gate = ProvenanceWriteGate(
            journal=journal,
            clock=FakeClock(_FIXED_TS),
            user_turn_signing_key=_TEST_SIGNING_KEY,
        )
        forged = sign_user_turn_attestation(
            secret=b"an-attacker-guessed-key",
            tenant_id="tenant-1",
            item_id="item-1",
            caller_identity="dashanan-orchestrator",
            retrieval_context_hash=_PLACEHOLDER_CONTEXT_HASH,
            issued_at=_FIXED_TS,
        )

        result = gate.submit_write(
            _request(
                source_type_raw="user_stated",
                user_turn_marker=True,
                user_turn_attestation=forged,
            ),
            _noop,
        )

        assert isinstance(result, WriteRejected)
        assert result.error_code == ERROR_FORGED_USER_TURN_MARKER
        assert journal.entries == []

    def test_accepted_entry_binds_caller_identity_and_context_hash(self) -> None:
        journal = RecordingJournal()
        gate = ProvenanceWriteGate(
            journal=journal,
            clock=FakeClock(_FIXED_TS),
            user_turn_signing_key=_TEST_SIGNING_KEY,
        )

        gate.submit_write(
            _request(
                caller_identity="dashanan-orchestrator",
                retrieval_context_hash=_PLACEHOLDER_CONTEXT_HASH,
            ),
            _noop,
        )

        entry = journal.entries[0]
        assert entry.caller_identity == "dashanan-orchestrator"
        assert entry.retrieval_context_hash == _PLACEHOLDER_CONTEXT_HASH

    def test_non_user_stated_write_does_not_require_the_marker(self) -> None:
        journal = RecordingJournal()
        gate = ProvenanceWriteGate(
            journal=journal,
            clock=FakeClock(_FIXED_TS),
            user_turn_signing_key=_TEST_SIGNING_KEY,
        )

        result = gate.submit_write(
            _request(source_type_raw="tool_output", user_turn_marker=False), _noop
        )

        assert isinstance(result, WriteAccepted)


class TestMustNotDeviateStructuralUnbypassability:
    """Must-not-deviate item 3: structurally unbypassable, never a convention."""

    def test_persist_fact_runs_only_after_journal_append_succeeds(self) -> None:
        journal = RecordingJournal()
        tracker = OrderTracker(journal)
        gate = ProvenanceWriteGate(
            journal=journal,
            clock=FakeClock(_FIXED_TS),
            user_turn_signing_key=_TEST_SIGNING_KEY,
        )

        gate.submit_write(_request(), tracker.persist_fact)

        assert tracker.calls == ["journal.append", "persist_fact"]

    def test_persist_fact_is_never_called_when_journal_append_raises(self) -> None:
        journal = RecordingJournal(raises=RuntimeError("disk full"))
        gate = ProvenanceWriteGate(
            journal=journal,
            clock=FakeClock(_FIXED_TS),
            user_turn_signing_key=_TEST_SIGNING_KEY,
        )
        persisted = []

        with pytest.raises(RuntimeError, match="disk full"):
            gate.submit_write(_request(), lambda: persisted.append(True))

        assert persisted == []

    def test_persist_fact_is_called_at_most_once_per_submit(self) -> None:
        journal = RecordingJournal()
        gate = ProvenanceWriteGate(
            journal=journal,
            clock=FakeClock(_FIXED_TS),
            user_turn_signing_key=_TEST_SIGNING_KEY,
        )
        call_count = {"n": 0}

        def counting_persist() -> None:
            call_count["n"] += 1

        gate.submit_write(_request(), counting_persist)

        assert call_count["n"] == 1

    def test_replayed_write_returns_cached_result_without_recalling_persist(
        self,
    ) -> None:
        """MEDIUM finding: a captured, verbatim-replayed WriteRequest must
        not be accepted -- and must not re-invoke persist_fact -- a
        second time. Same idempotency_key, submitted twice."""
        journal = RecordingJournal()
        gate = ProvenanceWriteGate(
            journal=journal,
            clock=FakeClock(_FIXED_TS),
            user_turn_signing_key=_TEST_SIGNING_KEY,
        )
        call_count = {"n": 0}

        def counting_persist() -> None:
            call_count["n"] += 1

        request = _request(idempotency_key="replay-nonce-1")

        first = gate.submit_write(request, counting_persist)
        second = gate.submit_write(request, counting_persist)

        assert isinstance(first, WriteAccepted)
        assert isinstance(second, WriteAccepted)
        assert first.write_id == second.write_id
        assert call_count["n"] == 1
        assert len(journal.entries) == 1


class TestMustNotDeviateCrossZoneScope:
    """Must-not-deviate item 4: applies across all Sprint-1 zones, no per-zone opt-in."""

    @pytest.mark.parametrize(
        "zone",
        [ZoneId.WORKING, ZoneId.EPISODIC, ZoneId.RETRIEVAL_INDEX, ZoneId.PROVENANCE],
    )
    def test_gate_accepts_a_resolvable_write_for_every_sprint_1_zone(
        self, zone: ZoneId
    ) -> None:
        journal = RecordingJournal()
        gate = ProvenanceWriteGate(
            journal=journal,
            clock=FakeClock(_FIXED_TS),
            user_turn_signing_key=_TEST_SIGNING_KEY,
        )

        result = gate.submit_write(_request(source_zone=zone), _noop)

        assert isinstance(result, WriteAccepted)
        assert journal.entries[0].source_zone is zone


class TestMustNotDeviateWalOutboxDurabilityBarrier:
    """Must-not-deviate item 2: WAL/outbox durability barrier (ADR-010)."""

    def test_append_issues_a_single_insert_and_commits_before_returning(self) -> None:
        connection = RecordingConnection()
        repo = SqlWriteJournalRepository(connection)
        entry = ProvenanceJournalEntry(
            tenant_id="tenant-1",
            write_id="write-1",
            item_id="item-1",
            source_zone=ZoneId.EPISODIC,
            source_type=SourceType.TOOL_OUTPUT,
            source_refs=(),
            caller_identity="dashanan-orchestrator",
            retrieval_context_hash=_PLACEHOLDER_CONTEXT_HASH,
            idempotency_key="idem-write-1",
            user_turn_marker=False,
            written_at=_FIXED_TS,
        )

        repo.append(entry)

        assert len(connection.cursor_obj.executed) == 1
        sql_used, params_used = connection.cursor_obj.executed[0]
        assert sql_used is _APPEND_SQL
        assert params_used[0] == "tenant-1"
        assert connection.commit_count == 1

    def test_no_journal_query_contains_update_or_delete(self) -> None:
        assert re.search(r"\bUPDATE\b", _APPEND_SQL, re.IGNORECASE) is None
        assert re.search(r"\bDELETE\b", _APPEND_SQL, re.IGNORECASE) is None

    def test_repository_exposes_no_update_or_delete_method(self) -> None:
        repo = SqlWriteJournalRepository(RecordingConnection())
        assert not hasattr(repo, "update")
        assert not hasattr(repo, "delete")

    def test_commit_failure_propagates_instead_of_being_swallowed(self) -> None:
        connection = RecordingConnection()
        connection.raise_on_commit = RuntimeError("fsync failed")
        repo = SqlWriteJournalRepository(connection)
        entry = ProvenanceJournalEntry(
            tenant_id="tenant-1",
            write_id="write-1",
            item_id="item-1",
            source_zone=ZoneId.EPISODIC,
            source_type=SourceType.TOOL_OUTPUT,
            source_refs=(),
            caller_identity="dashanan-orchestrator",
            retrieval_context_hash=_PLACEHOLDER_CONTEXT_HASH,
            idempotency_key="idem-write-1",
            user_turn_marker=False,
            written_at=_FIXED_TS,
        )

        with pytest.raises(RuntimeError, match="fsync failed"):
            repo.append(entry)

    def test_find_by_idempotency_key_maps_a_matching_row_back_to_an_entry(
        self,
    ) -> None:
        connection = RecordingConnection()
        connection.cursor_obj.queued_fetchone_result = (
            "tenant-1",
            "write-1",
            "item-1",
            ZoneId.EPISODIC.value,
            SourceType.TOOL_OUTPUT.value,
            [],
            "dashanan-orchestrator",
            _PLACEHOLDER_CONTEXT_HASH,
            "idem-write-1",
            False,
            _FIXED_TS,
        )
        repo = SqlWriteJournalRepository(connection)

        found = repo.find_by_idempotency_key("tenant-1", "idem-write-1")

        assert found is not None
        assert found.write_id == "write-1"
        assert found.idempotency_key == "idem-write-1"
        assert found.source_zone is ZoneId.EPISODIC
        assert found.source_type is SourceType.TOOL_OUTPUT

    def test_find_by_idempotency_key_returns_none_for_no_match(self) -> None:
        connection = RecordingConnection()
        repo = SqlWriteJournalRepository(connection)

        assert repo.find_by_idempotency_key("tenant-1", "never-journaled") is None


class TestBoundaryAndNegativeCases:
    """Boundary/negative cases per rule 33/40 test-roadmap conventions."""

    def test_write_request_rejects_blank_tenant_id(self) -> None:
        with pytest.raises(ValueError, match="tenant_id"):
            _request(tenant_id="")

    def test_write_request_rejects_blank_item_id(self) -> None:
        with pytest.raises(ValueError, match="item_id"):
            _request(item_id="  ")

    def test_write_request_rejects_blank_idempotency_key(self) -> None:
        with pytest.raises(ValueError, match="idempotency_key"):
            _request(idempotency_key="   ")

    def test_is_hex_sha256_accepts_a_well_formed_digest(self) -> None:
        assert is_hex_sha256(_PLACEHOLDER_CONTEXT_HASH) is True

    def test_is_hex_sha256_rejects_wrong_length(self) -> None:
        assert is_hex_sha256("abc123") is False

    def test_is_hex_sha256_rejects_uppercase_hex(self) -> None:
        assert is_hex_sha256(_PLACEHOLDER_CONTEXT_HASH.upper()) is False

    def test_every_accepted_write_gets_a_unique_write_id(self) -> None:
        journal = RecordingJournal()
        gate = ProvenanceWriteGate(
            journal=journal,
            clock=FakeClock(_FIXED_TS),
            user_turn_signing_key=_TEST_SIGNING_KEY,
        )

        first = gate.submit_write(_request(item_id="item-1"), _noop)
        second = gate.submit_write(_request(item_id="item-2"), _noop)

        assert isinstance(first, WriteAccepted)
        assert isinstance(second, WriteAccepted)
        assert first.write_id != second.write_id
