"""Zone8SubjectKeyedArchiver: envelope-encrypts Zone 8 archives per DPDP subject key.

Traces to FR-008 in SRS.md. See `dashanan.domain.zone8_crypto_shredding`'s
own docstring for FR-008's verbatim text and the DPDP-2/ADR-009 sourcing.

Traces to AC-008-DPDP-3: "A Zone 8 blob subject to DPDP erasure has its
per-subject encryption key individually destroyable (crypto-shredding)
so the blob becomes permanently unreadable, without an in-place rewrite
of the immutable blob."

This module is a Decorator (python-design-patterns-core Section 10)
around `dashanan.application.zone8_consolidation_store.
Zone8ConsolidationStore` -- DASH-STORY-018's own Facade, used here
exactly as any other caller would use it, never modified. Every
`ArchiveBatchItem` this class hands to `Zone8ConsolidationStore.
consolidate_batch` already carries AES-256-GCM ciphertext as its
`payload` (`dashanan.domain.zone8_crypto_shredding.encrypt_payload`'s
output) -- Zone 8's own Facade and its `ObjectStorePort`/`ManifestPort`
adapters never see, and never need to see, subject-key material or
plaintext; they continue to treat `payload` as the opaque bytes their
own PII notes already promise.

Composes two new, local ports this story owns:
  - `SubjectKeyStorePort`: generates, retrieves, and destroys one
    AES-256 key per `(tenant_id, subject_id)` (crypto-shredding's
    storage half).
  - `Zone8SubjectIndexPort`: ADR-006's mandatory subject_id secondary
    index (AC-008-DPDP-1), scoped to Zone 8 -- resolves `subject_id` to
    every `item_id` archived under it, so `erase_subject` can name every
    now-unreadable item without a manifest table scan.

MUST-NOT-DEVIATE (sprint2_ar1_assignments.json AR1-S2-020, binding):
  1. "Do not implement erasure as an in-place blob rewrite" --
     `erase_subject` below calls `SubjectKeyStorePort.destroy_key`
     exactly once and touches no `ObjectStorePort`/`ManifestPort`
     method; the ciphertext blob is never re-read, re-written, or
     deleted.
  3. "The cascade's reach into Zone 8 depends on the subject_id
     secondary index (ADR-006) being present on the Zone 8 manifest
     table" -- `Zone8SubjectIndexPort`'s concrete SQL adapter
     (`dashanan.infrastructure.sql_zone8_subject_index_repository.
     SqlZone8SubjectIndexRepository`) queries exactly that index, added
     by `dashanan.infrastructure.zone8_manifest_subject_index_migration
     .sql` (a migration, not a rewrite of DASH-STORY-018's own
     `zone8_consolidation_schema.sql` -- this story's file-disjointness
     requirement). See this story's dev report, judgment-call list, for
     why the fully-integrated column lives on a migration file rather
     than on the original CREATE TABLE, and the corresponding
     `shared_file_request`.

DEFINITION OF DONE: "ZoneRepository port treated as frozen (AR1-G2)" --
this module imports no `ZoneRepository` and defines no new zone-fetch
port; `ObjectStorePort`/`ManifestPort` (Zone8ConsolidationStore's own
seams) are used, not widened or re-opened.

PII NOTE: this module never logs subject key material, plaintext
payload, or ciphertext bytes -- only `tenant_id`/`subject_id`/`item_id`
routing metadata, the same PII classification ceiling
`Zone8ConsolidationStore`'s own logging already applies.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from dashanan.application.zone8_consolidation_store import (
    ConsolidationBatchResult,
    Zone8ConsolidationStore,
)
from dashanan.domain.consolidated_blob import ArchiveBatchItem
from dashanan.domain.exceptions import DashananError
from dashanan.domain.zone8_crypto_shredding import (
    SealedPayload,
    Zone8CryptoShreddingError,
    decrypt_payload,
    encrypt_payload,
)

logger = logging.getLogger(__name__)


class Zone8CryptoShreddingStoreError(DashananError):
    """Raised by a `SubjectKeyStorePort`/`Zone8SubjectIndexPort` operational failure.

    Kept local to this module, mirroring `zone8_consolidation_store.
    Zone8StorePortError`'s identical "kept local" rationale.
    """


@runtime_checkable
class SubjectKeyStorePort(Protocol):
    """Generates, retrieves, and destroys one AES-256 key per `(tenant_id, subject_id)`.

    Kept local to this module per this story's file-disjointness
    requirement, mirroring `zone8_consolidation_store.ObjectStorePort`'s
    identical local-Protocol choice.

    Security contract (application-security-core, Secrets Management):
    a conforming adapter never logs key material, and `destroy_key` is
    irreversible -- there is no "undo" or key-recovery operation on this
    Protocol, matching crypto-shredding's own "destruction-is-erasure"
    design (HLD Section 10, DPDP-2).
    """

    def get_or_create_key(self, tenant_id: str, subject_id: str) -> bytes:
        """Return `(tenant_id, subject_id)`'s AES-256 key, minting one on first use.

        Returns the SAME 32-byte key on every call for the same
        `(tenant_id, subject_id)` until `destroy_key` is called for it --
        required so every item archived for one subject, across however
        many `archive_for_subject` calls, can be decrypted by the one
        key `erase_subject` later destroys.

        Raises:
            Zone8CryptoShreddingStoreError: If the key cannot be
                generated or retrieved (e.g. the underlying store is
                unreachable).
        """
        ...

    def get_key(self, tenant_id: str, subject_id: str) -> bytes | None:
        """Return `(tenant_id, subject_id)`'s key, or `None` if never created or destroyed.

        Raises:
            Zone8CryptoShreddingStoreError: If the underlying store
                cannot be queried.
        """
        ...

    def destroy_key(self, tenant_id: str, subject_id: str) -> bool:
        """Irreversibly destroy `(tenant_id, subject_id)`'s key -- the crypto-shred itself.

        Returns:
            `True` if a key existed and was destroyed, `False` if none
            existed (idempotent: a repeated erasure request for the
            same subject is a no-op, never an error).

        Raises:
            Zone8CryptoShreddingStoreError: If the destroy operation
                itself cannot be completed (e.g. the underlying store is
                unreachable) -- distinct from "no key existed", which
                returns `False` rather than raising.
        """
        ...


@runtime_checkable
class Zone8SubjectIndexPort(Protocol):
    """ADR-006's mandatory subject_id secondary index, scoped to Zone 8 (AC-008-DPDP-1).

    Kept local to this module per this story's file-disjointness
    requirement. A concrete adapter's underlying query is an indexed
    `(tenant_id, subject_id)` lookup -- never a full manifest scan,
    mirroring `ManifestPort.find_by_item_id`'s own O(1)/O(log n)
    indexed-lookup contract.
    """

    def record_item(self, tenant_id: str, subject_id: str, item_id: str) -> None:
        """Ensure `item_id` is indexed under `subject_id`, so `find_item_ids` can resolve it later.

        Called once per `archive_for_subject` call, immediately after
        `Zone8ConsolidationStore.consolidate_batch` durably writes
        `item_id`. `item_id`'s own `subject_id` is already passed into
        `consolidate_batch` as part of the `ArchiveBatchItem` this call
        received (DASH-STORY-020's fully-integrated fix), so a
        conforming adapter whose index IS the manifest row itself (e.g.
        `SqlZone8SubjectIndexRepository`) has nothing left to do here and
        implements this as a no-op; an adapter whose index is a
        genuinely separate structure (e.g. `InMemoryZone8SubjectIndex`)
        still uses this call to populate it.

        Raises:
            Zone8CryptoShreddingStoreError: If the index write fails, or
                any argument is blank.
        """
        ...

    def find_item_ids(self, tenant_id: str, subject_id: str) -> tuple[str, ...]:
        """Resolve every Zone 8 `item_id` ever archived under `subject_id`.

        Raises:
            Zone8CryptoShreddingStoreError: If the index cannot be
                queried.
        """
        ...


class Zone8SubjectKeyedArchiver:
    """Envelope-encrypts one item per subject key before Zone 8 ever sees it.

    Composed from the existing `Zone8ConsolidationStore` Facade plus
    this story's two new local ports. Construction never touches I/O.
    """

    def __init__(
        self,
        zone8_store: Zone8ConsolidationStore,
        key_store: SubjectKeyStorePort,
        subject_index: Zone8SubjectIndexPort,
    ) -> None:
        self._zone8_store = zone8_store
        self._key_store = key_store
        self._subject_index = subject_index

    def archive_for_subject(
        self, item: ArchiveBatchItem, subject_id: str
    ) -> ConsolidationBatchResult:
        """Seal `item.payload` under `subject_id`'s key, then archive it into Zone 8.

        Args:
            item: The candidate item to archive. Its `payload` MUST be
                plaintext -- this method performs the sealing, callers
                must never pre-encrypt it themselves.
            subject_id: The data subject `item` belongs to. Never blank.

        Returns:
            `Zone8ConsolidationStore.consolidate_batch`'s own result for
            the one-item batch this call submits.

        Raises:
            Zone8CryptoShreddingError: If `subject_id` is blank, or the
                key store returns a malformed key.
            Zone8CryptoShreddingStoreError: If the key store or subject
                index cannot be reached.
            dashanan.domain.consolidated_blob.Zone8ConsolidationError:
                Propagated unchanged from `consolidate_batch` (e.g.
                AC-008-2 source-zone rejection).
        """
        if not subject_id.strip():
            raise Zone8CryptoShreddingError(
                "Zone8SubjectKeyedArchiver.archive_for_subject requires a "
                "non-blank subject_id"
            )
        key = self._key_store.get_or_create_key(item.tenant_id, subject_id)
        sealed = encrypt_payload(
            key, item.payload, tenant_id=item.tenant_id, item_id=item.item_id
        )
        sealed_item = ArchiveBatchItem(
            tenant_id=item.tenant_id,
            item_id=item.item_id,
            source_zone=item.source_zone,
            payload=sealed.sealed_bytes,
            subject_id=subject_id,
        )
        result = self._zone8_store.consolidate_batch([sealed_item])
        for written in result.written:
            self._subject_index.record_item(item.tenant_id, subject_id, written.item_id)
        return result

    def read_for_subject(self, tenant_id: str, item_id: str, subject_id: str) -> bytes:
        """Read `item_id`'s Zone 8 blob and decrypt it under `subject_id`'s key.

        Raises:
            Zone8CryptoShreddingError: If `subject_id`'s key was
                destroyed (crypto-shredded) or never existed -- the
                blob is permanently unreadable, by design (must-not-
                deviate item 1) -- or AES-GCM authentication fails.
            Zone8CryptoShreddingStoreError: If the key store cannot be
                reached.
            dashanan.domain.consolidated_blob.Zone8ConsolidationError:
                Propagated unchanged if `item_id` has no Zone 8
                manifest entry.
        """
        key = self._key_store.get_key(tenant_id, subject_id)
        if key is None:
            raise Zone8CryptoShreddingError(
                f"subject_id '{subject_id}' has no active Zone 8 key for tenant "
                f"'{tenant_id}' -- destroyed by a prior DPDP erasure, or never archived"
            )
        sealed_bytes = self._zone8_store.read_blob_range(tenant_id, item_id)
        return decrypt_payload(
            key,
            SealedPayload(sealed_bytes=sealed_bytes),
            tenant_id=tenant_id,
            item_id=item_id,
        )

    def erase_subject(self, tenant_id: str, subject_id: str) -> tuple[str, ...]:
        """Crypto-shred `subject_id`: destroy its key, without touching any blob.

        AC-008-DPDP-1's "cascade reaches Zone 8 archives": resolves
        every `item_id` this subject ever archived (via
        `Zone8SubjectIndexPort.find_item_ids`) purely for reporting --
        the actual erasure is the single `destroy_key` call, which alone
        renders every one of those items' ciphertext permanently
        unreadable (must-not-deviate item 1: no blob is read, rewritten,
        or deleted here).

        Returns:
            Every `item_id` this subject had archived into Zone 8, now
            unreadable. Empty if `subject_id` had no Zone 8 archives (a
            valid, non-error outcome -- an erasure cascade covering a
            subject with no Zone 8 footprint yet is not a failure).

        Raises:
            ValueError: If `tenant_id` or `subject_id` is blank.
            Zone8CryptoShreddingStoreError: If the key store or subject
                index cannot be reached.
        """
        if not tenant_id.strip():
            raise ValueError("erase_subject requires a non-blank tenant_id")
        if not subject_id.strip():
            raise ValueError("erase_subject requires a non-blank subject_id")

        item_ids = self._subject_index.find_item_ids(tenant_id, subject_id)
        destroyed = self._key_store.destroy_key(tenant_id, subject_id)
        logger.info(
            "zone8 crypto-shredding erasure applied",
            extra={
                "tenant_id": tenant_id,
                "item_count": len(item_ids),
                "key_destroyed": destroyed,
            },
        )
        return item_ids
