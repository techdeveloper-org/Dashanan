"""RotationState: the guarded lifecycle state machine (HLD Section 6, ADR-012).

Traces to FR-012 in SRS.md. HLD Section 6, "MemoryItem lifecycle" row:
"The locked `Active -> Compressed -> Archived` lifecycle with its ordering
guarantee is a state machine, and the Phase 0 review's own bug (a
fast-decaying item skipping compression) was caused by treating it as
score bands instead of states. Explicit states with guarded transitions
make the skip structurally impossible."

MUST-NOT-DEVIATE (ar1_assignments.json AR1-009, binding):
  1. "Guarded state machine -- Archived must be structurally unreachable
     without passing through Compressed (AC-012)" -- `_ALLOWED_
     TRANSITIONS` below is the single source of truth for every legal
     transition this module permits, and it is closed over: no entry,
     anywhere, ever names `RotationState.ARCHIVED` as an allowed target.
     A transition into `ARCHIVED` is therefore not merely undocumented,
     it is structurally absent from the table `guarded_transition` reads
     -- there is no code path in this module capable of producing it.
  2. "No code path may attempt an Archived transition in Sprint 1; Zone 8
     does not exist (AC-012-ROT-1, HLD 12F)" -- consequence of (1): this
     story implements `Active -> Compressed` only. `RotationState.
     ARCHIVED` is retained as an enum member solely so this module names
     the full three-state lifecycle the HLD locks (matching
     `dashanan.domain.episode.EpisodeState`'s identical three-member
     shape for Zone 2's own state field) -- it is never a transition
     target anywhere in this codebase.

This module is generic across every zone the Rotation Engine will ever
serve (HLD Section 6, "Per-zone rotation policy" row: "Strategy lets the
single RotationEngine execute eight policies without eight code paths"),
so `RotationState` is deliberately decoupled from `dashanan.domain.
episode.EpisodeState` -- Zone 2's own state field -- exactly as
`zone2_capacity_backstop.EvictionCandidate` is deliberately decoupled from
`Episode` (Interface Segregation). The two enums share member values
(`"active"`, `"compressed"`, `"archived"`) so a zone adapter's mapping
between its own state field and this module's `RotationState` is a
same-string, structurally-obvious conversion, never a lookup table.

PII NOTE: this module carries only a state-machine enum and a pure guard
function -- no item content, ever (dev_prompt's PII constraint: "the
state-machine and scheduling logic never reads the item body").
"""

from __future__ import annotations

from enum import Enum

from dashanan.domain.exceptions import DashananError


class RotationState(str, Enum):
    """The three states of the locked `Active -> Compressed -> Archived` lifecycle.

    Names and values taken directly from HLD Section 0 / the Phase 0
    locked rotation state machine, mirroring `dashanan.domain.episode.
    EpisodeState`'s identical naming (HLD Section 6, "MemoryItem
    lifecycle" row is a single, zone-agnostic state machine definition;
    each zone's own entity type is free to carry its own state field with
    the same values).
    """

    ACTIVE = "active"
    COMPRESSED = "compressed"
    ARCHIVED = "archived"


_ALLOWED_TRANSITIONS: dict[RotationState, frozenset[RotationState]] = {
    RotationState.ACTIVE: frozenset({RotationState.COMPRESSED}),
    RotationState.COMPRESSED: frozenset(),
}
"""The single source of truth for every legal transition (must-not-deviate
item 1). `RotationState.ARCHIVED` never appears as a value in any entry,
and has no key of its own -- both are deliberate: Sprint 1 implements
`Active -> Compressed` and stops there (HLD Section 12F), so there is no
transition, from any state, that this table could ever authorize into
`ARCHIVED`. `COMPRESSED` maps to an explicit empty set (not an absent key)
to make plain that a Compressed item has zero outbound transitions in
Sprint 1 -- it "dwells... indefinitely, bounded only by DASH-STORY-004"
(must-not-deviate item 5), which is a capacity-driven REMOVAL, never a
state-machine TRANSITION, so it is correctly outside this table entirely.
"""


class RotationTransitionError(DashananError):
    """Raised when a transition is not present in `_ALLOWED_TRANSITIONS`.

    Kept local to this module rather than added to the shared
    `dashanan.domain.exceptions` module -- mirrors `zone2_capacity_
    backstop.Zone2CapacityBackstopError`'s identical file-disjointness
    rationale. The application-layer sweep (`dashanan.application.
    rotation_engine`) catches this specific type to treat "not eligible
    for this transition right now" (e.g. an item popped from the timer
    wheel that a concurrent read-triggered promotion already moved out of
    `Active`) as a normal, expected skip -- never a crash and never a
    reason to abort the rest of the sweep.
    """

    def __init__(self, current: RotationState, target: RotationState) -> None:
        self.current = current
        self.target = target
        super().__init__(
            f"RotationState transition {current.value} -> {target.value} "
            "is not permitted"
        )


def guarded_transition(
    current: RotationState, target: RotationState
) -> RotationState:
    """Validate and return `target` as the item's next `RotationState`.

    The sole authority for whether a lifecycle transition may occur
    (State pattern, HLD Section 6). This function performs no I/O and
    mutates nothing -- the caller (`application.rotation_engine`) is
    responsible for persisting the returned state via a
    `RotationTransitionPort`; this function only answers "is this
    transition legal," matching every other domain module's pure-decision
    convention (`zone2_capacity_backstop.plan_backstop_eviction`,
    `retrieval_fusion`).

    Args:
        current: The item's `RotationState` immediately before this
            transition is attempted.
        target: The `RotationState` the caller wants to transition to.

    Returns:
        `target`, unchanged, when the transition is legal.

    Raises:
        RotationTransitionError: If `target` is not in `current`'s
            allowed-transition set -- including every attempt to reach
            `RotationState.ARCHIVED` from any state, which is always
            rejected (must-not-deviate items 1 and 2).
    """
    allowed = _ALLOWED_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise RotationTransitionError(current, target)
    return target
