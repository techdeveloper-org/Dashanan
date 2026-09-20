"""Zone 8 crypto-shredding: per-subject AES-256-GCM envelope encryption (DPDP-2).

Traces to FR-008 in SRS.md. FR-008 (verbatim): "The system SHALL provide a
Consolidation Memory zone as the long-term, cross-session consolidated
store that content reaches only via the Archived state of the rotation
state machine (FR-012)."

HLD Section 10, DPDP-2 (verbatim): "Each data subject's payloads are
encrypted under a per-subject key; erasure destroys the key. The
provenance *record structure* ... survives intact ... while the
*content* becomes permanently unrecoverable." ADR-009's India Layer note
(verbatim): "Zone 8 holds the longest-lived PII. Bucket must be
in-region (DPDP residency), versioned, object-locked for the audit
window, and its per-subject encryption keys must be individually
destroyable for crypto-shredding erasure (Section 10, DPDP-2)."

Traces to AC-008-DPDP-3: "A Zone 8 blob subject to DPDP erasure has its
per-subject encryption key individually destroyable (crypto-shredding)
so the blob becomes permanently unreadable, without an in-place rewrite
of the immutable blob."

MUST-NOT-DEVIATE (sprint2_ar1_assignments.json AR1-S2-020, binding):
  1. "Do not implement erasure as an in-place blob rewrite -- erasure
     works through key destruction, not mutation" -- this module exposes
     no blob-mutation operation of any kind; `decrypt_payload` is the
     only operation that consumes a key, and destroying the key (an
     application-layer `SubjectKeyStorePort.destroy_key` call, never a
     call into this module) is what "erases" content -- the ciphertext
     bytes this module produced are never touched again.
  2. "OAQ-10's legal question ... is not settled by this story" -- no
     docstring, log message, or identifier in this module or its callers
     asserts that crypto-shredding constitutes DPDP Act 2023 erasure;
     this module implements the ENGINEERING mechanism DPDP-2 already
     approved (crypto-shredding as *a* erasure mechanism), never a legal
     conclusion.

application-security-core (Cryptographic Failures, A02): AES-256-GCM is
used -- an AEAD cipher providing both confidentiality and integrity
(tamper detection), never a hand-rolled cipher (the skill's own
"Anti-Patterns to Avoid": "custom encryption schemes"). `cryptography`
(OpenSSL-backed) is this codebase's first third-party runtime
dependency (see `pyproject.toml`'s own comment) -- a genuine, narrowly
scoped exception to the "no new dependency" convention
`local_object_store.py` and `dpdp_erasure_cascade.py` otherwise follow,
because DPDP-2 crypto-shredding cannot be correctly built on
`hashlib`/`hmac` alone (see this story's dev report, judgment-call
list).

This module holds pure domain logic only -- no I/O, no key storage, no
key lifetime management. `generate_subject_key` and
`encrypt_payload`/`decrypt_payload` are the sole cryptographic
primitives; a per-subject key's storage, retrieval, and destruction are
an application-layer concern
(`dashanan.application.zone8_crypto_shredding_store.
SubjectKeyStorePort`), mirroring `consolidated_blob.compute_blob_id`'s
identical "pure function, orchestration lives one layer up" split.

PII NOTE: this module encrypts and decrypts exactly the opaque bytes it
is given -- it never decodes, parses, or logs `payload`, key material,
or ciphertext (the same opaque treatment
`dashanan.domain.consolidated_blob.ArchiveBatchItem.payload`'s own PII
note gives archived content).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from dashanan.domain.exceptions import DashananError

AES_256_KEY_LENGTH_BYTES = 32
"""AES-256-GCM's key length (application-security-core: 'strong, current algorithms')."""

_GCM_NONCE_LENGTH_BYTES = 12
"""NIST SP 800-38D's recommended GCM nonce length -- 96 bits."""


class Zone8CryptoShreddingError(DashananError):
    """Raised on a crypto-shredding key or envelope-encryption failure.

    Kept local to this module rather than added to the shared
    `dashanan.domain.exceptions`, mirroring `consolidated_blob.
    Zone8ConsolidationError`'s identical "kept local" rationale.
    """


def generate_subject_key() -> bytes:
    """Generate a fresh, cryptographically random AES-256 key for one subject.

    Returns:
        A 32-byte key, from `AESGCM.generate_key` (a CSPRNG-backed,
        peer-reviewed generator -- never `random`/a hand-rolled PRNG,
        application-security-core's M5 secret-entropy guidance).
    """
    return AESGCM.generate_key(bit_length=AES_256_KEY_LENGTH_BYTES * 8)


