"""SqlManifestRepository: the ADR-009 hot-manifest-index storage adapter.

Implements `dashanan.application.zone8_consolidation_store.ManifestPort`
against `zone8_consolidation_schema.sql`'s `zone8_manifest` table
(DASH-STORY-018, traces to FR-008).

Every method validates `tenant_id` before issuing a query and every query
is parameterized (application-security-core: never string-concatenate
user input into SQL) -- the same discipline `SqlProvenanceRepository` and
`SqlWriteJournalRepository` already established.

`insert_batch`'s `INSERT` also writes `subject_id`
(`zone8_manifest_subject_index_migration.sql`'s column, DASH-STORY-020,
ADR-006) in the same statement as every other field. This closes the
append-only-invariant defect a separate `SqlZone8SubjectIndexRepository.
record_item` `UPDATE` previously caused against this table -- see that
module's own docstring -- by fulfilling DASH-STORY-020's own
shared_file_request for "the fully-integrated version of this column ...
written in the same INSERT that already writes every other column."
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, cast, runtime_checkable

from dashanan.application.zone8_consolidation_store import Zone8StorePortError
from dashanan.domain.consolidated_blob import ByteRange, ManifestEntry
from dashanan.domain.zone import ZoneId

_SELECT_COLUMNS = (
    "tenant_id, item_id, blob_id, byte_start, byte_end, "
    "compression_generation, source_zone, written_at"
)

_FIND_BY_ITEM_ID_SQL = f"""
SELECT {_SELECT_COLUMNS}
FROM zone8_manifest
WHERE tenant_id = %s AND item_id = %s
"""

# Writes subject_id (DASH-STORY-020, ADR-006) in this SAME INSERT, alongside
# every other column -- the fully-integrated fix `zone8_manifest_subject_index_
# migration.sql`'s own comment names as the sanctioned resolution for the
# append-only violation a separate UPDATE-based backfill previously caused (a
# zone8_manifest row is never mutated after this one INSERT, per
# zone8_consolidation_schema.sql's REVOKE UPDATE, DELETE). This column exists
# only once that migration's ALTER TABLE ... ADD COLUMN IF NOT EXISTS
# subject_id has run against the target database -- that migration is this
# adapter's precondition, not something this module re-declares.
_INSERT_SQL = """
INSERT INTO zone8_manifest
    (tenant_id, item_id, blob_id, byte_start, byte_end,
     compression_generation, source_zone, written_at, subject_id)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


@runtime_checkable
class SqlCursor(Protocol):
    """The minimal DB-API 2.0 (PEP 249) cursor surface this adapter needs."""

    def execute(self, sql: str, params: Sequence[object]) -> object:
        """Execute a parameterized statement. Must never receive interpolated SQL."""
        ...

    def fetchone(self) -> Sequence[object] | None:
        """Return the next row of the last `execute()`'s result, or `None`."""
        ...


@runtime_checkable
class SqlConnection(Protocol):
    """The minimal DB-API 2.0 connection surface this adapter needs.

    Kept local to this infrastructure module rather than added to the
    frozen `dashanan.domain.ports` (AR1-G2) -- an adapter implementation
    seam, not a cross-cutting application port, mirroring
    `SqlProvenanceRepository`'s and `SqlWriteJournalRepository`'s
    identical local-Protocol choice. The composition root wires a real
    driver's connection (e.g. psycopg2) against Postgres; that wiring is
    a shared_file_request out of this story's own scope (see this
    story's dev report).

    `commit` is required: `insert_batch` below issues every row's INSERT
    on the same connection and commits once at the end, so a partial
    failure mid-batch leaves nothing committed (`ManifestPort.
    insert_batch`'s own "atomically insert every entry, or none of
    them" contract) provided the connection is not itself running in
    autocommit mode -- the composition root wiring that connection is
    responsible for that setting, mirroring `SqlWriteJournalRepository`'s
    identical assumption.
    """

    def cursor(self) -> SqlCursor:
        """Return a new cursor bound to this connection."""
        ...

    def commit(self) -> None:
        """Durably commit every statement issued on this connection so far."""
        ...


