"""ConsolidatedBlob / ManifestEntry: Zone 8's owned entities (HLD Section 3.9, ADR-009).

Traces to FR-008 in SRS.md. FR-008 (verbatim): "The system SHALL provide a
Consolidation Memory zone as the long-term, cross-session consolidated
store that content reaches only via the Archived state of the rotation
state machine (FR-012)."

HLD Section 3.10's Data Ownership Map RECEIVES rule for Zone 8 (AR1-S2-018
hld_ownership_crosscheck): Zone 8 accepts archived items from Zones 2, 3,
4 and 5 only -- never directly from Zone 1 (Working) or from Zones 6/7
(Retrieval-Index, Provenance), which are derived/audit tiers with no
content of their own to archive. `ALLOWED_SOURCE_ZONES` below is the
single source of truth for that rule (AC-008-2).

ADR-009 (APPROVED, must-not-deviate item 2): object-store blobs + a hot
manifest index, never re-opened as a design choice here. This module
holds the two structural halves of that shape as pure Value Objects:

  - `ManifestEntry`: the hot-index row a manifest-table lookup resolves
    `item_id` to -- `(blob_id, byte_range, compression_generation)`
    (AC-008-1).
  - `compute_blob_id`: the content-addressing function that makes a blob
    "immutable and content-addressed -- never overwritten" (must-not-
    deviate item 3) a structural property (same content -> same
    `blob_id`, always) rather than an adapter-level promise.

This module holds pure domain logic only -- no I/O, mirroring
`dashanan.domain.write_gate` and `dashanan.domain.zone2_capacity_backstop`'s
identical split. Orchestration (calling the object-store and manifest
ports, deciding defer-vs-write on an unavailable store) lives in
`dashanan.application.zone8_consolidation_store`.

MUST-NOT-DEVIATE (sprint2_ar1_assignments.json AR1-S2-018, binding):
  1. "Zone 8 is reachable only via the Archived state and is never a
     competing zone to Zones 2-5" -- this module defines no transition
     machinery of its own; it is a pure destination-store vocabulary
     that a rotation sweep (out of this story's scope, see the dev
     report's judgment-call list) is the only intended caller of.
  2. "The object-store + hot-manifest shape is locked by ADR-009
     (APPROVED) -- do not re-open it as a design choice" -- see above.
  3. "Blobs are immutable and content-addressed -- never overwritten" --
     `compute_blob_id` is the sole, deterministic authority for a blob's
     identity; two calls with identical `payload` bytes always produce
     the identical `blob_id`, so a write of already-present content is
     structurally a no-op, never an overwrite (the application-layer
     store's `ObjectStorePort.put_if_absent` port name states the same
     invariant at the adapter boundary).
  4. "Zone 8 owns ConsolidatedBlob and no other table may duplicate it,
     per Hard Rule 1" -- this module is the only place in the codebase
     that defines a consolidated-blob/manifest shape; no other zone's
     domain module (`episode.py`, `provenance_record.py`, etc.) is
     touched by this story.
  5. "PM note: only Zone 2 exists as a rotation source at ship time;
     re-test AC-008-2 against Zones 3/4/5 as each ships" -- `ALLOWED_
     SOURCE_ZONES` already lists all four zones (2/3/4/5), not just
     Zone 2, so `validate_source_zone` enforces the FULL RECEIVES rule
     from day one; no code change is needed as Zones 3/4/5 ship, only
     new tests exercising branches this function already implements.

PII NOTE: `ArchiveBatchItem.payload` is the zone's own already-Compressed
content, carried here as OPAQUE bytes this module never decodes,
inspects, or logs (dev_prompt's PII constraint: "never decoded blob
content"). Every other field is index/routing metadata -- `tenant_id`,
`item_id`, a `ZoneId`, an integer generation counter, a byte offset pair.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from dashanan.domain.exceptions import DashananError
from dashanan.domain.zone import ZoneId

ALLOWED_SOURCE_ZONES: frozenset[ZoneId] = frozenset(
    {ZoneId.EPISODIC, ZoneId.SEMANTIC, ZoneId.PROCEDURAL, ZoneId.ENTITY}
)
"""HLD Section 3.10's RECEIVES rule for Zone 8 (AC-008-2): Zones 2, 3, 4, 5
only. Zone 1 (Working) has no Archived state of its own to reach Zone 8
from (HLD Section 12A: Zone 1 evicts on TTL, never rotates), and Zones 6/7
(Retrieval-Index, Provenance) are derived/audit tiers with no owned
content to archive -- both are structurally excluded, not merely
undocumented (must-not-deviate item 1)."""


class Zone8ConsolidationError(DashananError):
    """Raised when a Zone 8 domain Value Object or batch invariant is violated.

    Kept local to this module rather than added to the shared
    `dashanan.domain.exceptions` -- this story's file-disjointness
    requirement keeps every new type this story introduces out of that
    shared file, mirroring `write_gate.ProvenanceJournalPort`'s and
    `zone2_capacity_backstop.Zone2CapacityBackstopError`'s identical
    rationale.
    """


class Zone8UnprovisionedTenantError(Zone8ConsolidationError):
    """Raised when a consolidation batch targets a tenant with no provisioned Zone 8 bucket.

    Sibling of `Zone8ConsolidationError`, mirroring `ByteRange`'s and
    `validate_source_zone`'s own "fail the whole batch, never partially
    write" precedent (AC-008-2, AC-008-3): a subclass rather than a
    bare `Zone8ConsolidationError` instance so a caller can catch this
    specific, provisioning-scoped rejection distinctly, while still
    being caught by any existing `except Zone8ConsolidationError`
    handler that does not need the distinction (DSHN-69, AC-008-DPDP-2).

    This is a structural, fail-closed rejection -- HLD Section 10
    DPDP-4's "all stores in-region for tenants handling Indian personal
    data" residency guarantee would be silently defeated if a batch for
    a never-provisioned tenant were accepted and durably written before
    that tenant's bucket exists. Raised by
    `dashanan.application.zone8_consolidation_store.
    Zone8ConsolidationStore.consolidate_batch` (an I/O-backed check
    against an injected `TenantProvisioningCheckPort`), never by this
    (I/O-free) domain module itself -- kept here only as the typed
    vocabulary, mirroring how `read_blob_range`'s "no manifest entry"
    case already reuses this module's `Zone8ConsolidationError` from
    the application layer.
    """


def validate_source_zone(source_zone: ZoneId) -> None:
    """AC-008-2: reject any write not attributed to an allowed source zone.

    The sole authority on whether Zone 8 may accept an item from
    `source_zone` -- both the application-layer store and this module's
    own `ArchiveBatchItem.__post_init__` call this, so there is exactly
    one place the RECEIVES rule is expressed (must-not-deviate item 5:
    already covers Zones 2/3/4/5, not merely Zone 2).

    Raises:
        Zone8ConsolidationError: If `source_zone` is not one of
            `ALLOWED_SOURCE_ZONES` -- including Zone 1 (Working) and
            Zones 6/7 (Retrieval-Index, Provenance), per HLD Section
            3.10's RECEIVES rule.
    """
    if source_zone not in ALLOWED_SOURCE_ZONES:
        raise Zone8ConsolidationError(
            f"Zone 8 does not accept writes originating from zone "
            f"'{source_zone.value}' -- only "
            f"{sorted(z.value for z in ALLOWED_SOURCE_ZONES)} may archive "
            "into Consolidation Memory (HLD Section 3.10 RECEIVES rule, "
            "AC-008-2)"
        )


def compute_blob_id(payload: bytes) -> str:
    """Content-address `payload`: `blob_id = sha256(payload)` hex digest.

    Must-not-deviate item 3's "immutable and content-addressed -- never
    overwritten" made structural: identical bytes always produce the
    identical `blob_id`, so `ObjectStorePort.put_if_absent` (application
    layer) writing under this id is, by construction, either a genuine
    first write or a no-op against already-present, byte-identical
    content -- there is no code path that could compute this id and then
    legitimately want to overwrite what it already names.

    Args:
        payload: The exact bytes to be stored as one object-store blob.
            Never empty -- see `ArchiveBatchItem.__post_init__`.

    Returns:
        A 64-character lowercase hex SHA-256 digest of `payload`.
    """
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class ByteRange:
    """A half-open `[start, end)` byte offset pair into one object-store blob.

    ADR-009's hot-manifest shape resolves `item_id` to `(blob_id,
    byte_range, compression_generation)` (AC-008-1) because a single
    consolidation merge batch (AC-008-3) packs more than one item's
    payload into ONE shared blob -- `byte_range` is what lets the
    manifest point one `item_id` at its own slice of that shared blob
    without a second, per-item blob.

    Attributes:
        start: The first byte offset (inclusive) this item's slice
            occupies within the named blob. Never negative.
        end: The byte offset (exclusive) this item's slice ends at.
            Always strictly greater than `start`.
    """

    start: int
    end: int

    def __post_init__(self) -> None:
        """Raises:
        Zone8ConsolidationError: If `start` is negative, or `end` is not
            strictly greater than `start` (a zero-or-negative-length
            range could never resolve to real content).
        """
        if self.start < 0:
            raise Zone8ConsolidationError(
                f"ByteRange.start must be >= 0, got {self.start}"
            )
        if self.end <= self.start:
            raise Zone8ConsolidationError(
                f"ByteRange.end ({self.end}) must be strictly greater "
                f"than ByteRange.start ({self.start})"
            )

    def length(self) -> int:
        """The number of bytes this range covers (`end - start`)."""
        return self.end - self.start


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    """One hot-manifest row: what `item_id` resolves to inside Zone 8 (AC-008-1).

    Attributes:
        tenant_id: The owning tenant (HLD 3.0 invariant 2). Never blank.
        item_id: The archived item this row resolves reads for. Never
            blank. Unique per `tenant_id` (the manifest-table PK a
            concrete `ManifestPort` adapter enforces).
        blob_id: Which object-store blob holds this item's content --
            always `compute_blob_id(...)` of the blob's exact bytes,
            never a caller-chosen identifier (must-not-deviate item 3).
        byte_range: Where within `blob_id` this item's own slice lives.
        compression_generation: Which compression-encoding generation
            `blob_id`'s bytes were written under (an opaque, monotonic
            version counter the zone that compressed the item assigns;
            this module never decodes or interprets it). Never negative.
        source_zone: Which zone originated this archived item (AC-008-2)
            -- always one of `ALLOWED_SOURCE_ZONES`.
        written_at: When this manifest row was written.
        subject_id: ADR-006's mandatory subject_id secondary index value
            (DASH-STORY-020), written into this row at the SAME INSERT
            that writes every other field -- `None` for a caller (e.g.
            `ArchiveEngine`'s own un-keyed sweep path) that has no
            subject to attribute this item to yet. Because this field is
            set here, at construction, a conforming `ManifestPort`
            adapter never needs a second write to backfill it -- see
            `dashanan.infrastructure.sql_manifest_repository.
            SqlManifestRepository.insert_batch`'s docstring, and
            `dashanan.infrastructure.sql_zone8_subject_index_repository.
            SqlZone8SubjectIndexRepository`'s module docstring for the
            append-only-invariant history this field's addition closes.
    """

    tenant_id: str
    item_id: str
    blob_id: str
    byte_range: ByteRange
    compression_generation: int
    source_zone: ZoneId
    written_at: datetime
    subject_id: str | None = None

    def __post_init__(self) -> None:
        """Raises:
        Zone8ConsolidationError: If `tenant_id`, `item_id`, or `blob_id`
            is blank, `compression_generation` is negative, `source_zone`
            is not in `ALLOWED_SOURCE_ZONES`, or `subject_id` is a
            non-`None` blank string.
        """
        if not self.tenant_id.strip():
            raise Zone8ConsolidationError("ManifestEntry.tenant_id must not be blank")
        if not self.item_id.strip():
            raise Zone8ConsolidationError("ManifestEntry.item_id must not be blank")
        if not self.blob_id.strip():
            raise Zone8ConsolidationError("ManifestEntry.blob_id must not be blank")
        if self.compression_generation < 0:
            raise Zone8ConsolidationError(
                "ManifestEntry.compression_generation must be >= 0, got "
                f"{self.compression_generation}"
            )
        if self.subject_id is not None and not self.subject_id.strip():
            raise Zone8ConsolidationError(
                "ManifestEntry.subject_id must be None or non-blank, never an "
                "empty/whitespace string"
            )
        validate_source_zone(self.source_zone)

    @classmethod
    def for_write(
        cls,
        *,
        tenant_id: str,
        item_id: str,
        blob_id: str,
        byte_range: ByteRange,
        compression_generation: int,
        source_zone: ZoneId,
        written_at: datetime,
        subject_id: str | None = None,
    ) -> ManifestEntry:
        """The sole sanctioned constructor for a manifest row about to be persisted.

        Builder-style factory (mirrors `ProvenanceRecord.create`'s
        identical "one sanctioned construction path" convention): every
        caller that builds a `ManifestEntry` for a real write goes
        through this classmethod rather than the bare dataclass
        constructor, so a future added invariant has exactly one call
        site to update. Today it is a thin, explicit pass-through over
        `__post_init__`'s own validation; kept as a named factory for
        that single-call-site property, not because it currently adds
        computation the dataclass constructor lacks.
        """
        return cls(
            tenant_id=tenant_id,
            item_id=item_id,
            blob_id=blob_id,
            byte_range=byte_range,
            compression_generation=compression_generation,
            source_zone=source_zone,
            written_at=written_at,
            subject_id=subject_id,
        )


@dataclass(frozen=True, slots=True)
class ArchiveBatchItem:
    """One item a caller wants Zone 8 to archive, as part of one merge batch.

    Attributes:
        tenant_id: The owning tenant. Never blank.
        item_id: The item's identifier in its `source_zone`. Never
            blank.
        source_zone: Which zone this item is being archived from.
        payload: The item's own already-Compressed content, as opaque
            bytes this module never decodes (PII note above). Never
            empty -- an empty payload could not be given a meaningful
            `ByteRange` inside a merged blob.
        subject_id: The DPDP data subject this item belongs to
            (DASH-STORY-020, ADR-006), when the caller already knows it
            at archive time -- e.g. `Zone8SubjectKeyedArchiver.
            archive_for_subject`. `None` for a caller with no subject to
            attribute this item to (e.g. `ArchiveEngine`'s un-keyed
            weekly sweep). Carried straight through to `ManifestEntry.
            subject_id` by `Zone8ConsolidationStore.consolidate_batch`,
            so a conforming `ManifestPort` adapter writes it in the same
            INSERT as every other field -- never a later backfill write.

    Raises:
        Zone8ConsolidationError: If `tenant_id` or `item_id` is blank,
            `payload` is empty, `source_zone` is not in
            `ALLOWED_SOURCE_ZONES` (AC-008-2), or `subject_id` is a
            non-`None` blank string.
    """

    tenant_id: str
    item_id: str
    source_zone: ZoneId
    payload: bytes
    subject_id: str | None = None

    def __post_init__(self) -> None:
        if not self.tenant_id.strip():
            raise Zone8ConsolidationError("ArchiveBatchItem.tenant_id must not be blank")
        if not self.item_id.strip():
            raise Zone8ConsolidationError("ArchiveBatchItem.item_id must not be blank")
        if not self.payload:
            raise Zone8ConsolidationError("ArchiveBatchItem.payload must not be empty")
        if self.subject_id is not None and not self.subject_id.strip():
            raise Zone8ConsolidationError(
                "ArchiveBatchItem.subject_id must be None or non-blank, never an "
                "empty/whitespace string"
            )
        validate_source_zone(self.source_zone)


def validate_tenant_uniform_batch(items: Sequence[ArchiveBatchItem]) -> None:
    """AC-008-3 / HLD threat I-5: fail the WHOLE batch on any cross-tenant mix.

    This is a hard, fail-closed gate -- it RAISES, it never merely warns
    or logs (the sprint2 review's own explicit stress: "verify the
    tenant-uniformity merge assertion actually FAILS the batch ... a
    warning-only implementation is a FAIL on this checklist, not a
    PASS"). The application-layer store calls this FIRST, before writing
    a single byte to the object store or the manifest for ANY item in
    `items`, so a cross-tenant batch produces zero partial writes, not a
    partially-completed cross-tenant merge.

    Args:
        items: The candidate merge batch, not yet validated per-item
            (source-zone validation already happened at each
            `ArchiveBatchItem`'s own construction).

    Raises:
        Zone8ConsolidationError: If `items` contains more than one
            distinct `tenant_id`. An empty or single-tenant `items` is
            always accepted (a batch of zero or one tenant cannot, by
            definition, be a cross-tenant merge).
    """
    tenant_ids = {item.tenant_id for item in items}
    if len(tenant_ids) > 1:
        raise Zone8ConsolidationError(
            "consolidation merge batch spans more than one tenant_id "
            f"({sorted(tenant_ids)}) -- rejecting the entire batch rather "
            "than completing a cross-tenant merge (HLD threat I-5, "
            "AC-008-3)"
        )