def _subject_associated_data(tenant_id: str, item_id: str) -> bytes:
    """Bind ciphertext to its exact `(tenant_id, item_id)` (AEAD authenticated data).

    Without this, a ciphertext produced for one item could be decrypted
    and accepted as though it belonged to a different item under the
    same subject key -- application-security-core's Tampering/STRIDE
    guidance (integrity checks at every trust boundary). AAD is
    authenticated but not encrypted; `tenant_id`/`item_id` are already
    non-secret routing metadata (the same PII classification
    `ManifestEntry`'s own fields carry), so binding them in the clear
    here leaks nothing new.

    Raises:
        Zone8CryptoShreddingError: If `tenant_id` or `item_id` is blank.
    """
    if not tenant_id.strip():
        raise Zone8CryptoShreddingError("tenant_id must not be blank")
    if not item_id.strip():
        raise Zone8CryptoShreddingError("item_id must not be blank")
    return f"{tenant_id}:{item_id}".encode("utf-8")


@dataclass(frozen=True, slots=True)
class SealedPayload:
    """One envelope-encrypted blob: a nonce plus its AES-256-GCM ciphertext.

    Attributes:
        sealed_bytes: `nonce || ciphertext_with_tag`, the exact bytes
            `Zone8ConsolidationStore.consolidate_batch` should receive
            as an `ArchiveBatchItem.payload` in place of the plaintext.
            Opaque to every caller except `decrypt_payload`.
    """

    sealed_bytes: bytes


def encrypt_payload(
    key: bytes, payload: bytes, *, tenant_id: str, item_id: str
) -> SealedPayload:
    """Seal `payload` under `key` with AES-256-GCM, bound to `(tenant_id, item_id)`.

    Args:
        key: A 32-byte AES-256 key from `generate_subject_key`.
        payload: The plaintext bytes to seal. May be empty (an empty
            payload is a valid AES-GCM plaintext); Zone 8's own
            `ArchiveBatchItem.__post_init__` separately rejects an empty
            payload before this function is ever reached.
        tenant_id: The owning tenant, bound as authenticated data.
        item_id: The item this payload belongs to, bound as
            authenticated data (see `_subject_associated_data`).

    Returns:
        The `SealedPayload` ready to hand to `Zone8ConsolidationStore.
        consolidate_batch` as the new, ciphertext `ArchiveBatchItem.
        payload`.

    Raises:
        Zone8CryptoShreddingError: If `key` is not exactly 32 bytes, or
            `tenant_id`/`item_id` is blank.
    """
    if len(key) != AES_256_KEY_LENGTH_BYTES:
        raise Zone8CryptoShreddingError(
            f"encrypt_payload requires a {AES_256_KEY_LENGTH_BYTES}-byte key, "
            f"got {len(key)} bytes"
        )
    associated_data = _subject_associated_data(tenant_id, item_id)
    nonce = os.urandom(_GCM_NONCE_LENGTH_BYTES)
    ciphertext = AESGCM(key).encrypt(nonce, payload, associated_data)
    return SealedPayload(sealed_bytes=nonce + ciphertext)


def decrypt_payload(
    key: bytes, sealed: SealedPayload, *, tenant_id: str, item_id: str
) -> bytes:
    """Open `sealed` under `key`, verifying it was bound to `(tenant_id, item_id)`.

    Args:
        key: The same 32-byte key `encrypt_payload` sealed this payload
            under. A caller presenting the WRONG subject's key, or a key
            that has since been destroyed (crypto-shredding, must-not-
            deviate item 1), can never successfully decrypt -- there is
            no fallback, master key, or bypass.
        sealed: The `SealedPayload` `Zone8ConsolidationStore.
            read_blob_range` returned as its raw bytes.
        tenant_id: The owning tenant this payload must have been sealed
            for.
        item_id: The item this payload must have been sealed for.

    Returns:
        The original plaintext bytes.

    Raises:
        Zone8CryptoShreddingError: If `key` is not 32 bytes,
            `tenant_id`/`item_id` is blank, `sealed.sealed_bytes` is
            too short to contain a nonce, or AES-GCM authentication
            fails (wrong key, tampered ciphertext, or a
            `tenant_id`/`item_id` mismatch from what the payload was
            actually sealed for) -- AES-GCM's authenticated-encryption
            guarantee makes tampering and cross-item ciphertext reuse
            both structurally detectable, never silently "succeeding"
            with wrong plaintext.
    """
    if len(key) != AES_256_KEY_LENGTH_BYTES:
        raise Zone8CryptoShreddingError(
            f"decrypt_payload requires a {AES_256_KEY_LENGTH_BYTES}-byte key, "
            f"got {len(key)} bytes"
        )
    associated_data = _subject_associated_data(tenant_id, item_id)
    if len(sealed.sealed_bytes) < _GCM_NONCE_LENGTH_BYTES:
        raise Zone8CryptoShreddingError(
            "decrypt_payload received sealed_bytes shorter than one GCM nonce"
        )
    nonce = sealed.sealed_bytes[:_GCM_NONCE_LENGTH_BYTES]
    ciphertext = sealed.sealed_bytes[_GCM_NONCE_LENGTH_BYTES:]
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, associated_data)
    except InvalidTag as exc:
        raise Zone8CryptoShreddingError(
            "decrypt_payload authentication failed -- wrong/destroyed subject key, "
            "tampered ciphertext, or tenant_id/item_id mismatch"
        ) from exc
