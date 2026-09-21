"""Migration runner for Shape B PostgreSQL deployment (DASH-STORY-024, FR-015).

Applies every zone-owned schema `.sql` file in this package against a real
PostgreSQL instance, in dependency order, then closes AC-024-2's production
DB-role-wiring gap for real: `dashanan_provenance_role`, `dashanan_episodic_role`,
`dashanan_semantic_role`, `dashanan_zone8_role`, and `dashanan_schema_owner` --
defined in `provenance_schema.sql` / `episodic_schema.sql` /
`semantic_schema.sql` / `zone8_consolidation_schema.sql` since
DSHN-55/DSHN-60/GitHub#22/GitHub#24 (backlog_draft.json's own FR-015 note calls
the pre-split pair `dashanan_app_role`, which does not appear anywhere in the
current schema SQL) -- get bound to real, non-default LOGIN credentials sourced
from env config (`dashanan.infrastructure.settings`), never hardcoded in a
committed file.

How each of the five named roles is actually bound to a real login credential
(read this before changing `bind_app_login_role`):

  - `dashanan_provenance_role` / `dashanan_episodic_role` / `dashanan_semantic_role`
    / `dashanan_zone8_role`: NOLOGIN by design (each schema file's own long
    comment explains why -- the application is expected to connect as its own
    LOGIN role and be GRANTed membership, never to log in as the zone role
    directly). `bind_app_login_role` creates (or updates) exactly one real
    LOGIN role from `PostgresSettings.app_login_user` / `.app_login_password`
    -- sourced entirely from env config -- and GRANTs it membership in all four.
  - `dashanan_schema_owner`: also NOLOGIN by design, and explicitly
    "never-granted-out" per `provenance_schema.sql`'s own long comment --
    inventing a second persistent LOGIN role granted membership in it here
    would directly contradict that design. Instead, each schema file's own
    DDL (applied by `apply_schema_files`, below, connected as
    `PostgresSettings.migration_user` -- itself a real, env-sourced LOGIN
    credential) grants `CURRENT_USER` (the migration role) brief membership,
    performs `ALTER ... OWNER TO dashanan_schema_owner`, and revokes that
    membership again before the file ends. The real login credential
    `dashanan_schema_owner` is bound to at migration time is therefore
    `migration_user` -- itself real and env-sourced -- not a role this module
    invents.

This module deliberately does not reach into a single schema file's DDL to
implement that binding differently; it relies on the contract each schema
file's own long comment already documents, and only supplies the two real
login credentials (migration and app) that make that contract meaningful
end-to-end.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path

import psycopg
from psycopg import sql

from dashanan.domain.exceptions import DashananError
from dashanan.infrastructure.settings import (
    PostgresSettings,
    load_postgres_settings_from_env,
)

logger = logging.getLogger(__name__)


class MigrationConnectionError(DashananError):
    """Raised when connecting to PostgreSQL for a migration run fails.

    Wraps every `psycopg.Error` `run_migrations` can raise while opening its
    connection. `psycopg.Error`'s own message can include a fragment of the
    connection string (host, dbname, and -- on some libpq builds -- user)
    that reached it; this type carries only a fixed, generic message so a
    caller that logs or re-raises it never leaks that fragment, matching
    `settings.py`'s own "never logs a decoded credential" promise. The
    original `psycopg.Error` is chained via `__cause__` for local debugging
    only (never rendered in this exception's own message).
    """

_SCHEMA_DIR = Path(__file__).resolve().parent

# Dependency order, per each file's own header comment:
#   - episodic_schema.sql / provenance_schema.sql: independent of each other,
#     each creates its own per-zone role plus the shared dashanan_schema_owner.
#   - semantic_schema.sql: its own header comment names dashanan_episodic_role
#     and dashanan_provenance_role as already-established precedent for its own
#     dashanan_semantic_role -- no executable dependency, but run after both
#     for that documented precedent to be literally true at run time too.
#   - write_journal_schema.sql: independent (REVOKE-only, no per-zone role or
#     ownership reassignment).
#   - zone8_consolidation_schema.sql: independent (REVOKE-only, no ownership
#     reassignment -- no trigger to protect -- but does create its own
#     dashanan_zone8_role, GitHub #22, following the same pattern as
#     episodic_schema.sql/semantic_schema.sql).
#   - zone8_manifest_subject_index_migration.sql: an ADD COLUMN / CREATE INDEX
#     migration against zone8_consolidation_schema.sql's own zone8_manifest
#     table (its own header comment) -- MUST run after that file.
SCHEMA_FILES: Sequence[str] = (
    "episodic_schema.sql",
    "provenance_schema.sql",
    "semantic_schema.sql",
    "write_journal_schema.sql",
    "zone8_consolidation_schema.sql",
    "zone8_manifest_subject_index_migration.sql",
)

# The four roles this module knows about; all NOLOGIN and the membership
# targets `bind_app_login_role` grants the real app login role into.
# `dashanan_schema_owner` is deliberately excluded here -- see this module's
# own docstring for why it is bound differently.
#
# `dashanan_semantic_role` (GitHub #24) and `dashanan_zone8_role` (GitHub #22)
# were both created by their own schema files from day one but never added
# here -- the real app login role had no grant path to either until now.
_PER_ZONE_LOGIN_MEMBER_ROLES: Sequence[str] = (
    "dashanan_provenance_role",
    "dashanan_episodic_role",
    "dashanan_semantic_role",
    "dashanan_zone8_role",
)


def apply_schema_files(
    connection: psycopg.Connection,
    *,
    schema_dir: Path = _SCHEMA_DIR,
    schema_files: Sequence[str] = SCHEMA_FILES,
) -> list[str]:
    """Apply every schema `.sql` file in `schema_files`, in order, one per transaction.

    Each file is executed as a single multi-statement batch (`cursor.execute`
    against the whole file's text -- every schema file already wraps its own
    conditional DDL in `DO $$ ... $$` blocks, so no client-side statement
    splitting is needed or attempted) and committed before the next file
    starts, so a failure partway through leaves only fully-applied files
    committed, never a half-applied one.

    Args:
        connection: An open `psycopg.Connection`, connected as the migration
            role (`PostgresSettings.migration_user`).
        schema_dir: Directory the schema files are read from. Defaults to
            this module's own package directory (where every
            `dashanan.infrastructure.*_schema.sql` file already lives).
        schema_files: The ordered list of file names to apply. Defaults to
            `SCHEMA_FILES`.

    Returns:
        The list of file names actually applied, in the order applied.

    Raises:
        psycopg.Error: Unchanged, on any DDL failure -- fails closed, no
            partial success is swallowed or retried silently.
    """
    applied: list[str] = []
    for filename in schema_files:
        path = schema_dir / filename
        sql_text = path.read_text(encoding="utf-8")
        with connection.cursor() as cursor:
            cursor.execute(sql_text)
        connection.commit()
        applied.append(filename)
        logger.info("Applied Shape B migration file", extra={"file": filename})
    return applied


def bind_app_login_role(
    connection: psycopg.Connection, settings: PostgresSettings
) -> None:
    """Bind the per-zone roles to a real, env-sourced LOGIN credential (AC-024-2).

    Idempotently creates (or updates the password of) exactly one real LOGIN
    role from `settings.app_login_user` / `settings.app_login_password` --
    sourced entirely from env config, never hardcoded -- and grants it
    membership in every role named in `_PER_ZONE_LOGIN_MEMBER_ROLES`.

    Role and member-role names are fixed, hardcoded identifiers controlled by
    this codebase (never derived from external input), quoted via
    `psycopg.sql.Identifier`. `app_login_password` -- the one field that is
    genuinely externally supplied -- is rendered via `psycopg.sql.Literal`,
    which performs the same client-side escaping/quoting a bind parameter
    would (application-security-core: never string-concatenate untrusted
    input into SQL), but as a properly quoted literal rather than a `$1`
    bind placeholder: PostgreSQL's `CREATE ROLE` / `ALTER ROLE ... PASSWORD`
    grammar does not accept extended-protocol bind parameters in that
    position (confirmed by this story's own dev pass against a real
    PostgreSQL 16 instance -- a `%s`/`$1` placeholder there is a syntax
    error, not merely a style preference), so `sql.Literal` is the correct
    -- and only -- safe-quoting mechanism for this specific statement shape.

    Args:
        connection: An open `psycopg.Connection`, connected as the migration
            role. Must be connected AFTER `apply_schema_files` has run, since
            the per-zone roles this function grants membership in do not
            exist until their owning schema file creates them.
        settings: The env-sourced settings carrying the real app login
            credential to create/update.

    Raises:
        psycopg.Error: Unchanged, if role creation or the membership grants
            fail (e.g. a per-zone role does not exist because
            `apply_schema_files` was not run first).
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s", (settings.app_login_user,)
        )
        role_exists = cursor.fetchone() is not None

        if role_exists:
            cursor.execute(
                sql.SQL("ALTER ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(settings.app_login_user),
                    sql.Literal(settings.app_login_password),
                )
            )
            logger.info(
                "Updated Shape B app login role password",
                extra={"role": settings.app_login_user},
            )
        else:
            cursor.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(settings.app_login_user),
                    sql.Literal(settings.app_login_password),
                )
            )
            logger.info(
                "Created Shape B app login role",
                extra={"role": settings.app_login_user},
            )

        for member_role in _PER_ZONE_LOGIN_MEMBER_ROLES:
            cursor.execute(
                sql.SQL("GRANT {} TO {}").format(
                    sql.Identifier(member_role),
                    sql.Identifier(settings.app_login_user),
                )
            )
            logger.info(
                "Granted per-zone role membership to Shape B app login role",
                extra={"member_role": member_role, "role": settings.app_login_user},
            )
    connection.commit()


