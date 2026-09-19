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
import threading
from collections.abc import Callable, Mapping
from uuid import uuid4

from dashanan.domain.ports import Clock
from dashanan.domain.write_gate import (
    DEFAULT_USER_TURN_SIGNING_KEY_ID,
    ProvenanceJournalEntry,
    ProvenanceJournalPort,
    UserTurnAttestation,
    WriteAccepted,
    WriteGateResult,
    WriteRejected,
    WriteRequest,
    is_hex_sha256,
    requires_user_turn_marker,
    resolve_source_type,
    verify_user_turn_attestation,
)
from dashanan.domain.write_rate_limiter import (
    TokenBucketConfig,
    TokenBucketState,
    new_full_bucket,
    try_consume,
)

logger = logging.getLogger(__name__)

ERROR_UNRESOLVABLE_SOURCE_TYPE = "unresolvable_source_type"
ERROR_MISSING_USER_TURN_MARKER = "missing_user_turn_marker"
ERROR_FORGED_USER_TURN_MARKER = "forged_user_turn_marker"
ERROR_MISSING_CALLER_BINDING = "missing_caller_binding"
ERROR_IDEMPOTENCY_KEY_REUSED_FOR_DIFFERENT_REQUEST = (
    "idempotency_key_reused_for_different_request"
)
ERROR_RATE_LIMIT_EXCEEDED = "rate_limit_exceeded"
_REJECT_HTTP_STATUS = 422
_RATE_LIMIT_HTTP_STATUS = 429
_MIN_SIGNING_KEY_BYTES = 32
"""HLD Threat S-2's HMAC-SHA256 signing key minimum length (DSHN-60 LOW
remediation): RFC 2104 recommends an HMAC key at least as long as the
underlying hash's digest (32 bytes for SHA-256); a shorter key narrows the
effective keyspace an attacker attempting to brute-force or otherwise
recover `user_turn_signing_key` must search."""

