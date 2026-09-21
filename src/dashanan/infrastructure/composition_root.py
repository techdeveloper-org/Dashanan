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

import logging
import os
from collections.abc import Mapping

import psycopg

from dashanan.application.conflict_aware_zone_writes import (
    ConflictAwareEntityMemoryRepository,
    ConflictAwareSemanticRepository,
)
from dashanan.application.conflict_detection_sweep import (
    ConflictDetectingProvenanceRepository,
    ProvenanceRepositoryPort,
)
from dashanan.application.memory_orchestrator import MemoryOrchestrator
from dashanan.domain.exceptions import DashananError
from dashanan.domain.ports import Clock, EventBus, ZoneRepository
from dashanan.domain.zone import ZoneId
from dashanan.infrastructure.entity_memory_repository import EntityMemoryRepository
from dashanan.infrastructure.settings import (
    PostgresSettings,
    load_postgres_settings_from_env,
)
from dashanan.infrastructure.sql_episodic_repository import (
    SqlConnection as EpisodicSqlConnection,
)
from dashanan.infrastructure.sql_episodic_repository import SqlEpisodicRepository
from dashanan.infrastructure.sql_provenance_repository import (
    SqlConnection as ProvenanceSqlConnection,
)
from dashanan.infrastructure.sql_provenance_repository import SqlProvenanceRepository
from dashanan.infrastructure.sql_semantic_repository import (
    SqlConnection as SemanticSqlConnection,
)
from dashanan.infrastructure.sql_semantic_repository import SqlSemanticRepository

logger = logging.getLogger(__name__)

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


