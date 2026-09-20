"""Zone 4 (Procedural) capacity cap and MaxAge forced-eviction backstop (HLD 12A/12F).

Traces to FR-004 in SRS.md. FR-004 (verbatim): "The system SHALL provide a
Procedural Memory zone holding learned task procedures, tool-use patterns,
and successful action sequences."

DASH-STORY-016 extends FR-004 with the same growth backstop HLD Section
12F already establishes as the sole Sprint-1-and-beyond guarantee against
unbounded zone growth while Zone 8 (Consolidation) is absent: "Items
dwell in `Compressed` indefinitely, bounded by the zone's capacity cap."
HLD Section 12A's per-zone policy table, Zone 4 row: `t_half = 120 days`,
`lambda_zone = 6.68e-8`, capacity cap `50,000 procedures`, MaxAge
`730 days`.

AC-004-CAP-1 (verbatim, sprint2_implementation_execution_plan.json
dev_prompt): "When Zone 4 reaches its capacity cap (50,000) or an item
exceeds MaxAge (730 days) in Compressed, the backstop sweep forcibly
evicts the lowest-ranked-by-MemoryScore Procedure(s), mirroring the Zone
2 backstop pattern."
AC-004-CAP-4 (verbatim): "Capacity cap and MaxAge are deployment
configuration; changing either takes effect on the next sweep without a
code change."

This module holds pure domain logic only: the `Zone4BackstopConfig` /
`ProcedureEvictionCandidate` / `EvictionDecision` Value Objects and the
pure selection functions (`select_capacity_overflow`,
`select_max_age_exceeded`, `plan_backstop_eviction`) that decide WHICH
Procedures a sweep must evict. Orchestration (loading config fresh per
sweep, fetching candidates, invoking the eviction/retrieval-index ports,
publishing `memory.evicted`) lives in `application.
zone4_capacity_backstop_sweep`, mirroring the `domain.
zone2_capacity_backstop` (pure types/functions) + `application.
zone2_capacity_backstop_sweep` (orchestration + logging) split
DASH-STORY-004 already established for Zone 2's own backstop -- the exact
precedent AC-004-CAP-1 names ("mirroring the Zone 2 backstop pattern").

This module deliberately mirrors `dashanan.domain.zone2_capacity_backstop`
structurally (same Value Object shapes, same pure selection functions, same
tie-break rule) rather than importing from it: Zone 4's candidate shape
(`ProcedureEvictionCandidate`, keyed by `task_signature_hash` and anchored
on `Procedure.last_used_at` rather than Zone 2's `Episode.written_at`) is
its own zone-scoped type (Interface Segregation, matching `EvictionCandidate`
itself being "deliberately decoupled from `dashanan.domain.episode.Episode`'s
concrete shape").

MUST-NOT-DEVIATE (dev_prompt, binding):
  1. "SemanticEdge and GeneralFact are the only two owned entity types for
     Zone 3" is Zone 3's own constraint (DASH-STORY-012), not Zone 4's;
     the analogous constraint for THIS story is that Zone 4 owns exactly
     one entity type, `Procedure` (HLD Section 3.5) -- this module never
     introduces a second Zone 4 owned-entity shape.
  2. "Do not specify or implement 'step-sequence generalization' as the
     Compressed-state transform" -- this module never reads or writes
     `Procedure.steps`; it ranks and selects candidates for REMOVAL only,
     never for compression, so the Compressed-state transform question
     never arises here.
  3. "Do not assume Zone 4 reaches Archived in this story" --
     `EvictionReason` below has exactly two members, `CAPACITY` and
     `MAX_AGE` (mirroring `zone2_capacity_backstop.EvictionReason`'s
     identical two-member shape); there is no "archived" outcome anywhere
     in this module.
  4. "Do not duplicate the generic deadline-scheduled sweep engine built
     in DASH-STORY-009" -- this module has no sweep loop, no timer wheel,
     and no `Active -> Compressed` transition logic at all; it is a
     distinct, additional backstop mechanism that operates on ANY current
     Zone 4 item regardless of rotation state, exactly as
     `zone2_capacity_backstop.py`'s own module docstring establishes for
     its zone ("This module holds pure domain logic only... Orchestration
     ... lives in `application.zone2_capacity_backstop_sweep`").
  5. Cap and MaxAge are deployment config, read fresh per sweep run, no
     code change (AC-004-CAP-4) -- enforced by this module NEVER
     hardcoding a cap/max_age literal; `Zone4BackstopConfig` is always an
     explicit, externally-supplied parameter to every function below.
  6. Eviction ordering is lowest-MemoryScore-first (AC-004-CAP-1's
     "forcibly evicts the lowest-ranked-by-MemoryScore Procedure(s)") --
     `plan_backstop_eviction`'s returned list is always sorted ascending
     by `_eviction_rank_key`.

PII NOTE: every field here is abstract ranking/config metadata -- an
`item_id` (`task_signature_hash`) string, a `[0,1]` MemoryScore float, a
boolean state flag, an age in seconds, a timestamp -- never a Procedure's
`steps` payload, mirroring `zone2_capacity_backstop.py`'s identical PII
posture for Zone 2's own payload.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from dashanan.domain.exceptions import DashananError

_SCORE_RANGE_EPSILON = 1e-9
"""Float-rounding tolerance for the `[0, 1]` MemoryScore range check below --
mirrors `dashanan.domain.zone2_capacity_backstop._SCORE_RANGE_EPSILON`'s
identical last-ULP accommodation rationale, not a relaxed invariant."""


class EvictionReason(str, Enum):
    """The two forced-eviction reasons this backstop can produce.

    Mirrors `dashanan.domain.zone2_capacity_backstop.EvictionReason`'s
    identical two-member shape (HLD Section 7.6's `memory.evicted` event
    contract: `reason ('ttl'|'capacity'|'max_age')`). `'ttl'` is Zone 1
    Working memory's own reason and is deliberately NOT a member here --
    this backstop never produces it (must-not-deviate item 3).
    """

    CAPACITY = "capacity"
    MAX_AGE = "max_age"


class Zone4CapacityBackstopError(DashananError):
    """Raised when a Zone 4 backstop Value Object is constructed with invalid data.

    Kept local to this module rather than added to the shared
    `dashanan.domain.exceptions` module -- mirrors `zone2_capacity_
    backstop.Zone2CapacityBackstopError`'s identical file-disjointness
    rationale (this story's own file-disjointness requirement keeps every
    new type this story introduces out of that shared file).
    """


@dataclass(frozen=True, slots=True)
class Zone4BackstopConfig:
    """The operator-configurable cap/MaxAge pair for Zone 4 (HLD Section 7.3, NFR-009).

    HLD Section 7.3: `/v1/zones/{zone}/config` (`GET`/`PUT`) exposes
    "capacity cap, max age" per zone. HLD Section 12A's Zone 4 row:
    "Capacity cap (per tenant): 50,000 procedures" / "MaxAge (forced
    archive): 730 days" -- this dataclass carries whatever value is
    currently configured, MVP default or operator override, with no
    branching between the two (AC-004-CAP-4's "operator-overridable
    without a code change" pattern, mirrored from `zone2_capacity_
    backstop.Zone2BackstopConfig`).

    Attributes:
        capacity_cap: Maximum item count Zone 4 may hold for one tenant
            before the backstop begins evicting. Must be positive.
        max_age_seconds: The MaxAge forced-eviction threshold, in
            seconds, for a Compressed Procedure (AC-004-CAP-1's "an item
            exceeds MaxAge... in Compressed"). Must be positive.
    """

    capacity_cap: int
    max_age_seconds: float

    def __post_init__(self) -> None:
        """Raises:
        Zone4CapacityBackstopError: If `capacity_cap` is not positive, or
            `max_age_seconds` is not positive -- a non-positive value
            here would make every sweep evict unconditionally, which is
            never what an operator-supplied config is meant to express.
        """
        if self.capacity_cap <= 0:
            raise Zone4CapacityBackstopError(
                f"Zone4BackstopConfig.capacity_cap must be positive, "
                f"got {self.capacity_cap}"
            )
        if self.max_age_seconds <= 0:
            raise Zone4CapacityBackstopError(
                f"Zone4BackstopConfig.max_age_seconds must be positive, "
                f"got {self.max_age_seconds}"
            )


@dataclass(frozen=True, slots=True)
class ProcedureEvictionCandidate:
    """One Zone 4 `Procedure`'s ranking inputs for one sweep run.

    Deliberately decoupled from `dashanan.domain.procedure.Procedure`'s
    concrete shape (Interface Segregation, mirroring `zone2_capacity_
    backstop.EvictionCandidate`'s identical decoupling from `Episode`):
    the application-layer sweep maps a `Procedure` to this narrower type,
    so this module stays reusable ranking logic that does not import, or
    know about, Zone 4's entity shape at all.

    Attributes:
        item_id: The Procedure's zone-scoped identifier
            (`Procedure.task_signature_hash`). Never blank.
        memory_score: The item's already-computed composite MemoryScore,
            in `[0, 1]`. Eviction selection ranks by this value ascending
            (must-not-deviate item 6).
        is_compressed: Whether the item is currently in the `Compressed`
            state (`ProcedureState.COMPRESSED`). Only a Compressed item is
            eligible for the MaxAge trigger (AC-004-CAP-1: "an item
            exceeds MaxAge WHILE Compressed").
        age_seconds: Seconds since the item's `last_used_at` (the closest
            structural equivalent Zone 4's own entity carries -- `Procedure`
            has no separate "created" timestamp). Never negative.
        last_used_at: The item's `Procedure.last_used_at` timestamp. Used
            only as the HLD Section 12G tie-break anchor when two
            candidates share an identical `memory_score` (see
            `_eviction_rank_key`), mirroring `zone2_capacity_backstop.
            EvictionCandidate.written_at`'s identical tie-break role for
            an entity with no `updated_at` field.
    """

    item_id: str
    memory_score: float
    is_compressed: bool
    age_seconds: float
    last_used_at: datetime

    def __post_init__(self) -> None:
        """Raises:
        Zone4CapacityBackstopError: If `item_id` is blank, `memory_score`
            is outside `[0, 1]` (epsilon-tolerant), or `age_seconds` is
            negative.
        """
        if not self.item_id.strip():
            raise Zone4CapacityBackstopError(
                "ProcedureEvictionCandidate.item_id must not be blank"
            )
        if not (
            -_SCORE_RANGE_EPSILON
            <= self.memory_score
            <= 1.0 + _SCORE_RANGE_EPSILON
        ):
            raise Zone4CapacityBackstopError(
                f"ProcedureEvictionCandidate.memory_score must be in [0, 1], "
                f"got {self.memory_score}"
            )
        if self.age_seconds < 0:
            raise Zone4CapacityBackstopError(
                f"ProcedureEvictionCandidate.age_seconds must be >= 0, "
                f"got {self.age_seconds}"
            )


@dataclass(frozen=True, slots=True)
class EvictionDecision:
    """One Procedure the backstop sweep has decided to forcibly evict, and why.

    Attributes:
        item_id: The item to evict (`ProcedureEvictionCandidate.item_id`).
        reason: Which trigger produced this decision -- carried straight
            into the `memory.evicted` event's `reason` field (HLD
            Section 7.6) by the application-layer sweep.
    """

    item_id: str
    reason: EvictionReason


def _eviction_rank_key(
    candidate: ProcedureEvictionCandidate,
) -> tuple[float, datetime, str]:
    """Ascending sort key: lowest `memory_score` evicted first.

    Implements HLD Section 12G's tie-break rule for equal/zero
    MemoryScore, applied here to Zone 4 eviction ordering exactly as
    `zone2_capacity_backstop._eviction_rank_key` applies it for Zone 2:
    "(1) most recent `updated_at` wins (is retained/promoted
    preferentially over the older item); (2) if `updated_at` is also
    tied, the item with the lowest `item_id`... is evicted/deprioritized
    first."

    `Procedure` has no `updated_at` field; this key uses `last_used_at` as
    the closest structural equivalent, mirroring `zone2_capacity_backstop.
    _eviction_rank_key`'s identical substitution of `Episode.written_at`
    for the same reason (a judgment call documented there and carried
    forward here for consistency across both zones' backstops).
    """
    return (candidate.memory_score, candidate.last_used_at, candidate.item_id)


def select_capacity_overflow(
    candidates: list[ProcedureEvictionCandidate], cap: int
) -> list[ProcedureEvictionCandidate]:
    """Select the lowest-scored items needed to bring the zone back under `cap`.

    AC-004-CAP-1's capacity trigger: "Zone 4 reaches its capacity cap."
    This is a zone-wide, state-agnostic trigger -- unlike `select_max_age_
    exceeded`, it is not restricted to Compressed items, since HLD Section
    12A's capacity cap ("50,000 procedures") is a total per-tenant item
    count, not a per-state count (mirrors `zone2_capacity_backstop.
    select_capacity_overflow`'s identical scope).

    Args:
        candidates: Every current Zone 4 item for one tenant.
        cap: `Zone4BackstopConfig.capacity_cap`.

    Returns:
        The `excess = max(0, len(candidates) - cap)` lowest-ranked
        candidates (must-not-deviate item 6 ordering, `_eviction_rank_
        key`), or an empty list when the zone is at or under `cap`.
    """
    excess = max(0, len(candidates) - cap)
    if excess == 0:
        return []
    ordered = sorted(candidates, key=_eviction_rank_key)
    return ordered[:excess]


def select_max_age_exceeded(
    candidates: list[ProcedureEvictionCandidate], max_age_seconds: float
) -> list[ProcedureEvictionCandidate]:
    """Select every Compressed item whose age exceeds `max_age_seconds`.

    AC-004-CAP-1's MaxAge trigger: "an item exceeds MaxAge WHILE
    Compressed." Every matching item is selected regardless of score --
    the hard backstop of last resort HLD Section 12F names as "the ONLY
    thing preventing unbounded growth" while Zone 8 is absent, mirrored
    unchanged from `zone2_capacity_backstop.select_max_age_exceeded`'s
    identical rationale for Zone 2.

    Args:
        candidates: Every current Zone 4 item for one tenant.
        max_age_seconds: `Zone4BackstopConfig.max_age_seconds`.

    Returns:
        Every `is_compressed` candidate with `age_seconds >
        max_age_seconds`, ordered ascending by `_eviction_rank_key`
        (must-not-deviate item 6) for consistent processing order.
    """
    matches = [
        c
        for c in candidates
        if c.is_compressed and c.age_seconds > max_age_seconds
    ]
    return sorted(matches, key=_eviction_rank_key)


def plan_backstop_eviction(
    candidates: list[ProcedureEvictionCandidate], config: Zone4BackstopConfig
) -> list[EvictionDecision]:
    """Combine both triggers into one ordered, deduplicated eviction plan.

    The single entry point the application-layer sweep calls per run
    (Template Method's "evaluate" step, mirroring `zone2_capacity_
    backstop.plan_backstop_eviction`). An item eligible under both
    triggers appears exactly once, tagged `MAX_AGE` -- the MaxAge
    backstop is the harder guarantee (HLD Section 12F), so it takes
    priority as the recorded reason when both apply to the same item.

    Args:
        candidates: Every current Zone 4 item for one tenant, already
            scored (`ProcedureEvictionCandidate.memory_score`) by the
            caller.
        config: The cap/MaxAge pair for this sweep run -- always freshly
            loaded by the caller (must-not-deviate item 5); this function
            never caches or defaults it.

    Returns:
        `EvictionDecision`s ordered lowest-`memory_score`-first
        (must-not-deviate item 6), covering the union of both triggers
        with no duplicate `item_id`.
    """
    reason_by_item: dict[str, EvictionReason] = {}
    by_item: dict[str, ProcedureEvictionCandidate] = {}

    for candidate in select_capacity_overflow(candidates, config.capacity_cap):
        reason_by_item[candidate.item_id] = EvictionReason.CAPACITY
        by_item[candidate.item_id] = candidate

    for candidate in select_max_age_exceeded(candidates, config.max_age_seconds):
        # MAX_AGE overrides CAPACITY when an item qualifies under both.
        reason_by_item[candidate.item_id] = EvictionReason.MAX_AGE
        by_item[candidate.item_id] = candidate

    ordered_candidates = sorted(by_item.values(), key=_eviction_rank_key)
    return [
        EvictionDecision(item_id=c.item_id, reason=reason_by_item[c.item_id])
        for c in ordered_candidates
    ]
