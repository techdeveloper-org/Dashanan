"""Composition root: the single place this codebase wires secure-by-default adapters.

DSHN-60 remediation, attempt 3. Attempt 2's re-audit found this module itself was the
unwired primitive it warned about: `build_memory_orchestrator`/`build_provenance_
repository`/`build_episodic_repository` were real and tested, but nothing in `src/`
outside this file ever called them, so every actual construction site of
`MemoryOrchestrator`, `SqlProvenanceRepository`, and `SqlEpisodicRepository` still ran
with HLD Threat S-1 verification and the DSHN-55 privilege check silently disabled by
those classes' own `= None`/`= False` defaults.

Attempt 3 closes that at the source instead of only at this composition root:
`MemoryOrchestrator.__init__`'s `tenant_credential_signing_key` and both
`SqlProvenanceRepository.__init__`/`SqlEpisodicRepository.__init__`'s
`verify_privileges` are now required keyword-only arguments with NO default (see each
class's own docstring). There is no longer a silent insecure default anywhere in this
codebase for either control -- every caller, this composition root included, must now
state its choice explicitly. This module's remaining job is exactly Clean
Architecture's definition of a composition root (`clean-architecture` skill, section
22, "the only place that knows about all layers"): it resolves the secret from the
environment (`load_tenant_credential_signing_key_from_env`) and always chooses the
secure value (verification/checks ON, the FR-013 sweep wrapped in) so a real deployment
that starts here gets the secure posture without repeating that choice at every call
site. The still-open gap attempt 3's own audit correctly names is architectural, not a
default: this codebase has no api/routes module or host application anywhere that
actually invokes this composition root, or any other constructor, outside its test
suite -- that gap is outside a security-patch pass's scope to invent.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from dashanan.application.conflict_detection_sweep import (
    ConflictDetectingProvenanceRepository,
    ProvenanceRepositoryPort,
)
from dashanan.application.memory_orchestrator import MemoryOrchestrator
from dashanan.domain.exceptions import DashananError
from dashanan.domain.ports import Clock, EventBus, ZoneRepository
from dashanan.domain.zone import ZoneId
from dashanan.infrastructure.sql_episodic_repository import SqlEpisodicRepository
from dashanan.infrastructure.sql_episodic_repository import (
    SqlConnection as EpisodicSqlConnection,
)
from dashanan.infrastructure.sql_provenance_repository import SqlProvenanceRepository
from dashanan.infrastructure.sql_provenance_repository import (
    SqlConnection as ProvenanceSqlConnection,
)

TENANT_CREDENTIAL_SIGNING_KEY_ENV_VAR = "DASHANAN_TENANT_CREDENTIAL_SIGNING_KEY"
"""The environment variable `load_tenant_credential_signing_key_from_env` reads.

