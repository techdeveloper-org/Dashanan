"""Tests for the DSHN-60 LOW remediations on `ProvenanceWriteGate`'s HMAC signing keys.

Covers:
  1. Minimum signing-key length validation (RFC 2104: an HMAC-SHA256 key
     should be at least 32 bytes) -- both the current key and any
     `previous_user_turn_signing_keys` grace-period key.
  2. Key-rotation support: `UserTurnAttestation.key_id` binds a signature
     to the specific key that produced it, and `ProvenanceWriteGate`
     accepts attestations signed under either its current key or a
     configured previous (grace-period) key, while rejecting one signed
     under a key it does not know about at all.

Runtime assumptions recorded per rule 33/40 test-roadmap conventions:
  - clock: FakeClock, fixed at 2026-01-01T00:00:00+00:00.
  - tenant_id: "tenant-1" for all requests.

PII NOTE: only pseudonymized item_id/caller_identity values and a
placeholder retrieval_context_hash appear below -- no fact/payload content.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from dashanan.application.provenance_write_gate import (
    ERROR_FORGED_USER_TURN_MARKER,
    ProvenanceWriteGate,
)
from dashanan.domain.write_gate import (
    ProvenanceJournalEntry,
    UserTurnAttestation,
    WriteAccepted,
    WriteRejected,
    WriteRequest,
    sign_user_turn_attestation,
)
from dashanan.domain.zone import ZoneId

_PLACEHOLDER_CONTEXT_HASH = hashlib.sha256(b"<PII_EXAMPLE_REDACTED>").hexdigest()
_FIXED_TS = datetime(2026, 1, 1, tzinfo=UTC)
_CURRENT_KEY = b"rotation-suite-current-user-turn-signing-key-32b"
_PREVIOUS_KEY = b"rotation-suite-previous-user-turn-signing-key-32"
_UNKNOWN_KEY = b"rotation-suite-key-the-gate-never-configures-at-all"


class FakeClock:
    def __init__(self, fixed: datetime) -> None:
        self._fixed = fixed

    def now(self) -> datetime:
        return self._fixed


class RecordingJournal:
    def __init__(self) -> None:
        self.entries: list[ProvenanceJournalEntry] = []

    def append(self, entry: ProvenanceJournalEntry) -> None:
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


def _request(*, user_turn_attestation: UserTurnAttestation | None) -> WriteRequest:
    return WriteRequest(
        tenant_id="tenant-1",
        item_id="item-1",
        source_zone=ZoneId.EPISODIC,
        source_type_raw="user_stated",
        caller_identity="dashanan-orchestrator",
        retrieval_context_hash=_PLACEHOLDER_CONTEXT_HASH,
        idempotency_key=str(uuid4()),
        user_turn_marker=True,
        user_turn_attestation=user_turn_attestation,
    )


class TestSigningKeyMinimumLength:
    def test_a_current_key_shorter_than_32_bytes_is_rejected_at_construction(
        self,
    ) -> None:
        with pytest.raises(ValueError, match="32 bytes"):
            ProvenanceWriteGate(
                journal=RecordingJournal(),
                clock=FakeClock(_FIXED_TS),
                user_turn_signing_key=b"too-short",
            )

    def test_a_previous_key_shorter_than_32_bytes_is_rejected_at_construction(
        self,
    ) -> None:
        with pytest.raises(ValueError, match="32 bytes"):
            ProvenanceWriteGate(
                journal=RecordingJournal(),
                clock=FakeClock(_FIXED_TS),
                user_turn_signing_key=_CURRENT_KEY,
                previous_user_turn_signing_keys={"old": b"also-too-short"},
            )

    def test_a_32_byte_key_is_accepted(self) -> None:
        gate = ProvenanceWriteGate(
            journal=RecordingJournal(),
            clock=FakeClock(_FIXED_TS),
            user_turn_signing_key=b"x" * 32,
        )
        assert gate is not None


class TestSigningKeyRotation:
    def _gate(self) -> ProvenanceWriteGate:
        return ProvenanceWriteGate(
            journal=RecordingJournal(),
            clock=FakeClock(_FIXED_TS),
            user_turn_signing_key=_CURRENT_KEY,
            user_turn_signing_key_id="key-2026-02",
            previous_user_turn_signing_keys={"key-2026-01": _PREVIOUS_KEY},
        )

    def test_attestation_signed_under_the_current_key_is_accepted(self) -> None:
        gate = self._gate()
        attestation = sign_user_turn_attestation(
            secret=_CURRENT_KEY,
            key_id="key-2026-02",
            tenant_id="tenant-1",
            item_id="item-1",
            caller_identity="dashanan-orchestrator",
            retrieval_context_hash=_PLACEHOLDER_CONTEXT_HASH,
            issued_at=_FIXED_TS,
        )
        result = gate.submit_write(
            _request(user_turn_attestation=attestation), lambda: None
        )
        assert isinstance(result, WriteAccepted)

    def test_attestation_signed_under_a_configured_previous_key_is_still_accepted(
        self,
    ) -> None:
        """The grace-period guarantee: rotating the CURRENT key must not
        retroactively invalidate an attestation minted under the previous
        one, as long as that previous key is still configured."""
        gate = self._gate()
        attestation = sign_user_turn_attestation(
            secret=_PREVIOUS_KEY,
            key_id="key-2026-01",
            tenant_id="tenant-1",
            item_id="item-1",
            caller_identity="dashanan-orchestrator",
            retrieval_context_hash=_PLACEHOLDER_CONTEXT_HASH,
            issued_at=_FIXED_TS,
        )
        result = gate.submit_write(
            _request(user_turn_attestation=attestation), lambda: None
        )
        assert isinstance(result, WriteAccepted)

    def test_attestation_signed_under_an_unconfigured_key_id_is_rejected(self) -> None:
        gate = self._gate()
        attestation = sign_user_turn_attestation(
            secret=_UNKNOWN_KEY,
            key_id="key-attacker-controlled",
            tenant_id="tenant-1",
            item_id="item-1",
            caller_identity="dashanan-orchestrator",
            retrieval_context_hash=_PLACEHOLDER_CONTEXT_HASH,
            issued_at=_FIXED_TS,
        )
        result = gate.submit_write(
            _request(user_turn_attestation=attestation), lambda: None
        )
        assert isinstance(result, WriteRejected)
        assert result.error_code == ERROR_FORGED_USER_TURN_MARKER

    def test_relabeling_a_genuine_signature_under_a_different_known_key_id_is_rejected(
        self,
    ) -> None:
        """A genuine signature produced under the previous key, but with its
        `key_id` swapped to claim the current key instead, must not verify
        -- `key_id` is itself bound into the signed payload."""
        gate = self._gate()
        genuine = sign_user_turn_attestation(
            secret=_PREVIOUS_KEY,
            key_id="key-2026-01",
            tenant_id="tenant-1",
            item_id="item-1",
            caller_identity="dashanan-orchestrator",
            retrieval_context_hash=_PLACEHOLDER_CONTEXT_HASH,
            issued_at=_FIXED_TS,
        )
        relabeled = UserTurnAttestation(
            issued_at=genuine.issued_at,
            signature=genuine.signature,
            key_id="key-2026-02",
        )
        result = gate.submit_write(
            _request(user_turn_attestation=relabeled), lambda: None
        )
        assert isinstance(result, WriteRejected)
        assert result.error_code == ERROR_FORGED_USER_TURN_MARKER

    def test_attestation_with_no_key_id_uses_the_default_id(self) -> None:
        """A caller that never passes `key_id` (pre-rotation-support code)
        keeps working unchanged: both `sign_user_turn_attestation` and
        `ProvenanceWriteGate` default to the same `DEFAULT_USER_TURN_
        SIGNING_KEY_ID`."""
        gate = ProvenanceWriteGate(
            journal=RecordingJournal(),
            clock=FakeClock(_FIXED_TS),
            user_turn_signing_key=_CURRENT_KEY,
        )
        attestation = sign_user_turn_attestation(
            secret=_CURRENT_KEY,
            tenant_id="tenant-1",
            item_id="item-1",
            caller_identity="dashanan-orchestrator",
            retrieval_context_hash=_PLACEHOLDER_CONTEXT_HASH,
            issued_at=_FIXED_TS,
        )
        result = gate.submit_write(
            _request(user_turn_attestation=attestation), lambda: None
        )
        assert isinstance(result, WriteAccepted)
