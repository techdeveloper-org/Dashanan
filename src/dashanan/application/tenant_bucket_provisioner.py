"""TenantBucketProvisioningService: the ADR-009 India Layer bucket-provisioning Facade.

Traces to FR-008 in SRS.md; see `dashanan.domain.tenant_bucket_policy`'s
own docstring for the ADR-009/ADR-015/HLD Section 10 sourcing and
AC-008-DPDP-2's exact wording.

This module holds the orchestration `dashanan.domain.tenant_bucket_
policy`'s own docstring assigns here: calling a `BucketProvisioningPort`
to actually create the bucket. The domain module's pure
`BucketProvisioningSpec` is the single source of WHAT policy a bucket
must satisfy; this module is the place that decides HOW that policy is
carried out against a real port -- mirroring `zone8_consolidation_store.
py`'s identical domain/application split.

PII NOTE: this module and its port carry only routing/policy metadata
(`tenant_id`, a `ResidencyRegion`, a retention day count) -- never
archived content or key material.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from dashanan.domain.exceptions import DashananError
from dashanan.domain.tenant_bucket_policy import BucketProvisioningSpec

logger = logging.getLogger(__name__)


class BucketProvisioningPortError(DashananError):
    """Raised by a `BucketProvisioningPort` operational failure.

    Kept local to this module, mirroring this story's other new ports'
    identical "kept local" rationale.
    """


@dataclass(frozen=True, slots=True)
class ProvisionedBucket:
    """The outcome of provisioning one tenant's Zone 8 bucket (AC-008-DPDP-2).

    Attributes:
        tenant_id: The owning tenant.
        residency_value: The residency policy's wire value the bucket
            was provisioned under (`ResidencyRegion.value`).
        bucket_uri: An adapter-defined identifier for where the
            provisioned bucket lives (a `LocalTenantBucketProvisioner`
            filesystem path in Shape A; an `s3://bucket-name` URI in a
            future Shape B).
        versioning_enabled: Always `True` -- echoes
            `BucketProvisioningSpec.versioning_enabled`.
        object_lock_enabled: Always `True` -- echoes
            `BucketProvisioningSpec.object_lock_enabled`.
        audit_retention_days: Echoes
            `BucketProvisioningSpec.audit_retention_days`.
        provisioned_at: When this bucket was provisioned.
    """

    tenant_id: str
    residency_value: str
    bucket_uri: str
    versioning_enabled: bool
    object_lock_enabled: bool
    audit_retention_days: int
    provisioned_at: datetime


@runtime_checkable
class BucketProvisioningPort(Protocol):
    """Provisions one tenant's Zone 8 bucket per its `BucketProvisioningSpec`.

    Kept local to this module per this story's file-disjointness
    requirement.
    """

    def provision(self, spec: BucketProvisioningSpec) -> ProvisionedBucket:
        """Provision (or return the already-provisioned) bucket for `spec.tenant_id`.

        Idempotent: calling this twice for the same `tenant_id` MUST NOT
        create a second bucket or silently downgrade an already-
        provisioned bucket's versioning/object-lock/retention settings.

        Raises:
            BucketProvisioningPortError: If provisioning fails (e.g. the
                underlying store is unreachable).
        """
        ...


class TenantBucketProvisioningService:
    """Facade: the sole entry point for provisioning a tenant's Zone 8 bucket.

    Composed from one `BucketProvisioningPort`. Construction never
    touches I/O.
    """

    def __init__(self, provisioning_port: BucketProvisioningPort) -> None:
        self._provisioning_port = provisioning_port

    def provision_for_tenant(self, spec: BucketProvisioningSpec) -> ProvisionedBucket:
        """AC-008-DPDP-2: provision `spec.tenant_id`'s Zone 8 bucket per its residency policy.

        Raises:
            BucketProvisioningPortError: Propagated unchanged from the
                underlying port.
        """
        provisioned = self._provisioning_port.provision(spec)
        logger.info(
            "zone8 tenant bucket provisioned",
            extra={
                "tenant_id": spec.tenant_id,
                "residency": spec.residency.value,
                "audit_retention_days": spec.audit_retention_days,
            },
        )
        return provisioned
