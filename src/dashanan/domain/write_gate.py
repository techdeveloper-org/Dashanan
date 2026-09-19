"""Domain types for the cross-cutting provenance write-path gate.

HLD Section 7.2 (write path) + ADR-010 (WAL/outbox durability barrier) +
HLD Threat S-2 (forged provenance), traced to FR-010 in SRS.md.

FR-010 (verbatim): "Every write to any of the 8 zones SHALL be required
to register a corresponding Provenance/Audit entry (FR-007) at write
time; no zone may accept a fact without an attributable source and
confidence value."

This module holds pure domain logic only: the request/result shapes, the
`ProvenanceJournalEntry` the WAL records, the `ProvenanceJournalPort` an
adapter implements, and the two pure functions (`resolve_source_type`,
`requires_user_turn_marker`) the gate's accept/reject decision is built
from. Orchestration (calling the journal, invoking the caller's own
zone-write callback) lives in `application.provenance_write_gate`,
mirroring the `domain/ports.py` + `application/memory_orchestrator.py`
split DASH-STORY-001 already established.

Sprint 1 scope note (SRS.md Section 2/5): FR-010 applies to the four
Sprint-1 zones (Working, Episodic, Retrieval-Index, Provenance) in
practice, but this module places no zone allowlist on the gate itself --
`source_zone` is a plain `ZoneId`, so extending FR-010 to the remaining
zones in a later sprint needs no change here (Open-Closed).

PII NOTE: every field here is write-path CONTROL METADATA -- a
caller-asserted `source_type` string, a caller identity, a pre-hashed
`retrieval_context_hash` -- never the underlying fact payload a write
carries (dev_prompt's PII constraint). This module never receives or
stores raw retrieval context text, mirroring `ProvenanceRecord`'s
identical `retrieval_context_hash` design (DASH-STORY-005).
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from dashanan.domain.provenance_record import SourceType
from dashanan.domain.zone import ZoneId

_HEX_SHA256_LENGTH = 64
_DEFAULT_USER_TURN_ATTESTATION_MAX_AGE_SECONDS = 300
_FIELD_LENGTH_PREFIX_WIDTH = 10
DEFAULT_USER_TURN_SIGNING_KEY_ID = "default"
"""The implicit `key_id` a caller gets when it does not pass one explicitly
to `sign_user_turn_attestation` -- matches `ProvenanceWriteGate`'s own
default mapping of its single required `user_turn_signing_key` constructor
argument, so every existing caller of either (pre-dating key rotation
support, DSHN-60 LOW remediation) keeps behaving identically."""


def _canonicalize_fields(*fields: str) -> bytes:
    """Encode `fields` into a byte string where no field-boundary forgery is possible.

    DSHN-60 remediation (CRITICAL, independently confirmed): the prior
    construction, `"|".join(fields).encode()`, let any field containing
    the `"|"` delimiter shift where the canonical string's field
    boundaries fall. A genuine attestation signed for
    `(tenant_id="tenantA", item_id="Y|victim-secret-item", ...)` and a
    forged one for `(tenant_id="tenantA|Y", item_id="victim-secret-item",
    ...)` produced the IDENTICAL canonical string and therefore the
    IDENTICAL HMAC signature -- `verify_user_turn_attestation` could not
    tell the two apart, letting a caller replay a genuine attestation
    against a different (tenant_id, item_id) split with no access to the
    signing key.

    Each field is prefixed with its own UTF-8 byte length, rendered as a
    fixed-width decimal ASCII header (`_FIELD_LENGTH_PREFIX_WIDTH`
    digits) followed by `":"`. A length-prefixed field can never be
    mistaken for a delimiter occurring inside an adjacent field's content
    -- the mapping from `fields` to the returned bytes is injective
    regardless of what characters any individual field contains, so no
    encoding-level restriction on field content is required.
    """
    parts: list[bytes] = []
    for field in fields:
        encoded = field.encode("utf-8")
        parts.append(f"{len(encoded):0{_FIELD_LENGTH_PREFIX_WIDTH}d}:".encode("ascii"))
        parts.append(encoded)
    return b"".join(parts)


def is_hex_sha256(value: str) -> bool:
    """True if `value` is a 64-character lowercase hex SHA-256 digest.

    Public (unlike `provenance_record._require_hex_sha256`) because both
    this module and the application-layer gate need it, and this story
    keeps its own local copy rather than importing a sibling story's
    private helper -- see this story's dev report, judgment-call list.
    """
    return len(value) == _HEX_SHA256_LENGTH and all(
        c in "0123456789abcdef" for c in value
    )


def resolve_source_type(raw: object) -> SourceType | None:
    """Resolve a caller-asserted `source_type` value to the fixed enum.

    This is the sole implementation of AC-010's "omits a resolvable
    source_type" test: a write's `source_type_raw` is resolvable if and
    only if this function returns non-`None` for it.

    Args:
        raw: The caller-asserted `source_type`, exactly as submitted.
            `WriteRequest.source_type_raw` is typed `str`, but a
            dataclass field annotation is not runtime-enforced, so a
            malformed caller can still hand this function a non-`str`
            (int, list, dict, ...); it is treated the same as an
            unresolvable string rather than raising, so AC-010's 422
            outcome holds regardless of the input's actual type.

    Returns:
        The matching `SourceType` member, or `None` when `raw` is not a
        `str`, is `None`, is blank, or does not match one of the six
        fixed values HLD Section 3.8 enumerates.
    """
    if not isinstance(raw, str):
        return None
    if not raw.strip():
        return None
    try:
        return SourceType(raw)
    except ValueError:
        return None


def requires_user_turn_marker(source_type: SourceType) -> bool:
    """HLD Threat S-2: `user_stated` requires an explicit host-side user-turn marker.

    S-2 (verbatim, HLD Section 10): "source_type is asserted by the
    caller but recorded with the caller's identity and the
    retrieval_context hash. user_stated requires an explicit host-side
    user-turn marker." This function isolates the one `source_type`
    value that condition applies to.
    """
    return source_type is SourceType.USER_STATED


@dataclass(frozen=True, slots=True)
class UserTurnAttestation:
    """A host-signed proof that `user_turn_marker=True` is genuine (HLD Threat S-2).

    `WriteRequest.user_turn_marker` by itself is an ordinary
    caller-writable bool: any caller of `ProvenanceWriteGate.submit_write`
    could previously set `source_type_raw='user_stated'` and
    `user_turn_marker=True` together and obtain a forged
    maximum-confidence (1.0) `user_stated` provenance attribution, with
    no independent verification that a real user conversational turn
    ever occurred -- the DB CHECK constraint on
    `provenance_write_journal` only enforces self-consistency between
    two caller-supplied fields, never genuineness.

    This attestation closes that gap: it is an HMAC-SHA256 signature
    over the exact write it is claimed for, producible only by whoever
    holds the host's own signing key (`sign_user_turn_attestation`) --
    a key the write path's caller is never given. `verify_user_turn_
    attestation` is the sole check `ProvenanceWriteGate` trusts before
    accepting `user_turn_marker=True`; a bare marker with no valid
    attestation is rejected (422) exactly like a missing marker.

    DSHN-60 LOW remediation: `key_id` identifies WHICH signing key
    produced `signature`, so a host can rotate `user_turn_signing_key`
    (provision a new key, keep the old one accepted for a grace period,
    then retire it) without invalidating every attestation already
    in-flight under the old key -- `ProvenanceWriteGate`'s
    `previous_user_turn_signing_keys` constructor parameter is exactly
    that grace-period mechanism. `key_id` is itself bound into the
    signed payload (`_user_turn_attestation_message`), so a caller
    cannot forge a different `key_id` onto a genuine signature to make
    verification resolve a different (and possibly weaker or
    attacker-known) secret than the one that actually produced it.

    Attributes:
        issued_at: When the host produced this attestation. Bound into
            the signed payload so a captured attestation cannot be
            replayed indefinitely -- `verify_user_turn_attestation`
            rejects one older than its freshness window.
        signature: Lowercase hex HMAC-SHA256 digest over the canonical
            write-identifying payload (tenant_id, item_id,
            caller_identity, retrieval_context_hash, key_id, issued_at).
        key_id: Which signing key produced `signature`. Defaults to
            `DEFAULT_USER_TURN_SIGNING_KEY_ID` for every caller that
            predates key rotation support and never passes one
            explicitly.
    """

    issued_at: datetime
    signature: str
    key_id: str = DEFAULT_USER_TURN_SIGNING_KEY_ID


def _user_turn_attestation_message(
    *,
    tenant_id: str,
    item_id: str,
    caller_identity: str,
    retrieval_context_hash: str,
    key_id: str,
    issued_at: datetime,
) -> bytes:
    """Build the exact canonical payload `sign_user_turn_attestation` signs.

    Every field identifying which write this attestation is for is
    included, so a valid attestation cannot be replayed against a
    different tenant, item, caller, or retrieval context -- only a
    replay of the identical write within the freshness window is
    possible, the same residual class `WriteRequest.idempotency_key`
    (see below) independently closes. `key_id` is included too (DSHN-60
    LOW remediation) so a caller cannot relabel a genuine signature under
    a different `key_id` to make verification resolve a different secret.
    """
    return _canonicalize_fields(
        tenant_id,
        item_id,
        caller_identity,
        retrieval_context_hash,
        key_id,
        issued_at.isoformat(),
    )


def sign_user_turn_attestation(
    *,
    secret: bytes,
    tenant_id: str,
    item_id: str,
    caller_identity: str,
    retrieval_context_hash: str,
    issued_at: datetime,
    key_id: str = DEFAULT_USER_TURN_SIGNING_KEY_ID,
) -> UserTurnAttestation:
    """Produce a genuine `UserTurnAttestation` -- a host-only capability.

    Only code that holds `secret` (the host's exclusive user-turn
    signing key identified by `key_id`, provisioned from a secrets
    manager or environment variable at the composition root and never
    handed to whatever submits `WriteRequest`s to `ProvenanceWriteGate`)
    can call this and get back an attestation `verify_user_turn_
    attestation` will accept. This is the module's only sanctioned way
    to construct a genuine attestation, mirroring `ProvenanceRecord.
    create` being the sole sanctioned constructor for its own invariant
    (DASH-STORY-005).

    Args:
        key_id: Which signing key `secret` is (DSHN-60 LOW remediation:
            key-rotation support). Defaults to
            `DEFAULT_USER_TURN_SIGNING_KEY_ID` for a deployment with a
            single, unrotated key.
    """
    message = _user_turn_attestation_message(
        tenant_id=tenant_id,
        item_id=item_id,
        caller_identity=caller_identity,
        retrieval_context_hash=retrieval_context_hash,
        key_id=key_id,
        issued_at=issued_at,
    )
    signature = hmac.new(secret, message, hashlib.sha256).hexdigest()
    return UserTurnAttestation(issued_at=issued_at, signature=signature, key_id=key_id)


def verify_user_turn_attestation(
    attestation: UserTurnAttestation | None,
    *,
    secret: bytes,
    tenant_id: str,
    item_id: str,
    caller_identity: str,
    retrieval_context_hash: str,
    now: datetime,
    max_age_seconds: int = _DEFAULT_USER_TURN_ATTESTATION_MAX_AGE_SECONDS,
) -> bool:
    """HLD Threat S-2: the sole check that a `user_turn_marker=True` claim is genuine.

    Returns `False` -- never raises -- for a missing attestation, a
    signature that does not match what `sign_user_turn_attestation`
    would have produced for this exact write, an attestation dated in
    the future, or one older than `max_age_seconds`.
    `hmac.compare_digest` is used for the signature comparison so this
    check runs in constant time regardless of where a mismatch occurs,
    defending against a timing side-channel.

    Args:
        attestation: The caller-supplied attestation, or `None` if the
            caller asserted `user_turn_marker=True` with no attestation
            at all -- the forgery this function exists to catch.
        secret: The signing key matching `attestation.key_id` --
            `ProvenanceWriteGate` resolves which key to pass here (its
            current key, or one of its `previous_user_turn_signing_keys`
            grace-period keys, DSHN-60 LOW remediation) before calling
            this function; this function itself does not do key
            lookup, keeping it a pure, single-key verification check.
        now: The gate's own clock reading (testing-core: injected,
            never `datetime.now()` directly).
        max_age_seconds: How long a genuine attestation remains valid.
    """
    if attestation is None:
        return False
    if attestation.issued_at > now:
        return False
    if now - attestation.issued_at > timedelta(seconds=max_age_seconds):
        return False
    expected = sign_user_turn_attestation(
        secret=secret,
        tenant_id=tenant_id,
        item_id=item_id,
        caller_identity=caller_identity,
        retrieval_context_hash=retrieval_context_hash,
        issued_at=attestation.issued_at,
        key_id=attestation.key_id,
    )
    return hmac.compare_digest(expected.signature, attestation.signature)


@dataclass(frozen=True, slots=True)
class WriteRequest:
    """One caller's write-path provenance block (HLD Section 7.2).

    Mirrors the mandatory FR-010 provenance block HLD Section 7.2's
    `POST /v1/memory/write` carries -- `{ source_type, source_refs[],
    purpose, subject_id? }` -- narrowed to the fields AC-010/AC-016/S-2
    actually gate on. `payload`/fact content is deliberately absent: the
    gate enforces the CONTROL metadata contract, never inspects the fact
    itself (PII note above).

    Attributes:
        tenant_id: The owning tenant (HLD 3.0 invariant 2's own rule,
            extended here even though this gate predates any concrete
            `ZoneRepository` write method).
        item_id: The fact this write is for, in its target zone.
        source_zone: Which zone this write targets. No allowlist is
            enforced here (see module docstring); Sprint 1 wiring is a
            caller/integration concern, not this dataclass's.
        source_type_raw: The caller-asserted source type, exactly as
            submitted -- may be blank, malformed, or a value outside
            the six fixed `SourceType` members. `resolve_source_type`
            is the sole authority on whether it is resolvable.
        caller_identity: Who or what is making this write. Bound into
            the journal entry per HLD Threat S-2 -- never a bare,
            caller-asserted `source_type` with no counter-signing
            identity.
        retrieval_context_hash: SHA-256 hex of the query/task this
            write was made under, pre-hashed by the caller (this
            dataclass never receives raw retrieval context text).
        idempotency_key: A caller-supplied nonce identifying this
            logical write attempt. `ProvenanceWriteGate` looks up any
            prior journal entry for `(tenant_id, idempotency_key)`
            before minting a fresh `write_id`; a verbatim replay of a
            previously accepted `WriteRequest` (same key) returns the
            original `WriteAccepted` result instead of being journaled
            and persisted a second time. Required and non-blank, like
            `tenant_id`/`item_id` -- there is no bare-write path that
            skips this check.
        source_refs: item_ids this write was derived from. Empty for
            `user_stated` (HLD Section 3.8).
        user_turn_marker: The caller's claim that this write is
            directly attributable to an explicit user conversational
            turn -- the S-2 binding `source_type == user_stated`
            requires. This bool alone is an ordinary caller-writable
            field with no independent verification; `user_turn_
            attestation` below is what the gate actually trusts before
            accepting a `True` claim.
        user_turn_attestation: A host-issued `UserTurnAttestation`
            proving `user_turn_marker=True` is genuine (HLD Threat
            S-2). Required whenever `user_turn_marker` is `True` for a
            `user_stated` write -- `ProvenanceWriteGate` rejects
            (422, `ERROR_FORGED_USER_TURN_MARKER`) a `True` marker
            with a missing or invalid attestation, closing the forgery
            path where any caller could previously set
            `source_type_raw='user_stated'` and `user_turn_marker=True`
            together to obtain a maximum-confidence (1.0) attribution
            with no host-side verification.
    """

    tenant_id: str
    item_id: str
    source_zone: ZoneId
    source_type_raw: str
    caller_identity: str
    retrieval_context_hash: str
    idempotency_key: str
    source_refs: tuple[str, ...] = ()
    user_turn_marker: bool = False
    user_turn_attestation: UserTurnAttestation | None = None

    def __post_init__(self) -> None:
        """Reject blank identifiers -- independent of source_type resolvability.

        Raises:
            ValueError: If `tenant_id`, `item_id`, or `idempotency_key`
                is blank. This is a caller-construction error
                (malformed request shape), distinct from the 422
                `WriteRejected` outcome the gate itself returns for an
                unresolvable `source_type` or a failed S-2 binding.
        """
        if not self.tenant_id.strip():
            raise ValueError("WriteRequest.tenant_id must not be blank")
        if not self.item_id.strip():
            raise ValueError("WriteRequest.item_id must not be blank")
        if not self.idempotency_key.strip():
            raise ValueError("WriteRequest.idempotency_key must not be blank")


@dataclass(frozen=True, slots=True)
class WriteAccepted:
    """The write cleared the gate and was durably journaled (ADR-010).

    Attributes:
        write_id: Fresh identifier for this accepted write, echoed back
            to the caller (mirrors HLD Section 7.2's `write_id` on the
            real `202` response this gate's outcome corresponds to).
        accepted_at: When the journal append (the ADR-010 durability
            barrier) completed.
    """

    write_id: str
    accepted_at: datetime


@dataclass(frozen=True, slots=True)
class WriteRejected:
    """The write was rejected BEFORE any persistence (AC-010, AC-016).

    A Result type (HLD Section 6, "Degraded responses" row) rather than
    a raised exception: rejection is a normal, typed outcome of the
    write path, not a caller bug, so every caller must read this value
    instead of assuming acceptance (error-handling-patterns section 8).

    Attributes:
        http_status: Always `422` for this gate's rejection paths
            (unresolvable `source_type`, failed S-2 caller/attestation
            binding) -- kept as a field rather than a literal so a
            future rejection reason with a different status has
            somewhere to put it without changing this shape.
        error_code: A stable, machine-matchable reason code.
        reason: A human-readable explanation, safe to surface to an
            operator (never echoes fact content -- PII note above).
    """

    http_status: int
    error_code: str
    reason: str


WriteGateResult = WriteAccepted | WriteRejected
"""The gate's Result type: exactly one of `WriteAccepted` or `WriteRejected`."""


@dataclass(frozen=True, slots=True)
class ProvenanceJournalEntry:
    """One durable WAL/outbox record (ADR-010).

    What `ProvenanceWriteGate` journals before returning `WriteAccepted`,
    and what a future provenance-relay worker (ADR-010's
    `dashanan-provenance-relay` container; out of this story's scope --
    see the dev report's judgment-call list) drains into Zone 7 as a
    `ProvenanceRecord` (DASH-STORY-005).

    Attributes mirror `WriteRequest` plus the two fields the gate itself
    adds: `write_id` (freshly minted) and the resolved `source_type`
    (never the raw, possibly-unresolvable string -- an entry only exists
    because resolution already succeeded).
    """

    tenant_id: str
    write_id: str
    item_id: str
    source_zone: ZoneId
    source_type: SourceType
    source_refs: tuple[str, ...]
    caller_identity: str
    retrieval_context_hash: str
    idempotency_key: str
    user_turn_marker: bool
    written_at: datetime


@runtime_checkable
class ProvenanceJournalPort(Protocol):
    """The ADR-010 durability barrier: append-only, fsync-before-return.

    Kept local to this domain module rather than added to the shared
    `dashanan.domain.ports` -- AR1-G2 freezes that module's
    `ZoneRepository` specifically, but this story's own
    file-disjointness requirement keeps every new port this story
    introduces out of that shared file regardless of AR1-G2's literal
    scope, mirroring `SqlProvenanceRepository`'s identical local-Protocol
    choice for `SqlConnection`/`SqlCursor` (DASH-STORY-005).
    """

    def append(self, entry: ProvenanceJournalEntry) -> None:
        """Durably append `entry`, or raise.

        Must not return before the durability barrier ADR-010 requires
        (an fsync, or the adapter's equivalent) has completed -- a
        `ProvenanceWriteGate` treats a normal return from this method as
        proof the write may now proceed to the caller's own zone-content
        persistence.
        """
        ...

    def find_by_idempotency_key(
        self, tenant_id: str, idempotency_key: str
    ) -> ProvenanceJournalEntry | None:
        """Return the previously journaled entry for this idempotency key, if any.

        `ProvenanceWriteGate.submit_write` calls this before minting a
        fresh `write_id`: a verbatim replay of a `WriteRequest` a caller
        already submitted (same `tenant_id`/`idempotency_key`) must
        return the original `WriteAccepted` result rather than being
        journaled and persisted a second time -- otherwise a captured
        valid `WriteRequest` could be replayed to trigger the caller's
        `persist_fact` callback repeatedly with no caller-visible
        freshness or nonce check.

        Returns:
            The matching entry, or `None` if this `(tenant_id,
            idempotency_key)` pair has never been journaled.
        """
        ...
