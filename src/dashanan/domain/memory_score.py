"""MemoryScore: the six-term composite score (HLD Sections 5, 6, 12A-12C, 12G).

Traces to FR-012 in SRS.md. FR-012 (verbatim): "The system SHALL compute a
composite Memory Score for every item from six weighted terms (Recency,
Frequency, Importance, UserAffinity, TaskRelevance, ProvenanceConfidence),
each normalized to [0,1], and SHALL use this score to drive zone lifecycle
transitions."

This module holds pure domain logic only: the term-computation functions
(Recency, Frequency, Importance, UserAffinity, TaskRelevance) and the three
immutable Value Objects (`ScoreWeights`, `ScoreTerms`, `MemoryScore`) HLD
Section 6's "Scoring" row requires ("immutable `MemoryScore`, `ScoreTerms` --
compared, never mutated"). ProvenanceConfidence is deliberately NOT
recomputed here -- HLD Section 12G says its formula is "unchanged... now
explicitly wrapped," and `dashanan.domain.provenance_record.compute_confidence`
(DASH-STORY-005, Zone 7) already IS that wrapped implementation; this module
only validates the already-computed value lands in `[0,1]` alongside the
other five terms (must-not-deviate item 1).

General-weight form throughout (mathematics-engineer delegation, AR1-008):
no `/6` is ever hardcoded. Every weight is a field on `ScoreWeights`, always
supplied explicitly (defaulting to `ScoreWeights.default()`'s `1/6` each,
never assumed implicitly), so AC-017 / NFR-009 ("operator-overridable
without a code change") holds structurally -- an operator-supplied
`ScoreWeights` flows through the identical `MemoryScore.compute` code path
the MVP default uses.

PII NOTE: every value here is an abstract numeric term (a cosine similarity,
an access count, a decay factor, a tag-derived constant) -- never
conversational content or an item's payload (dev_prompt's PII constraint).
Docstrings and tests use abstract term values only (e.g. `Recency=0.8`),
mirroring `retrieval_fusion.py`'s and `provenance_record.py`'s identical
PII posture.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

_WEIGHT_SUM_EPSILON = 1e-9
"""HLD Section 12G: "PUT /v1/config/scoring MUST REJECT |SUM(w) - 1| > 1e-9"."""

_TERM_RANGE_EPSILON = 1e-9
"""Float-rounding tolerance for the `[0, 1]` term/score range checks below --
not a relaxation of must-not-deviate item 1, just accommodation for the last
ULP of `dot-product`/`exp`/`log1p` rounding the Range Theorem's proof assumes
real-number arithmetic, not float64."""

IMPORTANCE_TAG_VALUES: dict[str, float] = {
    "critical": 1.0,
    "high": 0.8,
    "medium": 0.5,
    "low": 0.2,
}
"""HLD Section 12G: "critical=1.0, high=0.8, medium=0.5, low=0.2" -- the
Sprint 1 explicit-tag-only Importance mapping (AC-012-SCORE-1). Zone 4's
`success_count/(success_count+failure_count)` inference formula (HLD Section
3.5) is explicitly out of Sprint 1 scope (HLD Section 12F) and is NOT
implemented here -- see this story's dev report, judgment-call list."""

DEFAULT_IMPORTANCE_TAG = "medium"
"""HLD Section 12G: "Default tag is medium (0.5)"."""

USER_AFFINITY_COLD_START_DEFAULT = 0.5
"""HLD Section 12G (OAQ-19 Resolved: Adopted): "Default 0.5 on cold start
(no prior sessions for this user in this tenant)"."""

TASK_RELEVANCE_NO_QUERY_DEFAULT = 0.0
"""Mathematics-engineer delegation Part A.5 [DERIVED]: no default is stated
in HLD Section 12G itself for the no-active-query case (write path /
rotation re-score); `T_init = 0.0` is the derived, liveness-safe choice --
see this story's dev report, judgment-call list for the full rationale."""


def clamp01(value: float) -> float:
    """Clamp `value` to `[0, 1]` (HLD Section 12G: `clamp01(x) = max(0, min(1, x))`).

    The single shared clamp every term below that needs one calls -- HLD
    Section 12G defines it once "used by every term below that needs it,"
    and this is that one definition for the scoring domain (mirroring
    `provenance_record._clamp01`'s identical, independently-owned copy for
    the ProvenanceConfidence term this module does not recompute).
    """
    return max(0.0, min(1.0, value))


