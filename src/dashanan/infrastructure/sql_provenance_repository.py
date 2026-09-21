"""SqlProvenanceRepository: the Zone 7 storage adapter (HLD Section 3.8, FR-007).

Zone 7 does not implement the `ZoneRepository` Protocol (`domain/ports.py`,
frozen per AR1-G2): per HLD Section 3.10's Data Ownership Map, Zone 7
"READS: nothing" and "IS READ BY: the scoring service ... and the read path"
by `item_id`, not via the generic `ZoneQuery`-shaped context-assembly fetch
every other zone answers. This adapter's own read shape -- "for that fact's
item_id" (AC-007) -- is therefore a bespoke method, the same pattern
`SqlEpisodicRepository.locate` already established for a query shape outside
`ZoneRepository`.

Every method validates `tenant_id` before issuing a query and every query is
parameterized (application-security-core: never string-concatenate user
input into SQL).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, cast, runtime_checkable

from dashanan.domain.exceptions import ZoneRepositoryError
from dashanan.domain.provenance_record import (
    ConflictStatus,
    ProvenanceRecord,
    ProvenanceUpdateEntry,
    SourceType,
)
from dashanan.domain.zone import ZoneId
from dashanan.infrastructure.append_only_privilege_guard import (
    verify_append_only_role_is_safe,
)

logger = logging.getLogger(__name__)

_SELECT_COLUMNS = (
    "tenant_id, provenance_id, item_id, source_zone, source_type, "
    "source_refs, write_timestamp, update_history, retrieval_context_hash, "
    "conflict_status, invalidation_flag, confidence, prev_hash, record_hash"
)

_FIND_BY_ITEM_SQL = f"""
SELECT {_SELECT_COLUMNS}
FROM provenance_records
WHERE tenant_id = %s AND item_id = %s
ORDER BY write_timestamp ASC
"""

_FIND_LATEST_BY_ITEM_SQL = f"""
SELECT {_SELECT_COLUMNS}
FROM provenance_records
WHERE tenant_id = %s AND item_id = %s
ORDER BY write_timestamp DESC
LIMIT 1
"""

_APPEND_SQL = """
INSERT INTO provenance_records
    (tenant_id, provenance_id, item_id, source_zone, source_type,
     source_refs, write_timestamp, update_history, retrieval_context_hash,
     conflict_status, invalidation_flag, confidence, prev_hash, record_hash)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


@runtime_checkable
class SqlCursor(Protocol):
    """The minimal DB-API 2.0 (PEP 249) cursor surface this adapter needs."""

    def execute(self, sql: str, params: Sequence[object]) -> object:
        """Execute a parameterized statement. Must never receive interpolated SQL."""
        ...

    def fetchall(self) -> list[tuple[object, ...]]:
        """Return every row produced by the last `execute` call."""
        ...


@runtime_checkable
class SqlConnection(Protocol):
    """The minimal DB-API 2.0 connection surface this adapter needs.

    Kept local to this infrastructure module rather than added to the
    frozen `dashanan.domain.ports` (AR1-G2) -- it is an adapter
    implementation seam, not a cross-cutting application port, mirroring
    `SqlEpisodicRepository`'s identical local-Protocol choice. The
    composition root wires a real driver's connection (e.g. psycopg2)
    against Postgres; that wiring is out of this story's scope (no
    database driver dependency is added by DASH-STORY-005).
    """

    def cursor(self) -> SqlCursor:
        """Return a new cursor bound to this connection."""
        ...


