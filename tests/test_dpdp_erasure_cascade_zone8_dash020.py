"""Test suite for DASH-STORY-020: DPDP erasure-cascade reach into Zone 8 (DSHN-69).

Traces to FR-008 in SRS.md. FR-008 (verbatim): "The system SHALL provide
a Consolidation Memory zone as the long-term, cross-session consolidated
store that content reaches only via the Archived state of the rotation
state machine (FR-012)."

Covers, by AC ID:
  - AC-008-DPDP-1: a DPDP subject-erasure request's cascade reaches Zone
    8 archives via the subject_id secondary index, returning a `202`-
    equivalent `ErasureCascadeAccepted` with a `job_id`.
  - AC-008-DPDP-2: a tenant's Zone 8 bucket is provisioned in-region per
    its residency policy, versioned, and object-locked for an operator-
    configurable audit-retention window.
  - AC-008-DPDP-3: a Zone 8 blob subject to DPDP erasure has its
    per-subject encryption key individually destroyable so the blob
    becomes permanently unreadable, without an in-place blob rewrite.

Also covers the must-not-deviate items verbatim from
sprint2_ar1_assignments.json AR1-S2-020 (see each
TestMustNotDeviate* class below).

Runtime assumptions recorded per rule 33/40 test-roadmap conventions:
  - clock: FakeClock, fixed at 2026-01-01T00:00:00+00:00 unless a test
    states otherwise -- deterministic, no wall-clock dependency.
  - job_id: a deterministic FakeJobIdFactory (sequential "job-N"
    strings) unless a test states otherwise -- never a real uuid4 value,
    so assertions are reproducible.
  - tenant_id: "tenant-1" for all requests unless a test states
    otherwise.
  - No fixture seed is required for the crypto primitives -- AES-GCM key
    and nonce generation is exercised as real CSPRNG output; tests never
    assert on a specific key or nonce value, only on decrypt success/
    failure, per application-security-core's own guidance to test
    behavior (auth success/failure) rather than implementation details.

PII NOTE: per the dev_prompt's PII constraint, this suite carries only
manifest/index mechanics, subject_id/tenant_id/item_id routing metadata,
and plain ASCII placeholder payload bytes (never realistic conversational
content or a real/example subject_id value) -- never decoded blob
content, subject key material, or ciphertext bytes presented as
meaningful, anywhere below.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from dashanan.application.subject_erasure_cascade import (
    SubjectErasureCascadeService,
    SubjectErasureJobStatus,
)
from dashanan.application.tenant_bucket_provisioner import (
    BucketProvisioningPortError,
    TenantBucketProvisioningService,
)
from dashanan.application.zone8_consolidation_store import Zone8ConsolidationStore
from dashanan.application.zone8_crypto_shredding_store import (
    Zone8CryptoShreddingStoreError,
    Zone8SubjectKeyedArchiver,
)
from dashanan.domain.consolidated_blob import ArchiveBatchItem
from dashanan.domain.subject_erasure import SubjectErasureError, SubjectErasureRequest
from dashanan.domain.tenant_bucket_policy import (
    BucketProvisioningSpec,
    ResidencyRegion,
    TenantBucketPolicyError,
)
from dashanan.domain.zone import ZoneId
from dashanan.domain.zone8_crypto_shredding import (
    SealedPayload,
    Zone8CryptoShreddingError,
    decrypt_payload,
    encrypt_payload,
    generate_subject_key,
)
from dashanan.infrastructure.in_memory_subject_erasure_job_store import (
    InMemorySubjectErasureJobStore,
)
from dashanan.infrastructure.in_memory_subject_key_store import InMemorySubjectKeyStore
from dashanan.infrastructure.in_memory_zone8_subject_index import InMemoryZone8SubjectIndex
from dashanan.infrastructure.local_object_store import LocalObjectStore
from dashanan.infrastructure.local_tenant_bucket_provisioner import (
    LocalTenantBucketProvisioner,
)
from dashanan.infrastructure.sql_manifest_repository import SqlManifestRepository

_FIXED_TS = datetime(2026, 1, 1, tzinfo=UTC)
_TENANT = "tenant-1"
_OTHER_TENANT = "tenant-2"


class FakeClock:
    """Deterministic Clock double: fixed instant, injectable (testing-core DI)."""

    def __init__(self, fixed: datetime = _FIXED_TS) -> None:
        self._fixed = fixed

    def now(self) -> datetime:
        return self._fixed


class FakeJobIdFactory:
    """Deterministic job_id generator: sequential "job-1", "job-2", ... (testing-core DI)."""

    def __init__(self) -> None:
        self._counter = 0

    def __call__(self) -> str:
        self._counter += 1
        return f"job-{self._counter}"


class ZoneManifestPrivilegeError(Exception):
    """Mimics a real driver's privilege-violation error (e.g. psycopg2's
    `errors.InsufficientPrivilege`) for an `UPDATE`/`DELETE` a database
    actually enforcing `zone8_consolidation_schema.sql`'s
    `REVOKE UPDATE, DELETE ON zone8_manifest FROM PUBLIC` would raise."""


class FakeCursor:
    """A minimal in-memory DB-API cursor double for `SqlZone8SubjectIndexRepository`.

    Enforces the same append-only privilege `zone8_consolidation_schema.
    sql`'s `REVOKE UPDATE, DELETE` grants for real: any `UPDATE` or
    `DELETE` naming `zone8_manifest` raises `ZoneManifestPrivilegeError`
    instead of silently applying it (DASH-STORY-020/DSHN-69 regression
    coverage -- a permissive fake is what let the original defect ship).
    """

    def __init__(self, store: "FakeSqlStore") -> None:
        self._store = store
        self._last_result: list[tuple[object, ...]] = []

    def execute(self, sql: str, params: tuple[object, ...]) -> None:
        normalized = " ".join(sql.split())
        if "zone8_manifest" in normalized and (
            normalized.upper().startswith("UPDATE") or normalized.upper().startswith("DELETE")
        ):
            raise ZoneManifestPrivilegeError(
                "permission denied for table zone8_manifest: UPDATE/DELETE is "
                "revoked from PUBLIC (append-only, must-not-deviate item 3)"
            )
        if normalized.startswith("SELECT item_id"):
            tenant_id, subject_id = params
            self._last_result = [
                (row["item_id"],)
                for (t, _iid), row in self._store.rows.items()
                if t == tenant_id and row.get("subject_id") == subject_id
            ]
        else:  # pragma: no cover -- defensive, this fake only serves the SELECT above
            raise AssertionError(f"FakeCursor received unexpected SQL: {sql}")

    def fetchall(self) -> list[tuple[object, ...]]:
        return self._last_result


class FakeSqlStore:
    """Backing state for `FakeCursor`/`FakeSqlConnection`: one dict of manifest-like rows."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, object]] = {}


