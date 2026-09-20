"""Adversarial integration tests for Zone 8 crypto-shredding (DASH-STORY-020).

Traces to FR-008 in SRS.md and AC-008-DPDP-3: "A Zone 8 blob subject to
DPDP erasure has its per-subject encryption key individually destroyable
(crypto-shredding) so the blob becomes permanently unreadable, without an
in-place rewrite of the immutable blob."

This suite exercises `Zone8SubjectKeyedArchiver` (application layer) and
`dashanan.domain.zone8_crypto_shredding` (domain layer) via
`Sprint2WiredSystem` (`tests/integration/conftest_sprint2.py`) against
three adversarial scenarios that the happy-path suites do not cover:

  1. Cross-subject key confusion: decrypting subject A's ciphertext under
     subject B's key must fail closed with a typed exception, never a
     silent wrong plaintext.
  2. AAD tampering: decrypting subject A's ciphertext with a manipulated
     `item_id`/`tenant_id` (the exact bytes AES-GCM authenticates per
     `zone8_crypto_shredding._subject_associated_data`) must fail closed
     the same way.
  3. Post-erasure read: once `SubjectKeyStorePort.destroy_key` has run
     (the crypto-shred itself), any further decrypt attempt for that
     subject must fail closed.

A fourth scenario documents, rather than asserts away, a real finding:
`Zone8ConsolidationStore.consolidate_batch` (and therefore
`Zone8SubjectKeyedArchiver.archive_for_subject`, which is a thin
encrypt-then-delegate wrapper around it -- see
`zone8_crypto_shredding_store.py`'s own module docstring) never calls
`LocalTenantBucketProvisioner`/`TenantBucketProvisioningService` or any
other bucket-provisioning port. Nothing in the composed call chain checks
whether the tenant was ever provisioned via
`LocalTenantBucketProvisioner` before accepting a batch. This suite
confirms that gap exists today rather than forcing a false pass.

Does not modify `src/` or `tests/integration/conftest_sprint2.py`.

Runtime assumptions (mirrors `conftest_sprint2.py`'s own docstring):
  - tenant_id: `DEFAULT_TENANT_ID` ("tenant-1"), used for every request
    below unless a test names a second, unprovisioned tenant explicitly.
  - No fixture seed is random.

PII NOTE: every payload below is a plain ASCII placeholder byte string
(`b"payload-for-subject-a"` and similar), never realistic conversational
content -- mirrors `conftest_sprint2.py`'s own PII posture.
"""

from __future__ import annotations

import pytest

from dashanan.application.zone8_consolidation_store import Zone8ConsolidationStore
from dashanan.application.zone8_crypto_shredding_store import Zone8SubjectKeyedArchiver
from dashanan.domain.consolidated_blob import ArchiveBatchItem, Zone8UnprovisionedTenantError
from dashanan.domain.tenant_bucket_policy import BucketProvisioningSpec, ResidencyRegion
from dashanan.domain.zone import ZoneId
from dashanan.domain.zone8_crypto_shredding import (
    SealedPayload,
    Zone8CryptoShreddingError,
    decrypt_payload,
)
from dashanan.infrastructure.in_memory_subject_key_store import InMemorySubjectKeyStore
from dashanan.infrastructure.in_memory_zone8_subject_index import InMemoryZone8SubjectIndex

pytest_plugins = ["tests.integration.conftest_sprint2"]

DEFAULT_TENANT_ID = "tenant-1"
"""Mirrors `tests.integration.conftest.DEFAULT_TENANT_ID`'s own value (Sprint 1)."""


