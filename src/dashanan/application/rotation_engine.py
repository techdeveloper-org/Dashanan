"""RotationEngine: Active -> Compressed rotation orchestration (DASH-STORY-009).

HLD Rotation Engine (Section 5, "Rotation Engine" row) + ADR-012 (deadline
scheduling plus read-triggered promotion) + HLD Section 6 (State Machine,
Strategy, Observer/Event Bus, Template Method), traced to FR-012 in
SRS.md.

FR-012 (verbatim): "The system SHALL compute a composite Memory Score for
every item from six weighted terms (Recency, Frequency, Importance,
UserAffinity, TaskRelevance, ProvenanceConfidence), each normalized to
[0,1], and SHALL use this score to drive zone lifecycle transitions."

AC-012 (verbatim, ar1_assignments.json AR1-009 dev_prompt): "An item
crossing CompressThreshold (not ArchiveThreshold) transitions to
Compressed and stays retrievable; it SHALL NOT reach Archived without
passing through Compressed."
AC-012-ROT-1 (verbatim): "Zone 8 out of scope: an item that would cross
ArchiveThreshold instead stays in Compressed indefinitely -- no Archived
attempt with no destination."

This module holds the orchestration `dashanan.domain.rotation_deadline`,
`rotation_state` and `rotation_timer_wheel` deliberately leave out: Clock
injection, the two ports a concrete zone adapter implements, structured
logging, and the `memory.compressed` event publication -- mirroring the
`domain/write_gate.py` (pure types/functions) + `application/
provenance_write_gate.py` (orchestration + logging) split DASH-STORY-006
already established, and `domain/zone2_capacity_backstop.py` +
`application/zone2_capacity_backstop_sweep.py`'s identical split for
DASH-STORY-004. The three pure domain modules are the single source of
WHAT is mathematically/structurally correct; this module is the single
place that decides HOW each decision is carried out and observed.

MUST-NOT-DEVIATE (ar1_assignments.json AR1-009, binding):
  1. "Guarded state machine -- Archived structurally UNREACHABLE without
     passing through Compressed (AC-012)" -- `_execute_transition` below
     calls `rotation_state.guarded_transition(current, RotationState.
     COMPRESSED)` and nowhere else in this module names
     `RotationState.ARCHIVED` at all.
  2. "No Archived transition attempt in Sprint 1; Zone 8 does not exist
     (AC-012-ROT-1, HLD 12F)" -- consequence of (1) plus `RotationZone
     Policy` having no `archive_threshold` field (`rotation_zone_policy.
     py`'s own must-not-deviate note): there is no value anywhere in this
     module's call graph an archive deadline could even be computed from.
  3. "O(k log N) sweep, never an O(N) scan" -- `run_sweep` calls
     `RotationTimerWheel.pop_due` exactly once per invocation and does
     O(1) (port-call-bounded) work per popped item; it never iterates a
     zone's full item population.
  4. "Publish zone-transition events -- do not call reactors directly
     (Observer, HLD Section 6)" -- every successful transition publishes
     `memory.compressed` via the injected `EventBus` port
     (`dashanan.domain.ports.EventBus`); this module holds no reference
     to, and never imports, any concrete reactor (index maintenance,
     provenance relay, metrics).
  5. "Compressed dwell is indefinite, bounded only by DASH-STORY-004's
     backstop" -- `_execute_transition` never re-schedules a popped item
     after a successful `Compressed` transition; the item simply leaves
     the timer wheel (`RotationTimerWheel.pop_due` already removed its
     live entry) and is never re-inserted by this module.

PII NOTE: every port and payload here carries only scheduling and
state-machine control metadata -- `item_id`, `tenant_id`, `ZoneId`,
`RotationState`, a `MemoryScore` float, timestamps -- never an item's
payload/fact content (dev_prompt's PII constraint, mirrored from
`zone2_capacity_backstop_sweep.py`'s identical posture: "This module never
reads or logs a DPDP subject's erased content").
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from dashanan.domain.exceptions import DashananError
from dashanan.domain.ports import Clock, EventBus
from dashanan.domain.rotation_deadline import compute_rotation_deadline
from dashanan.domain.rotation_state import (
    RotationState,
    RotationTransitionError,
    guarded_transition,
)
from dashanan.domain.rotation_timer_wheel import RotationTimerWheel
from dashanan.domain.rotation_zone_policy import RotationZonePolicy
from dashanan.domain.zone import ZoneId

logger = logging.getLogger(__name__)

_COMPRESSED_EVENT_TYPE = "memory.compressed"
"""HLD Section 7.6's exact event name: `memory.compressed | rotation-worker
| index-worker, provenance-relay | tenant:item | item_id, zone, generation,
original_tokens, compressed_tokens`."""

_SCORE_GUARD_EPSILON = 1e-9
"""Float-rounding tolerance for the re-score guard's `<= theta` comparison
below -- mirrors `dashanan.domain.memory_score._TERM_RANGE_EPSILON`'s
identical last-ULP rationale, not a relaxation of the guard itself."""


class RotationEnginePortError(DashananError):
    """Raised by one of this module's ports on an expected, operational failure.

    Kept local to this module rather than added to the shared
    `dashanan.domain.exceptions` module -- mirrors `zone2_capacity_
    backstop_sweep.Zone2BackstopPortError`'s identical file-disjointness
    and fail-safe-isolation rationale: a port raising this type is an
    operational failure isolated to the one item being processed, so one
    unreachable zone adapter cannot halt the rest of a sweep tick. A port
    raising anything else is treated as a programming bug and propagates
    immediately (error-handling-patterns section 2, fail-fast).
    """


@runtime_checkable
class RotationCandidatePort(Protocol):
    """Supplies one item's current lifecycle state and MemoryScore for the sweep's re-score step.

    HLD Section 6's Template Method sweep skeleton names "re-score" as an
    explicit step between "pop due items" and "evaluate guarded
    transition." This port is that step's data source: a concrete zone
    adapter (e.g. Zone 2 Episodic) is responsible for producing the item's
    CURRENT state and score at sweep time, not the value it had when the
    deadline was originally scheduled -- this is what lets `run_sweep`
    defensively reject a transition attempt against a deadline that is
    stale for any reason DASH-STORY-010's invalidation wiring has not yet
    covered, rather than silently acting on it (HLD Section 12C's
    explicitly named "silent correctness bug, not a crash" liability).
    """

    def current_state(
        self, tenant_id: str, zone_id: ZoneId, item_id: str
    ) -> RotationCandidateSnapshot:
        """Return `item_id`'s current lifecycle state and MemoryScore.

        Raises:
            RotationEnginePortError: If the item's current state cannot
                be read (e.g. the zone's repository is unreachable, or
                the item no longer exists -- both are operational, not
                programming, failures for this port).
        """
        ...


@runtime_checkable
class RotationTransitionPort(Protocol):
    """Persists a successful `Active -> Compressed` transition in the owning zone.

    Deliberately narrow (Interface Segregation): this port's only
    responsibility is durably recording the new `RotationState` and
    returning the metadata HLD Section 7.6's `memory.compressed` event
    payload needs. The compression METHOD itself (HLD Section 12A: "LLM
    summarization" for Zone 2) is a separate concern this story does not
    implement -- see this story's dev report, judgment-call list.
    """

    def compress(
        self, tenant_id: str, zone_id: ZoneId, item_id: str
    ) -> RotationCompressionResult:
        """Durably transition `item_id` to `RotationState.COMPRESSED`.

        Raises:
            RotationEnginePortError: If the transition cannot be
                persisted (e.g. the zone's repository is unreachable).
        """
        ...


@dataclass(frozen=True, slots=True)
class RotationCandidateSnapshot:
    """One item's current lifecycle state and score, as of the sweep's re-score step.

    Attributes:
        lifecycle_state: The item's `RotationState` at the instant this
            snapshot was produced -- NOT necessarily `ACTIVE`; a
            concurrent read-triggered promotion (ADR-012 mechanism b) may
            already have moved it, which `guarded_transition` then
            correctly rejects.
        memory_score: The item's current composite `MemoryScore.value`
            (`dashanan.domain.memory_score`), in `[0, 1]`.
    """

    lifecycle_state: RotationState
    memory_score: float

    def __post_init__(self) -> None:
        """Raises:
        ValueError: If `memory_score` is outside `[0, 1]`.
        """
        if not (
            -_SCORE_GUARD_EPSILON
            <= self.memory_score
            <= 1.0 + _SCORE_GUARD_EPSILON
        ):
            raise ValueError(
                f"RotationCandidateSnapshot.memory_score must be in [0, 1], "
                f"got {self.memory_score}"
            )


@dataclass(frozen=True, slots=True)
class RotationCompressionResult:
    """Metadata `RotationTransitionPort.compress` returns for the `memory.compressed` event.

    Attributes:
        generation: The item's compression generation counter (HLD
            Section 7.6 payload field), starting at `1` for an item's
            first compression.
        original_tokens: The item's `token_count` before compression.
        compressed_tokens: The item's `token_count` after compression.
    """

    generation: int
    original_tokens: int
    compressed_tokens: int


@dataclass(frozen=True, slots=True)
class RotationScheduleOutcome:
    """The result of one `RotationEngine.schedule_or_pin` call.

    Attributes:
        item_id: The item this call scheduled or pinned.
        scheduled: `True` if a deadline was inserted into the zone's
            timer wheel, `False` if the item was pinned (mathematics-
            engineer delegation F5: "PINNED is a first-class path, not an
            error" -- `False` is not a failure outcome).
        due_at: The instant the item was scheduled to become due, or
            `None` when `scheduled` is `False`.
    """

    item_id: str
    scheduled: bool
    due_at: datetime | None


@dataclass(frozen=True, slots=True)
class RotationTransitionOutcome:
    """One item this sweep successfully transitioned to `Compressed`.

    Attributes:
        item_id: The transitioned item's identifier.
        compressed_at: The sweep's `as_of` instant.
        generation: `RotationCompressionResult.generation`.
    """

    item_id: str
    compressed_at: datetime
    generation: int


@dataclass(frozen=True, slots=True)
class RotationSkip:
    """One due item this sweep evaluated but did NOT transition, and why.

    Neither a failure nor an error -- the re-score guard (a stale
    deadline whose item no longer qualifies) and the guarded-transition
    check (an item no longer `Active`) both produce this outcome as their
    normal, expected result (mathematics-engineer delegation F5's "hot
    path, not an edge case" framing generalizes to every defensive
    no-op this sweep can reach).

    Attributes:
        item_id: The item this sweep did not transition.
        reason: A short, human-readable, PII-free explanation.
    """

    item_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class RotationFailure:
    """One due item this sweep attempted to process but could not, due to a port error.

    A Result-type field (HLD Section 6, "Degraded responses" row) rather
    than a raised exception propagating out of `run_sweep` -- mirrors
    `zone2_capacity_backstop_sweep.EvictionFailure`'s identical rationale:
    one item's operational failure must not abort every other due item's
    processing in the same tick.

    Attributes:
        item_id: The item this sweep failed to process.
        error: `str(exc)` from the `RotationEnginePortError` that was
            raised. Ports are contractually responsible for keeping this
            message PII-free.
    """

    item_id: str
    error: str


@dataclass(frozen=True, slots=True)
class RotationSweepResult:
    """The outcome of one `RotationEngine.run_sweep` call.

    Attributes:
        tenant_id: The tenant this sweep ran for.
        zone_id: The zone this sweep ran for.
        transitioned: Every item this run moved to `Compressed`.
        skipped: Every due item this run evaluated but did not
            transition (see `RotationSkip`).
        failed: Every due item this run could not process due to a port
            error (`RotationEnginePortError` only).
    """

    tenant_id: str
    zone_id: ZoneId
    transitioned: tuple[RotationTransitionOutcome, ...]
    skipped: tuple[RotationSkip, ...]
    failed: tuple[RotationFailure, ...]


class RotationEngine:
    """The Rotation Engine: `Active -> Compressed` scheduling and sweep, per zone.

    Composed from two ports (`RotationCandidatePort`,
    `RotationTransitionPort`) plus the existing `EventBus`/`Clock`
    (`dashanan.domain.ports`, reused unmodified). Owns one
    `RotationTimerWheel` per `ZoneId` it is asked to serve (HLD Section 6,
    "Per-zone rotation policy" row: Strategy lets one engine execute
    every zone's policy without a separate code path per zone).

    Two independent entry points mirror ADR-012's two mechanisms:
      - `schedule_or_pin`: mechanism (a), deadline scheduling -- called
        at write/access time (the actual wiring of every write/access
        call site into this method is DASH-STORY-010's scope, not this
        story's; this engine supplies the entry point that wiring calls).
      - `run_sweep`: the periodic sweep that pops due deadlines and
        drives the guarded transition (Template Method, HLD Section 6).
    """

    def __init__(
        self,
        transition_port: RotationTransitionPort,
        candidate_port: RotationCandidatePort,
        event_bus: EventBus,
        clock: Clock,
    ) -> None:
        self._transition_port = transition_port
        self._candidate_port = candidate_port
        self._event_bus = event_bus
        self._clock = clock
        self._wheels: dict[ZoneId, RotationTimerWheel] = {}

    def _wheel_for(self, zone_id: ZoneId) -> RotationTimerWheel:
        """Return this engine's `RotationTimerWheel` for `zone_id`, creating it on first use."""
        wheel = self._wheels.get(zone_id)
        if wheel is None:
            wheel = RotationTimerWheel()
            self._wheels[zone_id] = wheel
        return wheel

    def schedule_or_pin(
        self,
        zone_id: ZoneId,
        item_id: str,
        policy: RotationZonePolicy,
        w_recency: float,
        c_weighted: float,
        max_age_remaining_seconds: float,
        cooldown_remaining_seconds: float,
    ) -> RotationScheduleOutcome:
        """Compute and apply this item's next rotation deadline (ADR-012 mechanism a).

        `O(1)` deadline computation (`compute_rotation_deadline`) plus
        `O(log N)` timer-wheel insert/invalidate (`RotationTimerWheel`)
        -- HLD Section 12C's complexity table for this operation.

        Args:
            zone_id: The item's owning zone.
            item_id: The item to (re)schedule.
            policy: The zone's `RotationZonePolicy` (`compress_threshold`
                is the ONLY threshold ever passed to `compute_rotation_
                deadline` here -- F4's Sprint 1 scope restriction).
            w_recency: `ScoreWeights.w_recency` for this item's currently
                effective weight profile.
            c_weighted: `dashanan.domain.rotation_deadline.weighted_non_
                recency_sum`'s output for this item's current score
                terms.
            max_age_remaining_seconds: Seconds until the zone's MaxAge
                backstop fires for this item.
            cooldown_remaining_seconds: Seconds left on this item's HLD
                Section 12A state-change cooldown, `0` if none.

        Returns:
            A `RotationScheduleOutcome` recording whether a deadline was
            scheduled or the item was pinned.

        Raises:
            ValueError: Propagated from `compute_rotation_deadline` or
                `RotationTimerWheel.schedule`.
        """
        delta_t = compute_rotation_deadline(
            lambda_zone=policy.lambda_zone,
            theta=policy.compress_threshold,
            w_recency=w_recency,
            c_weighted=c_weighted,
            max_age_remaining_s=max_age_remaining_seconds,
            cooldown_remaining_s=cooldown_remaining_seconds,
        )
        wheel = self._wheel_for(zone_id)
        if delta_t is None:
            wheel.invalidate(item_id)
            logger.debug(
                "rotation engine pinned item -- not scheduled",
                extra={"zone": zone_id.value, "item_id": item_id},
            )
            return RotationScheduleOutcome(
                item_id=item_id, scheduled=False, due_at=None
            )

        due_at = self._clock.now() + timedelta(seconds=delta_t)
        wheel.schedule(item_id, due_at)
        logger.debug(
            "rotation engine scheduled item",
            extra={
                "zone": zone_id.value,
                "item_id": item_id,
                "due_at": due_at.isoformat(),
            },
        )
        return RotationScheduleOutcome(
            item_id=item_id, scheduled=True, due_at=due_at
        )

    def run_sweep(
        self, tenant_id: str, zone_id: ZoneId, policy: RotationZonePolicy
    ) -> RotationSweepResult:
        """Run one rotation sweep tick for `zone_id` (Template Method, HLD Section 6).

        Steps, matching the sweep skeleton HLD Section 6 names for
        rotation sweeps generally ("pop due items -> re-score -> evaluate
        guarded transition -> publish -> reschedule"):

          1. Pop every due entry from `zone_id`'s `RotationTimerWheel`
             (must-not-deviate item 3: `O(k log N)`, never an `O(N)`
             scan).
          2. Re-score each popped item via `RotationCandidatePort`
             (defends against acting on a deadline stale for a reason
             DASH-STORY-010's invalidation wiring has not yet covered).
          3. Evaluate the guarded transition (`rotation_state.
             guarded_transition`) -- rejects an item no longer `Active`.
          4. Persist the transition via `RotationTransitionPort` and
             publish `memory.compressed` (must-not-deviate item 4:
             Observer, never a direct reactor call).

        "Reschedule" (the skeleton's fifth step) is deliberately absent
        for a successful transition: a `Compressed` item is never
        re-inserted into the timer wheel in Sprint 1 (must-not-deviate
        item 5, F4's `CompressThreshold`-only scope) -- it dwells until
        DASH-STORY-004's capacity/MaxAge backstop removes it.

        Args:
            tenant_id: The tenant to sweep. Never blank.
            zone_id: The zone to sweep.
            policy: The zone's current `RotationZonePolicy`.

        Returns:
            A `RotationSweepResult` covering every due entry this tick
            popped -- `transitioned`, `skipped` and `failed` together
            always account for every popped `ScheduledRotation`.

        Raises:
            ValueError: If `tenant_id` is blank.
        """
        if not tenant_id.strip():
            raise ValueError("RotationEngine.run_sweep requires a non-blank tenant_id")

        as_of = self._clock.now()
        wheel = self._wheel_for(zone_id)
        due_entries = wheel.pop_due(as_of)

        transitioned: list[RotationTransitionOutcome] = []
        skipped: list[RotationSkip] = []
        failed: list[RotationFailure] = []

        for entry in due_entries:
            try:
                snapshot = self._candidate_port.current_state(
                    tenant_id, zone_id, entry.item_id
                )
            except RotationEnginePortError as exc:
                logger.error(
                    "rotation sweep failed to re-score one item",
                    extra={
                        "tenant_id": tenant_id,
                        "zone": zone_id.value,
                        "item_id": entry.item_id,
                    },
                    exc_info=True,
                )
                failed.append(RotationFailure(item_id=entry.item_id, error=str(exc)))
                continue

            if snapshot.memory_score > policy.compress_threshold + _SCORE_GUARD_EPSILON:
                skipped.append(
                    RotationSkip(
                        item_id=entry.item_id,
                        reason="re-scored above CompressThreshold at sweep time",
                    )
                )
                continue

            try:
                guarded_transition(snapshot.lifecycle_state, RotationState.COMPRESSED)
            except RotationTransitionError:
                skipped.append(
                    RotationSkip(
                        item_id=entry.item_id,
                        reason=(
                            f"not eligible from state "
                            f"{snapshot.lifecycle_state.value}"
                        ),
                    )
                )
                continue

            try:
                result = self._transition_port.compress(
                    tenant_id, zone_id, entry.item_id
                )
            except RotationEnginePortError as exc:
                logger.error(
                    "rotation sweep failed to persist a transition",
                    extra={
                        "tenant_id": tenant_id,
                        "zone": zone_id.value,
                        "item_id": entry.item_id,
                    },
                    exc_info=True,
                )
                failed.append(RotationFailure(item_id=entry.item_id, error=str(exc)))
                continue

            self._event_bus.publish(
                _COMPRESSED_EVENT_TYPE,
                {
                    "tenant_id": tenant_id,
                    "item_id": entry.item_id,
                    "zone": zone_id.value,
                    "generation": result.generation,
                    "original_tokens": result.original_tokens,
                    "compressed_tokens": result.compressed_tokens,
                },
            )
            logger.info(
                "rotation sweep transitioned item to Compressed",
                extra={
                    "tenant_id": tenant_id,
                    "zone": zone_id.value,
                    "item_id": entry.item_id,
                    "generation": result.generation,
                },
            )
            transitioned.append(
                RotationTransitionOutcome(
                    item_id=entry.item_id,
                    compressed_at=as_of,
                    generation=result.generation,
                )
            )

        return RotationSweepResult(
            tenant_id=tenant_id,
            zone_id=zone_id,
            transitioned=tuple(transitioned),
            skipped=tuple(skipped),
            failed=tuple(failed),
        )
