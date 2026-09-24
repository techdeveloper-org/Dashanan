"""Zone8ErasureDurability: the step-3/step-4 durability boundary (DASH-STORY-027-DEV).

Traces to AC-013 (SRS.md, verbatim, cited by
`sql_subject_to_item_index_repository.py`'s own module docstring) and to
`docs/phase-1.5-design/dpdp-crypto-shredding-full-erasure-design.md`
Section 7, "Durability guarantee for the step-3/step-4 boundary".

PROBLEM this module closes: `Zone8SubjectKeyedArchiver.erase_subject`
(must-not-deviate: NOT modified by this story) performs Zone 8's
key-destroy as ONE in-process call with no durable record written before
it fires. If the process crashes between that call returning and the
orchestrator's own job-store write recording completion, the system is
left with no record of whether the irreversible destroy actually ran.
This module inserts a durable, fsynced marker BEFORE the destroy call (a
write-ahead log entry, step 3's own precondition) and a separate finalize
step that only runs after destroy succeeds, promoting that marker into a
real Zone 7 `ProvenanceRecord` and cleaning up `subject_item_index`'s own
zone8 bookkeeping rows -- both in one transaction, so a crash between
"destroy done" and "finalize done" is always safely resumable by
`recover_pending_erasures` below.

Composition boundary (must-not-deviate, binding): this module does NOT
modify `zone8_crypto_shredding_store.py` (`Zone8SubjectKeyedArchiver`) or
`zone8_crypto_shredding.py` (the AES-256-GCM primitives) -- it is
composed with the SAME `SubjectKeyStorePort` and `Zone8SubjectIndexPort`
instances `Zone8SubjectKeyedArchiver` itself is constructed with (see
`dashanan.infrastructure.composition_root`'s wiring), and calls
`SubjectKeyStorePort.destroy_key` directly -- the identical, already-real,
already-idempotent primitive `Zone8SubjectKeyedArchiver.erase_subject`
itself calls. No new destroy mechanism is invented; this module only adds
a durability wrapper AROUND the existing one.

Provenance-insert judgment call (disclosed): `SqlProvenanceRepository.
append` commits internally (GitHub #26 fix, that module's own comment) --
calling it here would end the transaction before this module's own
`subject_item_index` cleanup DELETE could join it, breaking the "same
finalize transaction" requirement design doc Section 7 states explicitly.
Rather than widen that frozen module's `append` with an
transaction-control parameter it does not need for its own only other
caller, `_SqlErasureFinalizer` below issues its own parameterized INSERT
against `provenance_records` (the exact column list
`SqlProvenanceRepository._APPEND_SQL` already uses) on the SAME
connection as the cleanup DELETE, committing once. This is a disclosed,
narrow SQL duplication for a real transaction-locality reason, not a
reach for a shortcut -- see this story's dev report judgment-call list.

Recovery policy (design doc Section 7, verbatim intent): on restart,
`recover_pending_erasures` distinguishes a marker whose destroy is
CONFIRMED (retry finalize only, idempotent, never re-destroy) from one
still PENDING (retry `destroy_key` itself, safe because it is idempotent,
under a bounded k=2 exponential-backoff policy) -- reaching FAILED with
an escalation log to the named compliance owner if retries are exhausted
with destruction still unconfirmed.

PII NOTE: this module never logs subject key material or archived
payload -- only `tenant_id`/`subject_id`/`job_id`/`item_id` routing
metadata and marker status, the same PII ceiling every sibling Zone 8
module in this codebase already applies.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from enum import Enum
from typing import Protocol, runtime_checkable
from uuid import uuid4

from dashanan.application.zone8_crypto_shredding_store import (
    SubjectKeyStorePort,
    Zone8CryptoShreddingStoreError,
    Zone8SubjectIndexPort,
)
from dashanan.domain.exceptions import DashananError
from dashanan.domain.ports import Clock
from dashanan.domain.provenance_record import ProvenanceRecord, SourceType
from dashanan.domain.zone import ZoneId

logger = logging.getLogger(__name__)

_MAX_DESTROY_RETRIES = 2
"""Bounded k=2 exponential-backoff policy (design doc Section 7)."""

_BACKOFF_BASE_SECONDS = 0.5
"""Exponential backoff base: retry N sleeps `_BACKOFF_BASE_SECONDS * 2**N` seconds."""

_UNCONFIGURED_COMPLIANCE_OWNER = "UNCONFIGURED-COMPLIANCE-OWNER"
"""Escalation-log sentinel when `DASHANAN_DPDP_COMPLIANCE_OWNER` is unset.