def _seed_manifest_read_from_last_insert(sprint2_wired_system) -> tuple:
    """Work around `conftest_sprint2.py`'s fake manifest connection not persisting writes.

    `Sprint2WiredSystem.zone8_manifest_connection` is a
    `RecordingConnectionWithCommit` wrapping a `RecordingCursorWithFetchone`
    (both defined in `conftest_sprint2.py`, never modified here) -- a pure
    recorder, not a real store: `execute()` only appends `(sql, params)` to
    `.executed`, and `fetchone()` always returns whatever static `_rows`
    list was supplied at construction (empty by default), regardless of
    what was actually inserted. `SqlManifestRepository.insert_batch`'s
    `INSERT` is therefore invisible to `SqlManifestRepository.
    find_by_item_id`'s later `SELECT` through this fixture -- a real
    read-after-write via `Zone8ConsolidationStore.read_blob_range` (and
    therefore `Zone8SubjectKeyedArchiver.read_for_subject`) would always
    raise `Zone8ConsolidationError` ("no Zone 8 manifest entry"), for an
    item that really was archived, purely because this fixture's SQL
    double never round-trips.

    This helper captures the params of the most recently recorded
    `INSERT` (from `archive_for_subject`'s own `consolidate_batch` ->
    `insert_batch` call) and feeds them back as the canned row for the
    fake cursor's *next* `fetchone()` -- simulating the one row a real
    manifest table would already hold. It touches only the fixture
    instance's already-exposed, already-mutable recorder state
    (`cursor_obj.executed` / `cursor_obj._rows`); it does not modify
    `conftest_sprint2.py` or any `src/` file. Must be called again before
    every subsequent read of a *different* archived item, since the fake
    cursor answers every `fetchone()` with the one row most recently
    seeded, irrespective of the `SELECT`'s own `WHERE` clause.

    The 9-column `INSERT` row (`tenant_id, item_id, blob_id, byte_start,
    byte_end, compression_generation, source_zone, written_at,
    subject_id`) is truncated to the 8-column `SELECT` shape
    `SqlManifestRepository._row_to_entry` expects (`subject_id` is written
    but never selected back) -- see `sql_manifest_repository.py`'s own
    `_SELECT_COLUMNS`/`_INSERT_SQL` constants for the exact column sets
    this mirrors.

    Returns:
        The 8-column row tuple that was seeded, so a caller can re-seed
        it verbatim before a second read of the same item without
        re-deriving it from `.executed` again.
    """
    cursor = sprint2_wired_system.zone8_manifest_connection.cursor_obj
    _insert_sql, insert_params = cursor.executed[-1]
    manifest_row = tuple(insert_params[:8])
    cursor._rows = [manifest_row]
    return manifest_row


def _archive_item(
    *, tenant_id: str, item_id: str, payload: bytes
) -> ArchiveBatchItem:
    """Build one plaintext `ArchiveBatchItem` ready for `archive_for_subject`.

    `source_zone=ZoneId.PROCEDURAL` is an allowed Zone 8 source
    (`ALLOWED_SOURCE_ZONES`, AC-008-2) -- any allowed zone works equally
    for this suite's crypto-shredding focus.
    """
    return ArchiveBatchItem(
        tenant_id=tenant_id,
        item_id=item_id,
        source_zone=ZoneId.PROCEDURAL,
        payload=payload,
    )


class TestCrossSubjectKeyConfusionFailsClosed:
    """Decrypting subject A's ciphertext under subject B's key must fail closed."""

    def test_wrong_subject_key_raises_typed_error_not_wrong_plaintext(
        self, sprint2_wired_system
    ):
        archiver: Zone8SubjectKeyedArchiver = sprint2_wired_system.zone8_subject_keyed_archiver

        item_a = _archive_item(
            tenant_id=DEFAULT_TENANT_ID,
            item_id="item-subject-a-1",
            payload=b"payload-for-subject-a",
        )
        result = archiver.archive_for_subject(item_a, subject_id="subject-a")
        assert len(result.written) == 1, "precondition: item A must have archived"
        _seed_manifest_read_from_last_insert(sprint2_wired_system)

        # Force-mint subject B's key so it exists to be (wrongly) tried.
        sprint2_wired_system.zone8_subject_key_store.get_or_create_key(
            DEFAULT_TENANT_ID, "subject-b"
        )
        wrong_key = sprint2_wired_system.zone8_subject_key_store.get_key(
            DEFAULT_TENANT_ID, "subject-b"
        )

        sealed_bytes = sprint2_wired_system.zone8_consolidation_store.read_blob_range(
            DEFAULT_TENANT_ID, "item-subject-a-1"
        )

        with pytest.raises(Zone8CryptoShreddingError) as excinfo:
            decrypt_payload(
                wrong_key,
                SealedPayload(sealed_bytes=sealed_bytes),
                tenant_id=DEFAULT_TENANT_ID,
                item_id="item-subject-a-1",
            )
        assert "authentication failed" in str(excinfo.value)

    def test_archiver_read_for_subject_with_wrong_subject_id_fails_closed(
        self, sprint2_wired_system
    ):
        """The archiver's own `read_for_subject` entry point, not the bare domain function.

        `subject-b` here has never been granted a key at all, so
        `read_for_subject` must raise before even reaching
        `decrypt_payload` (its own documented "no active key" branch) --
        still fail-closed, still the typed crypto-shredding exception.
        """
        archiver: Zone8SubjectKeyedArchiver = sprint2_wired_system.zone8_subject_keyed_archiver

        item_a = _archive_item(
            tenant_id=DEFAULT_TENANT_ID,
            item_id="item-subject-a-2",
            payload=b"payload-for-subject-a-2",
        )
        archiver.archive_for_subject(item_a, subject_id="subject-a")

        with pytest.raises(Zone8CryptoShreddingError):
            archiver.read_for_subject(
                DEFAULT_TENANT_ID, "item-subject-a-2", "subject-b"
            )


