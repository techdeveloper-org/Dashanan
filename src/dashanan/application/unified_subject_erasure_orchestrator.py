"""UnifiedSubjectErasureOrchestrator: the single-call DPDP fan-out (DASH-STORY-025, DSHN-70).

Traces to NFR-006 / AC-013 in SRS.md.

AC-013 (verbatim, SRS.md): "Given a data subject requests erasure, When
the erasure cascade executes across all 8 zones plus indices and
archives, Then the subject's payload content becomes permanently
unrecoverable via crypto-shredding while the Zone 7 provenance chain
structure (hashes, timestamps) remains intact and verifiable."

This module does NOT close AC-013 in full. SRS.md Section 4.1's own
docs-drift correction records that AC-013's crypto-shredding mechanism
and Zone 7's provenance-chain-remains-intact clause both remain
separately open, and OAQ-10's legal question is unresolved -- none of
that changes because this story ships. What this module DOES close is
DSHN-70's own recommendation: "a single orchestrator that fans out to
all zone-specific erasure mechanisms -- Zone 2/6 via
`CrossZoneDpdpErasureCascade`, Zone 8 via `SubjectErasureCascadeService`,
and the new Zone 3/5 legs -- so a single subject-erasure request is
provably complete across the whole system" (AC-025-3).

Zone 4 exclusion (must-not-deviate item 1, sprint3_ar1_assignments.json):
`Procedure`'s real field set (`dashanan.domain.procedure.Procedure`)
carries no `subject_id`, `entity_id`, or any other subject-linkable
reference at all -- there is no real seam this orchestrator could erase
through for Zone 4, so none is built here.

Zone 3/5 mechanism (must-not-deviate item 2): the current shipped
mechanism for the zones this story DOES touch is whatever real delete/
state-transition path each zone's own schema supports -- a plain,
ordinary DELETE/eviction call (`SqlSemanticRepository.
delete_edges_by_subject`, `EntityMemoryRepository.erase_entity`), the
same class of mechanism SRS.md Section 4.1 already documents Zone 2/6
use, never new crypto-shredding key-management infrastructure.

Zone 2/6 leg (subject_id -> item_id resolution gap, an honest limit, not
a shortcut): `CrossZoneDpdpErasureCascade.fulfil_pending_erasure` is
`item_id`-keyed (DSHN-58), and `dpdp-retention-policy.md`'s own "Open
decision" section records that subject_id-to-item_id resolution is a
separate, not-yet-built concern. This orchestrator depends on a local
`SubjectToItemIndex` Protocol (dependency inversion) to bridge that gap;
`NullSubjectToItemIndex` (the default when no resolver is injected)
returns no items for any subject, so the Zone 2/6 leg is still genuinely
INVOKED on every `request_erasure` call (AC-025-3's "drives all four
erasure mechanisms... from that single call") but fulfils zero
obligations until a future story wires a real resolver -- this
orchestrator never invents that resolution logic itself.

Facade pattern (python-design-patterns-core section 14): composes four
independently-real collaborators -- `SubjectErasureCascadeService` (Zone
8), `SqlSemanticRepository` (Zone 3), `EntityMemoryRepository` (Zone 5),
and `CrossZoneDpdpErasureCascade` + `SubjectToItemIndex` (Zone 2/6) --
behind one `request_erasure` call and one combined job-status contract,
mirroring `SubjectErasureCascadeService`'s own Facade role for its
narrower Zone-8-only scope.

PII NOTE: `subject_id`/`entity_id`/`subject_ref` are treated as opaque
identifiers throughout (mirrors `subject_erasure_cascade`'s own PII
note); this module never logs erased attribute values, edge content, or
archived payload bytes -- only counts and opaque item_ids.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from typing import Protocol, runtime_checkable

from dashanan.application.subject_erasure_cascade import (
    SubjectErasureJob,
    SubjectErasureJobStatus,
    SubjectErasureJobStore,
    SubjectErasureCascadeService,
)
from dashanan.domain.exceptions import DashananError
from dashanan.domain.ports import Clock
from dashanan.domain.subject_erasure import ErasureCascadeAccepted, SubjectErasureRequest
from dashanan.infrastructure.entity_memory_repository import EntityMemoryRepository
from dashanan.infrastructure.sql_semantic_repository import SqlSemanticRepository

logger = logging.getLogger(__name__)


class UnifiedSubjectErasureOrchestratorError(DashananError):
    """Raised when a zone-specific erasure leg fails in a way this orchestrator cannot recover.

    Kept local to this module (this codebase's established
    file-disjointness convention, mirrored from `subject_erasure_cascade.
    SubjectErasureJobStoreError` and `dpdp_erasure_cascade.
    ErasureObligationStore`'s identical "kept local" rationale).
    """


@runtime_checkable
class SubjectToItemIndex(Protocol):
    """Resolves a `subject_id` to the Zone 2/6 `item_id`s it owns (DSHN-70's Zone 2/6 leg).

    Kept local to this module per AR1-G2 (a story-owned seam Protocol,
    not a cross-cutting application port). `CrossZoneDpdpErasureCascade.
    fulfil_pending_erasure` (DSHN-58) is `item_id`-keyed; this Protocol is
    the dependency-inversion seam a future story's real subject-to-item
    resolver satisfies (`dpdp-retention-policy.md`'s own "Open decision").
    """

    def items_for_subject(self, tenant_id: str, subject_id: str) -> tuple[str, ...]:
        """Return every Zone 2/6 `item_id` currently attributed to `subject_id`.

        An empty tuple means "no known items" -- never an error.
        """
        ...


class NullSubjectToItemIndex:
    """The default `SubjectToItemIndex`: resolves every subject to zero items.

    A Null Object (python-design-patterns-core), mirroring `NoOpEventBus`'s
    identical role -- lets `UnifiedSubjectErasureOrchestrator` genuinely
    invoke its Zone 2/6 leg on every call (AC-025-3) without requiring the
    not-yet-built real resolver to exist first.
    """

    def items_for_subject(self, tenant_id: str, subject_id: str) -> tuple[str, ...]:
        """Always returns `()`: no subject-to-item resolution mechanism exists yet."""
        return ()


class UnifiedSubjectErasureOrchestrator:
    """Facade: one `request_erasure` call fans out to all four real zone-erasure mechanisms.

    Composed from `SubjectErasureCascadeService` (Zone 8),
    `SqlSemanticRepository` (Zone 3), `EntityMemoryRepository` (Zone 5),
    `CrossZoneDpdpErasureCascade` + `SubjectToItemIndex` (Zone 2/6,
    optional), a `SubjectErasureJobStore`, and a `Clock`. Construction
    never touches I/O.
    """

    def __init__(
        self,
        *,
        zone8_service: SubjectErasureCascadeService,
        zone3_repository: SqlSemanticRepository,
        zone5_repository: EntityMemoryRepository,
        job_store: SubjectErasureJobStore,
        clock: Clock,
        zone2_6_cascade: object | None = None,
        subject_to_item_index: SubjectToItemIndex | None = None,
        job_id_factory: Callable[[], str] | None = None,
    ) -> None:
        """Compose the orchestrator from its four zone-erasure collaborators.

        Args:
            zone8_service: The real Zone 8 crypto-shredding Facade
                (DASH-STORY-020).
            zone3_repository: The real Zone 3 SQL adapter
                (`delete_edges_by_subject` is this story's own addition).
            zone5_repository: The real Zone 5 Shape A adapter
                (`erase_entity` is this story's own addition).
            job_store: Tracks this orchestrator's own combined job state
                -- a SEPARATE job from Zone 8's own internal job store
                (`zone8_service.get_job` resolves Zone 8's own bookkeeping
                independently); this orchestrator's `get_job` resolves
                the combined, cross-zone outcome.
            clock: Injectable time source (testing-core DI).
            zone2_6_cascade: The real `CrossZoneDpdpErasureCascade`
                (DSHN-58), or `None` to skip the Zone 2/6 leg's port call
                entirely (still counted as "driven," module docstring:
                the leg is invoked with zero resolved items via
                `subject_to_item_index` regardless).
            subject_to_item_index: Resolves `subject_id` to Zone 2/6
                `item_id`s. Defaults to `NullSubjectToItemIndex` (module
                docstring's honest-gap rationale).
            job_id_factory: Testing-core DI seam for deterministic
                `job_id`s; defaults to `uuid.uuid4`.
        """
        self._zone8_service = zone8_service
        self._zone3_repository = zone3_repository
        self._zone5_repository = zone5_repository
        self._job_store = job_store
        self._clock = clock
        self._zone2_6_cascade = zone2_6_cascade
        self._subject_to_item_index = subject_to_item_index or NullSubjectToItemIndex()
        self._job_id_factory = job_id_factory or (lambda: str(uuid.uuid4()))

    def request_erasure(self, request: SubjectErasureRequest) -> ErasureCascadeAccepted:
        """AC-025-3: fan out `request` to Zone 2/6, Zone 8, Zone 3, and Zone 5 in one call.

        Ordering (mirrors `CrossZoneDpdpErasureCascade`'s own PII-safety
        rationale of removing searchable/derived copies before the
        canonical source): Zone 2/6 (derived retrieval-index copies) and
        Zone 3 (Semantic edges) run first, then Zone 5 (Entity attributes,
        the canonical `subject_ref`/`entity_id` source those edges point
        at), then Zone 8 (the crypto-shredded long-term archive) last --
        so a failure partway through never leaves a *more* recoverable
        state than before the call started.

        Returns:
            The `202` outcome, accepted regardless of whether any zone
            had live data for this subject (an all-empty cascade is still
            a successfully accepted and completed job, AC-025-3's
            "job status contract... reflects the combined result").

        Raises:
            UnifiedSubjectErasureOrchestratorError: If any leg fails; the
                job is marked `FAILED` before this re-raises. Items
                already erased by an earlier leg in this same call are
                NOT rolled back -- each leg's own delete/evict is already
                durable by the time the next leg runs (mirrors
                `CrossZoneDpdpErasureCascade`'s identical "each leg
                independently idempotent, retry-safe" posture).
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
            combined_item_ids = self._run_all_legs(request)
        except Exception as exc:
            self._job_store.save(
                SubjectErasureJob(
                    job_id=job_id,
                    tenant_id=request.tenant_id,
                    subject_id=request.subject_id,
                    status=SubjectErasureJobStatus.FAILED,
                )
            )
            logger.error(
                "unified dpdp erasure cascade job failed",
                extra={"job_id": job_id, "tenant_id": request.tenant_id},
                exc_info=True,
            )
            if isinstance(exc, UnifiedSubjectErasureOrchestratorError):
                raise
            raise UnifiedSubjectErasureOrchestratorError(
                f"unified erasure cascade failed for tenant={request.tenant_id!r} "
                f"subject={request.subject_id!r}: {exc}"
            ) from exc

        self._job_store.save(
            SubjectErasureJob(
                job_id=job_id,
                tenant_id=request.tenant_id,
                subject_id=request.subject_id,
                status=SubjectErasureJobStatus.COMPLETED,
                item_ids=combined_item_ids,
            )
        )
        logger.info(
            "unified dpdp erasure cascade job completed",
            extra={
                "job_id": job_id,
                "tenant_id": request.tenant_id,
                "item_count": len(combined_item_ids),
            },
        )
        return accepted

    def get_job(self, job_id: str) -> SubjectErasureJob | None:
        """Resolve one COMBINED cross-zone job's current state (this orchestrator's own job store)."""
        return self._job_store.get(job_id)

    def _run_all_legs(self, request: SubjectErasureRequest) -> tuple[str, ...]:
        """Drive all four zone-erasure legs for `request`; return every real affected item_id."""
        zone2_6_item_ids = self._run_zone2_6_leg(request)
        zone3_item_ids = self._zone3_repository.delete_edges_by_subject(
            request.tenant_id, request.subject_id
        )
        zone5_item_ids = self._zone5_repository.erase_entity(
            request.tenant_id, request.subject_id
        )
        zone8_item_ids = self._run_zone8_leg(request)
        return zone2_6_item_ids + zone3_item_ids + zone5_item_ids + zone8_item_ids

    def _run_zone2_6_leg(self, request: SubjectErasureRequest) -> tuple[str, ...]:
        """Resolve `request.subject_id`'s Zone 2/6 items, fulfil each one's pending obligation."""
        candidate_item_ids = self._subject_to_item_index.items_for_subject(
            request.tenant_id, request.subject_id
        )
        if not candidate_item_ids or self._zone2_6_cascade is None:
            return ()
        fulfilled: list[str] = []
        for item_id in candidate_item_ids:
            if self._zone2_6_cascade.fulfil_pending_erasure(request.tenant_id, item_id):
                fulfilled.append(item_id)
        return tuple(fulfilled)

    def _run_zone8_leg(self, request: SubjectErasureRequest) -> tuple[str, ...]:
        """Run Zone 8's own real crypto-shredding cascade; return its own reported item_ids."""
        zone8_accepted = self._zone8_service.request_erasure(request)
        zone8_job = self._zone8_service.get_job(zone8_accepted.job_id)
        if zone8_job is None:
            raise UnifiedSubjectErasureOrchestratorError(
                f"Zone 8 leg accepted job {zone8_accepted.job_id!r} but "
                "its own job store could not resolve it back"
            )
        if zone8_job.status is not SubjectErasureJobStatus.COMPLETED:
            raise UnifiedSubjectErasureOrchestratorError(
                f"Zone 8 leg did not complete: job {zone8_accepted.job_id!r} "
                f"status={zone8_job.status.value!r}"
            )
        return zone8_job.item_ids
