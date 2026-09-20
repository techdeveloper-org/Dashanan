"""SubjectErasureCascadeService: the DPDP admin-job entrypoint (AC-008-DPDP-1).

Traces to FR-008 in SRS.md; see `dashanan.domain.subject_erasure`'s own
docstring for FR-008's verbatim text and the HLD Section 7.4 sourcing.

HLD Section 7.4 (verbatim): `DELETE /v1/tenants/{id}/subjects/{subject_id}`
-- "DPDP erasure cascade. 202 + job_id; cascades across all 8 zones, the
vector index, the lexical index and Zone 8 archives." This module is the
Zone-8-reaching HALF of that cascade -- the other zones' own erasure legs
(Zone 2 + Zone 6 already exist via `dashanan.infrastructure.
dpdp_erasure_cascade.CrossZoneDpdpErasureCascade`, DSHN-58) are composed
alongside this service by whatever future composition root wires the
full `DELETE` handler; this story adds the piece that was previously
entirely missing -- Zone 8's own reach (see this story's dev report,
judgment-call list, for the scope boundary between the two).

`SubjectErasureCascadeService` is a Facade (python-design-patterns-core
Section 14) over `Zone8SubjectKeyedArchiver.erase_subject` plus a job
store, giving the HLD's `202 + job_id` / `GET /v1/jobs/{job_id}` shape a
concrete, synchronous Shape A implementation: this codebase has no
async worker/queue infrastructure yet (the same "no api/routes module or
composition root exists yet" gap `dashanan.domain.tenant_credential`'s
own docstring documents), so `request_erasure` performs the Zone 8
cascade INLINE and records the job as already completed by the time it
returns -- the `ErasureCascadeAccepted`/`SubjectErasureJobStore` data
contract this module exposes is what a future async worker would
consume without any caller-visible change (see this story's dev report,
judgment-call list).

MUST-NOT-DEVIATE (sprint2_ar1_assignments.json AR1-S2-020, binding):
  2. "OAQ-10's legal question ... is not settled by this story" -- no
     docstring or log message in this module asserts DPDP Act 2023
     legal sufficiency; `SubjectErasureJobStatus` values name only the
     engineering outcome (accepted/completed/failed), never a legal
     conclusion.

PII NOTE: `subject_id` is treated as an opaque identifier throughout
(mirrors `dashanan.domain.subject_erasure`'s own PII note); this module
never logs subject key material or archived content.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from dashanan.application.zone8_crypto_shredding_store import (
    Zone8CryptoShreddingStoreError,
    Zone8SubjectKeyedArchiver,
)
from dashanan.domain.exceptions import DashananError
from dashanan.domain.ports import Clock
from dashanan.domain.subject_erasure import ErasureCascadeAccepted, SubjectErasureRequest

logger = logging.getLogger(__name__)


class SubjectErasureJobStoreError(DashananError):
    """Raised by a `SubjectErasureJobStore` operational failure.

    Kept local to this module, mirroring `zone8_crypto_shredding_store.
    Zone8CryptoShreddingStoreError`'s identical "kept local" rationale.
    """


class SubjectErasureJobStatus(str, Enum):
    """The engineering lifecycle of one erasure-cascade job (never a legal conclusion).

    Mirrors `dashanan.domain.zone.ZoneId`'s `str, Enum` shape so a
    status serializes directly as its wire value.
    """

    ACCEPTED = "accepted"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SubjectErasureJob:
    """One tracked erasure-cascade job's current state (`GET /v1/jobs/{job_id}`, HLD 7.3).

    Attributes:
        job_id: This job's identifier -- the same value
            `ErasureCascadeAccepted.job_id` carries.
        tenant_id: The owning tenant.
        subject_id: The data subject this job is erasing.
        status: This job's current lifecycle state.
        item_ids: Every Zone 8 `item_id` this job rendered unreadable.
            Empty until `status` is `COMPLETED`.
    """

    job_id: str
    tenant_id: str
    subject_id: str
    status: SubjectErasureJobStatus
    item_ids: tuple[str, ...] = ()


@runtime_checkable
class SubjectErasureJobStore(Protocol):
    """Tracks one erasure-cascade job per `job_id` (HLD Section 7.3's async admin-job pattern).

    Kept local to this module per this story's file-disjointness
    requirement.
    """

    def save(self, job: SubjectErasureJob) -> None:
        """Durably record `job`'s current state, replacing any prior state for its `job_id`.

        Raises:
            SubjectErasureJobStoreError: If the write fails.
        """
        ...

    def get(self, job_id: str) -> SubjectErasureJob | None:
        """Return `job_id`'s current state, or `None` if unknown.

        Raises:
            SubjectErasureJobStoreError: If the underlying store cannot
                be queried.
        """
        ...


class SubjectErasureCascadeService:
    """Facade: accept a DPDP erasure request, run Zone 8's crypto-shred, track the job.

    Composed from `Zone8SubjectKeyedArchiver`, a `SubjectErasureJobStore`,
    and a `Clock`. Construction never touches I/O.
    """

    def __init__(
        self,
        archiver: Zone8SubjectKeyedArchiver,
        job_store: SubjectErasureJobStore,
        clock: Clock,
        job_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._archiver = archiver
        self._job_store = job_store
        self._clock = clock
        self._job_id_factory = job_id_factory or (lambda: str(uuid.uuid4()))

    def request_erasure(self, request: SubjectErasureRequest) -> ErasureCascadeAccepted:
        """AC-008-DPDP-1: accept `request`, run Zone 8's cascade, return the `202` outcome.

        Steps:
          1. Mint a fresh `job_id` and record the job as `ACCEPTED` --
             the durability barrier `ErasureCascadeAccepted` reports,
             mirroring `ProvenanceWriteGate`'s own "journal append before
             returning 202" ordering (ADR-010), scoped here to the job
             store instead of the provenance journal.
          2. Run `Zone8SubjectKeyedArchiver.erase_subject` for
             `request`'s `(tenant_id, subject_id)`.
          3. Record the job `COMPLETED` with the affected `item_ids` on
             success, or `FAILED` (re-raising) on any port failure --
             never leaving a job silently stuck at `ACCEPTED`.

        Returns:
            The `202` outcome, accepted regardless of whether Zone 8 had
            any archives for this subject (an empty cascade is still a
            successfully accepted and completed job).

        Raises:
            Zone8CryptoShreddingStoreError: Propagated unchanged if the
                key store or subject index cannot be reached; the job is
                marked `FAILED` before this re-raises.
            SubjectErasureJobStoreError: If the job store itself cannot
                be written to.
        """
        job_id = self._job_id_factory()
        accepted_at = self._clock.now()
        accepted = ErasureCascadeAccepted.for_request(
            request, job_id=job_id, accepted_at=accepted_at
        )
        self._job_store.save(
            SubjectErasureJob(
                job_id=job_id,
                tenant_id=request.tenant_id,
                subject_id=request.subject_id,
                status=SubjectErasureJobStatus.ACCEPTED,
            )
        )

        try:
            item_ids = self._archiver.erase_subject(request.tenant_id, request.subject_id)
        except Zone8CryptoShreddingStoreError:
            self._job_store.save(
                SubjectErasureJob(
                    job_id=job_id,
                    tenant_id=request.tenant_id,
                    subject_id=request.subject_id,
                    status=SubjectErasureJobStatus.FAILED,
                )
            )
            logger.error(
                "zone8 dpdp erasure cascade job failed",
                extra={"job_id": job_id, "tenant_id": request.tenant_id},
                exc_info=True,
            )
            raise

        self._job_store.save(
            SubjectErasureJob(
                job_id=job_id,
                tenant_id=request.tenant_id,
                subject_id=request.subject_id,
                status=SubjectErasureJobStatus.COMPLETED,
                item_ids=item_ids,
            )
        )
        logger.info(
            "zone8 dpdp erasure cascade job completed",
            extra={
                "job_id": job_id,
                "tenant_id": request.tenant_id,
                "item_count": len(item_ids),
            },
        )
        return accepted

    def get_job(self, job_id: str) -> SubjectErasureJob | None:
        """`GET /v1/jobs/{job_id}` (HLD Section 7.3): resolve one job's current state."""
        return self._job_store.get(job_id)
