"""Zone 2 capacity cap and MaxAge forced-archival backstop (HLD 12A/12F, OAQ-4).

Traces to FR-002 in SRS.md. FR-002 (verbatim): "The system SHALL provide an
Episodic Memory zone that stores chronological interaction records with
recency-based decay, and SHALL retrieve them via keyset (cursor)
pagination or direct primary-key lookup."

This story (DASH-STORY-004) extends FR-002 with the growth backstop HLD
Section 12F elevates to a Sprint 1 hard dependency: "Items dwell in
`Compressed` indefinitely, bounded by the Zone 2 capacity cap (OAQ-4) --
which means OAQ-4 is not optional for Sprint 1; it is the only thing
preventing unbounded growth in a sprint with no archival destination."

AC-002-CAP-1 (verbatim, ar1_assignments.json AR1-004 dev_prompt): "Given
Zone 2 reaches capacity cap or an item exceeds MaxAge while Compressed,
the backstop sweep forcibly evicts the lowest-ranked-by-MemoryScore
item(s) so the zone never grows unbounded."

This module holds pure domain logic only: the `Zone2BackstopConfig` /
`EvictionCandidate` / `EvictionDecision` Value Objects and the pure
selection functions (`select_capacity_overflow`, `select_max_age_exceeded`,
`plan_backstop_eviction`) that decide WHICH items a sweep must evict.
Orchestration (loading config fresh per sweep, fetching candidates,
checking DPDP erasure obligations, invoking the eviction/cascade ports,
publishing `memory.evicted`) lives in
`application.zone2_capacity_backstop_sweep`, mirroring the
`domain/write_gate.py` (pure types/functions) + `application/
provenance_write_gate.py` (orchestration + logging) split DASH-STORY-006
already established.

MUST-NOT-DEVIATE (ar1_assignments.json AR1-004, binding):
  1. "Cap and MaxAge are deployment config, read per sweep run, no code
     change (AC-002-CAP-2)" -- enforced by this module NEVER hardcoding a
     cap/max_age literal; `Zone2BackstopConfig` is always an explicit,
     externally-supplied parameter to every function below, and the
     application-layer sweep (not this module) is responsible for loading
     it fresh on every `run()` call.
  2. "Eviction ordering is lowest-MemoryScore-first" -- `plan_backstop_
     eviction`'s returned list is always sorted ascending by the HLD
     Section 12G tie-break rule (see `_eviction_rank_key`'s docstring).
  3. "No attempt at an Archived transition -- there is no destination in
     Sprint 1 (HLD 12F)" -- `EvictionReason` has exactly two members,
     `CAPACITY` and `MAX_AGE` (the two `memory.evicted` reasons HLD
     Section 7.6 already defines for this backstop); there is no
     "archived" outcome anywhere in this module.
  4. "Eviction must satisfy, not bypass, a pending DPDP erasure
     obligation (AC-002-CAP-DPDP-1)" -- this module computes WHICH items
     are eviction targets only; it has no knowledge of DPDP erasure state
     at all (that is a cross-zone I/O concern, deliberately kept out of
     this I/O-free module) -- the application-layer sweep is the single
     place that routes an eviction target through the DPDP-aware branch
     before ever calling this domain module's decisions. See this
     story's dev report, judgment-call list, for the full rationale.

PII NOTE: every field here is abstract ranking/config metadata -- an
`item_id` string, a `[0,1]` MemoryScore float, a boolean state flag, an
age in seconds, a timestamp -- never conversational content or an item's
payload (dev_prompt's PII constraint, mirrored from `memory_score.py`'s
and `provenance_record.py`'s identical PII posture).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from dashanan.domain.exceptions import DashananError

_SCORE_RANGE_EPSILON = 1e-9
"""Float-rounding tolerance for the `[0, 1]` MemoryScore range check below --
mirrors `dashanan.domain.memory_score._TERM_RANGE_EPSILON`'s identical
tolerance rationale (last-ULP accommodation, not a relaxed invariant)."""


class EvictionReason(str, Enum):
    """The two forced-eviction reasons this backstop can produce.

    Values are the exact literal strings HLD Section 7.6's `memory.evicted`
    event contract already defines for this zone: `reason
    ('ttl'|'capacity'|'max_age')`. `'ttl'` is Zone 1 Working memory's own
    reason (HLD Section 12A: "Eviction primary: TTL (30 min idle)") and is
    deliberately NOT a member here -- this backstop never produces it.
    """

    CAPACITY = "capacity"
    MAX_AGE = "max_age"


class Zone2CapacityBackstopError(DashananError):
    """Raised when a Zone 2 backstop Value Object is constructed with invalid data.

    Kept local to this module rather than added to the shared
    `dashanan.domain.exceptions` -- this story's file-disjointness
    requirement keeps every new type this story introduces out of that
    shared file, mirroring `write_gate.ProvenanceJournalPort`'s identical
    "kept local... rather than added to the shared... module" choice for
    the same reason.
    """


@dataclass(frozen=True, slots=True)
class Zone2BackstopConfig:
    """The operator-configurable cap/MaxAge pair (HLD Section 7.3, NFR-009).

    HLD Section 7.3: `/v1/zones/{zone}/config` (`GET`/`PUT`) exposes
    "capacity cap, max age" per zone. HLD Section 12A's Sprint 1 default
    for Zone 2: "Capacity cap (per tenant): 100,000 items" / "MaxAge
    (forced archive): 180 days" -- this dataclass carries whatever value
    is currently configured, MVP default or operator override, with no
    branching between the two (must-not-deviate item 1: AC-017's
    "operator-overridable without a code change" pattern, mirrored from
    `dashanan.domain.memory_score.ScoreWeights`).

    Attributes:
        capacity_cap: Maximum item count Zone 2 may hold for one tenant
            before the backstop begins evicting. Must be positive.
        max_age_seconds: The MaxAge forced-eviction threshold, in
            seconds, for a Compressed item (AC-002-CAP-1's "an item
            exceeds MaxAge while Compressed"). Must be positive.
    """

    capacity_cap: int
    max_age_seconds: float

    def __post_init__(self) -> None:
        """Raises:
        Zone2CapacityBackstopError: If `capacity_cap` is not positive, or
            `max_age_seconds` is not positive -- a non-positive value
            here would make every sweep evict unconditionally, which is
            never what an operator-supplied config is meant to express
            (a zero/negative cap or age is a configuration error, not a
            valid "evict everything" instruction).
        """
        if self.capacity_cap <= 0:
            raise Zone2CapacityBackstopError(
                f"Zone2BackstopConfig.capacity_cap must be positive, "
                f"got {self.capacity_cap}"
            )
        if self.max_age_seconds <= 0:
            raise Zone2CapacityBackstopError(
                f"Zone2BackstopConfig.max_age_seconds must be positive, "
                f"got {self.max_age_seconds}"
            )


@dataclass(frozen=True, slots=True)
class EvictionCandidate:
    """One Zone 2 item's ranking inputs for one sweep run.

    Deliberately decoupled from `dashanan.domain.episode.Episode`'s
    concrete shape (Interface Segregation): the application-layer sweep
    maps an `Episode` to this narrower type, so this module stays
    reusable ranking logic that does not import, or know about, Zone 2's
    entity shape at all.

    Attributes:
        item_id: The item's zone-scoped identifier (`Episode.episode_id`
            for Zone 2). Never blank.
        memory_score: The item's already-computed composite MemoryScore
            (`dashanan.domain.memory_score.MemoryScore.value`), in
            `[0, 1]`. Eviction selection ranks by this value ascending
            (must-not-deviate item 2).
        is_compressed: Whether the item is currently in the `Compressed`
            state (`EpisodeState.COMPRESSED`). Only a Compressed item is
            eligible for the MaxAge trigger (AC-002-CAP-1: "an item
            exceeds MaxAge WHILE Compressed").
        age_seconds: Seconds since the item's `occurred_at` (mirrors
            `Episode.decay`'s own `dt_seconds` anchor point -- this is
            the item's total recorded age, not its dwell time in any one
            state). Never negative.
        written_at: The item's `written_at` timestamp. Used only as the
            HLD Section 12G tie-break anchor when two candidates share an
            identical `memory_score` (see `_eviction_rank_key`).
    """

    item_id: str
    memory_score: float
    is_compressed: bool
    age_seconds: float
    written_at: datetime

    def __post_init__(self) -> None:
        """Raises:
        Zone2CapacityBackstopError: If `item_id` is blank, `memory_score`
            is outside `[0, 1]` (epsilon-tolerant), or `age_seconds` is
            negative.
        """
        if not self.item_id.strip():
            raise Zone2CapacityBackstopError(
                "EvictionCandidate.item_id must not be blank"
            )
        if not (
            -_SCORE_RANGE_EPSILON
            <= self.memory_score
            <= 1.0 + _SCORE_RANGE_EPSILON
        ):
            raise Zone2CapacityBackstopError(
                f"EvictionCandidate.memory_score must be in [0, 1], "
                f"got {self.memory_score}"
            )
        if self.age_seconds < 0:
            raise Zone2CapacityBackstopError(
                f"EvictionCandidate.age_seconds must be >= 0, "
                f"got {self.age_seconds}"
            )


@dataclass(frozen=True, slots=True)
class EvictionDecision:
    """One item the backstop sweep has decided to forcibly evict, and why.

    Attributes:
        item_id: The item to evict (`EvictionCandidate.item_id`).
        reason: Which trigger produced this decision -- carried straight
            into the `memory.evicted` event's `reason` field (HLD
            Section 7.6) by the application-layer sweep.
    """

    item_id: str
    reason: EvictionReason


def _eviction_rank_key(
    candidate: EvictionCandidate,
) -> tuple[float, datetime, str]:
    """Ascending sort key: lowest `memory_score` evicted first.

    Implements HLD Section 12G's tie-break rule for equal/zero
    MemoryScore, applied here to eviction ordering (the rule's own text
    is symmetric: "resolves ordering between already-scored items"): "(1)
    most recent `updated_at` wins (is retained/promoted preferentially
    over the older item); (2) if `updated_at` is also tied, the item with
    the lowest `item_id`... is evicted/deprioritized first."

    `Episode` has no `updated_at` field (it is append-only -- `episode.py`
    docstring, `episodic_schema.sql`'s `REVOKE UPDATE`); this key uses
    `written_at` as the closest structural equivalent, since it is the
    one write-time timestamp every `Episode` carries. Ascending on
    `written_at` puts the OLDER write earlier in this evict-first-ordered
    list, which is exactly "most recent wins" (is retained) stated the
    other way around. This substitution is a judgment call -- see this
    story's dev report, judgment-call list.
    """
    return (candidate.memory_score, candidate.written_at, candidate.item_id)


def select_capacity_overflow(
    candidates: list[EvictionCandidate], cap: int
) -> list[EvictionCandidate]:
    """Select the lowest-scored items needed to bring the zone back under `cap`.

    AC-002-CAP-1's capacity trigger: "Zone 2 reaches capacity cap." This
    is a zone-wide, state-agnostic trigger -- unlike `select_max_age_
    exceeded`, it is not restricted to Compressed items, since HLD
    Section 12A's capacity cap ("100,000 items") is a total per-tenant
    item count, not a per-state count.

    Args:
        candidates: Every current Zone 2 item for one tenant.
        cap: `Zone2BackstopConfig.capacity_cap`.

    Returns:
        The `excess = max(0, len(candidates) - cap)` lowest-ranked
        candidates (must-not-deviate item 2 ordering, `_eviction_rank_
        key`), or an empty list when the zone is at or under `cap`.
    """
    excess = max(0, len(candidates) - cap)
    if excess == 0:
        return []
    ordered = sorted(candidates, key=_eviction_rank_key)
    return ordered[:excess]


def select_max_age_exceeded(
    candidates: list[EvictionCandidate], max_age_seconds: float
) -> list[EvictionCandidate]:
    """Select every Compressed item whose age exceeds `max_age_seconds`.

    AC-002-CAP-1's MaxAge trigger: "an item exceeds MaxAge WHILE
    Compressed." Every matching item is selected regardless of score --
    this is the hard backstop OAQ-3 exists to guarantee: a
    high-`Importance`/high-`ProvenanceConfidence` item can be pinned
    above any score threshold indefinitely (the structural score floor
    HLD Section 12B.2 derives), so MaxAge cannot itself be score-gated
    without defeating its own purpose as a backstop of last resort.

    Args:
        candidates: Every current Zone 2 item for one tenant.
        max_age_seconds: `Zone2BackstopConfig.max_age_seconds`.

    Returns:
        Every `is_compressed` candidate with `age_seconds >
        max_age_seconds`, ordered ascending by `_eviction_rank_key`
        (must-not-deviate item 2) for consistent processing order even
        though every returned item is, by definition, already selected.
    """
    matches = [
        c
        for c in candidates
        if c.is_compressed and c.age_seconds > max_age_seconds
    ]
    return sorted(matches, key=_eviction_rank_key)


def plan_backstop_eviction(
    candidates: list[EvictionCandidate], config: Zone2BackstopConfig
) -> list[EvictionDecision]:
    """Combine both triggers into one ordered, deduplicated eviction plan.

    The single entry point the application-layer sweep calls per run
    (Template Method's "evaluate" step). An item eligible under both
    triggers appears exactly once, tagged `MAX_AGE` -- the MaxAge
    backstop is the harder guarantee (HLD Section 12F: "the ONLY thing
    preventing unbounded growth"), so it takes priority as the recorded
    reason when both apply to the same item.

    Args:
        candidates: Every current Zone 2 item for one tenant, already
            scored (`EvictionCandidate.memory_score`) by the caller.
        config: The cap/MaxAge pair for this sweep run -- always
            freshly loaded by the caller (must-not-deviate item 1); this
            function never caches or defaults it.

    Returns:
        `EvictionDecision`s ordered lowest-`memory_score`-first
        (must-not-deviate item 2), covering the union of both triggers
        with no duplicate `item_id`.
    """
    reason_by_item: dict[str, EvictionReason] = {}
    by_item: dict[str, EvictionCandidate] = {}

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
