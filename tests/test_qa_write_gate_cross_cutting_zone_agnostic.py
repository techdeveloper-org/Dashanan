"""QA pytest suite for DASH-STORY-006 (Cross-cutting provenance write-path
enforcement / WAL-outbox gate, FR-010).

QA subtask, independent verification pass on top of the Dev subtask's own
suite (tests/test_smoke_write_gate.py), matching the split DASH-STORY-003/
DASH-STORY-005 precedent (test_smoke_* vs test_qa_*).

Acceptance criteria under test, verbatim from
docs/phase-7-routing/implementation_execution_plan.json's DASH-STORY-006
qa_prompt/dev_prompt (identical criteria cited in both) and SRS.md:

  AC-010: "Given a write request to any zone omits a resolvable
  source_type, when the Orchestrator's write path processes it, then the
  write is rejected (HTTP 422) and no fact is persisted without a
  corresponding Provenance/Audit entry, consistent with the WAL/outbox
  durability barrier."

  AC-016: "Given a zone write bypasses provenance recording, when the
  write path is exercised, then the write is rejected before persistence
  (same enforcement path as AC-010)."

FR-010 under test, verbatim from SRS.md: "Every write to any of the 8
zones SHALL be required to register a corresponding Provenance/Audit entry
(FR-007) at write time; no zone may accept a fact without an attributable
source and confidence value."

MUST-NOT-DEVIATE items under test (ar1_assignments.json, AR1-006):
  1. HTTP 422 on unresolvable source_type, rejected BEFORE persistence.
  2. WAL/outbox durability barrier -- the journal append (ADR-010) must
     complete before persist_fact is ever invoked.
  3. The gate must be structurally unbypassable -- persist_fact is
     reachable only through submit_write, only after a successful
     durable journal append, never on any rejection path and never when
     the journal append raises.
  4. Applies across ALL Sprint 1 zones, never a per-zone opt-in.
  5. HLD Threat S-2 (forged provenance): source_type is recorded with the
     caller's identity and the retrieval_context hash; user_stated
     requires an explicit host-side user-turn marker -- the gate binds
     the two, never accepts a bare caller-asserted source_type.

Plus the architecture-fitness invariant (HLD Section 3.0, invariant 1): no
domain/** module may import infrastructure/**, scoped here to this
story's own two new modules (domain/write_gate.py,
application/provenance_write_gate.py).

Runtime assumptions (matching test_smoke_write_gate.py's own conventions):
  - A `FakeClock` fixture supplies a fixed, deterministic `datetime.now()`.
  - A `RecordingJournal` fake implements `ProvenanceJournalPort`, recording
    every `append()` call and, when configured, raising instead.
  - tenant_id "tenant-1", a 64-char lowercase hex `retrieval_context_hash`
    fixture, unless a test states otherwise.

PII NOTE: this suite carries only schema-level control metadata --
pseudonymized item_id/caller_identity values and a placeholder hex hash;
no fact/payload content appears anywhere below.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path
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
    requires_user_turn_marker,
    resolve_source_type,
    sign_user_turn_attestation,
)
from dashanan.domain.zone import ZoneId

VALID_HASH = "a" * 64
# Test-only HMAC key for UserTurnAttestation signing/verification -- never a
# real secret; production keys come from a secrets manager (see
# ProvenanceWriteGate.__init__'s user_turn_signing_key docstring).
TEST_SIGNING_KEY = b"qa-suite-test-only-user-turn-signing-key"


class FakeClock:
    """Deterministic clock double (testing-core: DI over patching datetime.now)."""

    def __init__(self, fixed: datetime) -> None:
        self._fixed = fixed

    def now(self) -> datetime:
        return self._fixed


class RecordingJournal:
    """Records every `append()` call; can be configured to raise instead."""

    def __init__(self, *, raise_on_append: Exception | None = None) -> None:
        self.entries: list[ProvenanceJournalEntry] = []
        self._raise_on_append = raise_on_append

    def append(self, entry: ProvenanceJournalEntry) -> None:
        if self._raise_on_append is not None:
            raise self._raise_on_append
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


def make_request(**overrides: object) -> WriteRequest:
    """Build a valid `WriteRequest`, overridable per test.

    `idempotency_key` defaults to a fresh nonce per call so independent
    submissions never collide; a test exercising replay behavior passes
    the same `idempotency_key` explicitly on both calls.
    """
    defaults: dict[str, object] = {
        "tenant_id": "tenant-1",
        "item_id": "item-1",
        "source_zone": ZoneId.WORKING,
        "source_type_raw": "tool_output",
        "caller_identity": "svc-orchestrator",
        "retrieval_context_hash": VALID_HASH,
        "idempotency_key": str(uuid4()),
        "source_refs": (),
        "user_turn_marker": False,
        "user_turn_attestation": None,
    }
    defaults.update(overrides)
    return WriteRequest(**defaults)  # type: ignore[arg-type]


def make_valid_user_turn_attestation(
    *,
    tenant_id: str = "tenant-1",
    item_id: str = "item-1",
    caller_identity: str = "host-session-42",
    retrieval_context_hash: str = VALID_HASH,
    issued_at: datetime = datetime(2026, 1, 1, tzinfo=UTC),
) -> UserTurnAttestation:
    """Build a genuine attestation the way only the host (key-holder) can."""
    return sign_user_turn_attestation(
        secret=TEST_SIGNING_KEY,
        tenant_id=tenant_id,
        item_id=item_id,
        caller_identity=caller_identity,
        retrieval_context_hash=retrieval_context_hash,
        issued_at=issued_at,
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 1, 1, tzinfo=UTC))


@pytest.fixture
def journal() -> RecordingJournal:
    return RecordingJournal()


@pytest.fixture
def gate(journal: RecordingJournal, clock: FakeClock) -> ProvenanceWriteGate:
    return ProvenanceWriteGate(
        journal=journal, clock=clock, user_turn_signing_key=TEST_SIGNING_KEY
    )


class PersistTracker:
    """Records whether/when the caller's own zone-content write ran."""

    def __init__(self) -> None:
        self.calls = 0

    def persist(self) -> None:
        self.calls += 1


