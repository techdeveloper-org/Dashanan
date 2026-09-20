"""Zone8ConsolidationStore: the ADR-009 object-store + hot-manifest Facade.

HLD Section 3.9 (Zone 8, ConsolidatedBlob) + ADR-009 (object-store blobs,
hot manifest index) + HLD Section 3.10 (RECEIVES rule, Hard Rule 1) + HLD
Section 8.4 (degraded-mode/failure table) + HLD threat I-5 (consolidation
cross-tenant merge control), traced to FR-008 in SRS.md.

FR-008 (verbatim): "The system SHALL provide a Consolidation Memory zone
as the long-term, cross-session consolidated store that content reaches
only via the Archived state of the rotation state machine (FR-012)."

This module holds the orchestration `dashanan.domain.consolidated_blob`'s
own docstring assigns here: calling the object-store and manifest ports,
deciding defer-vs-write on an unavailable object store (AC-008-4), and
resolving a manifest lookup plus at most one ranged GET (AC-008-1) --
mirroring the `domain/zone2_capacity_backstop.py` (pure types/functions)
+ `application/zone2_capacity_backstop_sweep.py` (orchestration +
logging) split DASH-STORY-004 already established. The domain module's
pure functions (`validate_source_zone`, `validate_tenant_uniform_batch`,
`compute_blob_id`) are the single source of WHAT is accepted and WHY a
batch fails; this module is the single place that decides HOW each
outcome is carried out against real ports.

`Zone8ConsolidationStore` is a Facade (python-design-patterns-core
Section 14): it hides two ports (`ObjectStorePort`, `ManifestPort`) plus
`Clock` behind two entry points, `consolidate_batch` (write) and
`resolve_manifest` / `read_blob_range` (read) -- a caller never talks to
either port directly.

MUST-NOT-DEVIATE (sprint2_ar1_assignments.json AR1-S2-018, binding):
  1. "Zone 8 is reachable only via the Archived state and is never a
     competing zone to Zones 2-5" -- this Facade exposes no transition
     machinery; `consolidate_batch` is a pure destination write, never a
     state-machine mutation of the source zone's own item.
  2. "The object-store + hot-manifest shape is locked by ADR-009
     (APPROVED)" -- `ObjectStorePort` + `ManifestPort` below implement
     exactly that shape; neither is re-opened as a design choice.
  3. "Blobs are immutable and content-addressed -- never overwritten" --
     `ObjectStorePort.put_if_absent`'s own contract (never `put`/
     `overwrite`) plus `compute_blob_id`'s determinism (domain layer)
     together make an overwrite structurally absent from this module.
  4. "Zone 8 owns ConsolidatedBlob and no other table may duplicate it,
     per Hard Rule 1" -- no other zone's repository or port is imported
     here.
  5. "PM note: only Zone 2 exists as a rotation source at ship time;
     re-test AC-008-2 against Zones 3/4/5 as each ships" -- see
     `dashanan.domain.consolidated_blob.ALLOWED_SOURCE_ZONES`'s
     identical note; this module performs no additional, narrower
     filtering of its own.

PII NOTE: every port and Result-type payload here carries only manifest/
routing metadata and opaque blob bytes this module never decodes --
`tenant_id`, `item_id`, `blob_id`, a `ByteRange`, a `ZoneId`
(dev_prompt's PII constraint: "never decoded blob content"). This module
never logs `ArchiveBatchItem.payload` or any bytes read back from
`ObjectStorePort.get_range`.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from dashanan.domain.consolidated_blob import (
    ArchiveBatchItem,
    ByteRange,
    ManifestEntry,
    Zone8ConsolidationError,
    compute_blob_id,
    validate_source_zone,
    validate_tenant_uniform_batch,
)
from dashanan.domain.exceptions import DashananError
from dashanan.domain.ports import Clock

logger = logging.getLogger(__name__)


class Zone8StorePortError(DashananError):
    """Raised by a Zone 8 port on an expected, operational failure.

    Kept local to this module rather than added to the shared
    `dashanan.domain.exceptions` -- this story's file-disjointness
    requirement, mirroring `zone2_capacity_backstop_sweep.
    Zone2BackstopPortError`'s identical rationale. A port raising
    anything OTHER than this type (or a subtype) is a programming bug
    (error-handling-patterns Section 2, fail-fast) and propagates
    unchanged; a port raising THIS type is an operational failure
    (fail-safe), handled per the AC-008-4 degraded-mode rules below.
    """


class Zone8ObjectStoreUnavailableError(Zone8StorePortError):
    """The object store cannot be reached right now (AC-008-4, HLD Section 8.4).

    `ObjectStorePort.put_if_absent` and `ObjectStorePort.get_range` raise
    this SPECIFIC subtype -- never the broader `Zone8StorePortError` --
    for the one failure mode AC-008-4 gives a named, non-error outcome:
    on write, the archive transition DEFERS (the item stays Compressed,
    no partial write); on read, the read fails scoped to the one
    affected `item_id`, not the whole store. A port that cannot
    distinguish "object store unreachable" from "some other failure"
    must raise the narrower `Zone8ObjectStoreUnavailableError` only when
    it is genuinely confident the object store itself (not e.g. a
    malformed request) is the cause -- HLD Section 8.4's degraded-mode
    row is specifically about the STORE'S availability.
    """


@runtime_checkable
class ObjectStorePort(Protocol):
    """ADR-009's object-store half: content-addressed, write-once, ranged reads.

    Kept local to this module (not `dashanan.domain.ports`) per this
    story's file-disjointness requirement -- mirrors `write_gate.
    ProvenanceJournalPort`'s and `zone2_capacity_backstop_sweep.
    Zone2EvictionPort`'s identical local-Protocol choice, and keeps
    `ZoneRepository` in the shared module untouched (AR1-G2).
    """

    def put_if_absent(self, blob_id: str, payload: bytes) -> None:
        """Durably store `payload` under `blob_id`, unless it is already present.

        The name states must-not-deviate item 3 at the API boundary:
        there is no `put`/`overwrite` method on this Protocol, so a
        caller cannot even express "overwrite blob_id" against a
        conforming adapter. When `blob_id` (content-addressed via
        `compute_blob_id`) is already present, this call is a no-op --
        it MUST NOT raise merely because the blob already exists, since
        `compute_blob_id`'s determinism means "already present" can only
        mean "byte-identical content was written before."

        Raises:
            Zone8ObjectStoreUnavailableError: If the object store cannot
                be reached right now (AC-008-4's degraded-mode trigger).
            Zone8StorePortError: For any other operational failure this
                adapter cannot recover from.
        """
        ...

    def get_range(self, blob_id: str, byte_range: ByteRange) -> bytes:
        """Return exactly `byte_range`'s slice of the blob stored under `blob_id`.

        AC-008-1's "at most one ranged GET": a conforming adapter issues
        one network/disk read per call, never a full-object download
        followed by local slicing.

        Raises:
            Zone8ObjectStoreUnavailableError: If the object store cannot
                be reached right now (AC-008-4: reads of already-archived
                items fail scoped to the one affected item).
            Zone8StorePortError: For any other operational failure (e.g.
                `blob_id` does not exist -- a manifest/object-store
                consistency fault this adapter detected).
        """
        ...


@runtime_checkable
class ManifestPort(Protocol):
    """ADR-009's hot-manifest half: one indexed lookup, one atomic batch insert.

    Kept local to this module per this story's file-disjointness
    requirement, mirroring `ObjectStorePort` above.
    """

    def find_by_item_id(self, tenant_id: str, item_id: str) -> ManifestEntry | None:
        """Resolve `item_id` to its `ManifestEntry`, or `None` if never archived.

        AC-008-1: a single indexed manifest-table lookup -- never a
        full-manifest scan. A conforming adapter's underlying query is a
        primary-key or unique-index point lookup on `(tenant_id,
        item_id)`, structurally O(1)/O(log n), never O(manifest size).

        Raises:
            Zone8StorePortError: If the manifest index cannot be
                queried (e.g. the underlying store is unreachable).
        """
        ...

    def insert_batch(self, entries: Sequence[ManifestEntry]) -> None:
        """Atomically insert every entry in `entries`, or none of them.

        Called once per `consolidate_batch` call with every entry the
        batch produced (never one call per item): a partial manifest
        write -- some items resolvable, others silently missing from a
        blob the object store already durably holds -- would itself be
        the "partial write" AC-008-4 forbids, just relocated from the
        object-store leg to the manifest leg. An adapter backed by a
        relational store implements this as a single transaction.

        Raises:
            Zone8StorePortError: If the batch cannot be inserted (e.g.
                the underlying store is unreachable, or a
                `(tenant_id, item_id)` unique-constraint violation
                indicates `item_id` was already archived).
        """
        ...


@dataclass(frozen=True, slots=True)
class ArchiveWritten:
    """One item this `consolidate_batch` call durably archived (AC-008-1, AC-008-3).

    Attributes:
        item_id: The archived item's identifier.
        manifest_entry: The resulting hot-manifest row -- what a
            subsequent `resolve_manifest`/`read_blob_range` call for
            this `item_id` will return.
    """

    item_id: str
    manifest_entry: ManifestEntry


@dataclass(frozen=True, slots=True)
class ArchiveDeferred:
    """One item this call could NOT archive because the object store was unavailable.

    A Result-type outcome (HLD Section 6, "Degraded responses" row;
    mirrors `dashanan.domain.write_gate.WriteRejected`'s identical
    pattern) rather than a raised exception propagating out of
    `consolidate_batch`: AC-008-4's "an archive transition defers" is a
    normal, expected outcome of an unavailable object store, not a
    caller bug. The item this outcome names REMAINS in its source
    zone's `Compressed` state -- must-not-deviate item 3's "no loss/
    partial write" -- and is expected to be retried by a later sweep.

    Attributes:
        item_id: The item that was deferred.
        reason: A human-readable, PII-free explanation (`str` of the
            `Zone8ObjectStoreUnavailableError` that triggered the
            defer).
    """

    item_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class ArchiveRejected:
    """One item this call refused outright -- a source-zone violation (AC-008-2).

    Distinct from `ArchiveDeferred`: a rejection is a permanent,
    structural refusal (this `item_id`'s `source_zone` is never allowed
    to reach Zone 8, per HLD Section 3.10's RECEIVES rule), never
    something a later retry could resolve, unlike an object-store outage.

    Attributes:
        item_id: The item that was rejected.
        reason: A human-readable, PII-free explanation (`str` of the
            `Zone8ConsolidationError` that triggered the rejection).
    """

    item_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class ConsolidationBatchResult:
    """The outcome of one `Zone8ConsolidationStore.consolidate_batch` call.

    Attributes:
        written: Every item this call durably archived, in the same
            order `items` was given in.
        deferred: Every item this call could not archive because the
            object store was unavailable (AC-008-4). Empty unless the
            object store itself failed -- either ALL accepted items in
            a batch defer together (the merged blob write is one atomic
            `put_if_absent` call) or none do.
        rejected: Every item this call refused for originating from a
            disallowed source zone (AC-008-2). Rejection is per-item and
            does not affect any other item in the same batch.
    """

    written: tuple[ArchiveWritten, ...]
    deferred: tuple[ArchiveDeferred, ...]
    rejected: tuple[ArchiveRejected, ...]


class Zone8ConsolidationStore:
    """The Zone 8 Facade: one `consolidate_batch` (write) plus two reads.

    Composed from `ObjectStorePort`, `ManifestPort`, and `Clock`.
    Construction never touches I/O.
    """

    def __init__(
        self,
        object_store: ObjectStorePort,
        manifest: ManifestPort,
        clock: Clock,
    ) -> None:
        self._object_store = object_store
        self._manifest = manifest
        self._clock = clock

    def consolidate_batch(
        self, items: Sequence[ArchiveBatchItem]
    ) -> ConsolidationBatchResult:
        """Merge `items` into one shared blob and record one manifest row each.

        Steps (Template Method, mirrors `Zone2CapacityBackstopSweep.run`'s
        identical "validate whole-batch precondition, then process
        per-item" shape):

          1. Whole-batch precondition: `validate_tenant_uniform_batch`
             (AC-008-3 / threat I-5). Raises immediately, before ANY
             port is called, if `items` spans more than one tenant_id --
             the entire batch fails, not a subset of it (this is a
             RAISE, never a logged warning; see that function's own
             docstring).
          2. Per-item source-zone check (AC-008-2): an item whose
             `source_zone` is not in `ALLOWED_SOURCE_ZONES` was already
             rejected at `ArchiveBatchItem` construction time (the
             domain layer's `__post_init__` calls `validate_source_zone`
             unconditionally) -- there is no such item to see here in
             practice; this method still reports it via `rejected` if a
             caller somehow constructs a batch bypassing that guard
             (defense in depth), isolated per-item.
          3. Merge every accepted item's `payload` into one combined
             blob, in `items` order, recording each item's own
             `ByteRange` slice.
          4. `object_store.put_if_absent(blob_id, merged_payload)` --
             ONE call for the whole batch (content-addressed over the
             merged bytes, must-not-deviate item 3). If this raises
             `Zone8ObjectStoreUnavailableError`, EVERY accepted item in
             this batch defers together (AC-008-4's "no partial write":
             the manifest is never told about a blob the object store
             does not durably hold).
          5. `manifest.insert_batch(entries)` -- one atomic call
             covering every accepted item's `ManifestEntry`.

        Args:
            items: The candidate merge batch. An empty sequence returns
                an all-empty `ConsolidationBatchResult` with no port
                calls made.

        Returns:
            A `ConsolidationBatchResult` accounting for every item in
            `items` across exactly one of `written`/`deferred`/
            `rejected`.

        Raises:
            dashanan.domain.consolidated_blob.Zone8ConsolidationError: If
                `items` spans more than one `tenant_id` (AC-008-3) --
                the whole-batch precondition failure, never narrowed to
                a per-item outcome.
            Zone8StorePortError: If `manifest.insert_batch` fails for a
                reason OTHER than object-store unavailability (a
                programming/operational bug this method does not treat
                as a normal degraded-mode outcome).
        """
        if not items:
            return ConsolidationBatchResult(written=(), deferred=(), rejected=())

        validate_tenant_uniform_batch(items)

        accepted: list[ArchiveBatchItem] = []
        rejected: list[ArchiveRejected] = []
        for item in items:
            try:
                _revalidate_source_zone(item)
            except Zone8ConsolidationError as exc:
                rejected.append(ArchiveRejected(item_id=item.item_id, reason=str(exc)))
                continue
            accepted.append(item)

        if not accepted:
            return ConsolidationBatchResult(
                written=(), deferred=(), rejected=tuple(rejected)
            )

        merged_payload, ranges_by_item_id = _merge_payloads(accepted)
        blob_id = compute_blob_id(merged_payload)

        try:
            self._object_store.put_if_absent(blob_id, merged_payload)
        except Zone8ObjectStoreUnavailableError as exc:
            logger.warning(
                "zone8 consolidation batch deferred: object store unavailable",
                extra={"item_count": len(accepted)},
                exc_info=True,
            )
            deferred = tuple(
                ArchiveDeferred(item_id=item.item_id, reason=str(exc))
                for item in accepted
            )
            return ConsolidationBatchResult(
                written=(), deferred=deferred, rejected=tuple(rejected)
            )

        written_at = self._clock.now()
        entries = [
            ManifestEntry.for_write(
                tenant_id=item.tenant_id,
                item_id=item.item_id,
                blob_id=blob_id,
                byte_range=ranges_by_item_id[item.item_id],
                compression_generation=0,
                source_zone=item.source_zone,
                written_at=written_at,
                subject_id=item.subject_id,
            )
            for item in accepted
        ]
        self._manifest.insert_batch(entries)

        logger.info(
            "zone8 consolidation batch written",
            extra={
                "item_count": len(entries),
                "blob_id": blob_id,
                "tenant_id": accepted[0].tenant_id,
            },
        )
        written = tuple(
            ArchiveWritten(item_id=entry.item_id, manifest_entry=entry)
            for entry in entries
        )
        return ConsolidationBatchResult(
            written=written, deferred=(), rejected=tuple(rejected)
        )

    def resolve_manifest(self, tenant_id: str, item_id: str) -> ManifestEntry | None:
        """AC-008-1: resolve `item_id` via a single indexed manifest-table lookup.

        Args:
            tenant_id: The owning tenant. Never blank.
            item_id: The item to resolve.

        Returns:
            The item's `ManifestEntry`, or `None` if it was never
            archived into Zone 8.

        Raises:
            ValueError: If `tenant_id` is blank.
            Zone8StorePortError: If the manifest index cannot be
                queried.
        """
        if not tenant_id.strip():
            raise ValueError(
                "Zone8ConsolidationStore.resolve_manifest requires a non-blank tenant_id"
            )
        return self._manifest.find_by_item_id(tenant_id, item_id)

    def read_blob_range(self, tenant_id: str, item_id: str) -> bytes:
        """AC-008-1 + AC-008-4: resolve, then one ranged GET, scoped to `item_id`.

        Args:
            tenant_id: The owning tenant. Never blank.
            item_id: The item to read.

        Returns:
            The item's own byte-range slice of its archived blob.

        Raises:
            ValueError: If `tenant_id` is blank.
            dashanan.domain.consolidated_blob.Zone8ConsolidationError: If
                `item_id` has never been archived (no manifest entry).
            Zone8ObjectStoreUnavailableError: If the object store cannot
                be reached right now -- AC-008-4's "reads of already-
                archived items fail 503 scoped only to affected items":
                this exception names exactly the one `item_id` this call
                was for, never the whole store.
            Zone8StorePortError: For any other operational failure.
        """
        entry = self.resolve_manifest(tenant_id, item_id)
        if entry is None:
            raise Zone8ConsolidationError(
                f"item_id '{item_id}' has no Zone 8 manifest entry for tenant "
                f"'{tenant_id}' -- it was never archived"
            )
        return self._object_store.get_range(entry.blob_id, entry.byte_range)


def _revalidate_source_zone(item: ArchiveBatchItem) -> None:
    """Defense-in-depth re-check of AC-008-2, isolated to one item.

    `ArchiveBatchItem.__post_init__` already enforces this at
    construction time; this call exists purely so a hypothetical caller
    that bypasses normal construction (e.g. `object.__new__`) still gets
    a per-item `ArchiveRejected` outcome from `consolidate_batch` rather
    than an unhandled exception -- mirrors the domain layer's own
    `validate_source_zone`, called a second time deliberately rather
    than trusted transitively.
    """
    validate_source_zone(item.source_zone)


def _merge_payloads(
    items: Sequence[ArchiveBatchItem],
) -> tuple[bytes, dict[str, ByteRange]]:
    """Concatenate every item's payload into one blob, recording each item's slice.

    Args:
        items: The accepted batch, in the order their byte ranges will
            appear in the merged blob.

    Returns:
        A `(merged_payload, ranges_by_item_id)` pair: the concatenated
        bytes, and each `item_id`'s own `ByteRange` within it.
    """
    merged = bytearray()
    ranges: dict[str, ByteRange] = {}
    for item in items:
        start = len(merged)
        merged.extend(item.payload)
        ranges[item.item_id] = ByteRange(start=start, end=len(merged))
    return bytes(merged), ranges