def lambda_from_half_life(t_half_seconds: float | None) -> float:
    """Derive `lambda_zone` from a zone's half-life (HLD Section 12C / 12A).

    Mathematics-engineer delegation Part C.1 / D (implementation contract):
    "DERIVE lambda FROM t_half AT CONFIG LOAD. Never hardcode HLD 12A's
    rounded literals" -- Zone 4's published `6.68e-8` is a truncation of the
    exact `ln(2)/t_half` value and would silently break an exact-half-life
    unit test if hardcoded instead of derived. This function is the sole
    derivation point every caller must route through.

    Args:
        t_half_seconds: The zone's half-life in seconds, or `None` for a
            zone that never decays (HLD Section 12A, Zone 7: "never
            decays"). `None` is the explicit sentinel for that case -- not
            `0` or a large number -- per the math delegation's Part C.3:
            "Encode Zone 7 as lambda = 0 -> Recency == 1 identically. No
            special-case branch [needed in `compute_recency` itself]."

    Returns:
        `0.0` when `t_half_seconds` is `None` (never decays); otherwise
        `ln(2) / t_half_seconds`.

    Raises:
        ValueError: If `t_half_seconds` is not `None` and is `<= 0`.
    """
    if t_half_seconds is None:
        return 0.0
    if t_half_seconds <= 0:
        raise ValueError(
            f"lambda_from_half_life requires t_half_seconds > 0 or None, "
            f"got {t_half_seconds}"
        )
    return math.log(2.0) / t_half_seconds


def compute_recency(dt_seconds: float, lambda_zone: float) -> float:
    """Recency = `exp(-lambda_zone * max(0, dt_seconds))` (HLD Section 12C).

    The `max(0, ...)` clock-skew guard is mandatory, not defensive
    decoration -- mathematics-engineer delegation Part A.1 [DERIVED]: "a
    clock-skewed or future-dated `updated_at`... yields `dt<0` ->
    `exp(positive) > 1` -> `[0,1]` invariant violated -> `S > 1` ->
    thresholds misfire." `lambda_zone = 0` (Zone 7, "never decays") makes
    this identically `1.0` for any `dt_seconds` with no special-case branch,
    exactly as the math delegation's Part C.3 requires.

    Args:
        dt_seconds: Seconds since this item's last access/write. May be
            negative (clock skew); clamped to `0` before use.
        lambda_zone: The zone's decay rate, `ln(2)/t_half` -- always the
            output of `lambda_from_half_life`, never a caller-hardcoded
            literal (see that function's docstring).

    Returns:
        A value in `(0, 1]` for `lambda_zone > 0`, or exactly `1.0` for
        `lambda_zone == 0`.

    Raises:
        ValueError: If `lambda_zone` is negative.
    """
    if lambda_zone < 0:
        raise ValueError(
            f"compute_recency requires lambda_zone >= 0, got {lambda_zone}"
        )
    return math.exp(-lambda_zone * max(0.0, dt_seconds))


def compute_frequency(access_count: int, f_cap: int) -> float:
    """Frequency = `min(1, log(1 + access_count) / log(1 + f_cap))` (HLD Section 12G).

    Uses `math.log1p` for the `log(1+x)` form the HLD formula specifies --
    numerically equivalent to `math.log(1+x)` but avoids catastrophic
    cancellation for small `access_count`. Per the math delegation's Part
    A.2: this term is monotone NON-DECREASING and does NOT decay -- HLD
    Section 12B.2's classification of Frequency as a decaying term is the
    root error that delegation identifies; this implementation reflects the
    corrected, non-decaying behaviour the HLD Section 12G formula itself
    (no time term) already implies.

    Args:
        access_count: Read-triggered-or-write access count (HLD Section
            12G), non-negative, not reset on promotion.
        f_cap: The per-zone saturation cap (HLD Section 12G /
            `/v1/zones/{zone}/config`'s `frequency_cap`). Must be `>= 1` --
            the math delegation's Part A.2 guard: `f_cap=0` makes the
            denominator `log(1)=0`, a `ZeroDivisionError`.

    Returns:
        A value in `[0, 1]`, with both endpoints attainable
        (`access_count=0` -> `0.0`; `access_count >= f_cap` -> `1.0`).

    Raises:
        ValueError: If `access_count` is negative or `f_cap < 1`.
    """
    if access_count < 0:
        raise ValueError(
            f"compute_frequency requires access_count >= 0, got {access_count}"
        )
    if f_cap < 1:
        raise ValueError(f"compute_frequency requires f_cap >= 1, got {f_cap}")
    return min(1.0, math.log1p(access_count) / math.log1p(f_cap))