class PostgresConnectionError(DashananError):
    """Raised when `build_postgres_connection` cannot open a real connection.

    Wraps every `psycopg.Error` this module's own `psycopg.connect` call can
    raise. `psycopg.Error`'s own message can include a fragment of the
    connection string (host, dbname, and -- on some libpq builds -- user)
    that reached it; this type carries only a fixed, generic message so a
    caller that logs or re-raises it never leaks that fragment, matching
    `settings.py`'s own "never logs a decoded credential" promise. The
    original `psycopg.Error` is chained via `__cause__` for local debugging
    only (never rendered in this exception's own message).
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


def build_conflict_aware_semantic_repository(
    semantic_connection: SemanticSqlConnection,
    provenance_connection: ProvenanceSqlConnection,
    clock: Clock,
    *,
    verify_privileges: bool = True,
    detect_conflicts: bool = True,
) -> ConflictAwareSemanticRepository:
    """Build Zone 3's write path with the FR-013 sweep wired in by default (DASH-STORY-022).

    Closes the gap `sprint3_ar1_assignments.json` AR1-S3-022 confirmed by
    grep: `SqlSemanticRepository` never called `build_provenance_repository`
    or the repository it returns, so every Zone 3 write silently bypassed
    the FR-013 sweep despite the sweep itself being real and tested. This
    builder composes the real `SqlSemanticRepository` with the SAME
    `build_provenance_repository(detect_conflicts=True)` call every other
    Zone-7-writing caller in this composition root uses (must-not-deviate
    item 3, `application.conflict_aware_zone_writes` module docstring) --
    never a second, independently-constructed conflict-detecting wrapper.

    Args:
        semantic_connection: The DB-API connection `SqlSemanticRepository`
            issues its own parameterized queries against.
        provenance_connection: The DB-API connection the FR-013 sweep's
            underlying `SqlProvenanceRepository` issues ITS parameterized
            queries against -- deliberately a separate connection/table
            from `semantic_connection` (Zone 3 and Zone 7 are distinct
            schemas), mirroring `build_provenance_repository`'s own
            `connection` parameter.
        clock: Shared time source for both the sweep's correction-record
            `write_timestamp` and each Zone 3 write's own provenance
            `write_timestamp`.
        verify_privileges: Forwarded to `build_provenance_repository`
            (Zone 7's own DSHN-55 privilege check). `SqlSemanticRepository`
            itself has no such parameter (its own docstring: Zone 3's
            tables are ordinarily mutable, so that guard's precondition
            does not hold there).
        detect_conflicts: Forwarded to `build_provenance_repository`.
            Defaults to `True`; pass `False` only to get the bare,
            unwrapped `SqlProvenanceRepository` behind this writer (an
            explicit, visible opt-out, never a silent default).

    Returns:
        A `ConflictAwareSemanticRepository` whose `insert_edge`/
        `insert_general_fact` both run the FR-013 sweep before writing to
        Zone 3.
    """
    semantic_repository = SqlSemanticRepository(connection=semantic_connection)
    provenance_repository = build_provenance_repository(
        provenance_connection,
        clock,
        verify_privileges=verify_privileges,
        detect_conflicts=detect_conflicts,
    )
    return ConflictAwareSemanticRepository(
        semantic_repository=semantic_repository,
        provenance_repository=provenance_repository,
        clock=clock,
    )


def build_conflict_aware_entity_memory_repository(
    entity_memory_repository: EntityMemoryRepository,
    provenance_connection: ProvenanceSqlConnection,
    clock: Clock,
    *,
    verify_privileges: bool = True,
    detect_conflicts: bool = True,
) -> ConflictAwareEntityMemoryRepository:
    """Build Zone 5's write path with the FR-013 sweep wired in by default (DASH-STORY-022).

    Mirrors `build_conflict_aware_semantic_repository` exactly for Zone 5:
    closes the same grep-confirmed gap for `EntityMemoryRepository.
    write_attribute`, composing it with the SAME
    `build_provenance_repository(detect_conflicts=True)` call (must-not-
    deviate item 3) rather than a second, parallel wrapper.

    Args:
        entity_memory_repository: The real Zone 5 adapter every attribute
            write ultimately reaches. Taken pre-constructed (unlike Zone
            3/2/7, `EntityMemoryRepository.__init__` takes a `Clock` and
            an `EventBus`, not a DB-API connection -- Shape A, in-process
            storage, per its own module docstring) -- the caller composes
            it the same way every other Shape A adapter in this codebase
            already is, and hands it here.
        provenance_connection: The DB-API connection the FR-013 sweep's
            underlying `SqlProvenanceRepository` issues its own
            parameterized queries against.
        clock: Shared time source for both the sweep's correction-record
            `write_timestamp` and each Zone 5 write's own provenance
            `write_timestamp`.
        verify_privileges: Forwarded to `build_provenance_repository`.
        detect_conflicts: Forwarded to `build_provenance_repository`.
            Defaults to `True`.

    Returns:
        A `ConflictAwareEntityMemoryRepository` whose `write_attribute`
        runs the FR-013 sweep before writing to Zone 5.
    """
    provenance_repository = build_provenance_repository(
        provenance_connection,
        clock,
        verify_privileges=verify_privileges,
        detect_conflicts=detect_conflicts,
    )
    return ConflictAwareEntityMemoryRepository(
        entity_memory_repository=entity_memory_repository,
        provenance_repository=provenance_repository,
        clock=clock,
    )


def build_postgres_connection(
    settings: PostgresSettings | None = None,
    env: Mapping[str, str] | None = None,
) -> psycopg.Connection:
    """Build a real `psycopg` connection for Shape B (DASH-STORY-024, FR-015, AC-024-3).

    This is the composition root's real, non-embedded adapter path AC-024-3
    requires: it connects as `PostgresSettings.app_login_user` -- the real,
    env-sourced LOGIN credential `dashanan.infrastructure.migration_runner.
    bind_app_login_role` provisions and grants `dashanan_provenance_role`/
    `dashanan_episodic_role` membership to -- never as the migration role,
    matching the least-privilege posture `provenance_schema.sql`'s own long
    comment documents ("the application is expected to connect as its own
    LOGIN role and be GRANTed membership ... never to log in as this role
    directly").

    The returned connection structurally satisfies both
    `sql_provenance_repository.SqlConnection` and
    `sql_episodic_repository.SqlConnection` (each a minimal DB-API 2.0
    Protocol requiring only `.cursor()`), so it can be passed directly to
    `build_provenance_repository`/`build_episodic_repository` above.

    Args:
        settings: The env-sourced settings to connect with. When `None`
            (the default), loaded via `load_postgres_settings_from_env(env)`.
        env: Forwarded to `load_postgres_settings_from_env` when `settings`
            is not supplied directly.

    Returns:
        An open `psycopg.Connection` to the Shape B PostgreSQL instance,
        connected as the app login role.

    Raises:
        dashanan.infrastructure.settings.SettingsConfigurationError: If
            `settings` is not supplied and the environment does not hold a
            complete, valid `PostgresSettings` (re-raised unchanged from
            `load_postgres_settings_from_env`).
        PostgresConnectionError: If the connection itself fails (host
            unreachable, authentication rejected, database does not exist,
            etc.) -- wraps the underlying `psycopg.Error` without exposing
            its message, which can contain a connection-string fragment.
    """
    resolved = settings if settings is not None else load_postgres_settings_from_env(env)
    try:
        return psycopg.connect(resolved.app_dsn())
    except psycopg.Error as exc:
        logger.error("Composition root failed to connect to PostgreSQL")
        raise PostgresConnectionError(
            "Failed to connect to PostgreSQL. See server-side logs for "
            "detail; this error intentionally carries no connection-string "
            "or credential content."
        ) from exc