class TestAssociatedDataTamperingFailsClosed:
    """A manipulated item_id/tenant_id in the AAD must fail closed, never decrypt."""

    def test_manipulated_item_id_in_aad_raises_typed_error(self, sprint2_wired_system):
        archiver: Zone8SubjectKeyedArchiver = sprint2_wired_system.zone8_subject_keyed_archiver

        item_a = _archive_item(
            tenant_id=DEFAULT_TENANT_ID,
            item_id="item-real-id",
            payload=b"payload-bound-to-real-id",
        )
        archiver.archive_for_subject(item_a, subject_id="subject-aad")
        _seed_manifest_read_from_last_insert(sprint2_wired_system)

        correct_key = sprint2_wired_system.zone8_subject_key_store.get_key(
            DEFAULT_TENANT_ID, "subject-aad"
        )
        sealed_bytes = sprint2_wired_system.zone8_consolidation_store.read_blob_range(
            DEFAULT_TENANT_ID, "item-real-id"
        )

        with pytest.raises(Zone8CryptoShreddingError) as excinfo:
            decrypt_payload(
                correct_key,
                SealedPayload(sealed_bytes=sealed_bytes),
                tenant_id=DEFAULT_TENANT_ID,
                item_id="item-manipulated-id",
            )
        assert "authentication failed" in str(excinfo.value)

    def test_manipulated_tenant_id_in_aad_raises_typed_error(self, sprint2_wired_system):
        archiver: Zone8SubjectKeyedArchiver = sprint2_wired_system.zone8_subject_keyed_archiver

        item_a = _archive_item(
            tenant_id=DEFAULT_TENANT_ID,
            item_id="item-real-tenant",
            payload=b"payload-bound-to-real-tenant",
        )
        archiver.archive_for_subject(item_a, subject_id="subject-aad-2")
        _seed_manifest_read_from_last_insert(sprint2_wired_system)

        correct_key = sprint2_wired_system.zone8_subject_key_store.get_key(
            DEFAULT_TENANT_ID, "subject-aad-2"
        )
        sealed_bytes = sprint2_wired_system.zone8_consolidation_store.read_blob_range(
            DEFAULT_TENANT_ID, "item-real-tenant"
        )

        with pytest.raises(Zone8CryptoShreddingError) as excinfo:
            decrypt_payload(
                correct_key,
                SealedPayload(sealed_bytes=sealed_bytes),
                tenant_id="tenant-manipulated",
                item_id="item-real-tenant",
            )
        assert "authentication failed" in str(excinfo.value)