# ---------------------------------------------------------------------------
# AC-010: unresolvable source_type -> HTTP 422, rejected before persistence.
# ---------------------------------------------------------------------------


class TestAC010UnresolvableSourceType:
    def test_none_source_type_is_rejected_422(
        self, gate: ProvenanceWriteGate, journal: RecordingJournal
    ) -> None:
        persist = PersistTracker()
        request = make_request(source_type_raw="")
        result = gate.submit_write(request, persist.persist)

        assert isinstance(result, WriteRejected)
        assert result.http_status == 422
        assert result.error_code == ERROR_UNRESOLVABLE_SOURCE_TYPE
        assert journal.entries == []
        assert persist.calls == 0

    def test_garbage_source_type_is_rejected_422(
        self, gate: ProvenanceWriteGate, journal: RecordingJournal
    ) -> None:
        persist = PersistTracker()
        request = make_request(source_type_raw="not_a_real_source_type")
        result = gate.submit_write(request, persist.persist)

        assert isinstance(result, WriteRejected)
        assert result.http_status == 422
        assert result.error_code == ERROR_UNRESOLVABLE_SOURCE_TYPE
        assert journal.entries == []
        assert persist.calls == 0

    def test_whitespace_only_source_type_is_rejected_422(
        self, gate: ProvenanceWriteGate, journal: RecordingJournal
    ) -> None:
        persist = PersistTracker()
        request = make_request(source_type_raw="   ")
        result = gate.submit_write(request, persist.persist)

        assert isinstance(result, WriteRejected)
        assert result.http_status == 422
        assert journal.entries == []
        assert persist.calls == 0

    @pytest.mark.parametrize(
        "source_type_raw",
        [
            "tool_output",
            "llm_inferred",
            "summarized_from_n",
            "imported",
            "system_derived",
        ],
    )
    def test_every_resolvable_non_user_stated_source_type_is_accepted(
        self,
        gate: ProvenanceWriteGate,
        journal: RecordingJournal,
        source_type_raw: str,
    ) -> None:
        persist = PersistTracker()
        request = make_request(source_type_raw=source_type_raw)
        result = gate.submit_write(request, persist.persist)

        assert isinstance(result, WriteAccepted)
        assert len(journal.entries) == 1
        assert journal.entries[0].source_type is SourceType(source_type_raw)
        assert persist.calls == 1

    def test_accepted_write_has_corresponding_journal_entry(
        self, gate: ProvenanceWriteGate, journal: RecordingJournal
    ) -> None:
        """AC-010: "no fact is persisted without a corresponding
        Provenance/Audit entry" -- proven by asserting a durable journal
        entry exists for every accepted write's write_id."""
        persist = PersistTracker()
        request = make_request()
        result = gate.submit_write(request, persist.persist)

        assert isinstance(result, WriteAccepted)
        assert len(journal.entries) == 1
        assert journal.entries[0].write_id == result.write_id
        assert journal.entries[0].item_id == request.item_id
        assert journal.entries[0].tenant_id == request.tenant_id


