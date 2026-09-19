"""TenantCredential: a host-signed proof of `tenant_id` binding (HLD Threat S-1).

DSHN-60 remediation (HIGH): HLD Threat S-1 ("a host impersonates another
tenant") was structurally unmitigated -- every entry point (`ContextAssembly
Request.tenant_id`, `WriteRequest.tenant_id`) took `tenant_id` as a plain
caller-supplied string with no verified-credential binding anywhere in the
codebase, and no composition root, api/routes module, or auth middleware
existed to bind a caller's real identity to the `tenant_id` it asserted.

This module adds the same structural primitive `write_gate.
UserTurnAttestation` already established for HLD Threat S-2 ("`user_stated`
requires an explicit host-side user-turn marker"): an HMAC-SHA256 signature
over the exact `tenant_id` it is claimed for, producible only by whoever
holds the host's own tenant-credential signing key -- a key the entry
point's caller is never given. `verify_tenant_credential` is the sole
check `MemoryOrchestrator` trusts before treating a `ContextAssemblyRequest
.tenant_id` as verified rather than merely caller-asserted.

Composition-root note (mirrors `infrastructure.append_only_privilege_guard`
's own precedent for the identical class of gap): minting a genuine
`TenantCredential` from a caller's real network identity (an mTLS client
certificate, a JWT `tenant_id` claim, an API-gateway-injected header) is a
wire-boundary concern this story does not own -- no api/routes module or
composition root exists yet anywhere in this codebase (the S-1 finding's
own observation). What this module adds is the concrete, callable, testable
verification primitive a future wire boundary -- or `MemoryOrchestrator`
itself, opted in via its `tenant_credential_signing_key` constructor
parameter -- can require, exactly as `verify_append_only_role_is_safe` adds
a real, callable check for a composition-root gap it cannot itself close.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timedelta

from dashanan.domain.exceptions import DashananError

_DEFAULT_TENANT_CREDENTIAL_MAX_AGE_SECONDS = 300
_FIELD_LENGTH_PREFIX_WIDTH = 10


class TenantAuthenticationError(DashananError):
    """Raised when a `tenant_id` claim fails `TenantCredential` verification.

    Kept local to this module rather than added to the shared `dashanan.
    domain.exceptions`, mirroring `write_gate.ProvenanceJournalPort` and
    `zone2_capacity_backstop.Zone2CapacityBackstopError`'s identical
    "kept local" choice. Distinct from `ZoneRepositoryError`: this is a
    caller-identity failure (HLD Threat S-1), never a zone-adapter I/O
    failure, so `MemoryOrchestrator` must never fold it into
    `zones_unavailable`/`degraded` -- a rejected tenant claim is not a
    degraded read, it is a request `assemble_context` must refuse to
    serve at all (mirrors `ProvenanceWriteGate`'s S-2 rejection path,
    which rejects before any zone work rather than degrading it).
    """

    def __init__(self, tenant_id: str, reason: str) -> None:
        self.tenant_id = tenant_id
        self.reason = reason
        super().__init__(
            f"tenant credential verification failed for tenant_id={tenant_id!r}: {reason}"
        )


def _canonicalize_fields(*fields: str) -> bytes:
    """Encode `fields` into a byte string where no field-boundary forgery is possible.

    Local copy of the same length-prefixed construction `write_gate.
    _canonicalize_fields` and `provenance_record._canonicalize_fields`
    use, kept local per this codebase's established convention (see
    `write_gate.is_hex_sha256`'s docstring) rather than importing a
    sibling module's private helper.
    """
    parts: list[bytes] = []
    for field in fields:
        encoded = field.encode("utf-8")
        parts.append(f"{len(encoded):0{_FIELD_LENGTH_PREFIX_WIDTH}d}:".encode("ascii"))
        parts.append(encoded)
    return b"".join(parts)


@dataclass(frozen=True, slots=True)
class TenantCredential:
    """A host-signed proof that a `tenant_id` claim is genuine (HLD Threat S-1).

    Attributes:
        issued_at: When the host produced this credential. Bound into the
            signed payload so a captured credential cannot be replayed
            indefinitely -- `verify_tenant_credential` rejects one older
            than its freshness window.
        signature: Lowercase hex HMAC-SHA256 digest over the canonical
            `(tenant_id, issued_at)` payload.
    """

    issued_at: datetime
    signature: str


def _tenant_credential_message(*, tenant_id: str, issued_at: datetime) -> bytes:
    """Build the exact canonical payload `sign_tenant_credential` signs."""
    return _canonicalize_fields(tenant_id, issued_at.isoformat())


def sign_tenant_credential(
    *, secret: bytes, tenant_id: str, issued_at: datetime
) -> TenantCredential:
    """Produce a genuine `TenantCredential` -- a host-only capability.

    Only code that holds `secret` (the host's exclusive tenant-credential
    signing key, provisioned from a secrets manager or environment
    variable at the composition root and never handed to whatever submits
    a `ContextAssemblyRequest`) can call this and get back a credential
    `verify_tenant_credential` will accept.
    """
    message = _tenant_credential_message(tenant_id=tenant_id, issued_at=issued_at)
    signature = hmac.new(secret, message, hashlib.sha256).hexdigest()
    return TenantCredential(issued_at=issued_at, signature=signature)


def verify_tenant_credential(
    credential: TenantCredential | None,
    *,
    secret: bytes,
    tenant_id: str,
    now: datetime,
    max_age_seconds: int = _DEFAULT_TENANT_CREDENTIAL_MAX_AGE_SECONDS,
) -> bool:
    """HLD Threat S-1: the sole check that a `tenant_id` claim is genuine.

    Returns `False` -- never raises -- for a missing credential, a
    signature that does not match what `sign_tenant_credential` would
    have produced for this exact `tenant_id`, a credential dated in the
    future, or one older than `max_age_seconds`. A credential genuinely
    signed for a DIFFERENT `tenant_id` also returns `False` here: the
    signature is verified against the caller's claimed `tenant_id`, so a
    credential minted for tenant A can never verify against a claim of
    tenant B -- exactly the impersonation HLD Threat S-1 describes.
    `hmac.compare_digest` is used for the signature comparison so this
    check runs in constant time regardless of where a mismatch occurs.

    Args:
        credential: The caller-supplied credential, or `None` if the
            caller asserted `tenant_id` with no credential at all.
        secret: The same host-exclusive signing key
            `sign_tenant_credential` was called with.
        tenant_id: The `tenant_id` the caller is claiming.
        now: The caller's own clock reading (testing-core: injected,
            never `datetime.now()` directly).
        max_age_seconds: How long a genuine credential remains valid.
    """
    if credential is None:
        return False
    if credential.issued_at > now:
        return False
    if now - credential.issued_at > timedelta(seconds=max_age_seconds):
        return False
    expected = sign_tenant_credential(
        secret=secret, tenant_id=tenant_id, issued_at=credential.issued_at
    )
    return hmac.compare_digest(expected.signature, credential.signature)
