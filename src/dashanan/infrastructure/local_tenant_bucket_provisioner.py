"""LocalTenantBucketProvisioner: a filesystem-backed `BucketProvisioningPort` adapter.

Shape A stand-in for ADR-009's India Layer per-tenant Zone 8 bucket
(AC-008-DPDP-2) -- one subdirectory per `(residency, tenant_id)` under a
root directory, with a write-once JSON policy sidecar recording the
provisioning parameters. Mirrors `LocalObjectStore`'s own "ordinary
files, no cloud SDK, no new dependency" convention (this codebase's
established Shape A pattern) applied to bucket PROVISIONING instead of
blob storage.

HONESTY NOTE (does not overclaim Shape B guarantees): a real
S3-compatible object store's `PutBucketVersioning`/
`PutObjectLockConfiguration` APIs give durable, provider-enforced
immutability guarantees a local filesystem cannot. This adapter records
`versioning_enabled=True` / `object_lock_enabled=True` as the POLICY
this bucket was provisioned under (matching `BucketProvisioningSpec`'s
own structurally-locked `True` values) -- it does not claim the local
filesystem itself enforces object-lock; enforcing that is a Shape B
(real object-store SDK) concern, explicitly out of this story's scope,
mirroring `local_object_store.py`'s own "production deployment swaps
this adapter" convention.

PII NOTE: this adapter's policy sidecar records only routing/policy
metadata (`tenant_id`, `residency`, retention days, a timestamp) --
never archived content or key material.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path

from dashanan.application.tenant_bucket_provisioner import (
    BucketProvisioningPortError,
    ProvisionedBucket,
)
from dashanan.domain.ports import Clock
from dashanan.domain.tenant_bucket_policy import BucketProvisioningSpec

logger = logging.getLogger(__name__)

_POLICY_FILE_NAME = ".bucket_policy.json"


class LocalTenantBucketProvisioner:
    """Implements `BucketProvisioningPort` against `root_dir`, one directory per tenant.

    Each bucket lives at `root_dir / residency_value / tenant_id /` --
    mirrors `LocalObjectStore`'s identical "one root, deterministic
    sub-path" shape.
    """

    def __init__(self, root_dir: Path, clock: Clock) -> None:
        self._root_dir = root_dir
        self._clock = clock

    def provision(self, spec: BucketProvisioningSpec) -> ProvisionedBucket:
        """Provision (or return the already-provisioned) bucket for `spec.tenant_id`.

        Idempotent: if `_POLICY_FILE_NAME` already exists for this
        tenant, its ALREADY-recorded settings are returned unchanged --
        a repeat call never re-provisions or silently downgrades an
        existing bucket's policy.

        Raises:
            BucketProvisioningPortError: If the bucket directory or
                policy sidecar cannot be created/read (e.g. a
                permissions failure).
        """
        bucket_dir = self._root_dir / spec.residency.value / spec.tenant_id
        try:
            bucket_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise BucketProvisioningPortError(
                f"LocalTenantBucketProvisioner could not create bucket directory "
                f"for tenant '{spec.tenant_id}': {exc}"
            ) from exc

        policy_path = bucket_dir / _POLICY_FILE_NAME
        existing = self._read_existing_policy(policy_path)
        if existing is not None:
            return existing

        provisioned = ProvisionedBucket(
            tenant_id=spec.tenant_id,
            residency_value=spec.residency.value,
            bucket_uri=str(bucket_dir),
            versioning_enabled=spec.versioning_enabled,
            object_lock_enabled=spec.object_lock_enabled,
            audit_retention_days=spec.audit_retention_days,
            provisioned_at=self._clock.now(),
        )
        self._write_policy_once(policy_path, provisioned)
        logger.info(
            "zone8 local tenant bucket provisioned",
            extra={"tenant_id": spec.tenant_id, "residency": spec.residency.value},
        )
        return provisioned

    def _read_existing_policy(self, policy_path: Path) -> ProvisionedBucket | None:
        """Return the already-recorded `ProvisionedBucket`, or `None` if none exists yet.

        Raises:
            BucketProvisioningPortError: If the sidecar exists but
                cannot be read or parsed.
        """
        if not policy_path.exists():
            return None
        try:
            raw = json.loads(policy_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BucketProvisioningPortError(
                f"LocalTenantBucketProvisioner could not read existing policy "
                f"'{policy_path}': {exc}"
            ) from exc
        return ProvisionedBucket(
            tenant_id=raw["tenant_id"],
            residency_value=raw["residency_value"],
            bucket_uri=raw["bucket_uri"],
            versioning_enabled=raw["versioning_enabled"],
            object_lock_enabled=raw["object_lock_enabled"],
            audit_retention_days=raw["audit_retention_days"],
            provisioned_at=datetime.fromisoformat(raw["provisioned_at"]),
        )

    def _write_policy_once(self, policy_path: Path, provisioned: ProvisionedBucket) -> None:
        """Write `provisioned` to `policy_path`, atomically and exactly once.

        Raises:
            BucketProvisioningPortError: If the write fails for any
                reason other than the file already existing (a race
                with a concurrent provisioner is treated as
                already-provisioned, matching `LocalObjectStore.
                put_if_absent`'s own `FileExistsError`-as-no-op
                convention).
        """
        payload = json.dumps(
            {
                "tenant_id": provisioned.tenant_id,
                "residency_value": provisioned.residency_value,
                "bucket_uri": provisioned.bucket_uri,
                "versioning_enabled": provisioned.versioning_enabled,
                "object_lock_enabled": provisioned.object_lock_enabled,
                "audit_retention_days": provisioned.audit_retention_days,
                "provisioned_at": provisioned.provisioned_at.isoformat(),
            }
        ).encode("utf-8")
        try:
            fd = os.open(policy_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return
        except OSError as exc:
            raise BucketProvisioningPortError(
                f"LocalTenantBucketProvisioner could not create policy file "
                f"'{policy_path}': {exc}"
            ) from exc
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
        except OSError as exc:
            raise BucketProvisioningPortError(
                f"LocalTenantBucketProvisioner could not write policy file "
                f"'{policy_path}': {exc}"
            ) from exc