class TestPostErasureReadFailsClosed:
    """After `SubjectKeyStorePort.destroy_key`, decrypt must fail closed, never succeed."""

    def test_decrypt_after_key_destruction_raises_typed_error(self, sprint2_wired_system):
        archiver: Zone8SubjectKeyedArchiver = sprint2_wired_system.zone8_subject_keyed_archiver

        item_a = _archive_item(
            tenant_id=DEFAULT_TENANT_ID,
            item_id="item-erasure-1",
            payload=b"payload-to-be-erased",
        )
        archiver.archive_for_subject(item_a, subject_id="subject-erasure")
        _seed_manifest_read_from_last_insert(sprint2_wired_system)

        # Sanity: readable before erasure.
        plaintext_before = archiver.read_for_subject(
            DEFAULT_TENANT_ID, "item-erasure-1", "subject-erasure"
        )
        assert plaintext_before == b"payload-to-be-erased"

        destroyed = sprint2_wired_system.zone8_subject_key_store.destroy_key(
            DEFAULT_TENANT_ID, "subject-erasure"
        )
        assert destroyed is True, "precondition: a key must have existed to destroy"

        with pytest.raises(Zone8CryptoShreddingError) as excinfo:
            archiver.read_for_subject(
                DEFAULT_TENANT_ID, "item-erasure-1", "subject-erasure"
            )
        assert "no active Zone 8 key" in str(excinfo.value)

    def test_erase_subject_then_read_fails_closed(self, sprint2_wired_system):
        """The same fail-closed contract via `erase_subject` (the DPDP cascade's own entry point)."""
        archiver: Zone8SubjectKeyedArchiver = sprint2_wired_system.zone8_subject_keyed_archiver

        item_a = _archive_item(
            tenant_id=DEFAULT_TENANT_ID,
            item_id="item-erasure-2",
            payload=b"payload-to-be-cascaded-erased",
        )
        archiver.archive_for_subject(item_a, subject_id="subject-cascade")

        erased_item_ids = archiver.erase_subject(DEFAULT_TENANT_ID, "subject-cascade")
        assert "item-erasure-2" in erased_item_ids

        with pytest.raises(Zone8CryptoShreddingError):
            archiver.read_for_subject(
                DEFAULT_TENANT_ID, "item-erasure-2", "subject-cascade"
            )


def _provisioning_aware_archiver(sprint2_wired_system) -> Zone8SubjectKeyedArchiver:
    """Build a `Zone8SubjectKeyedArchiver` wired with DSHN-69's provisioning check.

    Reuses `sprint2_wired_system`'s own object store, manifest,
    subject-key store, and subject index -- only `Zone8ConsolidationStore`
    is reconstructed, with `sprint2_wired_system.zone8_bucket_provisioner`
    now supplied as `provisioning_check`. Deliberately does NOT modify
    `conftest_sprint2.py`'s own `zone8_consolidation_store`/
    `zone8_subject_keyed_archiver` fixtures: every OTHER test class in
    this suite (and every other Sprint 2 integration test) still depends
    on those fixtures composed WITHOUT provisioning enforcement, for
    `DEFAULT_TENANT_ID` ("tenant-1") which is never explicitly
    provisioned anywhere in `conftest_sprint2.py` -- reconstructing here,
    scoped to this test class only, is what keeps DSHN-69's fix from
    regressing every unrelated cross-zone integration test.
    """
    provisioning_aware_store = Zone8ConsolidationStore(
        object_store=sprint2_wired_system.zone8_object_store,
        manifest=sprint2_wired_system.zone8_manifest_repository,
        clock=sprint2_wired_system.clock,
        provisioning_check=sprint2_wired_system.zone8_bucket_provisioner,
    )
    return Zone8SubjectKeyedArchiver(
        zone8_store=provisioning_aware_store,
        key_store=InMemorySubjectKeyStore(),
        subject_index=InMemoryZone8SubjectIndex(),
    )