def compute_importance(tag: str | None) -> float:
    """Importance from an explicit tag only (AC-012-SCORE-1, HLD Section 12G).

    The sole implementation of AC-012-SCORE-1: "Given Zone 4 (Procedural) is
    out of Sprint 1 scope, when Importance is computed, then Importance is
    sourced from an explicit tag only and defaults to 0.5 when no tag is
    present." A `tag` outside `IMPORTANCE_TAG_VALUES` (unrecognized, not
    just absent) also falls back to the default -- matching the math
    delegation's Part D implementation contract literally:
    `importance = TAG_MAP.get(tag, 0.5)`.

    Args:
        tag: The caller-supplied importance tag, or `None` when absent.

    Returns:
        A value in `{1.0, 0.8, 0.5, 0.2}` -- `IMPORTANCE_TAG_VALUES[tag]`
        when `tag` is a recognized key, else `0.5`
        (`IMPORTANCE_TAG_VALUES[DEFAULT_IMPORTANCE_TAG]`).
    """
    if tag is None:
        return IMPORTANCE_TAG_VALUES[DEFAULT_IMPORTANCE_TAG]
    return IMPORTANCE_TAG_VALUES.get(tag, IMPORTANCE_TAG_VALUES[DEFAULT_IMPORTANCE_TAG])


def is_degenerate_calibration(cos_min: float, cos_max: float) -> bool:
    """True when an adapter's cosine calibration has collapsed to one point.

    HLD Section 12G: "Degenerate `cos_max == cos_min` => fall back to the
    `(cosine + 1)/2` mapping and log a warning." This predicate is the pure
    check; the caller decides what "log a warning" means for its own layer
    (`dashanan.application.memory_score_engine.MemoryScoreEngine` is the one
    that actually logs -- this module stays I/O-free, matching every other
    domain module's "domain-only -- no SQL, no I/O" convention).
    """
    return cos_max == cos_min


def _normalize_cosine(cosine: float, cos_min: float, cos_max: float) -> float:
    """The ONE calibrated-cosine code path `compute_user_affinity` and
    `compute_task_relevance` both call (HLD Section 12G: "the two formulas
    must never diverge on this point, since a per-adapter calibration
    mismatch... would silently skew one term's range relative to the
    other's"). Not part of this module's public API -- both public
    functions below exist specifically so callers never need to import this
    shared internal directly.
    """
    if is_degenerate_calibration(cos_min, cos_max):
        return clamp01((cosine + 1.0) / 2.0)
    return clamp01((cosine - cos_min) / (cos_max - cos_min))


def compute_user_affinity(
    cosine: float | None, cos_min: float, cos_max: float
) -> float:
    """UserAffinity (HLD Section 12G, OAQ-19 Resolved: Adopted).

    `clamp01((cosine - cos_min[model_id]) / (cos_max[model_id] -
    cos_min[model_id]))`, with the degenerate-calibration fallback
    (`_normalize_cosine`) and the cold-start default below.

    NEVER a tenant-isolation mechanism (must-not-deviate item 5 /
    ADR-013 / threat I-2): this function computes a purely numeric,
    within-tenant relevance signal from a caller-supplied cosine value --
    it has no tenant parameter, performs no tenant comparison, and cannot
    itself enforce or bypass tenant scoping. Tenant isolation is enforced
    structurally upstream (physical partition, per ADR-013), a concern this
    module does not participate in.

    Args:
        cosine: `cosine(session_embedding, mean(last N prior session
            embeddings))`, pre-computed by the caller, or `None` on cold
            start (no prior same-user session in this tenant).
        cos_min: The `EmbeddingProvider` adapter's calibrated 1st-percentile
            cosine similarity (HLD Section 12G), keyed by `model_id`.
        cos_max: The adapter's calibrated 99th-percentile cosine similarity.

    Returns:
        `USER_AFFINITY_COLD_START_DEFAULT` (`0.5`) when `cosine` is `None`;
        otherwise a value in `[0, 1]`.
    """
    if cosine is None:
        return USER_AFFINITY_COLD_START_DEFAULT
    return _normalize_cosine(cosine, cos_min, cos_max)