# ---------------------------------------------------------------------------
# AC-016: a write bypassing provenance recording is rejected BEFORE
# persistence, via the same enforcement path as AC-010.
# ---------------------------------------------------------------------------


class TestAC016BypassRejectedBeforePersistence:
    def test_missing_caller_identity_is_rejected_before_persistence(
        self, gate: ProvenanceWriteGate, journal: RecordingJournal
    ) -> None:
        persist = PersistTracker()
        request = make_request(caller_identity="")
        result = gate.submit_write(request, persist.persist)

        assert isinstance(result, WriteRejected)
        assert result.http_status == 422
        assert result.error_code == ERROR_MISSING_CALLER_BINDING
        assert journal.entries == []
        assert persist.calls == 0

    def test_malformed_retrieval_context_hash_is_rejected_before_persistence(
        self, gate: ProvenanceWriteGate, journal: RecordingJournal
    ) -> None:
        persist = PersistTracker()
        request = make_request(retrieval_context_hash="not-a-sha256-hash")
        result = gate.submit_write(request, persist.persist)

        assert isinstance(result, WriteRejected)
        assert result.http_status == 422
        assert result.error_code == ERROR_MISSING_CALLER_BINDING
        assert journal.entries == []
        assert persist.calls == 0

    def test_missing_user_turn_marker_is_rejected_before_persistence(
        self, gate: ProvenanceWriteGate, journal: RecordingJournal
    ) -> None:
        persist = PersistTracker()
        request = make_request(source_type_raw="user_stated", user_turn_marker=False)
        result = gate.submit_write(request, persist.persist)

        assert isinstance(result, WriteRejected)
        assert result.http_status == 422
        assert result.error_code == ERROR_MISSING_USER_TURN_MARKER
        assert journal.entries == []
        assert persist.calls == 0

    def test_rejection_uses_same_enforcement_path_and_result_type_as_ac010(
        self, gate: ProvenanceWriteGate, journal: RecordingJournal
    ) -> None:
        """Both AC-010's and AC-016's rejections resolve to the identical
        `WriteRejected(http_status=422, ...)` Result-type shape -- proving
        "same enforcement path" structurally, not by convention."""
        persist = PersistTracker()
        ac010_result = gate.submit_write(
            make_request(source_type_raw="garbage"), persist.persist
        )
        ac016_result = gate.submit_write(
            make_request(caller_identity=""), persist.persist
        )

        assert isinstance(ac010_result, WriteRejected)
        assert isinstance(ac016_result, WriteRejected)
        assert ac010_result.http_status == ac016_result.http_status == 422
        assert journal.entries == []
        assert persist.calls == 0


