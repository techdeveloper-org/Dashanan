"""InMemoryZone8SubjectIndex: Shape A `Zone8SubjectIndexPort` -- an in-process index.

Mirrors `InMemorySubjectKeyStore`'s identical Shape A rationale. A Shape
B adapter is `dashanan.infrastructure.sql_zone8_subject_index_repository.
SqlZone8SubjectIndexRepository`, querying the real `subject_id` column
`zone8_manifest_subject_index_migration.sql` adds to `zone8_manifest`
(ADR-006, must-not-deviate item 3).
"""

from __future__ import annotations

from collections import defaultdict

from dashanan.application.zone8_crypto_shredding_store import Zone8CryptoShreddingStoreError


class InMemoryZone8SubjectIndex:
    """Implements `Zone8SubjectIndexPort` against an in-process `dict` of lists."""

    def __init__(self) -> None:
        self._items_by_subject: dict[tuple[str, str], list[str]] = defaultdict(list)

    def record_item(self, tenant_id: str, subject_id: str, item_id: str) -> None:
        """Record `item_id` as archived under `(tenant_id, subject_id)`.

        Idempotent: recording the same `item_id` twice for the same
        subject does not duplicate it in `find_item_ids`'s result.

        Raises:
            Zone8CryptoShreddingStoreError: If any argument is blank.
        """
        self._require_non_blank(tenant_id, subject_id, item_id)
        key = (tenant_id, subject_id)
        if item_id not in self._items_by_subject[key]:
            self._items_by_subject[key].append(item_id)

    def find_item_ids(self, tenant_id: str, subject_id: str) -> tuple[str, ...]:
        """Return every `item_id` recorded under `(tenant_id, subject_id)`, in record order."""
        return tuple(self._items_by_subject.get((tenant_id, subject_id), ()))

    @staticmethod
    def _require_non_blank(tenant_id: str, subject_id: str, item_id: str) -> None:
        if not tenant_id.strip():
            raise Zone8CryptoShreddingStoreError(
                "InMemoryZone8SubjectIndex requires a non-blank tenant_id"
            )
        if not subject_id.strip():
            raise Zone8CryptoShreddingStoreError(
                "InMemoryZone8SubjectIndex requires a non-blank subject_id"
            )
        if not item_id.strip():
            raise Zone8CryptoShreddingStoreError(
                "InMemoryZone8SubjectIndex requires a non-blank item_id"
            )