def compute_task_relevance(
    cosine: float | None, cos_min: float, cos_max: float
) -> float:
    """TaskRelevance (HLD Section 12G, OAQ-19 Resolved: Adopted).

    `clamp01((cosine - cos_min[model_id]) / (cos_max[model_id] -
    cos_min[model_id]))` -- the "raw normalized cosine" OAQ-5 names, as
    distinct from the RRF fused ranking score (`dashanan.domain.
    retrieval_fusion`, ADR-008); this function and that module never
    compute the same value.

    Args:
        cosine: `cosine(item_embedding, query_embedding)`, pre-computed by
            the caller, or `None` when there is no active query (write
            path / rotation re-score -- mathematics-engineer delegation
            Part A.5 [DERIVED]).
        cos_min: Same calibrated 1st-percentile value `compute_user_affinity`
            uses for the same `model_id` (shared calibration, see
            `_normalize_cosine`'s docstring).
        cos_max: Same calibrated 99th-percentile value.

    Returns:
        `TASK_RELEVANCE_NO_QUERY_DEFAULT` (`0.0`) when `cosine` is `None`;
        otherwise a value in `[0, 1]`.
    """
    if cosine is None:
        return TASK_RELEVANCE_NO_QUERY_DEFAULT
    return _normalize_cosine(cosine, cos_min, cos_max)


@dataclass(frozen=True, slots=True)
class ScoreWeights:
    """The per-zone weight vector `w1..w6` (HLD Section 12A/12G, AC-017, NFR-009).

    Immutable Value Object (HLD Section 6): compared, never mutated. Every
    field is a plain, independently-settable weight -- there is no derived
    `/6` anywhere in this class or in `MemoryScore.compute`, so an operator
    profile constructed with different weights (AC-017: "operator sets a
    non-default weight profile... the configured values are used instead of
    MVP defaults, without a code change") flows through the identical
    composition code path as `ScoreWeights.default()`.

    Attributes:
        w_recency: Weight for the Recency term.
        w_frequency: Weight for the Frequency term.
        w_importance: Weight for the Importance term.
        w_user_affinity: Weight for the UserAffinity term.
        w_task_relevance: Weight for the TaskRelevance term.
        w_provenance_confidence: Weight for the ProvenanceConfidence term.
    """

    w_recency: float
    w_frequency: float
    w_importance: float
    w_user_affinity: float
    w_task_relevance: float
    w_provenance_confidence: float

    def __post_init__(self) -> None:
        """Enforce HLD Section 12G's "Global invariant": every weight
        non-negative and `sum(w1..w6) = 1` (within float epsilon) -- the
        sole premise the composite `[0, 1]` range guarantee (the Range
        Theorem, HLD 12G / mathematics-engineer delegation Part A.0) rests
        on. A negative weight breaks the theorem's lower bound
        independently of the sum, so it is checked and rejected on its own.

        Raises:
            ValueError: If any weight is negative, or the six weights do
                not sum to `1.0` within `1e-9`.
        """
        weights = (
            self.w_recency,
            self.w_frequency,
            self.w_importance,
            self.w_user_affinity,
            self.w_task_relevance,
            self.w_provenance_confidence,
        )
        for name, value in zip(
            (
                "w_recency",
                "w_frequency",
                "w_importance",
                "w_user_affinity",
                "w_task_relevance",
                "w_provenance_confidence",
            ),
            weights,
            strict=True,
        ):
            if value < 0.0:
                raise ValueError(f"ScoreWeights.{name} must be >= 0, got {value}")
        total = sum(weights)
        if abs(total - 1.0) > _WEIGHT_SUM_EPSILON:
            raise ValueError(
                f"ScoreWeights weights must sum to 1.0 (within {_WEIGHT_SUM_EPSILON}), "
                f"got sum={total}"
            )

    @classmethod
    def default(cls) -> ScoreWeights:
        """The Sprint 1 MVP default: `1/6` each (HLD Section 12A, PRD-locked)."""
        sixth = 1.0 / 6.0
        return cls(
            w_recency=sixth,
            w_frequency=sixth,
            w_importance=sixth,
            w_user_affinity=sixth,
            w_task_relevance=sixth,
            w_provenance_confidence=sixth,
        )


