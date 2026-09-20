"""SqlZone8SubjectIndexRepository: the ADR-006 Shape B `Zone8SubjectIndexPort` adapter.

Queries the `subject_id` column
`zone8_manifest_subject_index_migration.sql` adds to
`zone8_consolidation_schema.sql`'s `zone8_manifest` table
(DASH-STORY-018). Defines its own minimal `SqlCursor`/`SqlConnection`
Protocol pair rather than importing `sql_manifest_repository.py`'s --
that module's own docstring states its choice to keep these Protocols
"local to this infrastructure module rather than added to the frozen
dashanan.domain.ports", and this adapter needs `fetchall`, a method that
module's narrower `SqlCursor` does not declare; defining a second local
copy here follows that same established "kept local per adapter"
convention rather than widening someone else's file-scoped Protocol.

Every query is parameterized (application-security-core: never
string-concatenate user input into SQL), mirroring
`SqlManifestRepository`'s own discipline.

APPEND-ONLY FIX (DASH-STORY-020, DSHN-69): an earlier version of
`record_item` issued a live `UPDATE` against `zone8_manifest` to backfill
`subject_id` onto an already-archived row. `zone8_consolidation_schema.
sql` declares `zone8_manifest` append-only (`REVOKE UPDATE, DELETE ...
FROM PUBLIC`, DASH-STORY-018, must-not-deviate item 3) -- that `UPDATE`
was a real privilege violation against a real database enforcing that
grant, not merely a style issue. The fix is DASH-STORY-020's own
shared_file_request, now applied: `SqlManifestRepository.insert_batch`
(DASH-STORY-018) writes `subject_id` in the SAME `INSERT` that writes
every other column (`dashanan.domain.consolidated_blob.ArchiveBatchItem.
subject_id` -> `ManifestEntry.subject_id`, threaded through by
`Zone8SubjectKeyedArchiver.archive_for_subject` before it ever calls
`Zone8ConsolidationStore.consolidate_batch`). `record_item` below is
therefore a no-op for THIS adapter -- the row already carries
`subject_id` by the time `record_item` would be called -- kept only so
`Zone8SubjectIndexPort`'s single call site
(`Zone8SubjectKeyedArchiver.archive_for_subject`) can invoke it
unconditionally across both this adapter and Shape A's
`InMemoryZone8SubjectIndex`, whose separate in-process index genuinely
still needs the call.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from dashanan.application.zone8_crypto_shredding_store import Zone8CryptoShreddingStoreError

_FIND_ITEM_IDS_SQL = """
SELECT item_id
FROM zone8_manifest
WHERE tenant_id = %s AND subject_id = %s
"""


@runtime_checkable
class SqlCursor(Protocol):
    """The minimal DB-API 2.0 (PEP 249) cursor surface this adapter needs."""

    def execute(self, sql: str, params: Sequence[object]) -> object:
        """Execute a parameterized statement. Must never receive interpolated SQL."""
        ...

    def fetchall(self) -> Sequence[Sequence[object]]:
        """Return every remaining row of the last `execute()`'s result set."""
        ...


@runtime_checkable
class SqlConnection(Protocol):
    """The minimal DB-API 2.0 connection surface this adapter needs.

    No `commit` method is declared: every statement this adapter issues
    is a read (`find_item_ids`'s `SELECT`) -- `record_item` performs no
    SQL of its own (see this module's docstring), so there is nothing
    left for this adapter to commit.
    """

    def cursor(self) -> SqlCursor:
        """Return a new cursor bound to this connection."""
        ...


class SqlZone8SubjectIndexRepository:
    """Zone 8's ADR-006 subject_id index adapter: one indexed SELECT, no write of its own.

    `subject_id` reaches `zone8_manifest` via `SqlManifestRepository.
    insert_batch`'s own `INSERT` (DASH-STORY-018's file, not touched
    here) -- this adapter only ever reads that column back. There is
    deliberately no `UPDATE`/`DELETE` method on this class, matching
    `zone8_consolidation_schema.sql`'s `REVOKE UPDATE, DELETE` (a
    `zone8_manifest` row for an immutable, content-addressed blob is
    never mutated once written).
    """

    def __init__(self, connection: SqlConnection) -> None:
        self._connection = connection

    def record_item(self, tenant_id: str, subject_id: str, item_id: str) -> None:
        """No-op for this adapter: `subject_id` already reached the row via the INSERT.

        Validates its arguments and returns, issuing no SQL -- kept so
        `Zone8SubjectIndexPort.record_item`'s single call site
        (`Zone8SubjectKeyedArchiver.archive_for_subject`) can call every
        conforming adapter uniformly, including Shape A's
        `InMemoryZone8SubjectIndex`, whose separate in-process index
        still requires this call to populate it.

        Raises:
            Zone8CryptoShreddingStoreError: If any argument is blank.
        """
        self._require_non_blank(tenant_id, subject_id, item_id)

    def find_item_ids(self, tenant_id: str, subject_id: str) -> tuple[str, ...]:
        """AC-008-DPDP-1: resolve every Zone 8 item_id archived under `subject_id`.

        The underlying query uses `idx_zone8_manifest_subject`
        (`zone8_manifest_subject_index_migration.sql`) -- an indexed
        `(tenant_id, subject_id)` lookup, never a full-table scan.

        Raises:
            Zone8CryptoShreddingStoreError: If either argument is blank,
                or the query fails.
        """
        if not tenant_id.strip():
            raise Zone8CryptoShreddingStoreError(
                "find_item_ids requires a non-blank tenant_id"
            )
        if not subject_id.strip():
            raise Zone8CryptoShreddingStoreError(
                "find_item_ids requires a non-blank subject_id"
            )
        try:
            cursor = self._connection.cursor()
            cursor.execute(_FIND_ITEM_IDS_SQL, (tenant_id, subject_id))
            rows = cursor.fetchall()
        except Exception as exc:  # noqa: BLE001 -- converted to typed domain error below
            raise Zone8CryptoShreddingStoreError(
                f"SqlZone8SubjectIndexRepository.find_item_ids failed: {exc}"
            ) from exc
        return tuple(str(row[0]) for row in rows)

    @staticmethod
    def _require_non_blank(tenant_id: str, subject_id: str, item_id: str) -> None:
        if not tenant_id.strip():
            raise Zone8CryptoShreddingStoreError(
                "record_item requires a non-blank tenant_id"
            )
        if not subject_id.strip():
            raise Zone8CryptoShreddingStoreError(
                "record_item requires a non-blank subject_id"
            )
        if not item_id.strip():
            raise Zone8CryptoShreddingStoreError(
                "record_item requires a non-blank item_id"
            )
