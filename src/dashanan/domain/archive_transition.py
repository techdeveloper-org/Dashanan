"""ArchiveTransition: the Compressed -> Archived guarded transition (DASH-STORY-019).

HLD Section 12A (Per-Zone Policy Table, Zone 8 row: HalfLife 270d, weekly
batch sweep) + Section 12B.2-12B.3 (ArchiveThreshold derivation) +
Section 12C (ADR-012, closed-form deadline, read-triggered promotion) +
Section 6 (State Machine, HLD's own three-state `Active -> Compressed ->
Archived` lifecycle), traced to FR-008 in SRS.md.

FR-008 (verbatim): "The system SHALL provide a Consolidation Memory zone
as the long-term, cross-session consolidated store that content reaches
only via the Archived state of the rotation state machine (FR-012)."

This module completes the `Compressed -> Archived` half of the lifecycle
`dashanan.domain.rotation_state` (DASH-STORY-009, Sprint 1) deliberately
left unreachable: that module's own docstring states `RotationState.
ARCHIVED` "is retained as an enum member solely so this module names the
full three-state lifecycle the HLD locks... it is never a transition
target anywhere in this codebase" -- true of DASH-STORY-009's Sprint 1
scope, and DASH-STORY-018 (Zone 8, Sprint 2) has since given Archived a
real destination. This module is the Sprint 2 place that finally makes
`RotationState.ARCHIVED` reachable.

FILE-DISJOINTNESS NOTE: `dashanan.domain.rotation_state`'s own
`_ALLOWED_TRANSITIONS` table is a closed, locked table this story does
NOT edit (that module is owned by DASH-STORY-009's own AR1 assignment,
and its docstring calls the closed table a structural guarantee).
Reopening `RotationState.ARCHIVED` as a reachable target therefore lives
in its OWN, separate guarded-transition table below -- `_ALLOWED_ARCHIVE_
TRANSITIONS` -- which is deliberately scoped to exactly one legal edge,
`COMPRESSED -> ARCHIVED`. This is the only table anywhere in the codebase
that names `RotationState.ARCHIVED` as an allowed target; `rotation_
state.py` itself is untouched and its own closed table remains correct
and unchanged. This mirrors `dashanan.application.rotation_deadline_
invalidation`'s own precedent (DASH-STORY-010): a later story extends a
locked Sprint-1 module's behavior via a new, additively-composed file,
never by editing the frozen module directly.

MUST-NOT-DEVIATE (sprint2_ar1_assignments.json AR1-S2-019, binding):
  1. "ArchiveThreshold is fixed at 0.25, not the research brief's
     provisional 0.15 (structurally unreachable per Section 12B.2)" --
     `ARCHIVE_THRESHOLD` below is a module-level constant, not a
     `RotationZonePolicy` field (that dataclass has no `archive_
     threshold` field by its own must-not-deviate item, F4 of the
     DASH-STORY-009 mathematics-engineer delegation) and not an
     operator-overridable value anywhere in this module -- there is no
     parameter, default, or code path in this module that could produce
     `0.15` instead of `0.25`.
  2. "An item SHALL NOT reach Archived without having passed through
     Compressed" -- `_ALLOWED_ARCHIVE_TRANSITIONS` has exactly one key,
     `RotationState.COMPRESSED`, mapping to exactly one legal target,
     `RotationState.ARCHIVED`. Every other `RotationState` (`ACTIVE`,
     and `ARCHIVED` itself) maps to the empty set via `guarded_archive_
     transition`'s `.get(current, frozenset())` default, so an attempt
     to archive an `Active` item (or re-archive an already-`Archived`
     one) is structurally rejected, not merely convention.
  3. "The OAQ-12 fast-track is gated strictly by payload_tokens < 64,
     not by score-collapse velocity" -- `is_oaq12_fast_track_eligible`
     takes exactly one parameter, `payload_tokens: int`, and computes
     its result from that parameter alone; there is no second parameter
     this function could accept for a rate-of-change/velocity signal.

PII NOTE: every value here is an abstract numeric input (a threshold
float, a token count, a `RotationState` enum member) -- never an item's
payload/fact content, mirroring `dashanan.domain.rotation_state`'s and
`dashanan.domain.rotation_deadline`'s identical PII posture.
"""

from __future__ import annotations

from dashanan.domain.exceptions import DashananError
from dashanan.domain.rotation_state import RotationState

ARCHIVE_THRESHOLD: float = 0.25
"""HLD Section 12B.2-12B.3's FINALIZED `ArchiveThreshold`, fixed (must-not-
deviate item 1). NEVER the research brief's provisional `0.15` -- Section
12B.2 shows `0.15` is structurally unreachable under the zone's decay
model, so `0.25` is the only value this module ever compares a re-scored
`MemoryScore` against."""

_THRESHOLD_GUARD_EPSILON = 1e-9
"""Float-rounding tolerance for `is_archive_eligible`'s `<=` comparison --
mirrors `dashanan.application.rotation_engine._SCORE_GUARD_EPSILON`'s
identical last-ULP rationale (same guard, same magnitude, applied to a
different threshold)."""

OAQ12_FAST_TRACK_TOKEN_LIMIT: int = 64
"""OAQ-12's fast-track gate: an item with `payload_tokens` strictly below
this limit is fast-tracked through `Compressed` and `Archived` within the
same sweep (AC-008-ROT-2). Fixed, never a `RotationZonePolicy` field or a
per-call override -- must-not-deviate item 3 gates fast-track eligibility
on this single scalar alone."""