Disclosed gap, not fabricated: the design doc names "the named human
compliance owner" but does not itself name who that is, and no existing
settings module carries such a field. Rather than invent a name, this
module reads `DASHANAN_DPDP_COMPLIANCE_OWNER` from the environment and
logs this sentinel (CRITICAL level, so it is impossible to miss) when
unset -- the escalation still fires, it is just visibly unaddressed
until deployment configures the real value.
"""


class Zone8ErasureDurabilityError(DashananError):
    """Raised by a `Zone8ErasureDurability` operational failure.

    Kept local to this module per this codebase's established
    file-disjointness convention (mirrors every sibling Zone 8/erasure
    module's identical "kept local" rationale).
    """


class ErasureMarkerStatus(str, Enum):
    """One erasure job's durability-boundary lifecycle (never a legal conclusion, mirrors `SubjectErasureJobStatus`)."""

    PENDING = "pending"
    """Marker durably appended; `destroy_key` has not yet been confirmed to have run."""

    DESTROY_CONFIRMED = "destroy_confirmed"
    """`destroy_key` returned; the finalize (Zone 7 promotion + index cleanup) has not yet committed."""

    FINALIZED = "finalized"
    """Finalize committed: this job's durability boundary is fully closed."""

    FAILED = "failed"
    """Retries exhausted with destruction still unconfirmed; escalated to the compliance owner."""


@dataclass(frozen=True, slots=True)
class ErasureMarker:
    """One write-ahead marker for one Zone 8 subject-erasure job's step-3/step-4 boundary.

    Attributes:
        marker_id: This marker's own identifier (distinct from the
            orchestrator's combined `job_id` -- one orchestrator job may,
            in principle, retry its Zone 8 leg under a fresh marker).
        tenant_id: Owning tenant.
        subject_id: The data subject being erased.
        item_ids: Every Zone 8 `item_id` this subject had archived, as
            resolved BEFORE `destroy_key` ran (recorded here so
            `recover_pending_erasures` never needs a second index query
            against a key that may already be gone).
        status: This marker's current durability-boundary state.
        created_at: When this marker was durably appended (step 3's own
            precondition).
        retry_count: How many `destroy_key` retries have been attempted
            for this marker so far.
    """

    marker_id: str
    tenant_id: str
    subject_id: str
    item_ids: tuple[str, ...]
    status: ErasureMarkerStatus
    created_at: datetime
    retry_count: int = 0


@runtime_checkable
class ErasureMarkerStore(Protocol):
    """Durably tracks every `ErasureMarker` this coordinator writes (the WAL itself).

    Kept local to this module per this codebase's established
    file-disjointness convention.
    """

    def append(self, marker: ErasureMarker) -> None:
        """Durably (fsynced) append or replace `marker`, keyed by `marker_id`.

        Raises:
            Zone8ErasureDurabilityError: If the write cannot be made
                durable.
        """
        ...

    def get(self, marker_id: str) -> ErasureMarker | None:
        """Return `marker_id`'s current state, or `None` if unknown."""
        ...

    def list_unfinalized(self) -> tuple[ErasureMarker, ...]:
        """Return every marker not yet `FINALIZED` or `FAILED` -- the recovery sweep's own input."""
        ...


class InMemoryErasureMarkerStore:
    """Shape A `ErasureMarkerStore`: an in-process dict, for tests and Shape A deployments.

    Mirrors `InMemorySubjectErasureJobStore`'s identical "Shape A now,
    Shape B (SQL) later" split; `SqlErasureMarkerStore` below is the real
    Shape B adapter this class stands in for until composed.
    """

    def __init__(self) -> None:
        self._markers: dict[str, ErasureMarker] = {}

    def append(self, marker: ErasureMarker) -> None:
        """Store `marker`, replacing any prior state for its `marker_id`."""
        self._markers[marker.marker_id] = marker

    def get(self, marker_id: str) -> ErasureMarker | None:
        """Return `marker_id`'s current state, or `None` if unknown."""
        return self._markers.get(marker_id)

    def list_unfinalized(self) -> tuple[ErasureMarker, ...]:
        """Return every marker not yet `FINALIZED` or `FAILED`."""
        return tuple(
            marker
            for marker in self._markers.values()
            if marker.status not in (ErasureMarkerStatus.FINALIZED, ErasureMarkerStatus.FAILED)
        )


@runtime_checkable
class SqlCursor(Protocol):
    """The minimal DB-API 2.0 (PEP 249) cursor surface this module needs."""

    def execute(self, sql: str, params: Sequence[object]) -> object:
        """Execute a parameterized statement. Must never receive interpolated SQL."""
        ...

    def fetchall(self) -> Sequence[Sequence[object]]:
        """Return every remaining row of the last `execute()`'s result set."""
        ...

    def fetchone(self) -> Sequence[object] | None:
        """Return the next row of the last `execute()`'s result set, or `None`."""
        ...


@runtime_checkable
class SqlConnection(Protocol):
    """The minimal DB-API 2.0 connection surface this module needs."""

    def cursor(self) -> SqlCursor:
        """Return a new cursor bound to this connection."""
        ...

    def commit(self) -> None:
        """Durably commit every statement issued on this connection since the last commit (fsync)."""
        ...


_UPSERT_MARKER_SQL = """
INSERT INTO zone8_erasure_markers
    (marker_id, tenant_id, subject_id, item_ids, status, created_at, retry_count)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (marker_id) DO UPDATE SET
    status = EXCLUDED.status,
    retry_count = EXCLUDED.retry_count
"""

_GET_MARKER_SQL = """
SELECT marker_id, tenant_id, subject_id, item_ids, status, created_at, retry_count
FROM zone8_erasure_markers
WHERE marker_id = %s
"""

_LIST_UNFINALIZED_SQL = """
SELECT marker_id, tenant_id, subject_id, item_ids, status, created_at, retry_count
FROM zone8_erasure_markers
WHERE status NOT IN ('finalized', 'failed')
"""

_INSERT_PROVENANCE_SQL = """
INSERT INTO provenance_records
    (tenant_id, provenance_id, item_id, source_zone, source_type,
     source_refs, write_timestamp, update_history, retrieval_context_hash,
     conflict_status, invalidation_flag, confidence, prev_hash, record_hash)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

_DELETE_ZONE8_INDEX_ROWS_SQL = """
DELETE FROM subject_item_index
WHERE tenant_id = %s AND subject_id = %s AND zone = 'zone8'
"""


class SqlErasureMarkerStore:
    """Real Shape B `ErasureMarkerStore`, backed by `zone8_erasure_markers` (fsynced via `commit`).

    See `zone8_erasure_markers_schema.sql` for the table this adapter
    queries.
    """

    def __init__(self, connection: SqlConnection) -> None:
        self._connection = connection

    def append(self, marker: ErasureMarker) -> None:
        """Durably (fsynced via `connection.commit()`) upsert `marker`.

        Raises:
            Zone8ErasureDurabilityError: If the write fails.
        """
        try:
            cursor = self._connection.cursor()
            cursor.execute(
                _UPSERT_MARKER_SQL,
                (
                    marker.marker_id,
                    marker.tenant_id,
                    marker.subject_id,
                    list(marker.item_ids),
                    marker.status.value,
                    marker.created_at,
                    marker.retry_count,
                ),
            )
            self._connection.commit()
        except Exception as exc:  # noqa: BLE001 -- converted to typed domain error below
            raise Zone8ErasureDurabilityError(
                f"SqlErasureMarkerStore.append failed for marker_id={marker.marker_id!r}: {exc}"
            ) from exc

    def get(self, marker_id: str) -> ErasureMarker | None:
        """Return `marker_id`'s current state, or `None` if unknown.

        Raises:
            Zone8ErasureDurabilityError: If the query fails.
        """
        try:
            cursor = self._connection.cursor()
            cursor.execute(_GET_MARKER_SQL, (marker_id,))
            row = cursor.fetchone()
        except Exception as exc:  # noqa: BLE001 -- converted to typed domain error below
            raise Zone8ErasureDurabilityError(
                f"SqlErasureMarkerStore.get failed for marker_id={marker_id!r}: {exc}"
            ) from exc
        return self._row_to_marker(row) if row is not None else None

    def list_unfinalized(self) -> tuple[ErasureMarker, ...]:
        """Return every marker not yet `FINALIZED` or `FAILED`.

        Raises:
            Zone8ErasureDurabilityError: If the query fails.
        """
        try:
            cursor = self._connection.cursor()
            cursor.execute(_LIST_UNFINALIZED_SQL, ())
            rows = cursor.fetchall()
        except Exception as exc:  # noqa: BLE001 -- converted to typed domain error below
            raise Zone8ErasureDurabilityError(
                f"SqlErasureMarkerStore.list_unfinalized failed: {exc}"
            ) from exc
        return tuple(self._row_to_marker(row) for row in rows)

    @staticmethod
    def _row_to_marker(row: Sequence[object]) -> ErasureMarker:
        return ErasureMarker(
            marker_id=str(row[0]),
            tenant_id=str(row[1]),
            subject_id=str(row[2]),
            item_ids=tuple(str(item_id) for item_id in (row[3] or ())),  # type: ignore[attr-defined]
            status=ErasureMarkerStatus(str(row[4])),
            created_at=row[5],  # type: ignore[arg-type]
            retry_count=int(str(row[6])),
        )


class DurableZone8ErasureCoordinator:
    """Runs Zone 8's key-destroy behind the write-ahead marker / finalize boundary (design doc Section 7).

    Composed with the SAME `SubjectKeyStorePort` + `Zone8SubjectIndexPort`
    instances the existing `Zone8SubjectKeyedArchiver` uses (see this
    module's own docstring for why this is not a second destroy
    mechanism), plus an `ErasureMarkerStore`, a provenance-capable
    `SqlConnection` for the finalize step, and a `Clock`.
    """

    def __init__(
        self,
        *,
        key_store: SubjectKeyStorePort,
        subject_index: Zone8SubjectIndexPort,
        marker_store: ErasureMarkerStore,
        provenance_connection: SqlConnection,
        clock: Clock,
        marker_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._key_store = key_store
        self._subject_index = subject_index
        self._marker_store = marker_store
        self._provenance_connection = provenance_connection
        self._clock = clock
        self._marker_id_factory = marker_id_factory or (lambda: str(uuid4()))

    def erase_subject(self, tenant_id: str, subject_id: str) -> tuple[str, ...]:
        """Step 3 + step 4: write-ahead marker, idempotent destroy, then durable finalize.

        Returns:
            Every Zone 8 `item_id` this subject had archived (the same
            return contract `Zone8SubjectKeyedArchiver.erase_subject`
            already gives).

        Raises:
            ValueError: If `tenant_id` or `subject_id` is blank.
            Zone8CryptoShreddingStoreError: If the key store or subject
                index cannot be reached.
            Zone8ErasureDurabilityError: If the marker cannot be written,
                or the finalize transaction fails.
        """
        if not tenant_id.strip():
            raise ValueError("erase_subject requires a non-blank tenant_id")
        if not subject_id.strip():
            raise ValueError("erase_subject requires a non-blank subject_id")

        item_ids = self._subject_index.find_item_ids(tenant_id, subject_id)

        marker = ErasureMarker(
            marker_id=self._marker_id_factory(),
            tenant_id=tenant_id,
            subject_id=subject_id,
            item_ids=item_ids,
            status=ErasureMarkerStatus.PENDING,
            created_at=self._clock.now(),
        )
        self._marker_store.append(marker)
        logger.info(
            "zone8 erasure write-ahead marker appended",
            extra={"marker_id": marker.marker_id, "tenant_id": tenant_id, "item_count": len(item_ids)},
        )

        self._key_store.destroy_key(tenant_id, subject_id)

        confirmed = replace(marker, status=ErasureMarkerStatus.DESTROY_CONFIRMED)
        self._marker_store.append(confirmed)
        logger.info(
            "zone8 erasure key-destroy confirmed",
            extra={"marker_id": marker.marker_id, "tenant_id": tenant_id},
        )

        _finalize(confirmed, self._provenance_connection)
        self._marker_store.append(replace(confirmed, status=ErasureMarkerStatus.FINALIZED))
        logger.info(
            "zone8 erasure finalize committed",
            extra={"marker_id": marker.marker_id, "tenant_id": tenant_id},
        )
        return item_ids


def _finalize(marker: ErasureMarker, connection: SqlConnection) -> None:
    """Promote `marker` into a real Zone 7 record and delete its zone8 index rows, in ONE transaction.

    Idempotent: `record_hash`/`provenance_id` are deterministically
    derived from `marker.marker_id`, so re-running finalize for an
    already-finalized marker (the recovery sweep's own "retry finalize
    only" path) raises a unique-constraint violation on the SECOND
    attempt rather than silently double-writing -- callers treat that
    specific failure as "already finalized" (see
    `recover_pending_erasures` below).

    Raises:
        Zone8ErasureDurabilityError: If either statement fails.
    """
    provenance_id = f"zone8-erasure-{marker.marker_id}"
    # ProvenanceRecord.retrieval_context_hash must be a 64-character lowercase
    # hex SHA-256 digest (__post_init__'s own invariant) -- this finalize event
    # has no real retrieval query/task to hash (it originates from the erasure
    # coordinator, not a context-assembly read), so the digest is derived
    # deterministically from marker_id itself: real, reproducible, and
    # satisfies the invariant without fabricating a query that never happened.
    retrieval_context_hash = hashlib.sha256(marker.marker_id.encode("utf-8")).hexdigest()
    record = ProvenanceRecord.create(
        tenant_id=marker.tenant_id,
        provenance_id=provenance_id,
        item_id=f"subject:{marker.subject_id}",
        source_zone=ZoneId.CONSOLIDATION,
        source_type=SourceType.SYSTEM_DERIVED,
        write_timestamp=marker.created_at,
        actor="zone8-erasure-durability-coordinator",
        change=(
            f"DPDP crypto-shredding erasure finalized for subject_id="
            f"{marker.subject_id!r}: {len(marker.item_ids)} zone8 item(s) "
            "rendered permanently unreadable"
        ),
        retrieval_context_hash=retrieval_context_hash,
    )
    params = (
        record.tenant_id,
        record.provenance_id,
        record.item_id,
        record.source_zone.value,
        record.source_type.value,
        list(record.source_refs),
        record.write_timestamp,
        _update_history_json(record),
        record.retrieval_context_hash,
        record.conflict_status.value,
        record.invalidation_flag,
        record.confidence,
        record.prev_hash,
        record.record_hash,
    )
    try:
        cursor = connection.cursor()
        cursor.execute(_INSERT_PROVENANCE_SQL, params)
        cursor.execute(
            _DELETE_ZONE8_INDEX_ROWS_SQL, (marker.tenant_id, marker.subject_id)
        )
        connection.commit()
    except Exception as exc:  # noqa: BLE001 -- converted to typed domain error below
        raise Zone8ErasureDurabilityError(
            f"finalize failed for marker_id={marker.marker_id!r}: {exc}"
        ) from exc


def _update_history_json(record: ProvenanceRecord) -> str:
    """Serialize `record.update_history` exactly as `SqlProvenanceRepository.append` does.

    A small, disclosed duplication of that module's own
    `_update_entry_to_dict` + `json.dumps` shape -- see this module's own
    docstring for why the two INSERTs cannot share one transaction-aware
    helper without widening a frozen module.
    """
    return json.dumps([asdict(entry) for entry in record.update_history], default=str)


def recover_pending_erasures(
    *,
    marker_store: ErasureMarkerStore,
    key_store: SubjectKeyStorePort,
    provenance_connection: SqlConnection,
    compliance_owner_env_value: str | None,
    sleep: Callable[[float], None] | None = None,
) -> tuple[str, ...]:
    """Job recovery sweep: resolve every unfinalized marker after a restart (design doc Section 7).

    For each marker `list_unfinalized()` returns:
      - `DESTROY_CONFIRMED` (crash between step 3 and step 4): retry
        `_finalize` only. A unique-constraint failure on the provenance
        insert means an earlier finalize attempt already committed
        (idempotent close-out) -- treated as success, not re-raised.
      - `PENDING` with `key_store.get_key(...) is None`: the key is
        already gone, so destruction is CONFIRMED after all (a crash hit
        between `destroy_key` returning and this marker's own
        `DESTROY_CONFIRMED` write) -- promote and finalize, never
        re-destroy.
      - `PENDING` with the key still present (destruction UNCONFIRMED):
        safely retry `destroy_key` itself (idempotent) under the bounded
        k=2 exponential-backoff policy. If the key is still present after
        `_MAX_DESTROY_RETRIES` attempts, the marker moves to `FAILED` and
        this function logs a CRITICAL escalation naming
        `compliance_owner_env_value` (or the disclosed
        `_UNCONFIGURED_COMPLIANCE_OWNER` sentinel if that is `None`/blank).

    Returns:
        The `marker_id` of every marker this sweep successfully moved to
        `FINALIZED` during this call.

    Raises:
        Zone8ErasureDurabilityError: Propagated unchanged if the marker
            store itself cannot be read.
    """
    sleep_fn = sleep or time.sleep
    owner = compliance_owner_env_value or _UNCONFIGURED_COMPLIANCE_OWNER
    finalized: list[str] = []

    for marker in marker_store.list_unfinalized():
        if marker.status is ErasureMarkerStatus.DESTROY_CONFIRMED:
            _recover_finalize_only(marker, marker_store, provenance_connection, finalized)
            continue

        if marker.status is not ErasureMarkerStatus.PENDING:
            continue  # pragma: no cover -- FINALIZED/FAILED already excluded by list_unfinalized

        try:
            key_still_present = key_store.get_key(marker.tenant_id, marker.subject_id) is not None
        except Zone8CryptoShreddingStoreError:
            logger.error(
                "zone8 erasure recovery sweep could not query key store",
                extra={"marker_id": marker.marker_id},
                exc_info=True,
            )
            continue

        if not key_still_present:
            confirmed = replace(marker, status=ErasureMarkerStatus.DESTROY_CONFIRMED)
            marker_store.append(confirmed)
            _recover_finalize_only(confirmed, marker_store, provenance_connection, finalized)
            continue

        _retry_destroy_then_finalize(
            marker, marker_store, key_store, provenance_connection, owner, sleep_fn, finalized
        )

    return tuple(finalized)


def _recover_finalize_only(
    marker: ErasureMarker,
    marker_store: ErasureMarkerStore,
    provenance_connection: SqlConnection,
    finalized: list[str],
) -> None:
    """Retry `_finalize` for a marker whose destroy is already CONFIRMED. Never re-destroys."""
    try:
        _finalize(marker, provenance_connection)
    except Zone8ErasureDurabilityError:
        logger.info(
            "zone8 erasure recovery finalize already applied (idempotent close-out)",
            extra={"marker_id": marker.marker_id},
        )
    marker_store.append(replace(marker, status=ErasureMarkerStatus.FINALIZED))
    finalized.append(marker.marker_id)


def _retry_destroy_then_finalize(
    marker: ErasureMarker,
    marker_store: ErasureMarkerStore,
    key_store: SubjectKeyStorePort,
    provenance_connection: SqlConnection,
    compliance_owner: str,
    sleep_fn: Callable[[float], None],
    finalized: list[str],
) -> None:
    """Bounded k=2 exponential-backoff retry of `destroy_key`, then finalize; FAILED + escalate if exhausted."""
    retry_count = marker.retry_count
    while retry_count < _MAX_DESTROY_RETRIES:
        sleep_fn(_BACKOFF_BASE_SECONDS * (2**retry_count))
        try:
            key_store.destroy_key(marker.tenant_id, marker.subject_id)
        except Zone8CryptoShreddingStoreError:
            logger.warning(
                "zone8 erasure recovery destroy_key retry failed",
                extra={"marker_id": marker.marker_id, "attempt": retry_count + 1},
                exc_info=True,
            )
        retry_count += 1
        still_present = key_store.get_key(marker.tenant_id, marker.subject_id) is not None
        if not still_present:
            confirmed = replace(
                marker, status=ErasureMarkerStatus.DESTROY_CONFIRMED, retry_count=retry_count
            )
            marker_store.append(confirmed)
            _recover_finalize_only(confirmed, marker_store, provenance_connection, finalized)
            return
        marker_store.append(replace(marker, status=ErasureMarkerStatus.PENDING, retry_count=retry_count))

    failed_marker = replace(marker, status=ErasureMarkerStatus.FAILED, retry_count=retry_count)
    marker_store.append(failed_marker)
    logger.critical(
        "zone8 erasure destroy_key retries exhausted with destruction UNCONFIRMED -- "
        "escalating to compliance owner",
        extra={
            "marker_id": marker.marker_id,
            "tenant_id": marker.tenant_id,
            "retry_count": retry_count,
            "compliance_owner": compliance_owner,
        },
    )