Holds a hex-encoded secret (never raw bytes in an env var -- hex keeps the value
shell-safe and copy-pasteable) of at least `_MIN_SIGNING_KEY_BYTES` decoded bytes, matching
`MemoryOrchestrator`'s own minimum (mirrors `ProvenanceWriteGate`'s and
`TenantCredential`'s identical RFC 2104-derived HMAC-SHA256 key-length rationale).
"""

_MIN_SIGNING_KEY_BYTES = 32


class CompositionRootConfigurationError(DashananError):
    """Raised when this composition root cannot assemble a secure-by-default component.

    Distinct from `TenantAuthenticationError` (a runtime per-request rejection):
    this is a startup-time misconfiguration -- a missing or malformed secret --
    that must fail closed before any `MemoryOrchestrator` is even constructed,
    never silently fall back to the unverified `tenant_credential_signing_key=None`
    posture this composition root exists specifically to avoid.
    """


def load_tenant_credential_signing_key_from_env(
    env: Mapping[str, str] | None = None,
) -> bytes:
    """Load and decode the HLD Threat S-1 signing key from `env` (default `os.environ`).

    Args:
        env: The environment mapping to read from. Defaults to `os.environ`;
            a caller (or a test) may pass an explicit mapping instead of
            mutating real process environment variables (testing-core:
            dependency injection over monkeypatching global state).

    Returns:
        The decoded signing key, at least `_MIN_SIGNING_KEY_BYTES` bytes.

    Raises:
        CompositionRootConfigurationError: If
            `TENANT_CREDENTIAL_SIGNING_KEY_ENV_VAR` is unset or blank, is not
            valid hex, or decodes to fewer than `_MIN_SIGNING_KEY_BYTES`
            bytes. Fails closed rather than returning `None` and letting
            `MemoryOrchestrator` silently run with verification disabled.
    """
    source = env if env is not None else os.environ
    raw = source.get(TENANT_CREDENTIAL_SIGNING_KEY_ENV_VAR)
    if raw is None or not raw.strip():
        raise CompositionRootConfigurationError(
            f"{TENANT_CREDENTIAL_SIGNING_KEY_ENV_VAR} is not set. "
            "build_memory_orchestrator refuses to construct a MemoryOrchestrator "
            "with HLD Threat S-1 tenant-impersonation verification disabled by "
            f"default; set this variable to a hex-encoded secret of at least "
            f"{_MIN_SIGNING_KEY_BYTES} bytes, or pass tenant_credential_signing_key "
            "explicitly if this deployment provisions the secret another way "
            "(e.g. a secrets manager SDK call)."
        )
    try:
        key = bytes.fromhex(raw.strip())
    except ValueError as exc:
        raise CompositionRootConfigurationError(
            f"{TENANT_CREDENTIAL_SIGNING_KEY_ENV_VAR} must be a hex-encoded "
            f"string; failed to decode: {exc}"
        ) from exc
    if len(key) < _MIN_SIGNING_KEY_BYTES:
        raise CompositionRootConfigurationError(
            f"{TENANT_CREDENTIAL_SIGNING_KEY_ENV_VAR} decodes to {len(key)} "
            f"bytes, below the required minimum of {_MIN_SIGNING_KEY_BYTES} bytes"
        )
    return key


def build_memory_orchestrator(
    zone_repositories: Mapping[ZoneId, ZoneRepository],
    event_bus: EventBus,
    clock: Clock,
    *,
    tenant_credential_signing_key: bytes | None = None,
    env: Mapping[str, str] | None = None,
) -> MemoryOrchestrator:
    """Build a `MemoryOrchestrator` with HLD Threat S-1 verification ON by default.

    Unlike `MemoryOrchestrator.__init__` itself -- which defaults
    `tenant_credential_signing_key` to `None` (verification disabled) for
    backward compatibility with every pre-existing caller -- this composition
    root ALWAYS supplies a signing key: explicitly via
    `tenant_credential_signing_key`, or otherwise loaded from
    `TENANT_CREDENTIAL_SIGNING_KEY_ENV_VAR` via
    `load_tenant_credential_signing_key_from_env`. There is no code path
    through this function that returns a `MemoryOrchestrator` with tenant-
    credential verification disabled.

    Args:
        zone_repositories: Forwarded unchanged to `MemoryOrchestrator`.
        event_bus: Forwarded unchanged to `MemoryOrchestrator`.
        clock: Forwarded unchanged to `MemoryOrchestrator`.
        tenant_credential_signing_key: The signing key to use directly. When
            `None` (the default), this function loads it from the
            environment instead of falling back to "no verification."
        env: Forwarded to `load_tenant_credential_signing_key_from_env` when
            `tenant_credential_signing_key` is not supplied directly.

    Returns:
        A `MemoryOrchestrator` that rejects every `assemble_context` call
        whose `tenant_credential` does not verify (HLD Threat S-1).

    Raises:
        CompositionRootConfigurationError: If
            `tenant_credential_signing_key` is not supplied and the
            environment does not hold a valid one -- see
            `load_tenant_credential_signing_key_from_env`.
        ValueError: If a supplied `tenant_credential_signing_key` is shorter
            than `MemoryOrchestrator`'s own minimum (re-raised unchanged from
            `MemoryOrchestrator.__init__`).
    """
    key = (
        tenant_credential_signing_key
        if tenant_credential_signing_key is not None
        else load_tenant_credential_signing_key_from_env(env)
    )
    return MemoryOrchestrator(
        zone_repositories=zone_repositories,
        event_bus=event_bus,
        clock=clock,
        tenant_credential_signing_key=key,
    )


def build_provenance_repository(
    connection: ProvenanceSqlConnection,
    clock: Clock,
    *,
    verify_privileges: bool = True,
    detect_conflicts: bool = True,
) -> ProvenanceRepositoryPort:
    """Build Zone 7's storage adapter with defense-in-depth ON by default.

    Two independent DSHN-60 gaps close here at once:

      - `verify_privileges` (default `True` here, forwarded to
        `SqlProvenanceRepository.__init__`'s own required
        `verify_privileges` argument): the connected role's privileges are
        checked against `provenance_records`' append-only enforcement
        before this function returns (DSHN-55 defense-in-depth).
      - `detect_conflicts` (default `True`): the returned repository is
        `ConflictDetectingProvenanceRepository`-wrapped, so FR-013's
        contradiction sweep runs on every `append` in the live write path,
        not only in `application.conflict_detection_sweep`'s own isolated
        unit tests.

    Args:
        connection: The DB-API connection `SqlProvenanceRepository` issues
            parameterized queries against.
        clock: Time source for the conflict sweep's correction-record
            `write_timestamp` when `detect_conflicts` is `True`.
        verify_privileges: Forwarded to `SqlProvenanceRepository.__init__`.
            Defaults to `True` here (a real production connection SHOULD be
            checked); pass `False` explicitly for a test double with no
            `pg_roles`/`pg_class` catalog to query, exactly as
            `SqlProvenanceRepository`'s own docstring already documents.
        detect_conflicts: When `True` (the default), wraps the adapter in
            `ConflictDetectingProvenanceRepository`. Pass `False` to get the
            bare `SqlProvenanceRepository` back unwrapped.

    Returns:
        A `ProvenanceRepositoryPort` -- either a
        `ConflictDetectingProvenanceRepository` wrapping a
        `SqlProvenanceRepository`, or the bare `SqlProvenanceRepository`
        when `detect_conflicts=False`.

    Raises:
        dashanan.domain.exceptions.ZoneRepositoryError: If
            `verify_privileges=True` and the connected role can bypass
            append-only enforcement (re-raised unchanged from
            `SqlProvenanceRepository.__init__`).
    """
    repository: ProvenanceRepositoryPort = SqlProvenanceRepository(
        connection=connection, verify_privileges=verify_privileges
    )
    if detect_conflicts:
        return ConflictDetectingProvenanceRepository(wrapped=repository, clock=clock)
    return repository


def build_episodic_repository(
    connection: EpisodicSqlConnection,
    clock: Clock,
    *,
    verify_privileges: bool = True,
) -> SqlEpisodicRepository:
    """Build Zone 2's `ZoneRepository` adapter with the DSHN-55 privilege check ON.

    Mirrors `build_provenance_repository`'s `verify_privileges` default
    exactly, forwarded to `SqlEpisodicRepository.__init__`'s own required
    `verify_privileges` argument: a real production connection should be
    checked by default; a test double with no privilege catalog passes
    `verify_privileges=False` explicitly.

    Args:
        connection: The DB-API connection `SqlEpisodicRepository` issues
            parameterized queries against.
        clock: Forwarded unchanged to `SqlEpisodicRepository`.
        verify_privileges: Forwarded to `SqlEpisodicRepository.__init__`.
            Defaults to `True` here.

    Returns:
        The wired `SqlEpisodicRepository`.

    Raises:
        dashanan.domain.exceptions.ZoneRepositoryError: If
            `verify_privileges=True` and the connected role can bypass
            append-only enforcement (re-raised unchanged from
            `SqlEpisodicRepository.__init__`).
    """
    return SqlEpisodicRepository(
        connection=connection, clock=clock, verify_privileges=verify_privileges
    )