DEFAULT_WRITE_RATE_LIMIT_CONFIG = TokenBucketConfig(
    capacity=1000.0, refill_per_second=100.0
)
"""HLD Threat D-1's per-tenant write-throughput bound, applied by default.

A generous production-sane starting point -- a 1000-write burst allowance
refilling at 100 writes/second per tenant -- chosen so the mitigation is
real and active out of the box (unlike `append_only_privilege_guard.
verify_append_only_role_is_safe`'s `verify_privileges=False`-by-default,
which a prior review flagged as effectively dead code) without being tight
enough to throttle any legitimate single-tenant workload this codebase's
own test suite exercises. An operator who needs a tighter bound for their
deployment passes their own `TokenBucketConfig` to `ProvenanceWriteGate`.
"""


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
    that omits the marker entirely. Key rotation (DSHN-60 LOW
    remediation): an attestation verifies against whichever configured
    key its own `key_id` names -- the current `user_turn_signing_key` or
    any `previous_user_turn_signing_keys` grace-period entry -- so
    rotating to a new current key does not retroactively invalidate
    attestations already minted under the previous one, for as long as
    that previous key stays listed.

    Replay/idempotency: every `WriteRequest` carries a caller-supplied
    `idempotency_key`. Before minting a fresh `write_id`, `submit_write`
    looks up any prior journal entry for `(tenant_id, idempotency_key)`;
    a verbatim replay of a previously accepted request returns the
    original `WriteAccepted` result instead of journaling a duplicate
    entry and invoking `persist_fact` a second time. A request is only
    ever treated as a "verbatim replay" when every identifying field
    (`item_id`, `source_zone`, the resolved `source_type`, `source_refs`,
    `caller_identity`, `retrieval_context_hash`, `user_turn_marker`) also
    matches the journaled entry -- reusing an already-journaled
    `idempotency_key` for a request that differs in any of those fields
    is rejected (`ERROR_IDEMPOTENCY_KEY_REUSED_FOR_DIFFERENT_REQUEST`,
    HTTP 422) rather than silently returning the earlier write's cached
    `WriteAccepted`, which would otherwise hand the caller an
    unambiguous success signal for a write that was never actually
    applied.

    HLD Threat D-1 hardening: every non-replayed `submit_write` call spends
    one token from a per-`tenant_id` token bucket (`domain.
    write_rate_limiter`) before any further validation runs. A tenant that
    exhausts its bucket is rejected (429, `ERROR_RATE_LIMIT_EXCEEDED`)
    rather than allowed to flood the journal/persistence layer with an
    unbounded write burst; a verbatim replay (the idempotency-cache hit
    above) never spends a token, since it performs no new journal append.

    Concurrency: the idempotency check (`find_by_idempotency_key`) and
    the subsequent journal append + `persist_fact` invocation are one
    atomic critical section per `(tenant_id, idempotency_key)`, guarded
    by a per-key `threading.RLock` this instance owns (see
    `_lock_for_idempotency_key`). Without this lock, two threads racing
    on the same key could both observe a cache-miss on the read before
    either had journaled, so `persist_fact` would run twice for one
    logical idempotency key -- exactly the violation this class's own
    "at most once" guarantee (below) promises never happens. Locks for
    different keys are independent, so concurrent writes under different
    idempotency keys are never serialized against each other.
    """

    def __init__(
        self,
        journal: ProvenanceJournalPort,
        clock: Clock,
        user_turn_signing_key: bytes,
        rate_limit_config: TokenBucketConfig = DEFAULT_WRITE_RATE_LIMIT_CONFIG,
        user_turn_signing_key_id: str = DEFAULT_USER_TURN_SIGNING_KEY_ID,
        previous_user_turn_signing_keys: Mapping[str, bytes] | None = None,
    ) -> None:
        """Compose the gate from its ports.

        Args:
            journal: The ADR-010 durability barrier this gate journals
                every accepted write to before returning.
            clock: Injectable time source for `written_at`/`accepted_at`
                (testing-core: dependency injection over patching
                `datetime.now` directly, matching `MemoryOrchestrator`'s
                identical choice).
            user_turn_signing_key: The host's CURRENT exclusive secret
                used to verify `WriteRequest.user_turn_attestation` (HLD
                Threat S-2). Must be provisioned from a secrets manager
                or environment variable at the composition root
                (application-security-core: never hardcode secrets) and
                must never be exposed to whatever submits
                `WriteRequest`s to this gate -- that separation is what
                makes a genuine attestation unforgeable by an ordinary
                caller.
            rate_limit_config: The per-tenant write-throughput token-bucket
                policy (HLD Threat D-1). Defaults to
                `DEFAULT_WRITE_RATE_LIMIT_CONFIG`; an operator with a
                tighter or looser deployment-specific bound passes their
                own `TokenBucketConfig` here.
            user_turn_signing_key_id: Which key identifier
                `user_turn_signing_key` is (DSHN-60 LOW remediation: key
                rotation). Defaults to `DEFAULT_USER_TURN_SIGNING_KEY_ID`
                for a deployment with a single, unrotated key.
            previous_user_turn_signing_keys: Retired-but-still-accepted
                keys, by `key_id`, for a rotation grace period: an
                attestation signed under one of these still verifies
                (so already-in-flight attestations minted before a
                rotation are not suddenly all rejected), while
                `sign_user_turn_attestation` should no longer be called
                with them for NEW attestations. `None` (the default)
                means no grace period -- only the current key verifies.

        Raises:
            ValueError: If `user_turn_signing_key`, or any key in
                `previous_user_turn_signing_keys`, is shorter than
                `_MIN_SIGNING_KEY_BYTES` (32 bytes) -- a composition root
                that wires in a short or low-entropy key is a
                misconfiguration this constructor fails closed on,
                rather than silently accepting a weak HMAC key.
        """
        self._user_turn_signing_keys: dict[str, bytes] = {}
        for key_id, secret in (
            {user_turn_signing_key_id: user_turn_signing_key}
            | dict(previous_user_turn_signing_keys or {})
        ).items():
            if len(secret) < _MIN_SIGNING_KEY_BYTES:
                raise ValueError(
                    f"ProvenanceWriteGate signing key {key_id!r} must be at "
                    f"least {_MIN_SIGNING_KEY_BYTES} bytes, got {len(secret)}"
                )
            self._user_turn_signing_keys[key_id] = secret
        self._journal = journal
        self._clock = clock
        self._rate_limit_config = rate_limit_config
        self._idempotency_locks: dict[tuple[str, str], threading.RLock] = {}
        self._idempotency_locks_guard = threading.Lock()
        self._rate_limit_buckets: dict[str, TokenBucketState] = {}
        self._rate_limit_buckets_guard = threading.Lock()

    def _resolve_user_turn_signing_key(
        self, attestation: UserTurnAttestation | None
    ) -> bytes | None:
        """Look up the secret matching `attestation.key_id`, or `None` if unresolvable.

        `None` covers both a missing attestation and one whose `key_id`
        names neither the current key nor any configured
        `previous_user_turn_signing_keys` entry -- both cases must reject
        exactly like a forged attestation (HLD Threat S-2), never raise.
        """
        if attestation is None:
            return None
        return self._user_turn_signing_keys.get(attestation.key_id)

    def _consume_rate_limit_token(self, tenant_id: str) -> bool:
        """Atomically refill-and-consume one write's worth of `tenant_id`'s token bucket.

        Guarded by its own lock (distinct from the per-idempotency-key
        `RLock`s): the rate limiter's state is scoped to `tenant_id` alone,
        not `(tenant_id, idempotency_key)`, so it must serialize across
        every concurrent `submit_write` call for a tenant regardless of
        which idempotency key each call carries -- otherwise two threads
        under different keys could both read the same pre-consumption
        balance and both be admitted, silently doubling the effective
        burst allowance the configured `capacity` is meant to cap.
        """
        with self._rate_limit_buckets_guard:
            now = self._clock.now()
            state = self._rate_limit_buckets.get(tenant_id)
            if state is None:
                state = new_full_bucket(self._rate_limit_config, now)
            new_state, allowed = try_consume(state, self._rate_limit_config, now)
            self._rate_limit_buckets[tenant_id] = new_state
            return allowed

    def _lock_for_idempotency_key(
        self, tenant_id: str, idempotency_key: str
    ) -> threading.RLock:
        """Return the one `RLock` serializing `submit_write` for this exact key.

        Lazily creates and caches one `threading.RLock` per `(tenant_id,
        idempotency_key)` pair for this gate instance's lifetime, guarded
        by `_idempotency_locks_guard` against two threads racing to
        create the lock itself for the same key (the classic
        check-then-create race one level up from the one this lock
        exists to close). `RLock` (not `Lock`) so a caller whose own
        `persist_fact` callback re-enters `submit_write` on the same
        thread for the same key -- an unusual but not impossible calling
        pattern -- does not deadlock against itself.
        """
        key = (tenant_id, idempotency_key)
        with self._idempotency_locks_guard:
            lock = self._idempotency_locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._idempotency_locks[key] = lock
            return lock

    def _is_verbatim_replay(
        self, request: WriteRequest, replayed: ProvenanceJournalEntry
    ) -> bool:
        """True only if `request` matches every identifying field `replayed` journaled.

        The sole authority on whether a `find_by_idempotency_key` hit is
        a genuine replay of the exact same logical write (safe to answer
        from cache) versus a caller reusing the same `idempotency_key`
        for a materially different request (an accidental or adversarial
        collision `submit_write` must reject rather than silently
        answering with the first request's result -- see this module's
        `ERROR_IDEMPOTENCY_KEY_REUSED_FOR_DIFFERENT_REQUEST`).
        `tenant_id` and `idempotency_key` are already equal by
        construction (`replayed` came from looking those two up), so
        this compares every other field `ProvenanceJournalEntry` carries
        from the original request.
        """
        return (
            replayed.item_id == request.item_id
            and replayed.source_zone == request.source_zone
            and replayed.source_type == resolve_source_type(request.source_type_raw)
            and replayed.source_refs == request.source_refs
            and replayed.caller_identity == request.caller_identity
            and replayed.retrieval_context_hash == request.retrieval_context_hash
            and replayed.user_turn_marker == request.user_turn_marker
        )

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
            check below, including a non-verbatim reuse of an
            already-journaled `idempotency_key` -- `persist_fact` is
            never called in that case.
        """
        lock = self._lock_for_idempotency_key(
            request.tenant_id, request.idempotency_key
        )
        with lock:
            return self._submit_write_within_lock(request, persist_fact)

    def _submit_write_within_lock(
        self,
        request: WriteRequest,
        persist_fact: Callable[[], None],
    ) -> WriteGateResult:
        """The idempotency-check-through-persist critical section `submit_write` locks.

        Never called directly -- only `submit_write`, which first
        acquires this exact `(tenant_id, idempotency_key)`'s lock, calls
        this. Holding the lock across the whole method (the
        `find_by_idempotency_key` read, every validation check, the
        journal append, and the `persist_fact` call) is what makes the
        class docstring's "Concurrency" guarantee hold: two threads
        racing on the same key can never both observe a cache-miss and
        both proceed to journal and persist.
        """
        replayed = self._journal.find_by_idempotency_key(
            request.tenant_id, request.idempotency_key
        )
        if replayed is not None:
            if not self._is_verbatim_replay(request, replayed):
                return self._reject(
                    request,
                    ERROR_IDEMPOTENCY_KEY_REUSED_FOR_DIFFERENT_REQUEST,
                    f"idempotency_key {request.idempotency_key!r} was already "
                    "journaled for a different write request (item_id, "
                    "source_zone, source_type, source_refs, caller_identity, "
                    "retrieval_context_hash, or user_turn_marker does not match "
                    "the original request this key was first accepted for); "
                    "reusing an idempotency_key for a materially different "
                    "request is rejected rather than silently returning the "
                    "earlier write's cached result",
                )
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

        if not self._consume_rate_limit_token(request.tenant_id):
            return self._reject(
                request,
                ERROR_RATE_LIMIT_EXCEEDED,
                f"tenant {request.tenant_id!r} exceeded its per-tenant write "
                "throughput admission limit (HLD Threat D-1); retry after "
                "the token bucket has refilled",
                http_status=_RATE_LIMIT_HTTP_STATUS,
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
            resolved_secret = self._resolve_user_turn_signing_key(
                request.user_turn_attestation
            )
            if resolved_secret is None or not verify_user_turn_attestation(
                request.user_turn_attestation,
                secret=resolved_secret,
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
        self,
        request: WriteRequest,
        error_code: str,
        reason: str,
        *,
        http_status: int = _REJECT_HTTP_STATUS,
    ) -> WriteRejected:
        """Build the rejection outcome. Never reached after a journal append succeeds.

        Args:
            http_status: Defaults to `422` (every validation/S-2 rejection
                path). The rate-limit path passes `429` explicitly --
                `WriteRejected.http_status` is a field rather than a
                literal precisely so a rejection reason with a different
                status has somewhere to put it (see that field's own
                docstring).
        """
        logger.warning(
            "provenance write-path gate rejected write",
            extra={
                "tenant_id": request.tenant_id,
                "item_id": request.item_id,
                "source_zone": request.source_zone.value,
                "error_code": error_code,
                "http_status": http_status,
            },
        )
        return WriteRejected(
            http_status=http_status, error_code=error_code, reason=reason
        )
