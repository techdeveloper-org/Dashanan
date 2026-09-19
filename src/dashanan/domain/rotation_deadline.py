"""Rotation deadline closed form (HLD Section 12C, ADR-012, mathematics-engineer DASH-STORY-009).

Traces to FR-012 in SRS.md. FR-012 (verbatim): "The system SHALL compute a
composite Memory Score for every item from six weighted terms (Recency,
Frequency, Importance, UserAffinity, TaskRelevance, ProvenanceConfidence),
each normalized to [0,1], and SHALL use this score to drive zone lifecycle
transitions."

MATH DELEGATION (BLOCKING, ar1_assignments.json AR1-009): the closed-form
deadline and its three-branch validity domain are implemented here VERBATIM
from the mathematics-engineer's returned derivation -- this module does not
re-derive anything. The general-weight form is used throughout; the HLD
Section 12C literal "6" is `1/w_recency`, never a hardcoded constant, so an
operator-overridden `ScoreWeights` profile (AC-017/NFR-009) is computed
correctly without a code change.

This module holds pure domain logic only: `compute_rotation_deadline` and
its numeric guard. No I/O, no Clock, no logging -- mirrors every other
domain module's "domain-only" convention (`memory_score.py`,
`zone2_capacity_backstop.py`, `provenance_record.py`). The caller
(`application.rotation_engine`) supplies `lambda_zone`, `w_recency` and
`c_weighted` from `dashanan.domain.memory_score.ScoreWeights` /
`ScoreTerms` -- this module has no dependency on those types, only on the
five scalar floats the closed form actually needs (Interface Segregation).

PII NOTE: every value here is an abstract numeric input (a decay rate, a
weight, a weighted score sum, a duration in seconds) -- never conversational
content or an item's payload, mirroring `memory_score.py`'s identical PII
posture (dev_prompt's PII constraint).
"""

from __future__ import annotations

import math
import sys

_MACHINE_EPSILON = sys.float_info.epsilon
"""IEEE 754 float64 machine epsilon (`2.220446049250313e-16`), per the math
delegation's D4 numerical-stability derivation."""

_EPS_GUARD_MULTIPLIER = 10 * 6
"""The math delegation's F3 guard multiplier: `10 * 6 * eps_mach` --
10x safety factor over the `6 * eps_mach` catastrophic-cancellation bound
D4 derives for the equal-weight profile's `6*theta - C` difference. Kept as
a fixed multiplier (not `10 / w_recency`) per the delegation's own worked
numbers in D4/W-series, which hold this constant across both Sprint 1
decaying zones regardless of weight profile."""

_U_EPS_GUARD = _EPS_GUARD_MULTIPLIER * _MACHINE_EPSILON
"""`10 * 6 * eps_mach ~= 1.33e-14` -- the math delegation's F8 reference
implementation's `_EPS_GUARD` constant, verbatim."""


