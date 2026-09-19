"""ProvenanceWriteGate: cross-cutting write-path enforcement (DASH-STORY-006, FR-010).

HLD Section 7.2 (write path) + ADR-010 (WAL/outbox durability barrier) +
HLD Threat S-2 (forged provenance).

FR-010 (verbatim): "Every write to any of the 8 zones SHALL be required
to register a corresponding Provenance/Audit entry (FR-007) at write
time; no zone may accept a fact without an attributable source and
confidence value."
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from uuid import uuid4

from dashanan.domain.ports import Clock
from dashanan.domain.write_gate import (
    ProvenanceJournalEntry,
    ProvenanceJournalPort,
    WriteAccepted,
    WriteGateResult,
    WriteRejected,
    WriteRequest,
    is_hex_sha256,
    requires_user_turn_marker,
    resolve_source_type,
    verify_user_turn_attestation,
)

logger = logging.getLogger(__name__)

ERROR_UNRESOLVABLE_SOURCE_TYPE = "unresolvable_source_type"
ERROR_MISSING_USER_TURN_MARKER = "missing_user_turn_marker"
ERROR_FORGED_USER_TURN_MARKER = "forged_user_turn_marker"
ERROR_MISSING_CALLER_BINDING = "missing_caller_binding"
_REJECT_HTTP_STATUS = 422


class ProvenanceWriteGate:
    """The structural write-path gate every zone write must pass through.

    Implements AC-010 ("a write request to any zone omits a resolvable
    source_type ... the write is rejected (HTTP 422) and no fact is
    persisted without a corresponding Provenance/Audit entry, consistent
    with the WAL/outbox durability barrier") and AC-016 ("a zone write
    bypasses provenance recording ... rejected before persistence, same
    enforcement path as AC-010").

    Structural unbypassability (must-not-deviate item 3): `submit_write`
    is the ONLY method this class exposes that can lead to
    `persist_fact` running, and it invokes `persist_fact` at most once,
    only after `request` has passed every check below AND has been
    durably journaled (ADR-010's fsync-before-return barrier). A caller
    that wants AC-010's guarantee cannot get it any other way than
    routing its own zone-content write through this gate as the
    `persist_fact` callback -- there is no accept-without-journaling
    code path and no journal-then-reject code path.

    Cross-zone scope (must-not-deviate item 4): this class places no
    zone allowlist -- `WriteRequest.source_zone` is a plain `ZoneId`, so
    the same gate instance serves every zone a caller routes through it,
    with no per-zone opt-in/opt-out branch.

    HLD Threat S-2 hardening: `WriteRequest.user_turn_marker` is an
    ordinary caller-writable bool -- on its own it proves nothing. A
    `user_stated` write is only accepted when the marker is `True` AND
    `WriteRequest.user_turn_attestation` verifies against
    `user_turn_signing_key`, a secret this gate holds but the write
    path's caller never sees. A caller that sets the marker without a
    genuine, host-issued attestation is rejected exactly like a caller
    that omits the marker entirely.

    Replay/idempotency: every `WriteRequest` carries a caller-supplied
    `idempotency_key`. Before minting a fresh `write_id`, `submit_write`
    looks up any prior journal entry for `(tenant_id, idempotency_key)`;
    a verbatim replay of a previously accepted request returns the
    original `WriteAccepted` result instead of journaling a duplicate
    entry and invoking `persist_fact` a second time.
    """

    def __init__(
        self,
        journal: ProvenanceJournalPort,
        clock: Clock,
        user_turn_signing_key: bytes,
    ) -> None:
        """Compose the gate from its ports.

        Args:
            journal: The ADR-010 durability barrier this gate journals
                every accepted write to before returning.
            clock: Injectable time source for `written_at`/`accepted_at`
                (testing-core: dependency injection over patching
                `datetime.now` directly, matching `MemoryOrchestrator`'s
                identical choice).
            user_turn_signing_key: The host's exclusive secret used to
                verify `WriteRequest.user_turn_attestation` (HLD Threat
                S-2). Must be provisioned from a secrets manager or
                environment variable at the composition root
                (application-security-core: never hardcode secrets) and
                must never be exposed to whatever submits
                `WriteRequest`s to this gate -- that separation is what
                makes a genuine attestation unforgeable by an ordinary
                caller.
        """
        self._journal = journal
        self._clock = clock
        self._user_turn_signing_key = user_turn_signing_key

    def submit_write(
        self,
        request: WriteRequest,
        persist_fact: Callable[[], None],
    ) -> WriteGateResult:
        """Gate `request`; only on acceptance, invoke `persist_fact`.

        Args:
            request: The caller's write-path provenance block.
            persist_fact: The caller's own zone-content write, taking no
                arguments. Called at most once, and only after `request`
                has been durably journaled -- never on any rejection
                path. Any exception `persist_fact` raises propagates to
                the caller of `submit_write` unchanged: the journal
                entry is already durable at that point (ADR-010 makes
                the journal, not the zone write, the durability
                barrier), so a downstream zone-write failure is a
                separate failure mode this gate does not attempt to
                compensate for.

        Returns:
            `WriteAccepted` once `persist_fact` has returned normally,
            or a cached `WriteAccepted` (see `idempotency_key` below)
            for a verbatim replay -- in either case `persist_fact` runs
            at most once for a given `(tenant_id, idempotency_key)`.
            Returns `WriteRejected` (HTTP 422) if `request` fails any
            check below -- `persist_fact` is never called in that case.
        """
        replayed = self._journal.find_by_idempotency_key(
            request.tenant_id, request.idempotency_key
        )
        if replayed is not None:
            logger.info(
                "provenance write-path gate returned cached result for a "
                "replayed write (idempotency_key already journaled)",
                extra={
                    "tenant_id": request.tenant_id,
                    "item_id": request.item_id,
                    "idempotency_key": request.idempotency_key,
                    "write_id": replayed.write_id,
                },
            )
            return WriteAccepted(
                write_id=replayed.write_id, accepted_at=replayed.written_at
            )

        source_type = resolve_source_type(request.source_type_raw)
        if source_type is None:
            return self._reject(
                request,
                ERROR_UNRESOLVABLE_SOURCE_TYPE,
                f"source_type {request.source_type_raw!r} does not resolve to "
                "one of the six fixed HLD Section 3.8 source_type values",
            )

        if requires_user_turn_marker(source_type):
            if not request.user_turn_marker:
                return self._reject(
                    request,
                    ERROR_MISSING_USER_TURN_MARKER,
                    "source_type 'user_stated' requires an explicit host-side "
                    "user-turn marker (HLD Threat S-2)",
                )
            if not verify_user_turn_attestation(
                request.user_turn_attestation,
                secret=self._user_turn_signing_key,
                tenant_id=request.tenant_id,
                item_id=request.item_id,
                caller_identity=request.caller_identity,
                retrieval_context_hash=request.retrieval_context_hash,
                now=self._clock.now(),
            ):
                return self._reject(
                    request,
                    ERROR_FORGED_USER_TURN_MARKER,
                    "source_type 'user_stated' with user_turn_marker=True "
                    "requires a valid host-issued UserTurnAttestation; a "
                    "caller-asserted marker with no genuine attestation is "
                    "never accepted (HLD Threat S-2)",
                )

        if not request.caller_identity.strip() or not is_hex_sha256(
            request.retrieval_context_hash
        ):
            return self._reject(
                request,
                ERROR_MISSING_CALLER_BINDING,
                "source_type must bind to a non-blank caller_identity and a "
                "64-character hex retrieval_context_hash (HLD Threat S-2)",
            )

        write_id = str(uuid4())
        written_at = self._clock.now()
        entry = ProvenanceJournalEntry(
            tenant_id=request.tenant_id,
            write_id=write_id,
            item_id=request.item_id,
            source_zone=request.source_zone,
            source_type=source_type,
            source_refs=request.source_refs,
            caller_identity=request.caller_identity,
            retrieval_context_hash=request.retrieval_context_hash,
            idempotency_key=request.idempotency_key,
            user_turn_marker=request.user_turn_marker,
            written_at=written_at,
        )
        # ADR-010 durability barrier: must not return before this append
        # is durable -- see ProvenanceJournalPort.append's own contract.
        self._journal.append(entry)

        logger.info(
            "provenance write-path gate accepted write",
            extra={
                "tenant_id": request.tenant_id,
                "item_id": request.item_id,
                "source_zone": request.source_zone.value,
                "source_type": source_type.value,
                "write_id": write_id,
            },
        )
        persist_fact()

        return WriteAccepted(write_id=write_id, accepted_at=written_at)

    def _reject(
        self, request: WriteRequest, error_code: str, reason: str
    ) -> WriteRejected:
        """Build the 422 outcome. Never reached after a journal append succeeds."""
        logger.warning(
            "provenance write-path gate rejected write",
            extra={
                "tenant_id": request.tenant_id,
                "item_id": request.item_id,
                "source_zone": request.source_zone.value,
                "error_code": error_code,
            },
        )
        return WriteRejected(
            http_status=_REJECT_HTTP_STATUS, error_code=error_code, reason=reason
        )