# ---------------------------------------------------------------------------
# Must-not-deviate 2/3: WAL/outbox durability barrier + structural
# unbypassability -- persist_fact runs only after a successful durable
# journal append, never before, never on a failed append.
# ---------------------------------------------------------------------------


class TestDurabilityBarrierAndStructuralUnbypassability:
    def test_persist_fact_runs_only_after_journal_append_in_call_order(
        self, clock: FakeClock
    ) -> None:
        call_order: list[str] = []

        class OrderTrackingJournal:
            def append(self, entry: ProvenanceJournalEntry) -> None:
                call_order.append("journal.append")

            def find_by_idempotency_key(
                self, tenant_id: str, idempotency_key: str
            ) -> ProvenanceJournalEntry | None:
                return None

        gate = ProvenanceWriteGate(
            journal=OrderTrackingJournal(),
            clock=clock,
            user_turn_signing_key=TEST_SIGNING_KEY,
        )

        def persist_fact() -> None:
            call_order.append("persist_fact")

        result = gate.submit_write(make_request(), persist_fact)

        assert isinstance(result, WriteAccepted)
        assert call_order == ["journal.append", "persist_fact"]

    def test_persist_fact_is_never_called_when_journal_append_raises(
        self, clock: FakeClock
    ) -> None:
        failing_journal = RecordingJournal(raise_on_append=RuntimeError("wal failure"))
        gate = ProvenanceWriteGate(
            journal=failing_journal, clock=clock, user_turn_signing_key=TEST_SIGNING_KEY
        )
        persist = PersistTracker()

        with pytest.raises(RuntimeError, match="wal failure"):
            gate.submit_write(make_request(), persist.persist)

        assert persist.calls == 0

    def test_exception_from_persist_fact_still_propagates_after_durable_journal(
        self, gate: ProvenanceWriteGate, journal: RecordingJournal
    ) -> None:
        """A downstream zone-write failure is a separate failure mode the
        gate does not compensate for (ADR-010: the journal, not the zone
        write, is the durability barrier) -- the journal entry remains
        durable even though `submit_write` itself raises."""

        def failing_persist() -> None:
            raise ValueError("zone write failed")

        with pytest.raises(ValueError, match="zone write failed"):
            gate.submit_write(make_request(), failing_persist)

        assert len(journal.entries) == 1

    def test_submit_write_is_the_only_public_gate_method(self) -> None:
        """Structural unbypassability: `submit_write` is the sole path
        that can lead to `persist_fact` running."""
        public_methods = [
            name
            for name in vars(ProvenanceWriteGate)
            if not name.startswith("_") and callable(getattr(ProvenanceWriteGate, name))
        ]
        assert public_methods == ["submit_write"]


# ---------------------------------------------------------------------------
# Must-not-deviate 4: applies across ALL Sprint 1 zones, never a per-zone
# opt-in.
# ---------------------------------------------------------------------------


class TestMustNotDeviateCrossZoneScope:
    @pytest.mark.parametrize(
        "zone",
        [ZoneId.WORKING, ZoneId.EPISODIC, ZoneId.RETRIEVAL_INDEX, ZoneId.PROVENANCE],
    )
    def test_gate_accepts_identically_across_every_sprint1_zone(
        self, gate: ProvenanceWriteGate, journal: RecordingJournal, zone: ZoneId
    ) -> None:
        persist = PersistTracker()
        request = make_request(source_zone=zone)
        result = gate.submit_write(request, persist.persist)

        assert isinstance(result, WriteAccepted)
        assert journal.entries[0].source_zone is zone
        assert persist.calls == 1

    def test_no_zone_allowlist_constant_exists_on_write_request(self) -> None:
        """No allowlist mechanism anywhere on `WriteRequest` -- `source_zone`
        is a plain `ZoneId` field with no per-zone conditional."""
        field_names = {f for f in WriteRequest.__dataclass_fields__}
        assert not any("allow" in f.lower() for f in field_names)


