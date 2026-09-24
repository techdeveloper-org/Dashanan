"""SqlSubjectToItemIndexRepository: the real `SubjectToItemIndex` adapter (DASH-STORY-027-DEV).

Traces to AC-013 (SRS.md, verbatim): "Given a data subject requests
erasure, When the erasure cascade executes across all 8 zones plus
indices and archives, Then the subject's payload content becomes
permanently unrecoverable via crypto-shredding while the Zone 7
provenance chain structure (hashes, timestamps) remains intact and
verifiable."

Fulfils `dashanan.application.unified_subject_erasure_orchestrator.
SubjectToItemIndex` -- the dependency-inversion seam that Protocol's own
docstring names as "the dependency-inversion seam a future story's real
subject-to-item resolver satisfies" -- replacing
`NullSubjectToItemIndex` for Zone 2/6, the orchestrator's own documented
scope for this Protocol.

Backed by `subject_item_index_schema.sql`'s `subject_item_index` table.
Queries are parameterized throughout (application-security-core: never
string-concatenate external input into SQL), mirroring
`SqlZone8SubjectIndexRepository`'s identical discipline; this module
defines its own local minimal `SqlCursor`/`SqlConnection` pair for the
same reason that module's own docstring gives (needs `fetchall`, which
`sql_manifest_repository.py`'s narrower Protocol does not declare).

PII NOTE: `tenant_id`/`subject_id`/`zone`/`item_id` are opaque routing
identifiers throughout (mirrors every other subject-index adapter in
this codebase); this module never logs payload content.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from dashanan.application.zone8_crypto_shredding_store import Zone8CryptoShreddingStoreError

_ZONE2_6_LEG_ZONES: tuple[str, ...] = ("zone2", "zone6")
"""The zones `items_for_subject` resolves for -- the orchestrator's own Zone 2/6 leg scope."""

_ITEMS_FOR_SUBJECT_SQL = """
SELECT item_id
FROM subject_item_index
WHERE tenant_id = %s AND subject_id = %s AND zone = ANY(%s)
"""

_RECORD_ITEM_SQL = """
INSERT INTO subject_item_index (tenant_id, subject_id, zone, item_id)
VALUES (%s, %s, %s, %s)
ON CONFLICT (tenant_id, zone, item_id) DO NOTHING
"""

_DELETE_FOR_SUBJECT_AND_ZONE_SQL = """
DELETE FROM subject_item_index
WHERE tenant_id = %s AND subject_id = %s AND zone = %s
"""


class SubjectToItemIndexError(Zone8CryptoShreddingStoreError):
    """Raised by a `SqlSubjectToItemIndexRepository` operational failure.

    Subclasses `Zone8CryptoShreddingStoreError` rather than introducing a
    third, near-identical local error type for what is structurally the
    same failure class (a subject-index port unreachable/query failure)
    that `SqlZone8SubjectIndexRepository` already raises for its own
    sibling index table -- callers that already catch that type (the
    orchestrator's own exception translation in `_run_zone2_6_leg`) keep
    working unchanged.
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
    """The minimal DB-API 2.0 connection surface this adapter needs."""

    def cursor(self) -> SqlCursor:
        """Return a new cursor bound to this connection."""
        ...

    def commit(self) -> None:
        """Durably commit every statement issued on this connection since the last commit."""
        ...


class SqlSubjectToItemIndexRepository:
    """Real Shape B `SubjectToItemIndex`: a genuine subject_id -> item_id[] table.

    Composed with an open `SqlConnection`. Every write commits before
    returning, so a caller observing a successful `record_item`/
    `delete_for_subject_and_zone` call knows the change is durable.
    """

    def __init__(self, connection: SqlConnection) -> None:
        self._connection = connection

    def items_for_subject(self, tenant_id: str, subject_id: str) -> tuple[str, ...]:
        """Resolve `subject_id`'s Zone 2/6 `item_id`s (the orchestrator's `SubjectToItemIndex` contract).

        An empty tuple means "no known items" -- never an error, matching
        the Protocol's own contract exactly.

        Raises:
            SubjectToItemIndexError: If either argument is blank, or the
                query fails.
        """
        self._require_non_blank(tenant_id=tenant_id, subject_id=subject_id)
        try:
            cursor = self._connection.cursor()
            cursor.execute(
                _ITEMS_FOR_SUBJECT_SQL, (tenant_id, subject_id, list(_ZONE2_6_LEG_ZONES))
            )
            rows = cursor.fetchall()
        except Exception as exc:  # noqa: BLE001 -- converted to typed domain error below
            raise SubjectToItemIndexError(
                f"SqlSubjectToItemIndexRepository.items_for_subject failed: {exc}"
            ) from exc
        return tuple(str(row[0]) for row in rows)

    def record_item(self, tenant_id: str, subject_id: str, zone: str, item_id: str) -> None:
        """Index `item_id` under `(tenant_id, subject_id, zone)`. Idempotent (upsert-style no-op on repeat).

        A write-path caller (a future story's own scope -- see this
        story's dev report judgment-call list for why no write-path call
        site is added by this sub-task) calls this once per item written
        into a subject-linkable zone, so `items_for_subject`/
        `delete_for_subject_and_zone` have something real to resolve
        later.

        Raises:
            SubjectToItemIndexError: If any argument is blank, or the
                write fails.
        """
        self._require_non_blank(tenant_id=tenant_id, subject_id=subject_id, zone=zone)
        if not item_id.strip():
            raise SubjectToItemIndexError("record_item requires a non-blank item_id")
        try:
            cursor = self._connection.cursor()
            cursor.execute(_RECORD_ITEM_SQL, (tenant_id, subject_id, zone, item_id))
            self._connection.commit()
        except Exception as exc:  # noqa: BLE001 -- converted to typed domain error below
            raise SubjectToItemIndexError(
                f"SqlSubjectToItemIndexRepository.record_item failed: {exc}"
            ) from exc

    def delete_for_subject_and_zone(self, tenant_id: str, subject_id: str, zone: str) -> int:
        """Remove every `subject_item_index` row for `(tenant_id, subject_id, zone)` (design doc Section 7 step 6).

        Called once a zone's own erasure leg has actually removed the
        subject's data there -- the index entry for an already-erased
        item is stale metadata, not a record of anything still live.
        Idempotent: deleting an already-clean `(tenant_id, subject_id,
        zone)` triple returns `0`, never an error.

        Returns:
            The number of index rows removed.

        Raises:
            SubjectToItemIndexError: If any argument is blank, or the
                delete fails.
        """
        self._require_non_blank(tenant_id=tenant_id, subject_id=subject_id, zone=zone)
        try:
            cursor = self._connection.cursor()
            cursor.execute(_DELETE_FOR_SUBJECT_AND_ZONE_SQL, (tenant_id, subject_id, zone))
            self._connection.commit()
        except Exception as exc:  # noqa: BLE001 -- converted to typed domain error below
            raise SubjectToItemIndexError(
                f"SqlSubjectToItemIndexRepository.delete_for_subject_and_zone failed: {exc}"
            ) from exc
        return int(getattr(cursor, "rowcount", 0) or 0)

    @staticmethod
    def _require_non_blank(**fields: str) -> None:
        for name, value in fields.items():
            if not value.strip():
                raise SubjectToItemIndexError(f"{name} must not be blank")