class SqlManifestRepository:
    """Zone 8's hot-manifest adapter: one indexed lookup, one atomic batch insert.

    Exposes exactly the two `ManifestPort` methods -- there is
    deliberately no `update`/`delete`, matching
    `zone8_consolidation_schema.sql`'s `REVOKE UPDATE, DELETE`
    (must-not-deviate item 3: a manifest row for an immutable,
    content-addressed blob is itself never mutated once written).
    """

    def __init__(self, connection: SqlConnection) -> None:
        """Bind the adapter to its SQL connection.

        Args:
            connection: The DB-API connection this adapter issues its
                parameterized queries against.
        """
        self._connection = connection

    def find_by_item_id(self, tenant_id: str, item_id: str) -> ManifestEntry | None:
        """AC-008-1: resolve `item_id` via a single indexed point lookup.

        The underlying query is a `(tenant_id, item_id)` primary-key
        lookup against `zone8_consolidation_schema.sql`'s PK -- O(1)/
        O(log n), never a scan of every manifest row.

        Raises:
            ValueError: If `tenant_id` or `item_id` is blank.
            Zone8StorePortError: If the underlying query fails.
        """
        self._require_tenant(tenant_id)
        if not item_id.strip():
            raise ValueError("find_by_item_id requires a non-blank item_id")

        row = self._execute_and_fetchone(_FIND_BY_ITEM_ID_SQL, (tenant_id, item_id))
        if row is None:
            return None
        return self._row_to_entry(row)

    def insert_batch(self, entries: Sequence[ManifestEntry]) -> None:
        """Insert every entry in `entries` on one connection, then commit once.

        Raises:
            Zone8StorePortError: If any insert fails (e.g. a
                `(tenant_id, item_id)` primary-key violation, meaning
                `item_id` was already archived, or the connection is
                unreachable). Nothing is committed when this raises,
                provided the connection is not in autocommit mode (see
                `SqlConnection.commit`'s docstring).
        """
        if not entries:
            return
        try:
            cursor = self._connection.cursor()
            for entry in entries:
                cursor.execute(
                    _INSERT_SQL,
                    (
                        entry.tenant_id,
                        entry.item_id,
                        entry.blob_id,
                        entry.byte_range.start,
                        entry.byte_range.end,
                        entry.compression_generation,
                        entry.source_zone.value,
                        entry.written_at,
                        entry.subject_id,
                    ),
                )
            self._connection.commit()
        except Exception as exc:  # noqa: BLE001 -- converted to typed domain error below
            raise Zone8StorePortError(
                f"SqlManifestRepository.insert_batch failed: {exc}"
            ) from exc

    def _require_tenant(self, tenant_id: str) -> None:
        """Guard every query with a mandatory tenant_id (HLD 3.0 invariant 2)."""
        if not tenant_id.strip():
            raise ValueError("tenant_id is required for every Zone 8 manifest query")

    def _execute_and_fetchone(
        self, sql: str, params: Sequence[object]
    ) -> Sequence[object] | None:
        """Run one parameterized SELECT, converting any failure to a domain error."""
        try:
            cursor = self._connection.cursor()
            cursor.execute(sql, params)
            return cursor.fetchone()
        except Exception as exc:  # noqa: BLE001 -- converted to typed domain error below
            raise Zone8StorePortError(
                f"SqlManifestRepository query failed: {exc}"
            ) from exc

    def _row_to_entry(self, row: Sequence[object]) -> ManifestEntry:
        """Map one result row back into the domain `ManifestEntry` shape."""
        (
            tenant_id,
            item_id,
            blob_id,
            byte_start,
            byte_end,
            compression_generation,
            source_zone,
            written_at,
        ) = row
        return ManifestEntry(
            tenant_id=cast(str, tenant_id),
            item_id=cast(str, item_id),
            blob_id=cast(str, blob_id),
            byte_range=ByteRange(start=cast(int, byte_start), end=cast(int, byte_end)),
            compression_generation=cast(int, compression_generation),
            source_zone=ZoneId(cast(str, source_zone)),
            written_at=self._as_datetime(written_at),
        )

    @staticmethod
    def _as_datetime(value: object) -> datetime:
        """Accept either a driver-native `datetime` or an ISO-8601 string."""
        if isinstance(value, datetime):
            return value
        return datetime.fromisoformat(str(value))
