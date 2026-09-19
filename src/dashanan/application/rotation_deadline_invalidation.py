"""RotationDeadlineInvalidationService: ADR-012 deadline-invalidation wiring (DASH-STORY-010).

HLD Section 12C ("the one liability... must be wired into every
term-mutation path") + ADR-012, traced to FR-012 in SRS.md.

FR-012 (verbatim): "The system SHALL compute a composite Memory Score for
every item from six weighted terms (Recency, Frequency, Importance,
UserAffinity, TaskRelevance, ProvenanceConfidence), each normalized to
[0,1], and SHALL use this score to drive zone lifecycle transitions."

AC-012-INVAL-1 (verbatim, ar1_assignments.json AR1-010 dev_prompt): "Given a
non-Recency MemoryScore term (Frequency, ProvenanceConfidence, or
Importance) is mutated via access, conflict detection, or operator
override, when the mutation occurs, then the item's stored rotation
deadline is invalidated and re-inserted in O(log N) on all three mutation
paths, so no rotation decision is ever made against a stale deadline."

MUST-NOT-DEVIATE (ar1_assignments.json AR1-010, binding):
  1. "ALL THREE mutation paths must be wired, not a subset (HLD 12C: 'must
     be wired into every term-mutation path')" -- `on_access`,
     `on_conflict_detected` and `on_operator_override` below are this
     service's three public entry points, one per `MutatedTerm` member
     (`dashanan.domain.rotation_invalidation_trigger`), and the
     `_ENTRY_POINT_DESCRIPTION_BY_TERM` check below asserts at import time
     that the mapping is total over `MutatedTerm` -- adding a fourth term
     without a matching entry point fails at import, not silently at
     runtime.
  2. "O(log N) re-insert bound" -- every one of the three entry points
     below performs exactly one delegated call to `RotationEngine.
     schedule_or_pin` (DASH-STORY-009), whose own documented cost is "O(1)
     deadline computation... plus O(log N) timer-wheel insert/invalidate"
     -- this module adds no loop, no scan, and no additional heap
     operation of its own.
  3. "No rotation decision may ever act on a stale deadline
     (AC-012-INVAL-1)" -- `schedule_or_pin`'s own `RotationTimerWheel.
     schedule`/`invalidate` calls orphan (lazily invalidate) any prior live
     entry for `item_id` in the same call that inserts the fresh one
     (`dashanan.domain.rotation_timer_wheel`'s F6(b) lazy-deletion
     contract) -- there is no window between "old deadline still live" and
     "new deadline inserted" for a sweep to observe.
  4. "Do not re-implement the sweep itself -- that is DASH-STORY-009's
     scope, not yours" -- this module imports `RotationEngine` and calls
     only its existing public `schedule_or_pin` method; it does not
     import, call, or duplicate any logic from `RotationEngine.run_sweep`,
     `RotationTimerWheel.pop_due`, or `rotation_state.guarded_transition`.

WIRING NOTE (dev report, judgment-call list): the three call sites HLD
Section 12C names -- an access on the DASH-STORY-001 read path, a conflict
detection on the DASH-STORY-005/006 provenance path, and an operator
override on the DASH-STORY-008 scoring path -- do not yet exist as
concrete mutation code in this Sprint-1 snapshot (`MemoryOrchestrator`
only reads; `ProvenanceWriteGate` only gates first-time writes;
`MemoryScoreEngine` is a stateless, re-computed-fresh-every-call facade;
none of the three currently increments an access count, records a
conflict-driven confidence change, or applies an operator override tag).
This module is the complete, independently testable choke point HLD
Section 12C requires those future/present mutation call sites to route
through. Connecting an actual call site to the matching method below
requires editing `memory_orchestrator.py` / `provenance_write_gate.py` /
`memory_score_engine.py`, which this story's file-disjointness scope
excludes (those files are owned by DASH-STORY-001/006/008, not this
story) -- reported as `shared_file_request` entries in this story's dev
report rather than made silently.

PII NOTE: every parameter here is scheduling/score-recomputation metadata
-- a `MutatedTerm` enum member, a `ZoneId`, an `item_id` string, and the
same five scalar floats `RotationEngine.schedule_or_pin` already takes --
never the accessed item's content, the conflicting fact's text, or the
operator's override rationale (dev_prompt's PII constraint, mirrored from
`rotation_engine.py`'s identical posture).
"""

from __future__ import annotations

import logging

from dashanan.application.rotation_engine import (
    RotationEngine,
    RotationScheduleOutcome,
)
from dashanan.domain.rotation_invalidation_trigger import MutatedTerm
from dashanan.domain.rotation_zone_policy import RotationZonePolicy
from dashanan.domain.zone import ZoneId

logger = logging.getLogger(__name__)

_ENTRY_POINT_DESCRIPTION_BY_TERM: dict[MutatedTerm, str] = {
    MutatedTerm.FREQUENCY: "access",
    MutatedTerm.PROVENANCE_CONFIDENCE: "conflict detection",
    MutatedTerm.IMPORTANCE: "operator override",
}
"""HLD Section 12C's own three parenthetical examples, verbatim, keyed by
`MutatedTerm`. Checked below to be total over every `MutatedTerm` member
(must-not-deviate item 1: "structurally, not merely conventionally,
wired")."""

if frozenset(_ENTRY_POINT_DESCRIPTION_BY_TERM) != frozenset(MutatedTerm):
    raise AssertionError(
        "RotationDeadlineInvalidationService: _ENTRY_POINT_DESCRIPTION_BY_TERM "
        "must cover every MutatedTerm member (must-not-deviate: ALL THREE "
        "mutation paths must be wired, never a subset)"
    )


