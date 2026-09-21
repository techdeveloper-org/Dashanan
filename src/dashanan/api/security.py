"""JWT bearer verification and HLD Threat S-1 `TenantCredential` minting.

FR-014 / HLD Section 2 Communication Matrix: "Host AI -> Dashanan API:
gRPC primary, REST gateway; mTLS + JWT with tenant_id claim." mTLS is
enforced at the transport layer (openapi.yaml's own `x-transport-security`
note: not expressible in OpenAPI `securitySchemes`, and out of this
in-process ASGI host's own scope -- a real Shape B deployment terminates
mTLS at a reverse proxy/sidecar in front of this application, per
`x-transport-security`'s own wording). This module owns the JWT half:
verifying the bearer token's signature and `scope`/`tenant_id`/`aud`
claims (HLD Threat S-1, threat E-1).

Key separation (must-not-deviate, DASH-STORY-023 PII note item 2 --
"carry the mTLS+JWT tenant-binding CONTRACT ... but no actual signing-key
value"): this module's own `DASHANAN_JWT_SIGNING_KEY` is a SEPARATE
secret from `composition_root.TENANT_CREDENTIAL_SIGNING_KEY_ENV_VAR`.
The JWT key authenticates the HOST AI CALLER (a wire-boundary concern
this module owns); the tenant-credential key is `MemoryOrchestrator`'s
own internal HLD Threat S-1 check (a domain concern `composition_root.py`
owns). Reusing one HMAC key across two independently-keyed message
formats would still be cryptographically sound (domain separation via
distinct canonical payloads), but two independently rotatable secrets is
the more conservative posture and mirrors this codebase's own precedent
of one dedicated secret per control (`composition_root`'s tenant-credential
key, `zone8_crypto_shredding`'s per-subject key).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

import jwt

from dashanan.domain.exceptions import DashananError
from dashanan.domain.ports import Clock
from dashanan.domain.tenant_credential import TenantCredential, sign_tenant_credential

JWT_SIGNING_KEY_ENV_VAR = "DASHANAN_JWT_SIGNING_KEY"
"""Hex-encoded HMAC-SHA256 key this host verifies every bearer JWT against."""

_MIN_JWT_SIGNING_KEY_BYTES = 32
_JWT_ALGORITHM = "HS256"
DATA_PLANE_AUDIENCE = "dashanan-data-plane"
CONTROL_PLANE_AUDIENCE = "dashanan-control-plane"


class ApiSecurityConfigurationError(DashananError):
    """Raised when this host cannot resolve its own JWT verification key.

    A startup-time misconfiguration (mirrors `composition_root.
    CompositionRootConfigurationError`'s identical fail-closed rationale)
    -- never a per-request rejection.
    """


class TenantAuthenticationError(DashananError):
    """Raised when a bearer token fails signature, expiry, audience, or scope checks.

    Kept local to this module (this codebase's established
    file-disjointness convention) -- distinct from `dashanan.domain.
    tenant_credential.TenantAuthenticationError`, which is
    `MemoryOrchestrator`'s own internal HLD Threat S-1 check on the
    `TenantCredential` THIS module mints, not on the caller's raw JWT.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"bearer token rejected: {reason}")


def load_jwt_signing_key_from_env(env: Mapping[str, str] | None = None) -> bytes:
    """Load and decode this host's JWT verification key from `env`.

    Mirrors `composition_root.load_tenant_credential_signing_key_from_env`
    exactly (hex-encoded, minimum 32 bytes, fails closed) -- see this
    module's own docstring for why the two keys are kept separate.

    Raises:
        ApiSecurityConfigurationError: If unset, blank, not valid hex, or
            shorter than `_MIN_JWT_SIGNING_KEY_BYTES` bytes.
    """
    source = env if env is not None else os.environ
    raw = source.get(JWT_SIGNING_KEY_ENV_VAR)
    if raw is None or not raw.strip():
        raise ApiSecurityConfigurationError(
            f"{JWT_SIGNING_KEY_ENV_VAR} is not set. This host refuses to "
            "verify bearer tokens with no signing key configured; set this "
            "variable to a hex-encoded secret of at least "
            f"{_MIN_JWT_SIGNING_KEY_BYTES} bytes."
        )
    try:
        key = bytes.fromhex(raw.strip())
    except ValueError as exc:
        raise ApiSecurityConfigurationError(
            f"{JWT_SIGNING_KEY_ENV_VAR} must be hex-encoded; failed to decode: {exc}"
        ) from exc
    if len(key) < _MIN_JWT_SIGNING_KEY_BYTES:
        raise ApiSecurityConfigurationError(
            f"{JWT_SIGNING_KEY_ENV_VAR} decodes to {len(key)} bytes, below "
            f"the required minimum of {_MIN_JWT_SIGNING_KEY_BYTES} bytes"
        )
    return key


@dataclass(frozen=True, slots=True)
class AuthenticatedCaller:
    """The verified identity and authority of one bearer-token-carrying request.

    Attributes:
        tenant_id: The JWT's own `tenant_id` claim -- authoritative
            (ADR-013); never taken from a request body or path parameter
            except the documented `admin:tenants` platform-operator
            exemption (openapi.yaml `/tenants/{tenant_id}` path
            description).
        scopes: The space-delimited `scope` claim, split.
        is_platform_operator: True when this credential's `aud` claim is
            `CONTROL_PLANE_AUDIENCE` -- exempt from the
            `TENANT_SCOPE_MISMATCH` path/claim equality check
            (openapi.yaml `/tenants/{tenant_id}` description).
        tenant_credential: A freshly host-signed `TenantCredential` for
            this caller's verified `tenant_id`, ready to hand to
            `MemoryOrchestrator.assemble_context` (HLD Threat S-1) --
            this host holds the signing key, so it mints one on every
            authenticated request rather than trusting the caller to
            supply one (no wire-format field for it exists on any
            openapi.yaml request schema, correctly: HLD Threat S-1 is a
            trust boundary INTERNAL to this host, not a caller-supplied
            claim).
    """

    tenant_id: str
    scopes: frozenset[str]
    is_platform_operator: bool
    tenant_credential: TenantCredential

    def has_scope(self, scope: str) -> bool:
        """True when `scope` is present in this caller's verified `scope` claim."""
        return scope in self.scopes


def verify_bearer_token(
    token: str,
    *,
    jwt_signing_key: bytes,
    tenant_credential_signing_key: bytes,
    clock: Clock,
    required_scope: str,
) -> AuthenticatedCaller:
    """Verify `token`'s signature/expiry/audience, then require `required_scope`.

    HLD Threat S-1: `tenant_id` is read ONLY from the verified JWT claim,
    never from any request body field (no openapi.yaml schema carries one
    -- ADR-013). HLD Threat E-1: `aud` distinguishes a data-plane token
    (`context:read`/`memory:write` scopes) from a control-plane token
    (`admin:zones`/`admin:tenants` scopes); a data-plane token presented
    at an admin endpoint fails the `required_scope` check below and is
    rejected 403, never silently accepted with lesser privilege.

    Args:
        token: The raw bearer token (already stripped of the "Bearer "
            prefix by the caller).
        jwt_signing_key: This host's own `DASHANAN_JWT_SIGNING_KEY`.
        tenant_credential_signing_key: `composition_root`'s own tenant-
            credential key -- used ONLY to mint the `TenantCredential`
            `MemoryOrchestrator` will itself re-verify; never to verify
            the JWT itself (see this module's docstring, "Key
            separation").
        clock: Injectable time source (testing-core DI).
        required_scope: The single scope this operation requires
            (openapi.yaml `security: - bearerAuth: [<scope>]` per
            operation).

    Returns:
        The verified `AuthenticatedCaller`.

    Raises:
        TenantAuthenticationError: On any signature, expiry, `aud`,
            missing-claim, or insufficient-scope failure. Never leaks
            which specific check failed to the caller (control I-6,
            openapi.yaml `ErrorResponse.error.message` docstring) --
            callers map this to a generic 401/403, logging the real
            `reason` server-side only.
    """
    try:
        claims = jwt.decode(
            token,
            jwt_signing_key,
            algorithms=[_JWT_ALGORITHM],
            audience=[DATA_PLANE_AUDIENCE, CONTROL_PLANE_AUDIENCE],
            options={"require": ["exp", "iat", "tenant_id", "scope", "aud"]},
        )
    except jwt.InvalidTokenError as exc:
        raise TenantAuthenticationError(f"invalid or expired token: {exc}") from exc

    tenant_id = claims.get("tenant_id")
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise TenantAuthenticationError("token carries no non-blank tenant_id claim")

    scope_claim = claims.get("scope", "")
    scopes = frozenset(scope_claim.split()) if isinstance(scope_claim, str) else frozenset()
    if required_scope not in scopes:
        raise TenantAuthenticationError(
            f"token scope {sorted(scopes)!r} does not include required scope "
            f"{required_scope!r}"
        )

    aud_claim = claims.get("aud")
    auds = {aud_claim} if isinstance(aud_claim, str) else set(aud_claim or ())
    is_platform_operator = CONTROL_PLANE_AUDIENCE in auds

    issued_at = clock.now()
    if issued_at.tzinfo is None:
        issued_at = issued_at.replace(tzinfo=UTC)
    tenant_credential = sign_tenant_credential(
        secret=tenant_credential_signing_key,
        tenant_id=tenant_id,
        issued_at=issued_at,
    )
    return AuthenticatedCaller(
        tenant_id=tenant_id,
        scopes=scopes,
        is_platform_operator=is_platform_operator,
        tenant_credential=tenant_credential,
    )


def enforce_tenant_scope_match(caller: AuthenticatedCaller, path_tenant_id: str) -> None:
    """openapi.yaml `/tenants/{tenant_id}` invariant: reject a scope mismatch.

    Raises:
        TenantAuthenticationError: If `caller` is not a platform operator
            and `path_tenant_id` differs from `caller.tenant_id` --
            callers map this to `403 TENANT_SCOPE_MISMATCH`, never `404`
            (openapi.yaml's own documented rationale: the caller is
            authenticated and scoped, just not authorized for this
            tenant).
    """
    if caller.is_platform_operator:
        return
    if path_tenant_id != caller.tenant_id:
        raise TenantAuthenticationError(
            "TENANT_SCOPE_MISMATCH: path tenant_id does not match the "
            "caller's own verified JWT tenant_id claim"
        )
