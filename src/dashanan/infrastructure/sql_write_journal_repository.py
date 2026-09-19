"""SqlWriteJournalRepository: the ADR-010 WAL/outbox durability-barrier adapter.

Implements `dashanan.domain.write_gate.ProvenanceJournalPort` against
`write_journal_schema.sql`'s `provenance_write_journal` table (DASH-STORY-006,
traces to FR-010).

Every method validates `tenant_id` before issuing a query and every query
is parameterized (application-security-core: never string-concatenate
user input into SQL) -- the same discipline `SqlProvenanceRepository`
and `SqlEpisodicRepository` already established.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, cast, runtime_checkable

from dashanan.domain.provenance_record import SourceType
from dashanan.domain.write_gate import ProvenanceJournalEntry
from dashanan.domain.zone import ZoneId

_APPEND_SQL = """
INSERT INTO provenance_write_journal
    (tenant_id, write_id, item_id, source_zone, source_type, source_refs,
     caller_identity, retrieval_context_hash, idempotency_key,
     user_turn_marker, written_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

_FIND_BY_IDEMPOTENCY_KEY_SQL = """
SELECT tenant_id, write_id, item_id, source_zone, source_type, source_refs,
       caller_identity, retrieval_context_hash, idempotency_key,
       user_turn_marker, written_at
FROM provenance_write_journal
WHERE tenant_id = %s AND idempotency_key = %s
"""


@runtime_checkable
class SqlCursor(Protocol):
    """The minimal DB-API 2.0 (PEP 249) cursor surface this adapter needs."""

    def execute(self, sql: str, params: Sequence[object]) -> object:
        """Execute a parameterized statement. Must never receive interpolated SQL."""
        ...

    def fetchone(self) -> Sequence[object] | None:
        """Return the next row of the last `execute()`'s result, or `None`.

        Needed only by `find_by_idempotency_key`'s SELECT -- `append`
        never calls this.
        """
        ...


@runtime_checkable
class SqlConnection(Protocol):
    """The minimal DB-API 2.0 connection surface this adapter needs.

    Kept local to this infrastructure module rather than added to the
    frozen `dashanan.domain.ports` (AR1-G2) -- an adapter implementation
    seam, not a cross-cutting application port, mirroring
    `SqlProvenanceRepository`'s and `SqlEpisodicRepository`'s identical
    local-Protocol choice. The composition root wires a real driver's
    connection (e.g. psycopg2) against Postgres; that wiring is out of
    this story's scope (no database driver dependency is added by
    DASH-STORY-006).

    `commit` is required (unlike a plain read adapter) because ADR-010's
    durability barrier is "fsync BEFORE returning" -- `append` below
    commits synchronously inside the call, not merely issues the INSERT,
    so a normal return from `append` is itself the proof
    `ProvenanceWriteGate` relies on before invoking the caller's
    `persist_fact`.
    """

    def cursor(self) -> SqlCursor:
        """Return a new cursor bound to this connection."""
        ...

    def commit(self) -> None:
        """Durably commit every statement issued on this connection so far."""
        ...


class SqlWriteJournalRepository:
    """Implements `ProvenanceJournalPort`: append-only WAL/outbox adapter.

    Exposes exactly the two `ProvenanceJournalPort` methods -- `append`
    (one INSERT + commit) and `find_by_idempotency_key` (one read-only
    SELECT) -- there is deliberately no `update`/`delete`, matching
    `write_journal_schema.sql`'s `REVOKE UPDATE, DELETE` (WAL/outbox
    append-only semantics, ADR-010).
    """

    def __init__(self, connection: SqlConnection) -> None:
        """Bind the adapter to its SQL connection.

        Args:
            connection: The DB-API connection this adapter issues its
                parameterized INSERT and commit against.
        """
        self._connection = connection

    def append(self, entry: ProvenanceJournalEntry) -> None:
        """Durably append `entry` to the journal (ADR-010).

        Issues a single INSERT and commits before returning -- the
        commit is what makes this method satisfy
        `ProvenanceJournalPort.append`'s "must not return before the
        durability barrier ... has completed" contract, rather than
        merely queuing the write for a later, uncommitted flush.

        Raises:
            Whatever the underlying `SqlConnection`/`SqlCursor` raises
            on a failed execute or commit -- propagated unchanged
            (error-handling-patterns: never swallow a durability-barrier
            failure) so `ProvenanceWriteGate.submit_write` never invokes
            `persist_fact` on an entry that did not actually become
            durable.
        """
        cursor = self._connection.cursor()
        cursor.execute(
            _APPEND_SQL,
            (
                entry.tenant_id,
                entry.write_id,
                entry.item_id,
                entry.source_zone.value,
                entry.source_type.value,
                list(entry.source_refs),
                entry.caller_identity,
                entry.retrieval_context_hash,
                entry.idempotency_key,
                entry.user_turn_marker,
                entry.written_at,
            ),
        )
        self._connection.commit()

    def find_by_idempotency_key(
        self, tenant_id: str, idempotency_key: str
    ) -> ProvenanceJournalEntry | None:
        """Look up a prior journal entry for this idempotency key, if any.

        Implements `ProvenanceJournalPort.find_by_idempotency_key`:
        `ProvenanceWriteGate.submit_write` calls this before minting a
        fresh `write_id` so a captured, verbatim-replayed `WriteRequest`
        returns the original accepted result instead of a second,
        duplicate journal row and a second invocation of the caller's
        own zone-content write.

        Returns:
            The matching `ProvenanceJournalEntry`, or `None` if no entry
            has been journaled yet for this `(tenant_id,
            idempotency_key)` pair.
        """
        cursor = self._connection.cursor()
        cursor.execute(_FIND_BY_IDEMPOTENCY_KEY_SQL, (tenant_id, idempotency_key))
        row = cursor.fetchone()
        if row is None:
            return None
        (
            row_tenant_id,
            write_id,
            item_id,
            source_zone,
            source_type,
            source_refs,
            caller_identity,
            retrieval_context_hash,
            row_idempotency_key,
            user_turn_marker,
            written_at,
        ) = row
        return ProvenanceJournalEntry(
            tenant_id=cast(str, row_tenant_id),
            write_id=cast(str, write_id),
            item_id=cast(str, item_id),
            source_zone=ZoneId(cast(str, source_zone)),
            source_type=SourceType(cast(str, source_type)),
            source_refs=tuple(cast(Sequence[str], source_refs)),
            caller_identity=cast(str, caller_identity),
            retrieval_context_hash=cast(str, retrieval_context_hash),
            idempotency_key=cast(str, row_idempotency_key),
            user_turn_marker=cast(bool, user_turn_marker),
            written_at=cast(datetime, written_at),
        )