class TestUnprovisionedTenantConsolidationRejected:
    """DSHN-69 FIX, verified: `consolidate_batch` now rejects an unprovisioned tenant's batch.

    Was `TestUnprovisionedTenantConsolidationGapFinding` (documented the
    gap: `consolidate_batch` durably wrote a batch for a tenant never
    provisioned via `LocalTenantBucketProvisioner`/
    `TenantBucketProvisioningService`). `Zone8ConsolidationStore.
    consolidate_batch` now takes an optional `provisioning_check`
    (`TenantProvisioningCheckPort`) and, when supplied, fails closed --
    raises `Zone8UnprovisionedTenantError` before any port is called --
    for a tenant `provisioning_check.is_tenant_provisioned` reports as
    unprovisioned. `_provisioning_aware_archiver` above composes exactly
    that check against the real `LocalTenantBucketProvisioner` fixture.
    """

    def test_archive_raises_for_tenant_never_provisioned_via_bucket_provisioner(
        self, sprint2_wired_system
    ):
        never_provisioned_tenant = "tenant-never-provisioned"
        bucket_root = sprint2_wired_system.zone8_bucket_provisioner._root_dir

        # Explicit precondition: this tenant has no bucket per the real
        # `LocalTenantBucketProvisioner` fixture composed into this system.
        assert not (bucket_root / "in_region" / never_provisioned_tenant).exists(), (
            "precondition: tenant must have no provisioned bucket directory"
        )

        archiver = _provisioning_aware_archiver(sprint2_wired_system)
        item = _archive_item(
            tenant_id=never_provisioned_tenant,
            item_id="item-unprovisioned-tenant-1",
            payload=b"payload-for-unprovisioned-tenant",
        )

        # FIX VERIFIED: the batch is rejected fail-closed, before any
        # object-store or manifest write, rather than silently written.
        with pytest.raises(Zone8UnprovisionedTenantError) as excinfo:
            archiver.archive_for_subject(item, subject_id="subject-unprovisioned")
        assert never_provisioned_tenant in str(excinfo.value)

        # REGRESSION-PROOF (proves this is not a weakened test): the
        # gap-era assertion this test replaces was
        # `assert len(result.written) == 1` after an unguarded call to
        # `archive_for_subject` -- i.e. it required the call to RETURN.
        # `pytest.raises` above only passes because the call raised
        # instead; had `consolidate_batch` still silently written the
        # batch (the pre-fix behavior), `pytest.raises` itself would
        # fail with "DID NOT RAISE", which is exactly the failure mode
        # that proves this assertion is a real behavioral change, not a
        # weakened restatement of the old one.

        # Nothing was durably written: neither the object store nor the
        # manifest saw a call (the check runs before either port).
        assert sprint2_wired_system.zone8_manifest_connection.cursor_obj.executed == [], (
            "no manifest INSERT must have been issued for a rejected batch"
        )

    def test_bucket_provisioning_check_is_consulted_and_never_provisions_as_a_side_effect(
        self, sprint2_wired_system
    ):
        """Corroborates the fix from the provisioning side, both branches.

        `is_tenant_provisioned` is a pure existence check -- asking it
        about an unprovisioned tenant must not itself create that
        tenant's bucket directory (that would be `provision`'s job, not
        the check's). A subsequently *provisioned* tenant must still
        archive successfully -- proving the fix does not block
        legitimate, provisioned tenants (no false positive).
        """
        never_provisioned_tenant = "tenant-never-provisioned-2"
        archiver = _provisioning_aware_archiver(sprint2_wired_system)
        provisioner = sprint2_wired_system.zone8_bucket_provisioner

        with pytest.raises(Zone8UnprovisionedTenantError):
            archiver.archive_for_subject(
                _archive_item(
                    tenant_id=never_provisioned_tenant,
                    item_id="item-unprovisioned-tenant-2",
                    payload=b"payload-for-unprovisioned-tenant-2",
                ),
                subject_id="subject-unprovisioned-2",
            )

        assert not (provisioner._root_dir / "in_region" / never_provisioned_tenant).exists(), (
            "the provisioning CHECK must never provision a bucket as a side "
            "effect of being asked -- only TenantBucketProvisioningService.provision "
            "may create a bucket directory"
        )

        # Now provision the tenant for real, via the real Facade -- and
        # confirm archiving succeeds once it genuinely is provisioned.
        now_provisioned_tenant = "tenant-now-provisioned"
        sprint2_wired_system.zone8_bucket_provisioning_service.provision_for_tenant(
            BucketProvisioningSpec(
                tenant_id=now_provisioned_tenant,
                residency=ResidencyRegion.IN_REGION,
                audit_retention_days=365,
            )
        )
        result = archiver.archive_for_subject(
            _archive_item(
                tenant_id=now_provisioned_tenant,
                item_id="item-provisioned-tenant-1",
                payload=b"payload-for-provisioned-tenant",
            ),
            subject_id="subject-provisioned",
        )
        assert len(result.written) == 1
        assert result.deferred == ()
        assert result.rejected == ()
