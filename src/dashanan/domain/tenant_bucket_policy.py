"""BucketProvisioningSpec: the ADR-009 India Layer per-tenant bucket policy (DPDP-4).

Traces to FR-008 in SRS.md; see `dashanan.domain.subject_erasure`'s own
docstring for FR-008's verbatim text.

ADR-009's India Layer (verbatim): "Zone 8 holds the longest-lived PII.
Bucket must be in-region (DPDP residency), versioned, object-locked for
the audit window, and its per-subject encryption keys must be
individually destroyable for crypto-shredding erasure (Section 10,
DPDP-2)." HLD Section 10, DPDP-4 (verbatim): "Per-tenant
`embedding_residency` policy (ADR-015); all stores in-region for
tenants handling Indian personal data." ADR-015's own policy flag
(verbatim, HLD's embedding-provider-selection ADR): "a per-tenant
policy flag (`embedding_residency: local|in_region|any`) checked before
every provider call."

Traces to AC-008-DPDP-2: "Each tenant's Zone 8 bucket is provisioned
in-region per that tenant's embedding_residency policy, versioned, and
object-locked for an operator-configurable audit-retention window."

MUST-NOT-DEVIATE (sprint2_ar1_assignments.json AR1-S2-020, binding): no
item in this story's must-not-deviate list names this module directly;
`BucketProvisioningSpec.versioning_enabled` and `.object_lock_enabled`
are still made structurally non-optional below (`init=False`, always
`True`) rather than left as a caller-settable bool, because ADR-009's
India Layer states both as unconditional requirements, not configurable
choices -- the same "structural, not merely a promise" pattern
`consolidated_blob.compute_blob_id`'s own docstring uses for
must-not-deviate item 3.

This module holds pure domain logic only -- no I/O. Provisioning a real
bucket (Shape A: a local directory; Shape B: an S3
`PutBucketVersioning`/`PutObjectLockConfiguration` call) is an
application/infrastructure concern
(`dashanan.application.tenant_bucket_provisioner`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from dashanan.domain.exceptions import DashananError


class TenantBucketPolicyError(DashananError):
    """Raised when a `BucketProvisioningSpec` invariant is violated.

    Kept local to this module, mirroring `consolidated_blob.
    Zone8ConsolidationError`'s identical "kept local" rationale.
    """


class ResidencyRegion(str, Enum):
    """ADR-015's per-tenant `embedding_residency` policy value set (HLD Section 10, DPDP-4).

    Carries the exact three wire values ADR-015's own flag documents
    (`local|in_region|any`) -- this enum introduces no fourth value and
    renames none of them. Inherits from `str`, mirroring
    `dashanan.domain.zone.ZoneId`'s identical shape.
    """

    LOCAL = "local"
    IN_REGION = "in_region"
    ANY = "any"


_MIN_AUDIT_RETENTION_DAYS = 1


@dataclass(frozen=True, slots=True)
class BucketProvisioningSpec:
    """One tenant's Zone 8 bucket provisioning policy (AC-008-DPDP-2).

    Attributes:
        tenant_id: The owning tenant. Never blank.
        residency: The tenant's configured `embedding_residency` policy
            (ADR-015) -- `IN_REGION` for any tenant handling Indian
            personal data, per HLD Section 10 DPDP-4's "all stores
            in-region" requirement. Resolving a `tenant_id` to its
            actual configured residency is a tenant-configuration-store
            concern this module does not own (no such store exists yet
            anywhere in this codebase -- see this story's dev report,
            judgment-call list); this dataclass accepts the resolved
            value as an input.
        audit_retention_days: How long the bucket's object-lock retains
            each version before it may expire -- an operator-
            configurable surface (AC-008-DPDP-2's own wording), never a
            hardcoded constant. Always >= 1.
        versioning_enabled: Always `True` -- ADR-009's India Layer
            states bucket versioning as unconditional for Zone 8, not a
            caller choice (see module docstring). Not a constructor
            parameter (`init=False`): there is no way to construct a
            `BucketProvisioningSpec` with this `False`.
        object_lock_enabled: Always `True`, for the identical reason.

    Raises:
        TenantBucketPolicyError: If `tenant_id` is blank, or
            `audit_retention_days` is below
            `_MIN_AUDIT_RETENTION_DAYS`.
    """

    tenant_id: str
    residency: ResidencyRegion
    audit_retention_days: int
    versioning_enabled: bool = field(default=True, init=False)
    object_lock_enabled: bool = field(default=True, init=False)

    def __post_init__(self) -> None:
        if not self.tenant_id.strip():
            raise TenantBucketPolicyError("BucketProvisioningSpec.tenant_id must not be blank")
        if self.audit_retention_days < _MIN_AUDIT_RETENTION_DAYS:
            raise TenantBucketPolicyError(
                "BucketProvisioningSpec.audit_retention_days must be >= "
                f"{_MIN_AUDIT_RETENTION_DAYS}, got {self.audit_retention_days}"
            )
