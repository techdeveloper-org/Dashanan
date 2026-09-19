"""Runtime guard verifying a connected DB role cannot bypass append-only DDL.

DSHN-55 P1 re-review, attempt 2: the re-review's evidence for both the
CRITICAL (Zone 7) and HIGH (Zone 2) findings included that
`dashanan_app_role` "is referenced nowhere in the repo outside this SQL
file and the regression test -- no connection string, composition root,
docker-compose, or env config wires any actual login role to it." The
actual wiring of a real login role's membership is a composition-root
concern this schema-only story does not own (see `SqlProvenanceRepository`
and `SqlEpisodicRepository`'s docstrings), so this module cannot fix that
gap by itself. What it adds instead is a concrete, callable, testable
check that a composition root -- or these adapters themselves, opted in
via their `verify_privileges` constructor parameter -- can run against a
live connection to fail closed rather than silently trust that
provisioning was done correctly.

This is defense-in-depth alongside, not a replacement for,
`provenance_schema.sql` / `episodic_schema.sql`'s own `dashanan_schema_owner`
ownership reassignment, which is what unconditionally closes the
re-review's reproduced ALTER TABLE ... DISABLE TRIGGER exploit regardless
of whether any caller ever invokes this guard.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from dashanan.domain.exceptions import ZoneRepositoryError

_ROLE_PRIVILEGE_SQL = """
SELECT r.rolsuper, r.rolcreaterole, (c.relowner = r.oid) AS is_table_owner
FROM pg_roles r
CROSS JOIN pg_class c
WHERE r.rolname = current_user AND c.relname = %s
"""


@runtime_checkable
class PrivilegeCheckCursor(Protocol):
    """The minimal DB-API 2.0 cursor surface this guard needs."""

    def execute(self, sql: str, params: Sequence[object]) -> object:
        """Execute a parameterized statement."""
        ...

    def fetchall(self) -> list[tuple[object, ...]]:
        """Return every row produced by the last `execute` call."""
        ...


def verify_append_only_role_is_safe(
    cursor: PrivilegeCheckCursor, table_name: str, zone: str
) -> None:
    """Fail closed if the connected role can bypass append-only enforcement.

    Queries `pg_roles`/`pg_class` for the CURRENT connection's role and
    raises `ZoneRepositoryError` if that role holds SUPERUSER, holds
    CREATEROLE, or owns `table_name` -- every privilege class that lets a
    role run `ALTER TABLE ... DISABLE TRIGGER` (or, for CREATEROLE,
    self-escalate into a role that can) regardless of the table-level
    GRANT/REVOKE state the schema otherwise enforces.

    Args:
        cursor: An open cursor on the connection to check.
        table_name: The append-only table to check ownership against
            (`provenance_records` or `episodic_entries`).
        zone: The zone identifier used in the raised error, matching the
            `ZoneRepositoryError` shape the calling adapter already uses.

    Raises:
        dashanan.domain.exceptions.ZoneRepositoryError: If the privilege
            query itself fails, or if the connected role holds any
            privilege class that can bypass append-only enforcement.
    """
    try:
        cursor.execute(_ROLE_PRIVILEGE_SQL, (table_name,))
        rows = cursor.fetchall()
    except Exception as exc:  # noqa: BLE001 -- converted to typed domain error below
        raise ZoneRepositoryError(
            zone=zone, reason=f"append-only privilege check failed: {exc}"
        ) from exc

    for is_superuser, is_createrole, is_table_owner in rows:
        if is_superuser or is_createrole or is_table_owner:
            raise ZoneRepositoryError(
                zone=zone,
                reason=(
                    f"connected role can bypass append-only enforcement on "
                    f"{table_name} (SUPERUSER={bool(is_superuser)}, "
                    f"CREATEROLE={bool(is_createrole)}, "
                    f"is_table_owner={bool(is_table_owner)}) -- connect as a "
                    f"LOGIN role that holds none of these and is granted "
                    f"membership only in this zone's own least-privilege "
                    f"role (`dashanan_provenance_role` for Zone 7, "
                    f"`dashanan_episodic_role` for Zone 2 -- DSHN-60 "
                    f"narrowed these from one role shared across every "
                    f"zone's schema to one distinct role per zone)"
                ),
            )
