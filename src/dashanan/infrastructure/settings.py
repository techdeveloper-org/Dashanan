"""Env-sourced runtime settings for Shape B deployment (DASH-STORY-024, FR-015).

FR-015 (verbatim, `docs/phase-6-sprint-planning/backlog_draft.json`
`future_sprints_backlog.remaining_frs` entry, `note` field): "real Shape B
deployment per HLD Section 2's container/service table (Postgres, Redis,
Qdrant, OpenSearch, S3), a real DB driver dependency (pyproject.toml
currently declares zero), docker-compose, env config, and a migration
runner -- none of which exist anywhere in this repo today (confirmed by
direct exploration 2026-09-20: no docker-compose*, no .env*, no
settings/config module, no alembic). This is also where the production
DB-role-wiring gap closes for real: dashanan_app_role/dashanan_schema_owner
(defined in provenance_schema.sql/episodic_schema.sql since DSHN-55) get
bound to an actual login credential via this story's connection-string/
composition-root wiring, not before."

This module is that "settings/config module" the exploration above found
missing. It mirrors `composition_root.load_tenant_credential_signing_key_
from_env`'s established pattern exactly: every credential-bearing field is
read from an environment variable with NO hardcoded default and NO silent
fallback (application-security-core / cloud-security-core: never hardcode
secrets, never store them in a committed file). Non-secret connectivity
fields (host, port, database name) DO carry sensible local-dev defaults
matching `docker-compose.yml`'s own service names and documented ports, so
a developer running `docker-compose up` needs to export only the
credential-bearing variables to run the migration runner locally.

PII / secrets note: this module never logs a decoded credential value. Every
raised `SettingsConfigurationError` names the missing/invalid environment
variable, never the value that was (or was not) supplied.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from psycopg.conninfo import make_conninfo

from dashanan.domain.exceptions import DashananError


class SettingsConfigurationError(DashananError):
    """Raised when a required environment variable is missing, blank, or malformed.

    Fails closed -- mirrors `composition_root.CompositionRootConfigurationError`
    exactly: no loader in this module ever substitutes a hardcoded default for
    a missing credential and continues.
    """


_PLACEHOLDER_PREFIX = "REPLACE_WITH_"
"""The exact placeholder convention `.env.example` uses for every secret field
(e.g. `REPLACE_WITH_A_REAL_RANDOM_PASSWORD`). `_require_str` rejects any value
starting with this prefix so a developer who copies `.env.example` to `.env`
without actually replacing a placeholder fails closed at startup instead of
the literal placeholder string silently becoming a "real" credential.
"""


def _require_str(env: Mapping[str, str], var_name: str) -> str:
    """Return `env[var_name]`, stripped, raising if absent, blank, or a placeholder.

    Raises:
        SettingsConfigurationError: If the value is unset, blank, or starts
            with `_PLACEHOLDER_PREFIX` -- the exact convention
            `.env.example` ships (e.g. `REPLACE_WITH_A_REAL_RANDOM_PASSWORD`),
            meaning the developer copied that file to `.env` without actually
            replacing the placeholder.
    """
    raw = env.get(var_name)
    if raw is None or not raw.strip():
        raise SettingsConfigurationError(
            f"{var_name} is not set. This value must be sourced from env "
            "config (never hardcoded in a committed file) -- see "
            "settings.py's own module docstring and DASH-STORY-024 AC-024-2."
        )
    value = raw.strip()
    if value.startswith(_PLACEHOLDER_PREFIX):
        raise SettingsConfigurationError(
            f"{var_name} is still set to a `.env.example` placeholder value "
            f"(starts with {_PLACEHOLDER_PREFIX!r}). Replace it with a real, "
            "randomly generated value before running against a real "
            "deployment -- see .env.example's own header comment."
        )
    return value


def _optional_str(env: Mapping[str, str], var_name: str) -> str | None:
    """Return `env[var_name]`, stripped, or `None` if absent/blank."""
    raw = env.get(var_name)
    if raw is None or not raw.strip():
        return None
    return raw.strip()


def _int_with_default(env: Mapping[str, str], var_name: str, default: int) -> int:
    """Return `int(env[var_name])`, or `default` if absent/blank."""
    raw = env.get(var_name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise SettingsConfigurationError(
            f"{var_name} must be an integer port number; got {raw!r}"
        ) from exc


def _str_with_default(env: Mapping[str, str], var_name: str, default: str) -> str:
    """Return `env[var_name]`, stripped, or `default` if absent/blank."""
    raw = env.get(var_name)
    if raw is None or not raw.strip():
        return default
    return raw.strip()


@dataclass(frozen=True, slots=True)
class PostgresSettings:
    """Shape B PostgreSQL connectivity (HLD Section 2, ADR-006).

    `migration_user`/`migration_password` are the real, env-sourced LOGIN
    credential the migration runner connects as to apply every zone schema
    .sql file and to perform the brief `dashanan_schema_owner` membership
    grant/revoke each file's own DDL already implements (see
    `provenance_schema.sql`'s long comment on control 3). `app_login_user`/
    `app_login_password` are the real, env-sourced LOGIN credential the
    migration runner creates (or updates) and grants membership in
    `dashanan_provenance_role`/`dashanan_episodic_role` -- the credential
    the composition root's real adapters (`build_postgres_connection`)
    actually connect as at runtime, least-privilege, never as the migration
    role (AC-024-2).
    """

    host: str
    port: int
    database: str
    migration_user: str
    migration_password: str
    app_login_user: str
    app_login_password: str

    def migration_dsn(self, *, connect_timeout_seconds: int = 10) -> str:
        """Return a libpq keyword/value connection string for the migration role.

        Built via `psycopg.conninfo.make_conninfo` rather than manual
        f-string interpolation: `make_conninfo` escapes/quotes every value
        the same way libpq's own parser expects, so a value containing a
        space, a quote, or a `key=value`-shaped fragment is always treated
        as literal content of its own field, never parsed as a second,
        attacker-controlled DSN keyword (application-security-core: never
        string-concatenate untrusted input into a parsed grammar).

        `connect_timeout_seconds` bounds only libpq's own connection-phase
        wait (per `PQconnectdbParams`'s `connect_timeout` semantics); it does
        not bound how long a query issued after connecting may take. Always
        set, never left to libpq's no-timeout default, so a misconfigured or
        unreachable host fails fast and visibly instead of hanging the
        calling process indefinitely.
        """
        return make_conninfo(
            host=self.host,
            port=self.port,
            dbname=self.database,
            user=self.migration_user,
            password=self.migration_password,
            connect_timeout=connect_timeout_seconds,
        )

    def app_dsn(self, *, connect_timeout_seconds: int = 10) -> str:
        """Return a libpq keyword/value connection string for the app login role.

        See `migration_dsn`'s docstring for why `make_conninfo` is used
        instead of manual string interpolation, and for what
        `connect_timeout_seconds` does and does not bound.
        """
        return make_conninfo(
            host=self.host,
            port=self.port,
            dbname=self.database,
            user=self.app_login_user,
            password=self.app_login_password,
            connect_timeout=connect_timeout_seconds,
        )


def load_postgres_settings_from_env(
    env: Mapping[str, str] | None = None,
) -> PostgresSettings:
    """Load `PostgresSettings` from `env` (default `os.environ`).

    Raises:
        SettingsConfigurationError: If any of `DASHANAN_POSTGRES_MIGRATION_USER`,
            `DASHANAN_POSTGRES_MIGRATION_PASSWORD`, `DASHANAN_POSTGRES_APP_USER`,
            or `DASHANAN_POSTGRES_APP_PASSWORD` is unset or blank. Host, port,
            and database name fall back to this repo's `docker-compose.yml`
            defaults (`localhost`, `5432`, `dashanan`) when unset -- those are
            not secrets.
    """
    source = env if env is not None else os.environ
    return PostgresSettings(
        host=_str_with_default(source, "DASHANAN_POSTGRES_HOST", "localhost"),
        port=_int_with_default(source, "DASHANAN_POSTGRES_PORT", 5432),
        database=_str_with_default(source, "DASHANAN_POSTGRES_DATABASE", "dashanan"),
        migration_user=_require_str(source, "DASHANAN_POSTGRES_MIGRATION_USER"),
        migration_password=_require_str(source, "DASHANAN_POSTGRES_MIGRATION_PASSWORD"),
        app_login_user=_require_str(source, "DASHANAN_POSTGRES_APP_USER"),
        app_login_password=_require_str(source, "DASHANAN_POSTGRES_APP_PASSWORD"),
    )


@dataclass(frozen=True, slots=True)
class RedisSettings:
    """Shape B Redis connectivity (HLD Section 2, ADR-005: event bus + hot store)."""

    host: str
    port: int
    password: str | None


def load_redis_settings_from_env(env: Mapping[str, str] | None = None) -> RedisSettings:
    """Load `RedisSettings` from `env` (default `os.environ`).

    `password` is optional at the type level (`RESP` supports unauthenticated
    connections) but `docker-compose.yml`'s own Redis service always requires
    one via `--requirepass`; an unset `DASHANAN_REDIS_PASSWORD` will simply
    fail to authenticate against that compose service, which is the intended
    fail-closed behavior for a Shape B deployment.
    """
    source = env if env is not None else os.environ
    return RedisSettings(
        host=_str_with_default(source, "DASHANAN_REDIS_HOST", "localhost"),
        port=_int_with_default(source, "DASHANAN_REDIS_PORT", 6379),
        password=_optional_str(source, "DASHANAN_REDIS_PASSWORD"),
    )


@dataclass(frozen=True, slots=True)
class QdrantSettings:
    """Shape B Qdrant connectivity (HLD Section 2, ADR-007: vector index)."""

    host: str
    port: int
    api_key: str | None


def load_qdrant_settings_from_env(env: Mapping[str, str] | None = None) -> QdrantSettings:
    """Load `QdrantSettings` from `env` (default `os.environ`)."""
    source = env if env is not None else os.environ
    return QdrantSettings(
        host=_str_with_default(source, "DASHANAN_QDRANT_HOST", "localhost"),
        port=_int_with_default(source, "DASHANAN_QDRANT_PORT", 6333),
        api_key=_optional_str(source, "DASHANAN_QDRANT_API_KEY"),
    )


@dataclass(frozen=True, slots=True)
class OpenSearchSettings:
    """Shape B OpenSearch connectivity (HLD Section 2, ADR-008: lexical index)."""

    host: str
    port: int
    username: str
    password: str


def load_opensearch_settings_from_env(
    env: Mapping[str, str] | None = None,
) -> OpenSearchSettings:
    """Load `OpenSearchSettings` from `env` (default `os.environ`).

    Raises:
        SettingsConfigurationError: If `DASHANAN_OPENSEARCH_PASSWORD` is unset
            or blank -- OpenSearch's security plugin refuses a weak/default
            admin password at container startup, so this deployment has no
            safe hardcoded fallback to offer.
    """
    source = env if env is not None else os.environ
    return OpenSearchSettings(
        host=_str_with_default(source, "DASHANAN_OPENSEARCH_HOST", "localhost"),
        port=_int_with_default(source, "DASHANAN_OPENSEARCH_PORT", 9200),
        username=_str_with_default(source, "DASHANAN_OPENSEARCH_USERNAME", "admin"),
        password=_require_str(source, "DASHANAN_OPENSEARCH_PASSWORD"),
    )


@dataclass(frozen=True, slots=True)
class S3Settings:
    """Shape B S3-compatible object-store connectivity (HLD Section 2, ADR-009)."""

    endpoint_url: str
    access_key: str
    secret_key: str
    bucket: str


def load_s3_settings_from_env(env: Mapping[str, str] | None = None) -> S3Settings:
    """Load `S3Settings` from `env` (default `os.environ`).

    Raises:
        SettingsConfigurationError: If `DASHANAN_S3_ACCESS_KEY` or
            `DASHANAN_S3_SECRET_KEY` is unset or blank.
    """
    source = env if env is not None else os.environ
    return S3Settings(
        endpoint_url=_str_with_default(
            source, "DASHANAN_S3_ENDPOINT_URL", "http://localhost:9000"
        ),
        access_key=_require_str(source, "DASHANAN_S3_ACCESS_KEY"),
        secret_key=_require_str(source, "DASHANAN_S3_SECRET_KEY"),
        bucket=_str_with_default(source, "DASHANAN_S3_BUCKET", "dashanan-zone8"),
    )
