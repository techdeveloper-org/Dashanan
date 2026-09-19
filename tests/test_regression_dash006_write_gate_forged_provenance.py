"""Regression test for the DASH-STORY-006 P1 security review findings.

QA remediation-verification sub-task. This suite reproduces the exact exploits the
review demonstrated against `ProvenanceWriteGate.submit_write` / `resolve_source_type`
(`src/dashanan/application/provenance_write_gate.py`,
`src/dashanan/domain/write_gate.py`) and proves each one is now blocked by the Fix
sub-task's remediation, independently of the existing `tests/test_smoke_write_gate.py`
and `tests/test_qa_write_gate_cross_cutting_zone_agnostic.py` suites (this file does not
import from either -- it re-derives the exploit from the review's own finding text).

Exploit 1 reproduced, verbatim from the CRITICAL finding (HLD Threat S-2):
  "WriteRequest.user_turn_marker is an ordinary caller-writable bool with no
  independent host-side attestation, allowing any caller to forge
  source_type_raw='user_stated' + user_turn_marker=True for a max-confidence (1.0)
  'user_stated' provenance attribution." This test plays the attacker: it builds a
  WriteRequest exactly as the finding describes (source_type_raw='user_stated',
  user_turn_marker=True, and -- critically -- NO attestation, since a caller who does
  not hold the host's signing key cannot produce one) and asserts the gate now rejects
  it (422, before persist_fact runs, before anything is journaled), where before the
  fix it would have been accepted and journaled with SourceType.USER_STATED (whose
  compute_confidence base is 1.0 per HLD Section 3.8).

Exploit 2 reproduced, verbatim from the MEDIUM finding (AC-010):
  "resolve_source_type() has no type guard on source_type_raw -- a non-string value
  raises an unhandled exception instead of WriteRejected(422)." This test submits a
  WriteRequest whose source_type_raw has been overwritten with a non-string value (an
  int, mirroring a malformed/adversarial caller bypassing the str type hint that is not
  runtime-enforced) and asserts the gate returns a typed WriteRejected(422) rather than
  letting an unhandled exception escape submit_write.

Exploit 3 reproduced, verbatim from the MEDIUM finding (replay/idempotency):
  "no replay/idempotency protection -- write_id is minted server-side with no caller
  nonce/freshness check, so a captured valid WriteRequest replayed verbatim is accepted
  identically every time." This test captures one valid, accepted WriteRequest and
  resubmits the exact same object a second (and third) time, asserting persist_fact is
  invoked only once and the journal holds only one entry for it -- the effect a captured
  request replayed N times must have on the underlying zone-content store.

All three exploits are attempted through the SAME public entry point a real caller of
this gate would use (`ProvenanceWriteGate.submit_write`), against fake-but-behaviorally-
faithful `ProvenanceJournalPort`/`Clock` doubles -- no internals are reached into or
monkeypatched, matching this repo's existing regression-test convention (see
`test_regression_dash055_p1_append_only_privilege_bypass.py`).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from dashanan.application.provenance_write_gate import (
    ERROR_FORGED_USER_TURN_MARKER,
    ERROR_UNRESOLVABLE_SOURCE_TYPE,
    ProvenanceWriteGate,
)
from dashanan.domain.provenance_record import SourceType, compute_confidence
from dashanan.domain.write_gate import (
    ProvenanceJournalEntry,
    WriteAccepted,
    WriteRejected,
    WriteRequest,
)
from dashanan.domain.zone import ZoneId

_ATTACKER_CONTEXT_HASH = hashlib.sha256(b"attacker-controlled-context").hexdigest()
_FIXED_TS = datetime(2026, 1, 1, tzinfo=UTC)
_SIGNING_KEY_THE_ATTACKER_NEVER_HOLDS = b"host-only-user-turn-signing-key-for-this-suite"


class _FixedClock:
    """Deterministic Clock double -- no wall-clock dependency in this suite."""

    def now(self) -> datetime:
        return _FIXED_TS


class _RecordingJournalDouble:
    """ProvenanceJournalPort double faithful enough to prove the exploits are blocked.

    Records every `append` call and answers `find_by_idempotency_key` from that
    history, exactly like the real `SqlWriteJournalRepository` -- this is what lets
    Exploit 3 (replay) be proven through the gate's real replay-detection code path
    rather than by asserting on a mock's call count alone.
    """

    def __init__(self) -> None:
        self.entries: list[ProvenanceJournalEntry] = []

    def append(self, entry: ProvenanceJournalEntry) -> None:
        self.entries.append(entry)

    def find_by_idempotency_key(
        self, tenant_id: str, idempotency_key: str
    ) -> ProvenanceJournalEntry | None:
        for entry in self.entries:
            if entry.tenant_id == tenant_id and entry.idempotency_key == idempotency_key:
                return entry
        return None


def _attacker_write_request(**overrides: object) -> WriteRequest:
    """Build the baseline WriteRequest an ordinary (non-host) caller can submit."""
    defaults: dict[str, object] = {
        "tenant_id": "victim-tenant",
        "item_id": "victim-item-1",
        "source_zone": ZoneId.EPISODIC,
        "source_type_raw": "tool_output",
        "caller_identity": "ordinary-caller",
        "retrieval_context_hash": _ATTACKER_CONTEXT_HASH,
        "idempotency_key": str(uuid4()),
        "user_turn_marker": False,
        "user_turn_attestation": None,
    }
    defaults.update(overrides)
    return WriteRequest(**defaults)  # type: ignore[arg-type]


class TestExploit1ForgedUserStatedMaxConfidenceAttribution:
    """CRITICAL: forging source_type_raw='user_stated' + user_turn_marker=True with
    no host-issued attestation must never yield an accepted, journaled write."""

    def test_forged_marker_with_no_attestation_is_rejected_not_accepted(self) -> None:
        journal = _RecordingJournalDouble()
        gate = ProvenanceWriteGate(
            journal=journal,
            clock=_FixedClock(),
            user_turn_signing_key=_SIGNING_KEY_THE_ATTACKER_NEVER_HOLDS,
        )
        zone_content_was_persisted: list[bool] = []

        # The attacker holds no signing key and supplies no attestation at all --
        # exactly the forgery the CRITICAL finding describes: only the two
        # caller-writable fields are set, nothing independently verifiable.
        forged_request = _attacker_write_request(
            source_type_raw="user_stated",
            user_turn_marker=True,
        )

        result = gate.submit_write(
            forged_request, lambda: zone_content_was_persisted.append(True)
        )

        assert isinstance(result, WriteRejected), (
            "the forged user_stated + user_turn_marker=True write must be rejected, "
            f"got {result!r} instead"
        )
        assert result.http_status == 422
        assert result.error_code == ERROR_FORGED_USER_TURN_MARKER
        assert journal.entries == [], (
            "no provenance journal entry may exist for a forged write -- the "
            "ADR-010 durability barrier must never be crossed for this request"
        )
        assert zone_content_was_persisted == [], (
            "the caller's own zone-content write (persist_fact) must never run "
            "for a rejected, forged write"
        )

    def test_had_the_forgery_succeeded_it_would_have_bought_max_confidence(self) -> None:
        """Documents WHY this forgery is worth blocking: `user_stated` is the one
        source_type whose HLD Section 3.8 base confidence is 1.0 -- the maximum --
        strictly higher than every other resolvable source_type. This is the exact
        incentive the CRITICAL finding says a forger was pursuing."""
        forged_confidence = compute_confidence(SourceType.USER_STATED)
        honest_tool_output_confidence = compute_confidence(SourceType.TOOL_OUTPUT)

        assert forged_confidence == 1.0
        assert forged_confidence > honest_tool_output_confidence

    def test_forged_marker_is_rejected_across_every_sprint_1_zone(self) -> None:
        """The forgery must be closed cross-zone (no per-zone opt-out) -- mirrors
        the gate's own must-not-deviate cross-zone-scope guarantee."""
        for zone in (
            ZoneId.WORKING,
            ZoneId.EPISODIC,
            ZoneId.RETRIEVAL_INDEX,
            ZoneId.PROVENANCE,
        ):
            journal = _RecordingJournalDouble()
            gate = ProvenanceWriteGate(
                journal=journal,
                clock=_FixedClock(),
                user_turn_signing_key=_SIGNING_KEY_THE_ATTACKER_NEVER_HOLDS,
            )

            result = gate.submit_write(
                _attacker_write_request(
                    source_zone=zone,
                    source_type_raw="user_stated",
                    user_turn_marker=True,
                ),
                lambda: None,
            )

            assert isinstance(result, WriteRejected), f"zone={zone!r} was not protected"
            assert result.error_code == ERROR_FORGED_USER_TURN_MARKER