def run_migrations(settings: PostgresSettings) -> list[str]:
    """Entry point: connect as the migration role, apply every schema file, bind login.

    Args:
        settings: Env-sourced `PostgresSettings` (see
            `dashanan.infrastructure.settings.load_postgres_settings_from_env`).

    Returns:
        The list of schema file names applied, in order (from
        `apply_schema_files`).

    Raises:
        MigrationConnectionError: If opening the connection itself fails
            (host unreachable, authentication rejected, database does not
            exist, etc.) -- wraps the underlying `psycopg.Error` without
            exposing its message, which can contain a connection-string
            fragment.
        psycopg.Error: Unchanged, on any DDL/role failure once the
            connection is open (those failures do not carry a credential in
            their message the way a connection failure can).
    """
    try:
        connection = psycopg.connect(settings.migration_dsn())
    except psycopg.Error as exc:
        logger.error("Shape B migration run failed to connect to PostgreSQL")
        raise MigrationConnectionError(
            "Failed to connect to PostgreSQL for the migration run. "
            "See server-side logs for detail; this error intentionally "
            "carries no connection-string or credential content."
        ) from exc
    with connection:
        applied = apply_schema_files(connection)
        bind_app_login_role(connection, settings)
    return applied


def main() -> None:
    """CLI entry point: `python -m dashanan.infrastructure.migration_runner`."""
    logging.basicConfig(level=logging.INFO)
    settings = load_postgres_settings_from_env()
    applied = run_migrations(settings)
    logger.info(
        "Shape B migration run complete",
        extra={"files_applied": applied, "count": len(applied)},
    )


if __name__ == "__main__":
    main()
