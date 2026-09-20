"""InMemorySubjectKeyStore: Shape A `SubjectKeyStorePort` -- an in-process key registry.

Mirrors `dashanan.infrastructure.dpdp_erasure_cascade.
InMemoryErasureObligationStore`'s own "Shape A now, Shape B later" split
(this codebase's established convention). A Shape B adapter (a KMS/Vault
`SubjectKeyStorePort`, application-security-core's "Dynamic Secrets"/
"HashiCorp Vault" section) is explicitly out of this story's scope.

PII / SECRETS NOTE: this store holds raw key bytes in process memory
only -- it never logs, serializes, or persists them to disk
(application-security-core, Secrets Management: "never hardcode... log
sensitive data"). `destroy_key` removes the dict entry; Python's
garbage collector, not this class, is responsible for reclaiming the
underlying bytes object -- this adapter makes no further attempt at
secure memory wiping, a limitation shared with every pure-Python secrets
handler already in this codebase (mirrors `tenant_credential.py`'s own
signing key, held as an ordinary `bytes` object).
"""

from __future__ import annotations

from dashanan.application.zone8_crypto_shredding_store import Zone8CryptoShreddingStoreError
from dashanan.domain.zone8_crypto_shredding import generate_subject_key


class InMemorySubjectKeyStore:
    """Implements `SubjectKeyStorePort` against an in-process `dict`."""

    def __init__(self) -> None:
        self._keys: dict[tuple[str, str], bytes] = {}

    def get_or_create_key(self, tenant_id: str, subject_id: str) -> bytes:
        """Return `(tenant_id, subject_id)`'s key, minting one on first use.

        Raises:
            Zone8CryptoShreddingStoreError: If `tenant_id` or
                `subject_id` is blank.
        """
        self._require_non_blank(tenant_id, subject_id)
        key = (tenant_id, subject_id)
        if key not in self._keys:
            self._keys[key] = generate_subject_key()
        return self._keys[key]

    def get_key(self, tenant_id: str, subject_id: str) -> bytes | None:
        """Return `(tenant_id, subject_id)`'s key, or `None` if never created or destroyed."""
        return self._keys.get((tenant_id, subject_id))

    def destroy_key(self, tenant_id: str, subject_id: str) -> bool:
        """Remove `(tenant_id, subject_id)`'s key. Idempotent: `False` if none existed."""
        return self._keys.pop((tenant_id, subject_id), None) is not None

    @staticmethod
    def _require_non_blank(tenant_id: str, subject_id: str) -> None:
        if not tenant_id.strip():
            raise Zone8CryptoShreddingStoreError(
                "InMemorySubjectKeyStore requires a non-blank tenant_id"
            )
        if not subject_id.strip():
            raise Zone8CryptoShreddingStoreError(
                "InMemorySubjectKeyStore requires a non-blank subject_id"
            )
