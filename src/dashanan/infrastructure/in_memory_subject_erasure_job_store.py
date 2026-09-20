"""InMemorySubjectErasureJobStore: Shape A `SubjectErasureJobStore` -- an in-process job registry.

Mirrors `InMemorySubjectKeyStore`'s identical Shape A rationale, applied
to HLD Section 7.3's `GET /v1/jobs/{job_id}` async-admin-job data
contract instead of key storage.
"""

from __future__ import annotations

from dashanan.application.subject_erasure_cascade import (
    SubjectErasureJob,
    SubjectErasureJobStoreError,
)


class InMemorySubjectErasureJobStore:
    """Implements `SubjectErasureJobStore` against an in-process `dict`."""

    def __init__(self) -> None:
        self._jobs: dict[str, SubjectErasureJob] = {}

    def save(self, job: SubjectErasureJob) -> None:
        """Durably record `job`, replacing any prior state for its `job_id`.

        Raises:
            SubjectErasureJobStoreError: If `job.job_id` is blank.
        """
        if not job.job_id.strip():
            raise SubjectErasureJobStoreError(
                "InMemorySubjectErasureJobStore.save requires a non-blank job_id"
            )
        self._jobs[job.job_id] = job

    def get(self, job_id: str) -> SubjectErasureJob | None:
        """Return `job_id`'s current state, or `None` if unknown."""
        return self._jobs.get(job_id)