# ---------------------------------------------------------------------------
# Must-not-deviate 5 / HLD Threat S-2: source_type binds to caller_identity
# + retrieval_context_hash; user_stated requires the host-side marker.
# ---------------------------------------------------------------------------


class TestHldThreatS2Binding:
    def test_requires_user_turn_marker_is_true_only_for_user_stated(self) -> None:
        for source_type in SourceType:
            expected = source_type is SourceType.USER_STATED
            assert requires_user_turn_marker(source_type) is expected

    def test_user_stated_with_marker_is_accepted_and_binds_identity_and_hash(
        self, gate: ProvenanceWriteGate, journal: RecordingJournal
    ) -> None:
        persist = PersistTracker()
        request = make_request(
            source_type_raw="user_stated",
            user_turn_marker=True,
            caller_identity="host-session-42",
            retrieval_context_hash=VALID_HASH,
            user_turn_attestation=make_valid_user_turn_attestation(
                caller_identity="host-session-42", retrieval_context_hash=VALID_HASH
            ),
        )
        result = gate.submit_write(request, persist.persist)

        assert isinstance(result, WriteAccepted)
        entry = journal.entries[0]
        assert entry.source_type is SourceType.USER_STATED
        assert entry.user_turn_marker is True
        assert entry.caller_identity == "host-session-42"
        assert entry.retrieval_context_hash == VALID_HASH

    def test_user_stated_marker_true_with_no_attestation_is_rejected_as_forged(
        self, gate: ProvenanceWriteGate, journal: RecordingJournal
    ) -> None:
        """CRITICAL finding this remediation closes: any caller of
        `submit_write` could previously set source_type_raw='user_stated'
        and user_turn_marker=True together and obtain a forged
        maximum-confidence (1.0) 'user_stated' provenance attribution --
        the bool alone carried no independent host-side attestation."""
        persist = PersistTracker()
        request = make_request(
            source_type_raw="user_stated",
            user_turn_marker=True,
            caller_identity="host-session-42",
            retrieval_context_hash=VALID_HASH,
        )
        result = gate.submit_write(request, persist.persist)

        assert isinstance(result, WriteRejected)
        assert result.http_status == 422
        assert result.error_code == ERROR_FORGED_USER_TURN_MARKER
        assert journal.entries == []
        assert persist.calls == 0

    def test_user_stated_marker_true_with_tampered_signature_is_rejected(
        self, gate: ProvenanceWriteGate, journal: RecordingJournal
    ) -> None:
        persist = PersistTracker()
        genuine = make_valid_user_turn_attestation()
        tampered = UserTurnAttestation(
            issued_at=genuine.issued_at, signature="f" * len(genuine.signature)
        )
        request = make_request(
            source_type_raw="user_stated",
            user_turn_marker=True,
            user_turn_attestation=tampered,
        )
        result = gate.submit_write(request, persist.persist)

        assert isinstance(result, WriteRejected)
        assert result.error_code == ERROR_FORGED_USER_TURN_MARKER
        assert journal.entries == []
        assert persist.calls == 0

    def test_user_stated_marker_true_with_attestation_for_a_different_write_is_rejected(
        self, gate: ProvenanceWriteGate, journal: RecordingJournal
    ) -> None:
        """A genuine attestation issued for one write cannot be replayed
        against a different item_id -- the signed payload binds the
        exact write it was issued for."""
        persist = PersistTracker()
        attestation_for_other_item = make_valid_user_turn_attestation(
            item_id="item-OTHER"
        )
        request = make_request(
            item_id="item-1",
            source_type_raw="user_stated",
            user_turn_marker=True,
            user_turn_attestation=attestation_for_other_item,
        )
        result = gate.submit_write(request, persist.persist)

        assert isinstance(result, WriteRejected)
        assert result.error_code == ERROR_FORGED_USER_TURN_MARKER
        assert journal.entries == []

    def test_user_stated_marker_true_with_expired_attestation_is_rejected(
        self, gate: ProvenanceWriteGate, journal: RecordingJournal
    ) -> None:
        persist = PersistTracker()
        stale_issued_at = datetime(2020, 1, 1, tzinfo=UTC)
        expired = make_valid_user_turn_attestation(issued_at=stale_issued_at)
        request = make_request(
            source_type_raw="user_stated",
            user_turn_marker=True,
            user_turn_attestation=expired,
        )
        result = gate.submit_write(request, persist.persist)

        assert isinstance(result, WriteRejected)
        assert result.error_code == ERROR_FORGED_USER_TURN_MARKER
        assert journal.entries == []

    def test_bare_caller_asserted_source_type_without_identity_never_accepted(
        self, gate: ProvenanceWriteGate, journal: RecordingJournal
    ) -> None:
        """S-2: "never accept a bare caller-asserted source_type" -- a
        resolvable source_type with no caller_identity binding is still
        rejected, proving resolution alone is insufficient."""
        persist = PersistTracker()
        request = make_request(source_type_raw="tool_output", caller_identity="  ")
        result = gate.submit_write(request, persist.persist)

        assert isinstance(result, WriteRejected)
        assert result.error_code == ERROR_MISSING_CALLER_BINDING
        assert journal.entries == []
        assert persist.calls == 0

    def test_non_user_stated_write_with_marker_true_still_journals_marker_value(
        self, gate: ProvenanceWriteGate, journal: RecordingJournal
    ) -> None:
        """The journaled `user_turn_marker` reflects the caller's actual
        value verbatim, regardless of whether S-2 required it for this
        particular source_type."""
        persist = PersistTracker()
        request = make_request(source_type_raw="tool_output", user_turn_marker=True)
        result = gate.submit_write(request, persist.persist)

        assert isinstance(result, WriteAccepted)
        assert journal.entries[0].user_turn_marker is True