class TestExploit2NonStringSourceTypeCrash:
    """MEDIUM (AC-010): a non-string source_type_raw must resolve to a typed 422,
    never an unhandled exception escaping the write path."""

    @pytest.mark.parametrize(
        "malformed_source_type_raw",
        [123, 1.5, True, ["user_stated"], {"source_type": "user_stated"}, (1, 2)],
    )
    def test_non_string_source_type_never_crashes_and_is_rejected_with_422(
        self, malformed_source_type_raw: object
    ) -> None:
        journal = _RecordingJournalDouble()
        gate = ProvenanceWriteGate(
            journal=journal,
            clock=_FixedClock(),
            user_turn_signing_key=_SIGNING_KEY_THE_ATTACKER_NEVER_HOLDS,
        )
        request = _attacker_write_request()
        # The str type hint on source_type_raw is not runtime-enforced; a frozen
        # dataclass's own __setattr__ is bypassed here the same way a malformed
        # caller (or a deserializer that skips validation) could produce this
        # object in practice.
        object.__setattr__(request, "source_type_raw", malformed_source_type_raw)

        # The exploit-under-test: this call must not raise. Before the fix, a
        # non-string source_type_raw propagated an unhandled AttributeError out of
        # resolve_source_type's `raw.strip()` call.
        result = gate.submit_write(request, lambda: None)

        assert isinstance(result, WriteRejected)
        assert result.http_status == 422
        assert result.error_code == ERROR_UNRESOLVABLE_SOURCE_TYPE
        assert journal.entries == []