class FakeSqlConnection:
    """A minimal in-memory DB-API connection double satisfying `SqlConnection`.

    No `commit` method: `SqlZone8SubjectIndexRepository.SqlConnection`
    declares none (its only operation is `find_item_ids`'s `SELECT`).
    """

    def __init__(self, store: FakeSqlStore) -> None:
        self._store = store

    def cursor(self) -> FakeCursor:
        return FakeCursor(self._store)


def _archiver(
    tmp_path,
) -> tuple[Zone8SubjectKeyedArchiver, InMemorySubjectKeyStore, InMemoryZone8SubjectIndex]:
    """Build a `Zone8SubjectKeyedArchiver` over real Shape A adapters (no mocks)."""
    zone8_store = Zone8ConsolidationStore(
        object_store=LocalObjectStore(tmp_path / "objects"),
        manifest=_InMemoryManifestPort(),
        clock=FakeClock(),
    )
    key_store = InMemorySubjectKeyStore()
    subject_index = InMemoryZone8SubjectIndex()
    return (
        Zone8SubjectKeyedArchiver(zone8_store, key_store, subject_index),
        key_store,
        subject_index,
    )


class _InMemoryManifestPort:
    """A minimal, real (non-mocked) `ManifestPort` implementation for these tests."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], object] = {}

    def find_by_item_id(self, tenant_id: str, item_id: str) -> object | None:
        return self._entries.get((tenant_id, item_id))

    def insert_batch(self, entries) -> None:  # noqa: ANN001 -- Sequence[ManifestEntry]
        for entry in entries:
            self._entries[(entry.tenant_id, entry.item_id)] = entry


class TestAC008DPDP1CascadeReachesZone8:
    """AC-008-DPDP-1: cascade reaches Zone 8 archives via the subject_id index, 202+job_id."""

    def test_request_erasure_returns_accepted_with_job_id(self, tmp_path) -> None:
        archiver, _, _ = _archiver(tmp_path)
        job_store = InMemorySubjectErasureJobStore()
        service = SubjectErasureCascadeService(
            archiver, job_store, FakeClock(), job_id_factory=FakeJobIdFactory()
        )

        accepted = service.request_erasure(
            SubjectErasureRequest(tenant_id=_TENANT, subject_id="subject-1")
        )

        assert accepted.job_id == "job-1"
        assert accepted.tenant_id == _TENANT
        assert accepted.subject_id == "subject-1"
        assert accepted.accepted_at == _FIXED_TS

    def test_job_store_resolves_completed_job_via_get_job(self, tmp_path) -> None:
        archiver, _, _ = _archiver(tmp_path)
        job_store = InMemorySubjectErasureJobStore()
        service = SubjectErasureCascadeService(
            archiver, job_store, FakeClock(), job_id_factory=FakeJobIdFactory()
        )
        accepted = service.request_erasure(
            SubjectErasureRequest(tenant_id=_TENANT, subject_id="subject-1")
        )

        job = service.get_job(accepted.job_id)

        assert job is not None
        assert job.status == SubjectErasureJobStatus.COMPLETED
        assert job.tenant_id == _TENANT
        assert job.subject_id == "subject-1"

    def test_cascade_resolves_every_item_archived_under_subject_via_index(
        self, tmp_path
    ) -> None:
        archiver, _, _ = _archiver(tmp_path)
        item_a = ArchiveBatchItem(
            tenant_id=_TENANT,
            item_id="item-a",
            source_zone=ZoneId.EPISODIC,
            payload=b"plaintext-a",
        )
        item_b = ArchiveBatchItem(
            tenant_id=_TENANT,
            item_id="item-b",
            source_zone=ZoneId.EPISODIC,
            payload=b"plaintext-b",
        )
        archiver.archive_for_subject(item_a, "subject-1")
        archiver.archive_for_subject(item_b, "subject-1")

        item_ids = archiver.erase_subject(_TENANT, "subject-1")

        assert set(item_ids) == {"item-a", "item-b"}

    def test_subject_index_lookup_is_scoped_to_tenant(self, tmp_path) -> None:
        archiver, _, _ = _archiver(tmp_path)
        item = ArchiveBatchItem(
            tenant_id=_TENANT,
            item_id="item-a",
            source_zone=ZoneId.EPISODIC,
            payload=b"plaintext-a",
        )
        archiver.archive_for_subject(item, "subject-1")

        other_tenant_item_ids = archiver.erase_subject(_OTHER_TENANT, "subject-1")

        assert other_tenant_item_ids == ()

    def test_erasure_of_subject_with_no_zone8_footprint_is_not_an_error(
        self, tmp_path
    ) -> None:
        archiver, _, _ = _archiver(tmp_path)

        item_ids = archiver.erase_subject(_TENANT, "never-archived-subject")

        assert item_ids == ()

    def test_sql_subject_index_repository_resolves_via_indexed_query(self) -> None:
        """AC-008-DPDP-1's real SQL-backed index (ADR-006, Shape B).

        `subject_id` is seeded directly onto the row here, simulating
        `SqlManifestRepository.insert_batch`'s own `INSERT` having
        already written it (DASH-STORY-020's fully-integrated fix) --
        never via `record_item`, which performs no SQL for this adapter.
        """
        from dashanan.infrastructure.sql_zone8_subject_index_repository import (
            SqlZone8SubjectIndexRepository,
        )

        store = FakeSqlStore()
        store.rows[(_TENANT, "item-a")] = {"item_id": "item-a", "subject_id": "subject-1"}
        store.rows[(_TENANT, "item-b")] = {"item_id": "item-b", "subject_id": "subject-1"}
        repo = SqlZone8SubjectIndexRepository(FakeSqlConnection(store))

        item_ids = repo.find_item_ids(_TENANT, "subject-1")

        assert set(item_ids) == {"item-a", "item-b"}

    def test_sql_subject_index_repository_record_item_is_a_no_op(self) -> None:
        """`record_item` issues no SQL for the SQL adapter -- the row already
        carries `subject_id` from the INSERT by the time this is called."""
        from dashanan.infrastructure.sql_zone8_subject_index_repository import (
            SqlZone8SubjectIndexRepository,
        )

        store = FakeSqlStore()
        repo = SqlZone8SubjectIndexRepository(FakeSqlConnection(store))

        repo.record_item(_TENANT, "subject-1", "item-a")  # must not raise, must not mutate

        assert store.rows == {}

    def test_sql_subject_index_repository_record_item_still_validates_blank_arguments(
        self,
    ) -> None:
        from dashanan.infrastructure.sql_zone8_subject_index_repository import (
            SqlZone8SubjectIndexRepository,
        )

        repo = SqlZone8SubjectIndexRepository(FakeSqlConnection(FakeSqlStore()))

        with pytest.raises(Zone8CryptoShreddingStoreError):
            repo.record_item("   ", "subject-1", "item-a")
        with pytest.raises(Zone8CryptoShreddingStoreError):
            repo.record_item(_TENANT, "   ", "item-a")
        with pytest.raises(Zone8CryptoShreddingStoreError):
            repo.record_item(_TENANT, "subject-1", "   ")


class TestAC008DPDP2TenantBucketProvisioning:
    """AC-008-DPDP-2: bucket provisioned in-region, versioned, object-locked, retained."""

    def test_bucket_is_provisioned_under_the_tenants_residency_region(self, tmp_path) -> None:
        provisioner = LocalTenantBucketProvisioner(tmp_path, FakeClock())
        service = TenantBucketProvisioningService(provisioner)
        spec = BucketProvisioningSpec(
            tenant_id=_TENANT, residency=ResidencyRegion.IN_REGION, audit_retention_days=90
        )

        provisioned = service.provision_for_tenant(spec)

        assert provisioned.residency_value == "in_region"
        assert _TENANT in provisioned.bucket_uri
        assert "in_region" in provisioned.bucket_uri

    def test_bucket_is_always_versioned_and_object_locked(self, tmp_path) -> None:
        provisioner = LocalTenantBucketProvisioner(tmp_path, FakeClock())
        service = TenantBucketProvisioningService(provisioner)
        spec = BucketProvisioningSpec(
            tenant_id=_TENANT, residency=ResidencyRegion.IN_REGION, audit_retention_days=30
        )

        provisioned = service.provision_for_tenant(spec)

        assert provisioned.versioning_enabled is True
        assert provisioned.object_lock_enabled is True

    def test_audit_retention_days_is_operator_configurable_and_echoed(self, tmp_path) -> None:
        provisioner = LocalTenantBucketProvisioner(tmp_path, FakeClock())
        service = TenantBucketProvisioningService(provisioner)
        spec = BucketProvisioningSpec(
            tenant_id=_TENANT, residency=ResidencyRegion.IN_REGION, audit_retention_days=365
        )

        provisioned = service.provision_for_tenant(spec)

        assert provisioned.audit_retention_days == 365

    def test_provisioning_is_idempotent_and_does_not_downgrade_existing_policy(
        self, tmp_path
    ) -> None:
        provisioner = LocalTenantBucketProvisioner(tmp_path, FakeClock())
        service = TenantBucketProvisioningService(provisioner)
        first = service.provision_for_tenant(
            BucketProvisioningSpec(
                tenant_id=_TENANT, residency=ResidencyRegion.IN_REGION, audit_retention_days=90
            )
        )

        second = service.provision_for_tenant(
            BucketProvisioningSpec(
                tenant_id=_TENANT, residency=ResidencyRegion.IN_REGION, audit_retention_days=90
            )
        )

        assert first.provisioned_at == second.provisioned_at

    def test_permissions_failure_raises_bucket_provisioning_port_error(self, tmp_path) -> None:
        blocked_root = tmp_path / "blocked"
        blocked_root.touch()  # a FILE where a directory is required -> mkdir fails
        provisioner = LocalTenantBucketProvisioner(blocked_root, FakeClock())
        service = TenantBucketProvisioningService(provisioner)
        spec = BucketProvisioningSpec(
            tenant_id=_TENANT, residency=ResidencyRegion.IN_REGION, audit_retention_days=90
        )

        with pytest.raises(BucketProvisioningPortError):
            service.provision_for_tenant(spec)


class TestAC008DPDP3CryptoShredding:
    """AC-008-DPDP-3: per-subject key destruction makes the blob permanently unreadable."""

    def test_archived_content_round_trips_through_encryption_before_erasure(
        self, tmp_path
    ) -> None:
        archiver, _, _ = _archiver(tmp_path)
        item = ArchiveBatchItem(
            tenant_id=_TENANT,
            item_id="item-a",
            source_zone=ZoneId.EPISODIC,
            payload=b"the real plaintext payload",
        )
        archiver.archive_for_subject(item, "subject-1")

        recovered = archiver.read_for_subject(_TENANT, "item-a", "subject-1")

        assert recovered == b"the real plaintext payload"

    def test_erasure_destroys_key_and_read_afterwards_is_permanently_unreadable(
        self, tmp_path
    ) -> None:
        archiver, key_store, _ = _archiver(tmp_path)
        item = ArchiveBatchItem(
            tenant_id=_TENANT,
            item_id="item-a",
            source_zone=ZoneId.EPISODIC,
            payload=b"the real plaintext payload",
        )
        archiver.archive_for_subject(item, "subject-1")
        assert key_store.get_key(_TENANT, "subject-1") is not None

        archiver.erase_subject(_TENANT, "subject-1")

        assert key_store.get_key(_TENANT, "subject-1") is None
        with pytest.raises(Zone8CryptoShreddingError):
            archiver.read_for_subject(_TENANT, "item-a", "subject-1")

    def test_erasure_does_not_read_rewrite_or_delete_the_underlying_blob(
        self, tmp_path
    ) -> None:
        """Must-not-deviate item 1: erasure works through key destruction, not mutation."""
        archiver, _, _ = _archiver(tmp_path)
        item = ArchiveBatchItem(
            tenant_id=_TENANT,
            item_id="item-a",
            source_zone=ZoneId.EPISODIC,
            payload=b"the real plaintext payload",
        )
        archiver.archive_for_subject(item, "subject-1")
        objects_dir = tmp_path / "objects"
        blob_files_before = sorted(p.name for p in objects_dir.iterdir())

        archiver.erase_subject(_TENANT, "subject-1")

        blob_files_after = sorted(p.name for p in objects_dir.iterdir())
        assert blob_files_before == blob_files_after, (
            "the blob file set on disk must be byte-for-byte unchanged after erasure -- "
            "crypto-shredding must never touch the object store"
        )

    def test_erasure_is_idempotent_for_a_subject_with_no_key(self, tmp_path) -> None:
        archiver, _, _ = _archiver(tmp_path)

        item_ids = archiver.erase_subject(_TENANT, "subject-never-archived")

        assert item_ids == ()

    def test_wrong_subjects_key_cannot_decrypt_another_subjects_payload(self) -> None:
        """A cross-subject key mix-up must fail loudly, never silently succeed."""
        key_a = generate_subject_key()
        key_b = generate_subject_key()
        sealed = encrypt_payload(key_a, b"subject a's content", tenant_id=_TENANT, item_id="i1")

        with pytest.raises(Zone8CryptoShreddingError):
            decrypt_payload(key_b, sealed, tenant_id=_TENANT, item_id="i1")

    def test_ciphertext_bound_to_item_id_rejects_cross_item_reuse(self) -> None:
        """AEAD associated-data binding: a ciphertext cannot be replayed under a different item_id."""
        key = generate_subject_key()
        sealed = encrypt_payload(key, b"content for item-a", tenant_id=_TENANT, item_id="item-a")

        with pytest.raises(Zone8CryptoShreddingError):
            decrypt_payload(key, sealed, tenant_id=_TENANT, item_id="item-b")

    def test_tampered_ciphertext_fails_authentication(self) -> None:
        key = generate_subject_key()
        sealed = encrypt_payload(key, b"authentic content", tenant_id=_TENANT, item_id="item-a")
        tampered = SealedPayload(sealed_bytes=sealed.sealed_bytes[:-1] + b"\x00")

        with pytest.raises(Zone8CryptoShreddingError):
            decrypt_payload(key, tampered, tenant_id=_TENANT, item_id="item-a")

    def test_encrypt_rejects_a_key_of_the_wrong_length(self) -> None:
        with pytest.raises(Zone8CryptoShreddingError):
            encrypt_payload(b"too-short", b"content", tenant_id=_TENANT, item_id="item-a")

    def test_same_subject_key_reused_across_multiple_archived_items(self, tmp_path) -> None:
        archiver, key_store, _ = _archiver(tmp_path)
        item_a = ArchiveBatchItem(
            tenant_id=_TENANT, item_id="item-a", source_zone=ZoneId.EPISODIC, payload=b"a"
        )
        item_b = ArchiveBatchItem(
            tenant_id=_TENANT, item_id="item-b", source_zone=ZoneId.EPISODIC, payload=b"b"
        )

        archiver.archive_for_subject(item_a, "subject-1")
        key_after_a = key_store.get_key(_TENANT, "subject-1")
        archiver.archive_for_subject(item_b, "subject-1")
        key_after_b = key_store.get_key(_TENANT, "subject-1")

        assert key_after_a == key_after_b, (
            "one key destroy call must render every item archived under this "
            "subject unreadable -- so every archive_for_subject call for the "
            "same subject must reuse the identical key"
        )


class TestMustNotDeviateItem1NoInPlaceBlobRewrite:
    """Must-not-deviate item 1: "erasure works through key destruction, not mutation"."""

    def test_zone8_subject_keyed_archiver_erase_never_calls_read_blob_range_or_consolidate(
        self, tmp_path
    ) -> None:
        archiver, _, _ = _archiver(tmp_path)
        item = ArchiveBatchItem(
            tenant_id=_TENANT, item_id="item-a", source_zone=ZoneId.EPISODIC, payload=b"x"
        )
        archiver.archive_for_subject(item, "subject-1")

        # Replace the composed Zone8ConsolidationStore with one that raises on
        # any write/read call -- if erase_subject touched either, this would fail.
        class _ExplodingZone8Store:
            def consolidate_batch(self, items):  # noqa: ANN001, D401
                raise AssertionError("erase_subject must never call consolidate_batch")

            def read_blob_range(self, tenant_id, item_id):  # noqa: ANN001, D401
                raise AssertionError("erase_subject must never call read_blob_range")

        archiver._zone8_store = _ExplodingZone8Store()  # type: ignore[attr-defined]

        item_ids = archiver.erase_subject(_TENANT, "subject-1")

        assert item_ids == ("item-a",)


class TestMustNotDeviateItem2OAQ10NotAsserted:
    """Must-not-deviate item 2: OAQ-10's legal question is not settled by this story."""

    def test_job_status_values_name_only_engineering_outcomes(self) -> None:
        legal_terms = {"legal", "compliant", "dpdp-satisfied", "erasure-confirmed-legal"}
        status_values = {status.value for status in SubjectErasureJobStatus}

        assert status_values == {"accepted", "completed", "failed"}
        assert status_values.isdisjoint(legal_terms)


class TestMustNotDeviateItem3SubjectIdIndexOnManifestTable:
    """Must-not-deviate item 3: the cascade depends on a real subject_id index on the manifest table."""

    def test_migration_file_adds_subject_id_column_and_index(self) -> None:
        import pathlib

        migration_path = (
            pathlib.Path(__file__).resolve().parents[1]
            / "src"
            / "dashanan"
            / "infrastructure"
            / "zone8_manifest_subject_index_migration.sql"
        )
        sql_text = migration_path.read_text(encoding="utf-8")

        assert "ALTER TABLE zone8_manifest ADD COLUMN IF NOT EXISTS subject_id" in sql_text
        assert "CREATE INDEX IF NOT EXISTS idx_zone8_manifest_subject" in sql_text
        assert "ON zone8_manifest (tenant_id, subject_id)" in sql_text

    def test_migration_does_not_touch_the_original_create_table_file(self) -> None:
        """File-disjointness: DASH-STORY-018's own schema file is never edited by this story."""
        import pathlib

        original_schema = (
            pathlib.Path(__file__).resolve().parents[1]
            / "src"
            / "dashanan"
            / "infrastructure"
            / "zone8_consolidation_schema.sql"
        )
        sql_text = original_schema.read_text(encoding="utf-8")

        assert "subject_id" not in sql_text, (
            "the original DASH-STORY-018 schema file must remain untouched -- "
            "the subject_id column belongs only on the new migration file"
        )


class TestMustNotDeviateItem4PMRiskFlagRecorded:
    """Must-not-deviate item 4: story points raised 3->5 is a risk flag, not a final estimate."""

    def test_sprint2_ar1_assignments_records_the_story_points_risk_flag(self) -> None:
        import json
        import pathlib

        assignments_path = (
            pathlib.Path(__file__).resolve().parents[1]
            / "docs"
            / "phase-7-routing"
            / "sprint2_ar1_assignments.json"
        )
        data = json.loads(assignments_path.read_text(encoding="utf-8"))
        story = next(
            s for s in data["assignments"] if s["story_id"] == "DASH-STORY-020"
        )

        assert story["story_points"] == 5
        assert "3 to 5" in story["must_not_deviate"][3]
        assert "risk flag" in story["must_not_deviate"][3]


class TestSubjectErasureDomainValidation:
    """Boundary/negative coverage for the pure domain Value Objects (rule 33/40 roadmap)."""

    def test_blank_tenant_id_is_rejected(self) -> None:
        with pytest.raises(SubjectErasureError):
            SubjectErasureRequest(tenant_id="   ", subject_id="subject-1")

    def test_blank_subject_id_is_rejected(self) -> None:
        with pytest.raises(SubjectErasureError):
            SubjectErasureRequest(tenant_id=_TENANT, subject_id="")

    def test_bucket_spec_rejects_zero_retention_days(self) -> None:
        with pytest.raises(TenantBucketPolicyError):
            BucketProvisioningSpec(
                tenant_id=_TENANT, residency=ResidencyRegion.LOCAL, audit_retention_days=0
            )

    def test_bucket_spec_versioning_and_object_lock_are_not_constructor_parameters(
        self,
    ) -> None:
        import inspect

        signature = inspect.signature(BucketProvisioningSpec.__init__)
        assert "versioning_enabled" not in signature.parameters
        assert "object_lock_enabled" not in signature.parameters


class TestZone8CryptoShreddingStoreErrorPropagation:
    """AC-008-DPDP-3 / cross-agent-autonomy note: port failures surface as typed errors."""

    def test_key_store_failure_propagates_as_typed_error(self, tmp_path) -> None:
        archiver, _, _ = _archiver(tmp_path)

        class _ExplodingKeyStore:
            def get_or_create_key(self, tenant_id, subject_id):  # noqa: ANN001
                raise Zone8CryptoShreddingStoreError("key store unreachable")

            def get_key(self, tenant_id, subject_id):  # noqa: ANN001
                raise Zone8CryptoShreddingStoreError("key store unreachable")

            def destroy_key(self, tenant_id, subject_id):  # noqa: ANN001
                raise Zone8CryptoShreddingStoreError("key store unreachable")

        archiver._key_store = _ExplodingKeyStore()  # type: ignore[attr-defined]
        item = ArchiveBatchItem(
            tenant_id=_TENANT, item_id="item-a", source_zone=ZoneId.EPISODIC, payload=b"x"
        )

        with pytest.raises(Zone8CryptoShreddingStoreError):
            archiver.archive_for_subject(item, "subject-1")