class ArchiveTransitionError(DashananError):
    """Raised when a `Compressed -> Archived` transition is not permitted.

    Kept local to this module rather than added to the shared `dashanan.
    domain.exceptions` module -- mirrors `rotation_state.
    RotationTransitionError`'s identical file-disjointness rationale. The
    application-layer archive engine (`dashanan.application.
    archive_engine`) catches this specific type to treat "not eligible
    for archiving right now" (e.g. a candidate whose live state moved out
    of `Compressed` between listing and processing) as a normal, expected
    skip -- never a crash and never a reason to abort the rest of a
    sweep.
    """

    def __init__(self, current: RotationState, target: RotationState) -> None:
        self.current = current
        self.target = target
        super().__init__(
            f"RotationState archive transition {current.value} -> "
            f"{target.value} is not permitted"
        )


_ALLOWED_ARCHIVE_TRANSITIONS: dict[RotationState, frozenset[RotationState]] = {
    RotationState.COMPRESSED: frozenset({RotationState.ARCHIVED}),
}
"""The single source of truth for every legal archive transition
(must-not-deviate item 2). Deliberately holds exactly one entry: an item
must be `RotationState.COMPRESSED` to become `RotationState.ARCHIVED`.
`RotationState.ACTIVE` has no entry (falls through to the empty-set
default in `guarded_archive_transition`), so an `Active` item can never
skip `Compressed` and reach `Archived` directly through this table --
completing the same "structurally, not merely conventionally, closed"
guarantee `dashanan.domain.rotation_state._ALLOWED_TRANSITIONS` already
gives the `Active -> Compressed` edge."""


def guarded_archive_transition(
    current: RotationState, target: RotationState
) -> RotationState:
    """Validate and return `target` as the item's next `RotationState`.

    The sole authority for whether a `Compressed -> Archived` transition
    may occur (State pattern, HLD Section 6) -- mirrors `dashanan.domain.
    rotation_state.guarded_transition`'s identical pure-decision
    convention, scoped to this story's own additional edge. This
    function performs no I/O and mutates nothing; the caller (`dashanan.
    application.archive_engine`) persists the returned state via an
    `ArchiveTransitionPort` and then relays the archived item's payload
    to Zone 8's `Zone8ConsolidationStore`.

    Args:
        current: The item's `RotationState` immediately before this
            transition is attempted.
        target: The `RotationState` the caller wants to transition to.
            Every caller in this codebase passes `RotationState.ARCHIVED`
            here; the parameter exists (rather than a hardcoded target)
            purely to mirror `rotation_state.guarded_transition`'s own
            signature shape.

    Returns:
        `target`, unchanged, when the transition is legal.

    Raises:
        ArchiveTransitionError: If `target` is not in `current`'s
            allowed-transition set -- including every attempt to archive
            an item that is not currently `Compressed` (must-not-deviate
            item 2).
    """
    allowed = _ALLOWED_ARCHIVE_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise ArchiveTransitionError(current, target)
    return target


def is_archive_eligible(memory_score: float) -> bool:
    """AC-008-ROT-1: has `memory_score` crossed `ARCHIVE_THRESHOLD`?

    Mirrors `dashanan.application.rotation_engine.run_sweep`'s own
    re-score guard shape (`memory_score > threshold + epsilon` to skip),
    inverted here to a plain eligibility predicate the caller applies at
    weekly-sweep re-score time (AC-008-ROT-4): a re-scored item at or
    below `ARCHIVE_THRESHOLD` (within float-rounding tolerance) is
    eligible to archive; a re-scored item still above it stays
    `Compressed`, dwelling indefinitely per DASH-STORY-009's must-not-
    deviate item 5, bounded only by DASH-STORY-004's capacity/MaxAge
    backstop until it crosses.

    Args:
        memory_score: The item's CURRENT composite `MemoryScore.value`
            (`dashanan.domain.memory_score`), re-scored fresh at sweep
            time -- never a value cached from when the item became
            `Compressed` (AC-008-ROT-4's non-stale requirement).

    Returns:
        `True` if `memory_score <= ARCHIVE_THRESHOLD` (within
        `_THRESHOLD_GUARD_EPSILON`), `False` otherwise.
    """
    return memory_score <= ARCHIVE_THRESHOLD + _THRESHOLD_GUARD_EPSILON


def is_oaq12_fast_track_eligible(payload_tokens: int) -> bool:
    """AC-008-ROT-2 / OAQ-12: is this item small enough to fast-track?

    The ONLY signal this predicate ever reads is `payload_tokens` (must-
    not-deviate item 3) -- never a rate, a velocity, a score delta, or
    any other per-item time-series signal. A caller that wires this
    function into the `Active -> Compressed` sweep tick (DASH-STORY-009's
    `RotationEngine.run_sweep`, wiring out of this story's scope -- see
    the dev report's judgment-call list) uses a `True` result to
    immediately continue the same item through `Compressed -> Archived`
    within that same sweep, rather than waiting for the next weekly
    archive batch window (AC-008-ROT-3's named exception: "no individual
    per-item archive call outside the batch, other than the OAQ-12
    fast-track").

    Args:
        payload_tokens: The item's current token count. Never negative
            in practice (a caller supplies `dashanan.domain.memory_item`
            token-count semantics), but this function performs no range
            validation of its own -- it is a pure comparison, mirroring
            `is_archive_eligible`'s identical "no I/O, no validation,
            just the comparison HLD names" shape.

    Returns:
        `True` if `payload_tokens < OAQ12_FAST_TRACK_TOKEN_LIMIT`,
        `False` otherwise.
    """
    return payload_tokens < OAQ12_FAST_TRACK_TOKEN_LIMIT