class TestExploit3CapturedRequestReplayedVerbatim:
    """MEDIUM: a captured, valid WriteRequest replayed verbatim must not be accepted
    identically every time -- persist_fact must run at most once per logical write."""

    def test_replaying_a_captured_valid_request_does_not_re_trigger_the_side_effect(
        self,
    ) -> None:
        journal = _RecordingJournalDouble()
        gate = ProvenanceWriteGate(
            journal=journal,
            clock=_FixedClock(),
            user_turn_signing_key=_SIGNING_KEY_THE_ATTACKER_NEVER_HOLDS,
        )
        zone_content_writes: list[int] = []

        def persist_fact_side_effect() -> None:
            # Stands in for the caller's real zone-content write -- what an
            # attacker replaying a captured request is trying to trigger again.
            zone_content_writes.append(1)

        captured_request = _attacker_write_request(
            source_type_raw="tool_output",
            idempotency_key="captured-nonce-replayed-by-attacker",
        )

        first_result = gate.submit_write(captured_request, persist_fact_side_effect)
        # The attacker (or a network retry, a replay proxy, a man-in-the-middle
        # capture-and-resend) submits the exact same captured object again, and
        # again.
        second_result = gate.submit_write(captured_request, persist_fact_side_effect)
        third_result = gate.submit_write(captured_request, persist_fact_side_effect)

        assert isinstance(first_result, WriteAccepted)
        assert isinstance(second_result, WriteAccepted)
        assert isinstance(third_result, WriteAccepted)
        assert second_result.write_id == first_result.write_id, (
            "a replay must return the ORIGINAL write_id, never mint a fresh one"
        )
        assert third_result.write_id == first_result.write_id

        assert zone_content_writes == [1], (
            "persist_fact (the caller's zone-content write) must run exactly "
            f"once across 3 submissions of the same captured request, ran "
            f"{len(zone_content_writes)} times instead"
        )
        assert len(journal.entries) == 1, (
            "exactly one durable journal entry may exist for one logical write, "
            f"found {len(journal.entries)}"
        )