@dataclass(frozen=True, slots=True)
class ScoreTerms:
    """The six normalized score terms for one item (HLD Section 6, 12G).

    Immutable Value Object: compared, never mutated (must-not-deviate item
    2). Every field must already be in `[0, 1]` at construction
    (must-not-deviate item 1: "All six terms normalized to [0,1] before
    weighting") -- this dataclass is the single checkpoint that enforces
    it, regardless of which function (or, for `provenance_confidence`,
    which OTHER story's function) produced the value.

    Attributes:
        recency: `compute_recency`'s output.
        frequency: `compute_frequency`'s output.
        importance: `compute_importance`'s output.
        user_affinity: `compute_user_affinity`'s output.
        task_relevance: `compute_task_relevance`'s output.
        provenance_confidence: The Zone 7 `ProvenanceRecord.confidence`
            value (`dashanan.domain.provenance_record.compute_confidence`'s
            output, DASH-STORY-005) -- NOT recomputed by this module (see
            this module's docstring).
    """

    recency: float
    frequency: float
    importance: float
    user_affinity: float
    task_relevance: float
    provenance_confidence: float

    def __post_init__(self) -> None:
        """Raises:
        ValueError: If any of the six terms is outside `[0, 1]`
            (`_TERM_RANGE_EPSILON` tolerance for float rounding).
        """
        for name, value in (
            ("recency", self.recency),
            ("frequency", self.frequency),
            ("importance", self.importance),
            ("user_affinity", self.user_affinity),
            ("task_relevance", self.task_relevance),
            ("provenance_confidence", self.provenance_confidence),
        ):
            if not (
                -_TERM_RANGE_EPSILON <= value <= 1.0 + _TERM_RANGE_EPSILON
            ):
                raise ValueError(
                    f"ScoreTerms.{name} must be in [0, 1], got {value}"
                )


@dataclass(frozen=True, slots=True)
class MemoryScore:
    """The composite Memory Score: `S = sum_i w_i * x_i` (HLD Section 5, 6, 12G).

    Immutable Value Object (must-not-deviate item 2): construct only via
    `MemoryScore.compute`, never by assembling this dataclass with an
    independently-derived `value` -- that would defeat the Range Theorem
    guarantee `__post_init__` checks. Carries both `terms` and `weights`
    alongside `value` so a `score:explain` caller (HLD Section 7.1,
    `POST /v1/items/{item_id}/score:explain`) can reproduce the composite
    from this object alone, with no recomputation and no additional lookup
    -- exactly the "trivially reproducible" property HLD Section 6
    attributes to this class's immutability.

    Attributes:
        value: The weighted composite score, `sum(w_i * x_i)` -- six
            multiply-adds, `O(1)` (HLD Section 5, "Scoring service" row).
        terms: The six input terms this score was computed from.
        weights: The weight vector this score was computed with.
    """

    value: float
    terms: ScoreTerms
    weights: ScoreWeights

    def __post_init__(self) -> None:
        """Raises:
        ValueError: If `value` falls outside `[0, 1]` -- the Range
            Theorem (HLD Section 12G / mathematics-engineer delegation
            Part A.0: "S in [min_i x_i, max_i x_i] subset of [0,1]")
            guarantees this cannot happen for any valid `ScoreTerms` /
            `ScoreWeights` pair, so a violation here signals a real
            construction bug (e.g. `value` assembled independently of
            `terms`/`weights`) rather than a data problem.
        """
        if not (-_TERM_RANGE_EPSILON <= self.value <= 1.0 + _TERM_RANGE_EPSILON):
            raise ValueError(
                f"MemoryScore.value must be in [0, 1] (Range Theorem), got {self.value}"
            )

    @classmethod
    def compute(cls, terms: ScoreTerms, weights: ScoreWeights) -> MemoryScore:
        """Compute the composite score: six multiply-adds, `O(1)`.

        General-weight form throughout -- `weights` is always an explicit
        parameter, never a hardcoded `1/6`, so this single code path serves
        both the MVP default and any operator-overridden profile (AC-017).

        Args:
            terms: Already-validated `ScoreTerms` (each in `[0, 1]`).
            weights: Already-validated `ScoreWeights` (non-negative,
                summing to `1.0`).

        Returns:
            The resulting `MemoryScore`, itself validated to land in
            `[0, 1]` by `__post_init__`.
        """
        value = (
            weights.w_recency * terms.recency
            + weights.w_frequency * terms.frequency
            + weights.w_importance * terms.importance
            + weights.w_user_affinity * terms.user_affinity
            + weights.w_task_relevance * terms.task_relevance
            + weights.w_provenance_confidence * terms.provenance_confidence
        )
        return cls(value=value, terms=terms, weights=weights)
