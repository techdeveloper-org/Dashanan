"""SubjectErasureRequest / ErasureCascadeAccepted: the DPDP subject-erasure admin-job contract.

Traces to FR-008 in SRS.md. FR-008 (verbatim): "The system SHALL provide a
Consolidation Memory zone as the long-term, cross-session consolidated
store that content reaches only via the Archived state of the rotation
state machine (FR-012)."

HLD Section 7.4's `DELETE /v1/tenants/{id}/subjects/{subject_id}` --
"DPDP erasure cascade. 202 + job_id; cascades across all 8 zones, the
vector index, the lexical index and Zone 8 archives" -- is the wire
contract this module's `ErasureCascadeAccepted` stands in for. No
api/routes module or composition root exists yet anywhere in this
codebase (the same gap `dashanan.domain.tenant_credential`'s own
docstring documents for the tenant-credential wire boundary, and
`dashanan.domain.write_gate.WriteAccepted`'s docstring for FR-010's own
`202` contract) -- `ErasureCascadeAccepted` is the concrete, callable,
testable Result type a future HTTP layer maps directly onto that `202`
response, exactly as `WriteAccepted.write_id` already does for
`POST /v1/memory/write`.

Traces to AC-008-DPDP-1: "A DPDP subject-erasure request's cascade
reaches Zone 8 archives via the subject_id secondary index (ADR-006),
returning 202 with a job_id per the async admin-job pattern."

This module holds pure domain logic only -- no I/O -- mirroring
`dashanan.domain.write_gate` and `dashanan.domain.consolidated_blob`'s
identical split. Orchestration (resolving `subject_id` to Zone 8
`item_id`s, destroying the per-subject key, recording job status) lives
in `dashanan.application.subject_erasure_cascade`.

PII NOTE: `subject_id` here is an opaque identifier this module never
decodes or interprets -- the same opaque treatment
`dashanan.domain.consolidated_blob.ArchiveBatchItem.payload`'s own PII
note gives its own opaque bytes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from dashanan.domain.exceptions import DashananError


class SubjectErasureError(DashananError):
    """Raised when a `SubjectErasureRequest` or its acceptance outcome is invalid.

    Kept local to this module rather than added to the shared
    `dashanan.domain.exceptions`, mirroring `consolidated_blob.
    Zone8ConsolidationError`'s and `write_gate.ProvenanceJournalPort`'s
    identical "kept local" rationale (this story's file-disjointness
    requirement).
    """


@dataclass(frozen=True, slots=True)
class SubjectErasureRequest:
    """One caller's DPDP erasure-cascade request for `subject_id` (HLD Section 7.4).

    Attributes:
        tenant_id: The owning tenant. Never blank.
        subject_id: The data subject whose content the cascade must
            reach across all owning zones, including Zone 8 (AC-008-
            DPDP-1). Never blank. Treated as an opaque identifier this
            module never decodes (PII note above).

    Raises:
        SubjectErasureError: If `tenant_id` or `subject_id` is blank.
    """

    tenant_id: str
    subject_id: str

    def __post_init__(self) -> None:
        if not self.tenant_id.strip():
            raise SubjectErasureError("SubjectErasureRequest.tenant_id must not be blank")
        if not self.subject_id.strip():
            raise SubjectErasureError("SubjectErasureRequest.subject_id must not be blank")


@dataclass(frozen=True, slots=True)
class ErasureCascadeAccepted:
    """The `202` outcome HLD Section 7.4's DELETE endpoint returns (AC-008-DPDP-1).

    A Result type (HLD Section 6, "Degraded responses" row; mirrors
    `dashanan.domain.write_gate.WriteAccepted`'s identical "what a real
    202 response corresponds to" pattern) rather than a bare string --
    accepting a cascade request is a distinct, typed outcome from
    actually having finished it; `job_id` is what
    `GET /v1/jobs/{job_id}` (HLD Section 7.3) would resolve to this
    cascade's own completion status.

    Attributes:
        job_id: Fresh identifier for this accepted cascade request,
            echoed back to the caller. Never blank.
        tenant_id: The owning tenant this job was accepted for.
        subject_id: The data subject this job was accepted for.
        accepted_at: When the cascade request was durably accepted.
    """

    job_id: str
    tenant_id: str
    subject_id: str
    accepted_at: datetime

    @classmethod
    def for_request(
        cls,
        request: SubjectErasureRequest,
        *,
        job_id: str,
        accepted_at: datetime,
    ) -> ErasureCascadeAccepted:
        """The sole sanctioned constructor for an accepted cascade job.

        Builder-style factory (mirrors `ManifestEntry.for_write`'s
        identical "one sanctioned construction path" convention).

        Raises:
            SubjectErasureError: If `job_id` is blank.
        """
        if not job_id.strip():
            raise SubjectErasureError("ErasureCascadeAccepted.job_id must not be blank")
        return cls(
            job_id=job_id,
            tenant_id=request.tenant_id,
            subject_id=request.subject_id,
            accepted_at=accepted_at,
        )