class RotationDeadlineInvalidationService:
    """The ADR-012 deadline-invalidation choke point for non-Recency term mutations.

    Composed from exactly one collaborator, the existing DASH-STORY-009
    `RotationEngine` -- this service adds no new port, no new timer wheel,
    and no new state; it is a thin, semantically-named Adapter (HLD
    Section 6's pattern vocabulary) in front of `RotationEngine.
    schedule_or_pin`, giving each of HLD Section 12C's three named
    mutation triggers its own intention-revealing call signature instead
    of one shared, anonymous "recompute" method three unrelated call sites
    would otherwise each have to remember to call identically.
    """

    def __init__(self, rotation_engine: RotationEngine) -> None:
        """Compose the service from the `RotationEngine` it wires into.

        Args:
            rotation_engine: The DASH-STORY-009 engine whose
                `schedule_or_pin` performs the actual O(log N)
                invalidate-and-reinsert this story is required to trigger.
        """
        self._rotation_engine = rotation_engine

    def on_access(
        self,
        zone_id: ZoneId,
        item_id: str,
        policy: RotationZonePolicy,
        w_recency: float,
        c_weighted: float,
        max_age_remaining_seconds: float,
        cooldown_remaining_seconds: float,
    ) -> RotationScheduleOutcome:
        """Frequency mutated via access (HLD Section 12C, AC-012-INVAL-1).

        Call this once Frequency's `access_count` has been incremented for
        `item_id` and `c_weighted` recomputed to reflect it (`dashanan.
        domain.rotation_deadline.weighted_non_recency_sum`) -- never
        before, since this call's whole purpose is to make the stored
        deadline consistent with the just-updated score inputs.

        Returns:
            The `RotationScheduleOutcome` `RotationEngine.schedule_or_pin`
            produces for the freshly recomputed inputs.
        """
        return self._invalidate_and_reschedule(
            MutatedTerm.FREQUENCY,
            zone_id,
            item_id,
            policy,
            w_recency,
            c_weighted,
            max_age_remaining_seconds,
            cooldown_remaining_seconds,
        )

    def on_conflict_detected(
        self,
        zone_id: ZoneId,
        item_id: str,
        policy: RotationZonePolicy,
        w_recency: float,
        c_weighted: float,
        max_age_remaining_seconds: float,
        cooldown_remaining_seconds: float,
    ) -> RotationScheduleOutcome:
        """ProvenanceConfidence mutated via conflict detection (HLD Section 12C, AC-012-INVAL-1).

        Call this once conflict detection (the DASH-STORY-005/006 Zone 7
        path) has produced a new `ProvenanceConfidence` value for
        `item_id` and `c_weighted` has been recomputed to reflect it --
        same ordering requirement as `on_access`.

        Returns:
            The `RotationScheduleOutcome` `RotationEngine.schedule_or_pin`
            produces for the freshly recomputed inputs.
        """
        return self._invalidate_and_reschedule(
            MutatedTerm.PROVENANCE_CONFIDENCE,
            zone_id,
            item_id,
            policy,
            w_recency,
            c_weighted,
            max_age_remaining_seconds,
            cooldown_remaining_seconds,
        )

    def on_operator_override(
        self,
        zone_id: ZoneId,
        item_id: str,
        policy: RotationZonePolicy,
        w_recency: float,
        c_weighted: float,
        max_age_remaining_seconds: float,
        cooldown_remaining_seconds: float,
    ) -> RotationScheduleOutcome:
        """Importance mutated via operator override (HLD Section 12C, AC-012-INVAL-1).

        Call this once an operator override has set a new explicit
        Importance tag (`dashanan.domain.memory_score.compute_importance`)
        for `item_id` and `c_weighted` has been recomputed to reflect it --
        same ordering requirement as `on_access`.

        Returns:
            The `RotationScheduleOutcome` `RotationEngine.schedule_or_pin`
            produces for the freshly recomputed inputs.
        """
        return self._invalidate_and_reschedule(
            MutatedTerm.IMPORTANCE,
            zone_id,
            item_id,
            policy,
            w_recency,
            c_weighted,
            max_age_remaining_seconds,
            cooldown_remaining_seconds,
        )

    def _invalidate_and_reschedule(
        self,
        mutated_term: MutatedTerm,
        zone_id: ZoneId,
        item_id: str,
        policy: RotationZonePolicy,
        w_recency: float,
        c_weighted: float,
        max_age_remaining_seconds: float,
        cooldown_remaining_seconds: float,
    ) -> RotationScheduleOutcome:
        """The single dispatch point every public entry point above routes through.

        `O(log N)`: delegates exactly once to `RotationEngine.
        schedule_or_pin` (must-not-deviate item 2) -- no loop, no scan, no
        independent heap access.
        """
        logger.debug(
            "rotation deadline invalidation triggered",
            extra={
                "mutated_term": mutated_term.value,
                "trigger": _ENTRY_POINT_DESCRIPTION_BY_TERM[mutated_term],
                "zone": zone_id.value,
                "item_id": item_id,
            },
        )
        return self._rotation_engine.schedule_or_pin(
            zone_id=zone_id,
            item_id=item_id,
            policy=policy,
            w_recency=w_recency,
            c_weighted=c_weighted,
            max_age_remaining_seconds=max_age_remaining_seconds,
            cooldown_remaining_seconds=cooldown_remaining_seconds,
        )