class SqlProvenanceRepository:
    """Zone 7's storage adapter: append-only, hash-chained, item_id-locatable.

    Exposes exactly two reads (`find_by_item_id`, `find_latest_by_item_id`)
    and one write (`append`) -- there is deliberately no `update`/`delete`
    method, matching `episodic_schema.sql`'s `REVOKE UPDATE, DELETE`
    (must-not-deviate item 1).
    """

    def __init__(self, connection: SqlConnection, *, verify_privileges: bool) -> None:
        """Bind the adapter to its SQL connection.

        Args:
            connection: The DB-API connection this adapter issues
                parameterized queries against.
            verify_privileges: When True, immediately checks (via
                `append_only_privilege_guard`) that the connected role
                cannot bypass `provenance_records`' append-only
                enforcement (SUPERUSER, CREATEROLE, or table ownership),
                raising `ZoneRepositoryError` if it can (DSHN-55 P1
                re-review, attempt 2 -- see this module's docstring and
                `provenance_schema.sql` for why this is defense-in-depth,
                not the primary control).

                DSHN-60 remediation, attempt 3: this parameter carries NO
                default. Attempt 2's `= False` default meant every real
                construction site -- there being no composition root any
                caller in this codebase actually invokes -- silently ran
                with the DSHN-55 defense-in-depth check DISABLED. A
                required keyword-only argument closes that silent default
                the same way `MemoryOrchestrator.tenant_credential_signing_
                key` does: every caller, test double included, must now
                write `verify_privileges=False` explicitly to accept the
                weaker posture, which is a visible, grep-able,
                code-reviewable choice rather than an invisible one. Pass
                `False` explicitly for a test double with no
                `pg_roles`/`pg_class` catalog to query; pass `True` for
                any connection backed by a real Postgres role.
        """
        self._connection = connection
        if verify_privileges:
            cursor = connection.cursor()
            verify_append_only_role_is_safe(
                cursor, "provenance_records", ZoneId.PROVENANCE.value
            )
        else:
            logger.warning(
                "SqlProvenanceRepository constructed with verify_privileges="
                "False -- the DSHN-55 defense-in-depth append-only "
                "privilege check is DISABLED for this connection. Pass "
                "verify_privileges=True at the composition root once a "
                "real production connection is wired."
            )

    def find_by_item_id(self, tenant_id: str, item_id: str) -> list[ProvenanceRecord]:
        """Return the full chronological lineage for one fact (AC-007).

        Every record ever written for `item_id` under `tenant_id`,
        oldest-first -- the shape `provenance_record.verify_chain` expects
        and the shape that satisfies AC-007's "a source attribution,
        confidence score, and edit history entry exists and is
        retrievable" for the complete history, not just the latest state.

        Raises:
            ValueError: If `tenant_id` or `item_id` is blank.
            dashanan.domain.exceptions.ZoneRepositoryError: If the
                underlying query fails.
        """
        self._require_tenant(tenant_id)
        if not item_id.strip():
            raise ValueError("find_by_item_id requires a non-blank item_id")

        rows = self._execute_or_raise(_FIND_BY_ITEM_SQL, (tenant_id, item_id))
        return [self._row_to_record(row) for row in rows]

    def find_latest_by_item_id(
        self, tenant_id: str, item_id: str
    ) -> ProvenanceRecord | None:
        """Return the current (most recently written) record for one fact.

        A convenience over `find_by_item_id` for callers that only need
        "the" current source attribution and confidence score (AC-007's
        singular reading) rather than the full lineage -- e.g. the scoring
        service reading the `ProvenanceConfidence` term (HLD Section 3.10:
        "IS READ BY: the scoring service").

        Raises:
            ValueError: If `tenant_id` or `item_id` is blank.
            dashanan.domain.exceptions.ZoneRepositoryError: If the
                underlying query fails.
        """
        self._require_tenant(tenant_id)
        if not item_id.strip():
            raise ValueError("find_latest_by_item_id requires a non-blank item_id")

        rows = self._execute_or_raise(_FIND_LATEST_BY_ITEM_SQL, (tenant_id, item_id))
        if not rows:
            return None
        return self._row_to_record(rows[0])

    def append(self, record: ProvenanceRecord) -> None:
        """Insert one provenance record. The only write this adapter exposes.

        `record` must already be a correctly-hashed, computed-confidence
        instance -- i.e. produced by `ProvenanceRecord.create`, never a
        hand-built instance -- since this method performs no recomputation;
        it persists exactly what it is given (must-not-deviate item 3's
        "never assigned once" is enforced at construction time in the
        domain layer, not re-derived here).

        Raises:
            dashanan.domain.exceptions.ZoneRepositoryError: If the insert
                fails (e.g. a PK or `record_hash` unique-constraint
                violation -- the latter would indicate a hash collision or
                a duplicate append attempt).
        """
        self._require_tenant(record.tenant_id)
        params = (
            record.tenant_id,
            record.provenance_id,
            record.item_id,
            record.source_zone.value,
            record.source_type.value,
            list(record.source_refs),
            record.write_timestamp,
            json.dumps([self._update_entry_to_dict(e) for e in record.update_history]),
            record.retrieval_context_hash,
            record.conflict_status.value,
            record.invalidation_flag,
            record.confidence,
            record.prev_hash,
            record.record_hash,
        )
        try:
            # GitHub #26: _APPEND_SQL is a plain INSERT with no RETURNING
            # clause, so (unlike this class's SELECT-issuing methods) it
            # must not go through the shared _execute_or_raise() helper --
            # that helper's cursor.fetchall() call raises
            # psycopg.ProgrammingError against a real driver when there is
            # no result set to fetch. The connection is opened with
            # autocommit=False (composition_root.build_postgres_connection),
            # so the write is explicitly committed here, mirroring
            # SqlEpisodicRepository.append()'s own identical fix. This
            # class's own SqlConnection Protocol does not declare `commit`,
            # and several existing tests pass a minimal double that
            # implements only `cursor()` -- `commit` is called only when
            # the connection actually exposes it, so a real driver
            # connection is durably committed while those doubles are
            # unaffected.
            cursor = self._connection.cursor()
            cursor.execute(_APPEND_SQL, params)
            commit = getattr(self._connection, "commit", None)
            if commit is not None:
                commit()
        except Exception as exc:  # noqa: BLE001 -- converted to typed domain error below
            raise ZoneRepositoryError(
                zone=ZoneId.PROVENANCE.value, reason=str(exc)
            ) from exc

    def _require_tenant(self, tenant_id: str) -> None:
        """Guard every query with a mandatory tenant_id (HLD 3.0 invariant 2)."""
        if not tenant_id.strip():
            raise ValueError("tenant_id is required for every Provenance zone query")

    def _execute_or_raise(
        self, sql: str, params: Sequence[object]
    ) -> list[tuple[object, ...]]:
        """Run one parameterized statement, converting any failure to a domain error."""
        try:
            cursor = self._connection.cursor()
            cursor.execute(sql, params)
            return cursor.fetchall()
        except Exception as exc:  # noqa: BLE001 -- converted to typed domain error below
            raise ZoneRepositoryError(
                zone=ZoneId.PROVENANCE.value, reason=str(exc)
            ) from exc

    def _row_to_record(self, row: tuple[object, ...]) -> ProvenanceRecord:
        """Map one result row back into the domain `ProvenanceRecord` shape.

        Every field is explicitly `cast` from the DB-API boundary's opaque
        `object` type -- `_SELECT_COLUMNS`' fixed column order is this
        method's only contract with the driver.
        """
        (
            tenant_id,
            provenance_id,
            item_id,
            source_zone,
            source_type,
            source_refs,
            write_timestamp,
            update_history,
            retrieval_context_hash,
            conflict_status,
            invalidation_flag,
            confidence,
            prev_hash,
            record_hash,
        ) = row
        return ProvenanceRecord(
            tenant_id=cast(str, tenant_id),
            provenance_id=cast(str, provenance_id),
            item_id=cast(str, item_id),
            source_zone=ZoneId(cast(str, source_zone)),
            source_type=SourceType(cast(str, source_type)),
            source_refs=tuple(cast(Sequence[str], source_refs))
            if source_refs is not None
            else (),
            write_timestamp=self._as_datetime(write_timestamp),
            update_history=tuple(
                self._dict_to_update_entry(entry)
                for entry in self._as_update_history(update_history)
            ),
            retrieval_context_hash=cast(str, retrieval_context_hash),
            conflict_status=ConflictStatus(cast(str, conflict_status)),
            invalidation_flag=cast(bool, invalidation_flag),
            confidence=cast(float, confidence),
            prev_hash=cast(str | None, prev_hash),
            record_hash=cast(str, record_hash),
        )

    @staticmethod
    def _update_entry_to_dict(entry: ProvenanceUpdateEntry) -> dict[str, object]:
        return {
            "ts": entry.ts.isoformat(),
            "actor": entry.actor,
            "change": entry.change,
            "prev_provenance_id": entry.prev_provenance_id,
        }

    @staticmethod
    def _dict_to_update_entry(entry: dict[str, object]) -> ProvenanceUpdateEntry:
        return ProvenanceUpdateEntry(
            ts=datetime.fromisoformat(cast(str, entry["ts"])),
            actor=cast(str, entry["actor"]),
            change=cast(str, entry["change"]),
            prev_provenance_id=cast(str | None, entry.get("prev_provenance_id")),
        )

    @staticmethod
    def _as_datetime(value: object) -> datetime:
        """Accept either a driver-native `datetime` or an ISO-8601 string."""
        if isinstance(value, datetime):
            return value
        return datetime.fromisoformat(str(value))

    @staticmethod
    def _as_update_history(value: object) -> list[dict[str, object]]:
        """Accept either a driver-native list (JSONB auto-decode) or a JSON string."""
        if isinstance(value, list):
            return cast(list[dict[str, object]], value)
        return cast(list[dict[str, object]], json.loads(str(value)))