# ---------------------------------------------------------------------------
# Domain-layer pure function unit tests: resolve_source_type, is_hex_sha256.
# ---------------------------------------------------------------------------


class TestResolveSourceType:
    def test_none_resolves_to_none(self) -> None:
        assert resolve_source_type(None) is None

    def test_empty_string_resolves_to_none(self) -> None:
        assert resolve_source_type("") is None

    @pytest.mark.parametrize(
        "raw",
        [
            "user_stated",
            "tool_output",
            "llm_inferred",
            "summarized_from_n",
            "imported",
            "system_derived",
        ],
    )
    def test_every_fixed_value_resolves_to_its_enum_member(self, raw: str) -> None:
        assert resolve_source_type(raw) is SourceType(raw)

    def test_case_sensitive_mismatch_resolves_to_none(self) -> None:
        assert resolve_source_type("USER_STATED") is None

    def test_non_string_values_resolve_to_none_without_raising(self) -> None:
        """MEDIUM finding: resolve_source_type had no type guard -- a
        non-string source_type_raw raised an unhandled exception instead
        of WriteRejected(422), violating AC-010."""
        assert resolve_source_type(123) is None
        assert resolve_source_type(1.5) is None
        assert resolve_source_type(["user_stated"]) is None
        assert resolve_source_type({"source_type": "user_stated"}) is None
        assert resolve_source_type(True) is None
        # SourceType is itself a `str` subclass, so a genuine enum member
        # is not a forgery case -- it still resolves correctly.
        assert resolve_source_type(SourceType.USER_STATED) is SourceType.USER_STATED


class TestIsHexSha256:
    def test_valid_64_char_lowercase_hex_is_true(self) -> None:
        assert is_hex_sha256("a" * 64) is True

    def test_short_string_is_false(self) -> None:
        assert is_hex_sha256("a" * 63) is False

    def test_long_string_is_false(self) -> None:
        assert is_hex_sha256("a" * 65) is False

    def test_uppercase_hex_is_false(self) -> None:
        assert is_hex_sha256("A" * 64) is False

    def test_non_hex_characters_is_false(self) -> None:
        assert is_hex_sha256("z" * 64) is False


