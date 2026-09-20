"""ArchiveEngine: Compressed -> Archived rotation completion (DASH-STORY-019).

HLD Section 12A (Per-Zone Policy Table, Zone 8 row: HalfLife 270d, weekly
batch sweep) + Section 12B.2-12B.3 (ArchiveThreshold=0.25 derivation) +
Section 12C (ADR-012, closed-form deadline, deadline-invalidation wiring)
+ Section 6 (State Machine, Strategy, Observer/Event Bus, Template
Method), traced to FR-008 in SRS.md.

FR-008 (verbatim): "The system SHALL provide a Consolidation Memory zone
as the long-term, cross-session consolidated store that content reaches
only via the Archived state of the rotation state machine (FR-012)."

AC-008-ROT-1 (verbatim): "A Compressed item crossing ArchiveThreshold
(0.25) transitions to Archived and is persisted into Zone 8 as a
ConsolidatedBlob with a resolvable manifest entry, completing
DASH-STORY-009's AC-012-ROT-1-deferred transition."
AC-008-ROT-2 (verbatim): "An item with payload_tokens < 64 is
fast-tracked through Compressed and Archived within the same sweep, per
OAQ-12."
AC-008-ROT-3 (verbatim): "All eligible Compressed items are consolidated
in a single weekly batch window, no individual per-item archive call
outside the batch (other than the OAQ-12 fast-track)."
AC-008-ROT-4 (verbatim): "The Archived-transition decision uses the
post-mutation, non-stale rotation deadline from DASH-STORY-010's
deadline-invalidation wiring, not a cached pre-mutation value."

This module holds the orchestration `dashanan.domain.archive_transition`
deliberately leaves out: Clock injection, the two ports a concrete zone
adapter implements, structured logging, `memory.archived` event
publication, and the call into `dashanan.application.
zone8_consolidation_store.Zone8ConsolidationStore` -- mirroring the
`domain/rotation_state.py` (pure types/functions) + `application/
rotation_engine.py` (orchestration + logging) split DASH-STORY-009
already established, and this story's own `domain/archive_transition.py`
+ this file's identical split.

MUST-NOT-DEVIATE (sprint2_ar1_assignments.json AR1-S2-019, binding):
  1. "ArchiveThreshold is fixed at 0.25" -- `run_weekly_archive_sweep`
     and `fast_track_archive` both delegate eligibility entirely to
     `dashanan.domain.archive_transition.is_archive_eligible`, which
     reads the fixed `ARCHIVE_THRESHOLD` module constant; neither method
     in this class accepts a threshold parameter of its own.
  2. "An item SHALL NOT reach Archived without having passed through
     Compressed" -- both `run_weekly_archive_sweep` and
     `fast_track_archive` call `dashanan.domain.archive_transition.
     guarded_archive_transition(snapshot.lifecycle_state, RotationState.
     ARCHIVED)` before calling `ArchiveTransitionPort.mark_archived`; a
     candidate whose re-scored `lifecycle_state` is not `Compressed` is
     rejected by that call and recorded as a skip, never persisted.
  3. "The OAQ-12 fast-track is gated strictly by payload_tokens < 64" --
     `fast_track_archive` is the ONLY method in this class that performs
     the OAQ-12 fast-track write path, and it is called by a caller
     (DASH-STORY-009's rotation sweep wiring, out of this story's scope,
     see the dev report's judgment-call list) only when `dashanan.domain.
     archive_transition.is_oaq12_fast_track_eligible(payload_tokens)` is
     `True` -- this class performs no independent fast-track eligibility
     re-check of its own beyond the archive-eligibility re-score guard
     both paths share.
  4. "DASH-STORY-004's Zone-2-only capacity/MaxAge backstop must not be
     left as the sole bound on Compressed growth once this story ships"
     -- satisfied structurally by this module's existence: a `Compressed`
     item now has a second, real exit path (`Archived`, via
     `Zone8ConsolidationStore.consolidate_batch`) in addition to
     DASH-STORY-004/016's capacity-driven removal. This module makes no
     change to `zone2_capacity_backstop.py`, `zone2_capacity_backstop_
     sweep.py`, `zone4_capacity_backstop.py`, or `zone4_capacity_
     backstop_sweep.py` (file-disjointness; those files are owned by
     DASH-STORY-004/016, not this story).
  5. "No individual per-item archive call outside the batch (other than
     the OAQ-12 fast-track)" (AC-008-ROT-3) -- `run_weekly_archive_sweep`
     calls `Zone8ConsolidationStore.consolidate_batch` EXACTLY ONCE per
     invocation, covering every eligible candidate from `candidate_item_
     ids` in a single call; `fast_track_archive` is the one sanctioned
     exception the AC itself names, calling `consolidate_batch` with a
     single-item batch for its one item.

PII NOTE: every port and payload here carries only scheduling and
state-machine control metadata -- `item_id`, `tenant_id`, a `ZoneId`, a
`RotationState`, a `MemoryScore` float, a token count, timestamps --
never an item's payload/fact content as anything other than the opaque
`bytes` `ArchiveTransitionPort.mark_archived` returns and this module
relays unread into `Zone8ConsolidationStore` (dev_prompt's PII
constraint, mirrored from `rotation_engine.py`'s and
`zone8_consolidation_store.py`'s identical posture).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from dashanan.application.zone8_consolidation_store import (
    ArchiveWritten,
    Zone8ConsolidationStore,
)
from dashanan.application.zone8_crypto_shredding_store import (
    Zone8CryptoShreddingStoreError,
    Zone8SubjectIndexPort,
)
from dashanan.domain.archive_transition import (
    ArchiveTransitionError,
    guarded_archive_transition,
    is_archive_eligible,
)
from dashanan.domain.consolidated_blob import ArchiveBatchItem
from dashanan.domain.exceptions import DashananError
from dashanan.domain.ports import Clock, EventBus
from dashanan.domain.rotation_state import RotationState
from dashanan.domain.zone import ZoneId

logger = logging.getLogger(__name__)

_ARCHIVED_EVENT_TYPE = "memory.archived"
"""This story's Observer event name, mirrors `dashanan.application.
rotation_engine._COMPRESSED_EVENT_TYPE`'s identical naming convention
(`memory.<past-tense-state>`) for the sibling `Compressed` transition."""

_SCORE_GUARD_EPSILON = 1e-9
"""Float-rounding tolerance for `RotationCandidateSnapshot`'s bounds
check below -- mirrors `dashanan.application.rotation_engine.
_SCORE_GUARD_EPSILON`'s identical rationale, kept as a separate constant
in this file per this story's file-disjointness scope."""


class ArchiveEnginePortError(DashananError):
    """Raised by one of this module's ports on an expected, operational failure.

    Kept local to this module rather than added to the shared `dashanan.
    domain.exceptions` module -- mirrors `dashanan.application.
    rotation_engine.RotationEnginePortError`'s identical file-
    disjointness and fail-safe-isolation rationale: a port raising this
    type is an operational failure isolated to the one item being
    processed, so one unreachable zone adapter cannot halt the rest of a
    sweep tick. A port raising anything else is treated as a programming
    bug and propagates immediately (error-handling-patterns section 2,
    fail-fast).
    """


@runtime_checkable
class ArchiveCandidatePort(Protocol):
    """Supplies one item's current lifecycle state, MemoryScore and token count.

    Mirrors `dashanan.application.rotation_engine.RotationCandidatePort`'s
    "re-score at sweep time, not at scheduling time" contract
    (AC-008-ROT-4's non-stale requirement): a concrete zone adapter
    returns the item's CURRENT state and score, freshly read, never a
    value cached from when the item became `Compressed`.
    """

    def current_state(
        self, tenant_id: str, zone_id: ZoneId, item_id: str
    ) -> ArchiveCandidateSnapshot:
        """Return `item_id`'s current lifecycle state, score, and token count.

        Raises:
            ArchiveEnginePortError: If the item's current state cannot
                be read (e.g. the zone's repository is unreachable, or
                the item no longer exists -- both are operational, not
                programming, failures for this port).
        """
        ...


@runtime_checkable
class ArchiveTransitionPort(Protocol):
    """Persists a successful `Compressed -> Archived` transition in the owning zone.

    Deliberately narrow (Interface Segregation): this port's only
    responsibility is durably recording the new `RotationState` in the
    source zone and returning the item's already-Compressed payload
    bytes plus its compression generation, so the caller
    (`ArchiveEngine`) can hand both to `Zone8ConsolidationStore.
    consolidate_batch` -- mirrors `dashanan.application.rotation_engine.
    RotationTransitionPort`'s identical "persist state, return payload
    metadata" shape for the sibling `Compressed` transition.
    """

    def mark_archived(
        self, tenant_id: str, zone_id: ZoneId, item_id: str
    ) -> ArchiveMarkResult:
        """Durably transition `item_id` to `RotationState.ARCHIVED` in its source zone.

        Raises:
            ArchiveEnginePortError: If the transition cannot be
                persisted (e.g. the zone's repository is unreachable).
        """
        ...


@dataclass(frozen=True, slots=True)
class ArchiveCandidateSnapshot:
    """One item's current lifecycle state, score and token count, as of re-score time.

    Attributes:
        lifecycle_state: The item's `RotationState` at the instant this
            snapshot was produced -- NOT necessarily `COMPRESSED`; a
            concurrent read-triggered promotion or an already-archived
            item both produce a state `guarded_archive_transition` then
            correctly rejects.
        memory_score: The item's current composite `MemoryScore.value`
            (`dashanan.domain.memory_score`), in `[0, 1]`, re-scored
            fresh (AC-008-ROT-4).
        payload_tokens: The item's current token count -- OAQ-12's sole
            fast-track signal (`dashanan.domain.archive_transition.
            is_oaq12_fast_track_eligible`).
        subject_id: The DPDP data subject `item_id` is derivably owned
            by, when the source zone's own item carries one at re-score
            time (DSHN-69). `None` when the source zone item has no
            subject-linkable field at all (e.g. a Zone 4 Procedure, per
            DSHN-70) -- a concrete `ArchiveCandidatePort` adapter for
            such a zone always returns `None` here, never a fabricated
            value. When non-`None`, `ArchiveEngine` carries it through
            to `dashanan.domain.consolidated_blob.ArchiveBatchItem.
            subject_id` and registers it with `Zone8SubjectIndexPort.
            record_item` after a successful Zone 8 write, so a later
            `Zone8SubjectKeyedArchiver.erase_subject` call can resolve
            and account for this item (AC-008-DPDP-1's "cascade reaches
            Zone 8 archives" read as a blanket promise, not one scoped
            only to the separate subject-keyed crypto-shredding write
            path). This field carries no encryption of its own -- full
            crypto-shredding of un-keyed sweep payloads remains out of
            this fix's scope; only the subject_id index entry is added.
    """

    lifecycle_state: RotationState
    memory_score: float
    payload_tokens: int
    subject_id: str | None = None

    def __post_init__(self) -> None:
        """Raises:
        ValueError: If `memory_score` is outside `[0, 1]`,
            `payload_tokens` is negative, or `subject_id` is a
            non-`None` blank string.
        """
        if not (
            -_SCORE_GUARD_EPSILON
            <= self.memory_score
            <= 1.0 + _SCORE_GUARD_EPSILON
        ):
            raise ValueError(
                f"ArchiveCandidateSnapshot.memory_score must be in [0, 1], "
                f"got {self.memory_score}"
            )
        if self.payload_tokens < 0:
            raise ValueError(
                "ArchiveCandidateSnapshot.payload_tokens must be >= 0, got "
                f"{self.payload_tokens}"
            )
        if self.subject_id is not None and not self.subject_id.strip():
            raise ValueError(
                "ArchiveCandidateSnapshot.subject_id must be None or "
                "non-blank, never an empty/whitespace string"
            )


@dataclass(frozen=True, slots=True)
class ArchiveMarkResult:
    """Metadata `ArchiveTransitionPort.mark_archived` returns for the Zone 8 hand-off.

    Attributes:
        payload: The item's already-Compressed content, as opaque bytes
            this module never decodes (PII note above) -- passed
            straight through to `dashanan.domain.consolidated_blob.
            ArchiveBatchItem.payload`.
        generation: The item's compression generation counter at the
            moment it was archived (HLD Section 7.6 payload-field
            convention, mirrors `dashanan.application.rotation_engine.
            RotationCompressionResult.generation`).
    """

    payload: bytes
    generation: int


@dataclass(frozen=True, slots=True)
class ArchiveTransitionOutcome:
    """One item this call successfully archived (state transition AND Zone 8 write).

    Attributes:
        item_id: The archived item's identifier.
        archived_at: The sweep's `as_of` instant.
        generation: `ArchiveMarkResult.generation`.
        manifest_blob_id: The Zone 8 `ManifestEntry.blob_id` this item's
            content now resolves to (AC-008-ROT-1's "resolvable manifest
            entry").
    """

    item_id: str
    archived_at: datetime
    generation: int
    manifest_blob_id: str


@dataclass(frozen=True, slots=True)
class ArchiveSkip:
    """One candidate this call evaluated but did NOT archive, and why.

    Neither a failure nor an error -- the re-score guard (a candidate no
    longer eligible under `ARCHIVE_THRESHOLD`) and the guarded-transition
    check (a candidate no longer `Compressed`) both produce this outcome
    as their normal, expected result -- mirrors `dashanan.application.
    rotation_engine.RotationSkip`'s identical convention.

    Attributes:
        item_id: The item this call did not archive.
        reason: A short, human-readable, PII-free explanation.
    """

    item_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class ArchiveFailure:
    """One candidate this call attempted to process but could not, due to a port error.

    A Result-type field (HLD Section 6, "Degraded responses" row) rather
    than a raised exception propagating out of the sweep -- mirrors
    `dashanan.application.rotation_engine.RotationFailure`'s identical
    rationale: one item's operational failure must not abort every other
    candidate's processing in the same tick.

    Attributes:
        item_id: The item this call failed to process.
        error: `str(exc)` from the `ArchiveEnginePortError` (or,
            for the whole-batch Zone 8 write, `Zone8StorePortError`/
            `Zone8ObjectStoreUnavailableError`) that was raised. Ports
            are contractually responsible for keeping this message
            PII-free.
    """

    item_id: str
    error: str


@dataclass(frozen=True, slots=True)
class _MarkedCandidate:
    """One candidate `_evaluate_and_mark` successfully transitioned, plus its subject_id.

    Kept private and local to this module: the payload/generation pair
    still comes from `ArchiveTransitionPort.mark_archived`'s own
    `ArchiveMarkResult`, while `subject_id` comes from the same
    `ArchiveCandidateSnapshot` re-score `_evaluate_and_mark` already read
    -- combined here so both `run_weekly_archive_sweep` and
    `fast_track_archive` can build a subject-carrying `ArchiveBatchItem`
    and a subject-index registration from one return value.
    """

    mark_result: ArchiveMarkResult
    subject_id: str | None


@dataclass(frozen=True, slots=True)
class ArchiveSweepResult:
    """The outcome of one `ArchiveEngine.run_weekly_archive_sweep` call.

    Attributes:
        tenant_id: The tenant this sweep ran for.
        zone_id: The source zone this sweep ran for.
        archived: Every item this run durably archived (state transition
            persisted AND written into Zone 8).
        deferred: Every item this run could not archive because Zone 8's
            object store was unavailable (AC-008-4, relayed from
            `Zone8ConsolidationStore.consolidate_batch`'s own
            `ArchiveDeferred` outcomes) -- these items remain
            `Compressed` in their source zone, expected to be retried by
            a later weekly sweep.
        skipped: Every candidate this run evaluated but did not
            transition (see `ArchiveSkip`).
        failed: Every candidate this run could not process due to a port
            error (`ArchiveEnginePortError` only).
    """

    tenant_id: str
    zone_id: ZoneId
    archived: tuple[ArchiveTransitionOutcome, ...]
    deferred: tuple[ArchiveSkip, ...]
    skipped: tuple[ArchiveSkip, ...]
    failed: tuple[ArchiveFailure, ...]


class ArchiveEngine:
    """The Archive Engine: `Compressed -> Archived` weekly sweep and OAQ-12 fast-track.

    Composed from two of this story's own ports (`ArchiveCandidatePort`,
    `ArchiveTransitionPort`) plus the existing `Zone8ConsolidationStore`
    (DASH-STORY-018), `EventBus`/`Clock` (`dashanan.domain.ports`, reused
    unmodified). Two independent entry points mirror this story's two
    named paths (AC-008-ROT-2, AC-008-ROT-3):

      - `run_weekly_archive_sweep`: the periodic batch sweep that
        re-scores every candidate, transitions the eligible ones, and
        writes ALL of them into Zone 8 with exactly ONE `consolidate_
        batch` call (AC-008-ROT-3).
      - `fast_track_archive`: OAQ-12's single-item exception, called
        (by wiring out of this story's scope) immediately after an
        `Active -> Compressed` transition for an item with
        `payload_tokens < 64`, so it reaches `Archived` within the same
        sweep tick rather than waiting for the next weekly window
        (AC-008-ROT-2).
    """

    def __init__(
        self,
        transition_port: ArchiveTransitionPort,
        candidate_port: ArchiveCandidatePort,
        zone8_store: Zone8ConsolidationStore,
        event_bus: EventBus,
        clock: Clock,
        subject_index: Zone8SubjectIndexPort | None = None,
    ) -> None:
        """Compose this engine's ports.

        Args:
            transition_port: Persists a successful source-zone transition.
            candidate_port: Supplies each candidate's current, re-scored
                state (including, per DSHN-69, its `subject_id` when the
                source zone item carries one).
            zone8_store: Zone 8's object-store + hot-manifest Facade.
            event_bus: Publishes `memory.archived`.
            clock: The shared `Clock` every sweep timestamps itself from.
            subject_index: ADR-006's Zone 8 subject_id secondary index
                (DSHN-69 fix). When supplied, `run_weekly_archive_sweep`
                and `fast_track_archive` register every successfully
                archived item that carries a non-`None` `subject_id`
                (`ArchiveCandidateSnapshot.subject_id`) with `record_item`
                immediately after its Zone 8 write, so a later DPDP
                `erase_subject` call can resolve it. `None` (the default)
                preserves every existing caller composed before this fix
                and performs no subject-index registration -- a
                deployment composition root that wants weekly-sweep
                items reachable by subject-scoped erasure MUST supply a
                real `Zone8SubjectIndexPort` adapter here.
        """
        self._transition_port = transition_port
        self._candidate_port = candidate_port
        self._zone8_store = zone8_store
        self._event_bus = event_bus
        self._clock = clock
        self._subject_index = subject_index

    def run_weekly_archive_sweep(
        self,
        tenant_id: str,
        zone_id: ZoneId,
        candidate_item_ids: Sequence[str],
    ) -> ArchiveSweepResult:
        """Run one weekly archive-sweep tick for `zone_id` (Template Method, HLD Section 6).

        Steps (AC-008-ROT-1, AC-008-ROT-3, AC-008-ROT-4):

          1. Re-score each candidate via `ArchiveCandidatePort` (defends
             against acting on a value stale for any reason DASH-
             STORY-010's invalidation wiring has not yet covered --
             AC-008-ROT-4).
          2. Evaluate `dashanan.domain.archive_transition.
             is_archive_eligible` against the freshly re-scored
             `memory_score` (AC-008-ROT-1's `ArchiveThreshold` check).
          3. Evaluate the guarded transition (`dashanan.domain.
             archive_transition.guarded_archive_transition`) -- rejects
             a candidate no longer `Compressed`.
          4. Persist the transition via `ArchiveTransitionPort.
             mark_archived` for every eligible candidate.
          5. Collect every successfully marked candidate's payload into
             ONE `ArchiveBatchItem` list and call `Zone8ConsolidationStore.
             consolidate_batch` EXACTLY ONCE for the whole set
             (AC-008-ROT-3: "no individual per-item archive call outside
             the batch").
          6. Publish `memory.archived` (Observer, mirrors `dashanan.
             application.rotation_engine`'s identical convention) for
             every item Zone 8 durably wrote.

        Args:
            tenant_id: The tenant to sweep. Never blank.
            zone_id: The source zone to sweep -- must be one of
                `dashanan.domain.consolidated_blob.ALLOWED_SOURCE_ZONES`
                (AC-008-2), enforced transitively by `ArchiveBatchItem`'s
                own construction.
            candidate_item_ids: Every `Compressed` item this zone's
                weekly sweep should re-evaluate. Listing these
                candidates (a Compressed-item index scan) is out of this
                engine's scope -- the caller supplies them, mirroring
                `dashanan.application.rotation_engine.RotationEngine.
                run_sweep`'s own "the timer wheel supplies due entries,
                this method never scans a zone's full population" split,
                here delegated to the caller instead of an internal
                timer wheel (this story introduces no new timer-wheel
                instance; DASH-STORY-009's per-zone wheel already tracks
                `Active -> Compressed` deadlines only, per that module's
                own must-not-deviate item 2).

        Returns:
            An `ArchiveSweepResult` covering every id in
            `candidate_item_ids` across exactly one of `archived`/
            `deferred`/`skipped`/`failed`.

        Raises:
            ValueError: If `tenant_id` is blank.
        """
        if not tenant_id.strip():
            raise ValueError(
                "ArchiveEngine.run_weekly_archive_sweep requires a non-blank tenant_id"
            )

        as_of = self._clock.now()
        skipped: list[ArchiveSkip] = []
        failed: list[ArchiveFailure] = []
        marked: list[tuple[str, _MarkedCandidate]] = []

        for item_id in candidate_item_ids:
            outcome = self._evaluate_and_mark(tenant_id, zone_id, item_id)
            if isinstance(outcome, ArchiveSkip):
                skipped.append(outcome)
            elif isinstance(outcome, ArchiveFailure):
                failed.append(outcome)
            else:
                marked.append((item_id, outcome))

        if not marked:
            return ArchiveSweepResult(
                tenant_id=tenant_id,
                zone_id=zone_id,
                archived=(),
                deferred=(),
                skipped=tuple(skipped),
                failed=tuple(failed),
            )

        batch_items = [
            ArchiveBatchItem(
                tenant_id=tenant_id,
                item_id=item_id,
                source_zone=zone_id,
                payload=candidate.mark_result.payload,
                subject_id=candidate.subject_id,
            )
            for item_id, candidate in marked
        ]
        generation_by_item_id = {
            item_id: candidate.mark_result.generation for item_id, candidate in marked
        }
        subject_id_by_item_id = {item_id: candidate.subject_id for item_id, candidate in marked}

        batch_result = self._zone8_store.consolidate_batch(batch_items)

        archived: list[ArchiveTransitionOutcome] = []
        for written in batch_result.written:
            self._record_subject_index(
                tenant_id=tenant_id,
                item_id=written.item_id,
                subject_id=subject_id_by_item_id[written.item_id],
            )
            self._publish_archived(
                tenant_id=tenant_id,
                zone_id=zone_id,
                item_id=written.item_id,
                generation=generation_by_item_id[written.item_id],
            )
            archived.append(
                _to_archive_outcome(written, as_of, generation_by_item_id)
            )

        deferred = tuple(
            ArchiveSkip(item_id=d.item_id, reason=d.reason)
            for d in batch_result.deferred
        )
        for d in batch_result.rejected:
            failed.append(ArchiveFailure(item_id=d.item_id, error=d.reason))

        logger.info(
            "archive sweep completed",
            extra={
                "tenant_id": tenant_id,
                "zone": zone_id.value,
                "archived_count": len(archived),
                "deferred_count": len(deferred),
                "skipped_count": len(skipped),
                "failed_count": len(failed),
            },
        )

        return ArchiveSweepResult(
            tenant_id=tenant_id,
            zone_id=zone_id,
            archived=tuple(archived),
            deferred=deferred,
            skipped=tuple(skipped),
            failed=tuple(failed),
        )

    def fast_track_archive(
        self, tenant_id: str, zone_id: ZoneId, item_id: str
    ) -> ArchiveTransitionOutcome | ArchiveSkip | ArchiveFailure:
        """OAQ-12's sanctioned single-item archive call (AC-008-ROT-2, AC-008-ROT-3's exception).

        Called by wiring out of this story's scope, immediately after an
        `Active -> Compressed` transition, only when `dashanan.domain.
        archive_transition.is_oaq12_fast_track_eligible(payload_tokens)`
        was `True` for that item (must-not-deviate item 3: the caller's
        responsibility, not re-checked here beyond the shared
        re-score/guarded-transition steps `run_weekly_archive_sweep`
        also performs). Never call this for a candidate that has not
        already passed the fast-track gate -- it performs no fast-track
        eligibility check of its own, only the archive-eligibility
        re-score guard both entry points share.

        Args:
            tenant_id: The tenant this item belongs to. Never blank.
            zone_id: The item's source zone.
            item_id: The item to fast-track archive.

        Returns:
            An `ArchiveTransitionOutcome` on success, an `ArchiveSkip`
            if the candidate was not eligible or not `Compressed` at
            re-score time, or an `ArchiveFailure` if a port or the Zone 8
            write failed.

        Raises:
            ValueError: If `tenant_id` is blank.
        """
        if not tenant_id.strip():
            raise ValueError(
                "ArchiveEngine.fast_track_archive requires a non-blank tenant_id"
            )

        as_of = self._clock.now()
        outcome = self._evaluate_and_mark(tenant_id, zone_id, item_id)
        if isinstance(outcome, (ArchiveSkip, ArchiveFailure)):
            return outcome

        batch_result = self._zone8_store.consolidate_batch(
            [
                ArchiveBatchItem(
                    tenant_id=tenant_id,
                    item_id=item_id,
                    source_zone=zone_id,
                    payload=outcome.mark_result.payload,
                    subject_id=outcome.subject_id,
                )
            ]
        )

        if batch_result.deferred:
            return ArchiveSkip(item_id=item_id, reason=batch_result.deferred[0].reason)
        if batch_result.rejected:
            return ArchiveFailure(
                item_id=item_id, error=batch_result.rejected[0].reason
            )

        written = batch_result.written[0]
        self._record_subject_index(
            tenant_id=tenant_id, item_id=item_id, subject_id=outcome.subject_id
        )
        self._publish_archived(
            tenant_id=tenant_id,
            zone_id=zone_id,
            item_id=item_id,
            generation=outcome.mark_result.generation,
        )
        logger.info(
            "OAQ-12 fast-track archive completed",
            extra={
                "tenant_id": tenant_id,
                "zone": zone_id.value,
                "item_id": item_id,
                "generation": outcome.mark_result.generation,
            },
        )
        return ArchiveTransitionOutcome(
            item_id=item_id,
            archived_at=as_of,
            generation=outcome.mark_result.generation,
            manifest_blob_id=written.manifest_entry.blob_id,
        )

    def _evaluate_and_mark(
        self, tenant_id: str, zone_id: ZoneId, item_id: str
    ) -> _MarkedCandidate | ArchiveSkip | ArchiveFailure:
        """Re-score, check eligibility, guard-transition, and mark one candidate.

        The shared per-item pipeline both `run_weekly_archive_sweep` and
        `fast_track_archive` delegate to -- kept as a single method so
        the AC-008-ROT-1/AC-008-ROT-4 re-score-then-guard sequence has
        exactly one implementation, mirroring `dashanan.application.
        rotation_engine.RotationEngine.run_sweep`'s identical per-item
        pipeline shape (this class simply has two callers instead of one
        inline loop).
        """
        try:
            snapshot = self._candidate_port.current_state(tenant_id, zone_id, item_id)
        except ArchiveEnginePortError as exc:
            logger.error(
                "archive engine failed to re-score one candidate",
                extra={"tenant_id": tenant_id, "zone": zone_id.value, "item_id": item_id},
                exc_info=True,
            )
            return ArchiveFailure(item_id=item_id, error=str(exc))

        if not is_archive_eligible(snapshot.memory_score):
            return ArchiveSkip(
                item_id=item_id,
                reason="re-scored above ArchiveThreshold at sweep time",
            )

        try:
            guarded_archive_transition(snapshot.lifecycle_state, RotationState.ARCHIVED)
        except ArchiveTransitionError:
            return ArchiveSkip(
                item_id=item_id,
                reason=f"not eligible from state {snapshot.lifecycle_state.value}",
            )

        try:
            mark_result = self._transition_port.mark_archived(tenant_id, zone_id, item_id)
        except ArchiveEnginePortError as exc:
            logger.error(
                "archive engine failed to persist a transition",
                extra={"tenant_id": tenant_id, "zone": zone_id.value, "item_id": item_id},
                exc_info=True,
            )
            return ArchiveFailure(item_id=item_id, error=str(exc))

        return _MarkedCandidate(mark_result=mark_result, subject_id=snapshot.subject_id)

    def _record_subject_index(
        self, tenant_id: str, item_id: str, subject_id: str | None
    ) -> None:
        """Best-effort `Zone8SubjectIndexPort.record_item` call after a Zone 8 write.

        A no-op when this engine was composed without a `subject_index`
        (`None`, the default) or when `item_id` carries no `subject_id`
        (DSHN-70: not every archived item is subject-linkable). An index
        write failure is logged and swallowed rather than raised: the
        item's Zone 8 archive write already durably succeeded by the
        time this is called, so a subject-index outage must not undo or
        fail that already-completed archive outcome.
        """
        if self._subject_index is None or subject_id is None:
            return
        try:
            self._subject_index.record_item(tenant_id, subject_id, item_id)
        except Zone8CryptoShreddingStoreError:
            logger.error(
                "archive engine failed to register item in the Zone 8 "
                "subject index -- a later DPDP erase_subject call for "
                "this subject will not find this item",
                extra={"tenant_id": tenant_id, "item_id": item_id},
                exc_info=True,
            )

    def _publish_archived(
        self, tenant_id: str, zone_id: ZoneId, item_id: str, generation: int
    ) -> None:
        """Publish `memory.archived` (Observer, HLD Section 6) -- never a direct reactor call."""
        self._event_bus.publish(
            _ARCHIVED_EVENT_TYPE,
            {
                "tenant_id": tenant_id,
                "item_id": item_id,
                "zone": zone_id.value,
                "generation": generation,
            },
        )


def _to_archive_outcome(
    written: ArchiveWritten,
    as_of: datetime,
    generation_by_item_id: dict[str, int],
) -> ArchiveTransitionOutcome:
    """Build one `ArchiveTransitionOutcome` from a `Zone8ConsolidationStore` write result."""
    return ArchiveTransitionOutcome(
        item_id=written.item_id,
        archived_at=as_of,
        generation=generation_by_item_id[written.item_id],
        manifest_blob_id=written.manifest_entry.blob_id,
    )