def compute_rotation_deadline(
    lambda_zone: float,
    theta: float,
    w_recency: float,
    c_weighted: float,
    max_age_remaining_s: float,
    cooldown_remaining_s: float,
) -> float | None:
    """Solve the closed-form threshold-crossing deadline for one item.

    Solves `S(dt) = theta` for `dt`, where
    `S(dt) = w_recency * exp(-lambda_zone * dt) + c_weighted` is strictly
    decreasing in `dt` (HLD Section 12C; the general-weight form and its
    three-branch validity domain are derived in the DASH-STORY-009
    mathematics-engineer delegation, sections F1-F3).

    General closed form (F1, used here verbatim -- never the equal-weight
    `6*theta - C` shortcut, since `NFR-009`/`AC-017` make the weight
    profile operator-overridable without a code change):

    ```
    u = (theta - c_weighted) / w_recency
    delta_t* = -(1 / lambda_zone) * ln(u)
    ```

    Three branches (F2/F3, mathematics-engineer delegation):
      - `u <= u_floor(zone)` -> PINNED. The item's floor is at or above
        `theta`; it never crosses by decay alone. Returns `None` -- the
        caller MUST NOT insert this item into the timer wheel.
      - `u >= 1.0` -> IMMEDIATE. Already at or below `theta` at `dt=0`.
        `delta_t*` is clamped to `0` before the cooldown/`MaxAge` floor
        below is applied.
      - `u_floor(zone) < u < 1.0` -> SCHEDULE at the computed `delta_t*`,
        clamped below.

    The returned value, when not `None`, is the F3 "effective scheduled
    deadline": `clamp(max(delta_t*, cooldown_remaining_s), 0,
    max_age_remaining_s)`. The `max(..., cooldown_remaining_s)` floor is
    mandatory -- omitting it schedules a transition the guarded-transition
    check (`dashanan.domain.rotation_state`) must then reject on the very
    next tick if the item changed lifecycle state within the last zone
    half-life, burning a pop+re-insert per tick on the same item (the
    pathological `k` inflation the sweep-bound validity condition warns
    about).

    Args:
        lambda_zone: The zone's decay constant, `ln(2) / t_half`
            (`dashanan.domain.memory_score.lambda_from_half_life`'s
            output). Must be strictly positive -- Zones 6 and 7 have no
            decay constant (HLD Section 12A: "inherits host item" /
            "never decays") and must never reach this function.
        theta: The threshold being crossed. Per this story's Sprint 1
            scope restriction (F4, HLD Section 12F, AC-012-ROT-1),
            callers pass `CompressThreshold` ONLY -- never
            `ArchiveThreshold`, since Zone 8 has no destination in
            Sprint 1.
        w_recency: The weight on the Recency term
            (`ScoreWeights.w_recency`). Must be strictly positive --
            a zero Recency weight removes all time dependence from the
            score, so no deadline exists.
        c_weighted: The frozen weighted sum of the five non-Recency terms
            between accesses (`w_frequency*F + w_importance*I +
            w_user_affinity*U + w_task_relevance*T +
            w_provenance_confidence*P`, HLD Section 12C: "Between
            accesses... only Recency varies").
        max_age_remaining_s: Seconds until the zone's MaxAge backstop
            (DASH-STORY-004) fires for this item. Also the F3 semantic
            ceiling: a computed deadline beyond this is unreachable
            because the backstop fires first, so it is treated as pinned.
        cooldown_remaining_s: Seconds left on HLD Section 12A's
            state-change cooldown ("an item cannot change lifecycle state
            twice within one zone half-life"), `0` when no cooldown is
            outstanding.

    Returns:
        Seconds from now at which the rotation sweep should re-evaluate
        this item for a `Compressed` transition, or `None` when the item
        is pinned (must not be scheduled at all).

    Raises:
        ValueError: If `lambda_zone` or `w_recency` is not strictly
            positive.
    """
    if lambda_zone <= 0.0:
        raise ValueError(
            f"compute_rotation_deadline requires lambda_zone > 0, got {lambda_zone}"
        )
    if w_recency <= 0.0:
        raise ValueError(
            f"compute_rotation_deadline requires w_recency > 0, got {w_recency}"
        )

    u = (theta - c_weighted) / w_recency
    u_floor = max(_U_EPS_GUARD, math.exp(-lambda_zone * max_age_remaining_s))

    if u <= u_floor:
        return None
    if u >= 1.0:
        delta_t = 0.0
    else:
        delta_t = -math.log(u) / lambda_zone
        if delta_t > max_age_remaining_s:
            return None

    return min(max(delta_t, cooldown_remaining_s), max_age_remaining_s)


def weighted_non_recency_sum(
    w_frequency: float,
    frequency: float,
    w_importance: float,
    importance: float,
    w_user_affinity: float,
    user_affinity: float,
    w_task_relevance: float,
    task_relevance: float,
    w_provenance_confidence: float,
    provenance_confidence: float,
) -> float:
    """Compute `c_weighted`, the frozen weighted sum of the five non-Recency terms.

    The single place `compute_rotation_deadline`'s `c_weighted` argument is
    assembled from a caller's already-computed `ScoreTerms`/`ScoreWeights`
    (`dashanan.domain.memory_score`) -- kept as a standalone function
    taking five scalar `(weight, term)` pairs, rather than importing
    `ScoreTerms`/`ScoreWeights` directly, so this module stays decoupled
    from DASH-STORY-008's types (Interface Segregation, mirrors
    `zone2_capacity_backstop.EvictionCandidate`'s identical decoupling
    from `Episode`).

    Args:
        w_frequency: `ScoreWeights.w_frequency`.
        frequency: `ScoreTerms.frequency`.
        w_importance: `ScoreWeights.w_importance`.
        importance: `ScoreTerms.importance`.
        w_user_affinity: `ScoreWeights.w_user_affinity`.
        user_affinity: `ScoreTerms.user_affinity`.
        w_task_relevance: `ScoreWeights.w_task_relevance`.
        task_relevance: `ScoreTerms.task_relevance`.
        w_provenance_confidence: `ScoreWeights.w_provenance_confidence`.
        provenance_confidence: `ScoreTerms.provenance_confidence`.

    Returns:
        The weighted sum of the five non-Recency terms, `C_w` in the math
        delegation's D2 notation.
    """
    return (
        w_frequency * frequency
        + w_importance * importance
        + w_user_affinity * user_affinity
        + w_task_relevance * task_relevance
        + w_provenance_confidence * provenance_confidence
    )