# ---------------------------------------------------------------------------
# WriteRequest construction guards (independent of gate resolution).
# ---------------------------------------------------------------------------


class TestWriteRequestConstructionGuards:
    def test_blank_tenant_id_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="tenant_id"):
            make_request(tenant_id="")

    def test_blank_item_id_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="item_id"):
            make_request(item_id="   ")

    def test_blank_idempotency_key_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="idempotency_key"):
            make_request(idempotency_key="   ")


# ---------------------------------------------------------------------------
# MEDIUM finding: no replay/idempotency protection -- write_id was minted
# server-side with no caller nonce/freshness check, so a captured valid
# WriteRequest replayed verbatim was accepted identically every time.
# ---------------------------------------------------------------------------


class TestReplayIdempotencyProtection:
    def test_verbatim_replay_returns_original_result_without_recalling_persist(
        self, gate: ProvenanceWriteGate, journal: RecordingJournal
    ) -> None:
        persist = PersistTracker()
        request = make_request(idempotency_key="replay-nonce-1")

        first = gate.submit_write(request, persist.persist)
        second = gate.submit_write(request, persist.persist)

        assert isinstance(first, WriteAccepted)
        assert isinstance(second, WriteAccepted)
        assert first.write_id == second.write_id
        assert persist.calls == 1
        assert len(journal.entries) == 1

    def test_replay_with_mutated_payload_still_returns_cached_result(
        self, gate: ProvenanceWriteGate, journal: RecordingJournal
    ) -> None:
        """Standard idempotency-key semantics: the key alone identifies
        the logical operation, so a second submission under the same key
        is treated as the same request even if a field differs -- it is
        never re-validated or re-persisted a second time."""
        persist = PersistTracker()
        first_request = make_request(
            idempotency_key="replay-nonce-2", item_id="item-original"
        )
        replayed_with_different_item = make_request(
            idempotency_key="replay-nonce-2", item_id="item-mutated"
        )

        first = gate.submit_write(first_request, persist.persist)
        second = gate.submit_write(replayed_with_different_item, persist.persist)

        assert isinstance(first, WriteAccepted)
        assert isinstance(second, WriteAccepted)
        assert first.write_id == second.write_id
        assert persist.calls == 1
        assert len(journal.entries) == 1

    def test_different_idempotency_keys_are_independent_writes(
        self, gate: ProvenanceWriteGate, journal: RecordingJournal
    ) -> None:
        persist = PersistTracker()
        first = gate.submit_write(
            make_request(idempotency_key="key-a"), persist.persist
        )
        second = gate.submit_write(
            make_request(idempotency_key="key-b"), persist.persist
        )

        assert isinstance(first, WriteAccepted)
        assert isinstance(second, WriteAccepted)
        assert first.write_id != second.write_id
        assert persist.calls == 2
        assert len(journal.entries) == 2


# ---------------------------------------------------------------------------
# Architecture-fitness invariant (HLD Section 3.0, invariant 1): no
# domain/** module may import infrastructure/**.
# ---------------------------------------------------------------------------


class TestArchitectureFitnessNoDomainImportsInfrastructure:
    @pytest.mark.parametrize(
        "module_path",
        ["src/dashanan/domain/write_gate.py"],
    )
    def test_domain_module_imports_no_infrastructure_module(
        self, module_path: str
    ) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        source = (repo_root / module_path).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=module_path)

        imported_modules: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.append(node.module)

        assert not any(
            "infrastructure" in name for name in imported_modules
        ), f"{module_path} imports infrastructure/**: {imported_modules}"

    def test_application_gate_module_imports_no_infrastructure_module(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        module_path = "src/dashanan/application/provenance_write_gate.py"
        source = (repo_root / module_path).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=module_path)

        imported_modules: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.append(node.module)

        assert not any(
            "infrastructure" in name for name in imported_modules
        ), f"{module_path} imports infrastructure/**: {imported_modules}"
