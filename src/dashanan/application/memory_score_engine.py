"""MemoryScoreEngine: the six-term formula facade (DASH-STORY-008, FR-012).

HLD Sections 5, 6, 12A-12C, 12G, mathematics-engineer delegation AR1-008.

FR-012 (verbatim, SRS.md): "The system SHALL compute a composite Memory
Score for every item from six weighted terms (Recency, Frequency,
Importance, UserAffinity, TaskRelevance, ProvenanceConfidence), each
normalized to [0,1], and SHALL use this score to drive zone lifecycle
transitions."

This module composes the pure term-computation functions and Value Objects
in `dashanan.domain.memory_score` into one entry point (`MemoryScoreEngine.
score`) a zone story or the Rotation Engine calls to obtain a `MemoryScore`
for one item, mirroring the `domain/write_gate.py` (pure types/functions) +
`application/provenance_write_gate.py` (orchestration + logging) split
DASH-STORY-006 already established. The only reason this facade lives in
the application layer rather than alongside its pure domain building blocks
is the one piece of required I/O: HLD Section 12G's "log a warning" on a
degenerate adapter calibration -- every other domain module in this repo
stays logging-free (see `provenance_record.py`'s "domain-only -- no SQL, no
I/O" docstring), and `ProvenanceWriteGate` (application layer) is this
codebase's own precedent for where that logging belongs.

PII NOTE: `ScoreInputs` carries only numeric term inputs (a cosine
similarity, an access count, a decay half-life, a tag string, a
pre-computed confidence float) -- never conversational content or an
item's payload (dev_prompt's PII constraint, mirrored from
`domain/memory_score.py`'s identical note).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from dashanan.domain.memory_score import (
    MemoryScore,
    ScoreTerms,
    ScoreWeights,
    compute_frequency,
    compute_importance,
    compute_recency,
    compute_task_relevance,
    compute_user_affinity,
    is_degenerate_calibration,
    lambda_from_half_life,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ScoreInputs:
    """Raw, per-item inputs `MemoryScoreEngine.score` needs to compute all six terms.

    One `ScoreInputs` bundles exactly what HLD Section 12G's per-term
    formulas each take as input, deliberately excluding anything the
    Orchestrator/zone layer already owns (`item_id`, `tenant_id`, payload)
    -- this dataclass is scoped strictly to score computation, matching
    `dashanan.domain.ports.ZoneQuery`'s identical "only what this operation
    needs" scoping choice.

    Attributes:
        dt_seconds: Seconds since this item's last access/write (Recency).
            May be negative under clock skew; `compute_recency` clamps it.
        half_life_seconds: This item's zone's half-life in seconds, or
            `None` for a zone that never decays (HLD Section 12A, Zone 7).
        access_count: Read-triggered-or-write access count (Frequency).
        f_cap: The per-zone Frequency saturation cap (HLD Section 12G).
        importance_tag: The explicit importance tag, or `None` (Importance,
            AC-012-SCORE-1). Zone 4's inferred-salience path is out of
            Sprint 1 scope and has no field here (HLD Section 12F).
        provenance_confidence: The already-computed ProvenanceConfidence
            value (`dashanan.domain.provenance_record.compute_confidence`'s
            output, DASH-STORY-005/Zone 7) -- this engine does not
            recompute it (see this module's docstring).
        user_cosine: `cosine(session_embedding, mean(prior sessions))`, or
            `None` on cold start (UserAffinity).
        task_cosine: `cosine(item_embedding, query_embedding)`, or `None`
            when there is no active query (TaskRelevance).
        cos_min: The `EmbeddingProvider` adapter's calibrated 1st-percentile
            cosine similarity for this `model_id` (HLD Section 12G), shared
            by `user_cosine` and `task_cosine` normalization.
        cos_max: The adapter's calibrated 99th-percentile cosine similarity.
    """

    dt_seconds: float
    half_life_seconds: float | None
    access_count: int
    f_cap: int
    importance_tag: str | None
    provenance_confidence: float
    user_cosine: float | None
    task_cosine: float | None
    cos_min: float
    cos_max: float


class MemoryScoreEngine:
    """Computes a `MemoryScore` from `ScoreInputs` (HLD Section 5, "Scoring service" row).

    Stateless: holds no per-instance mutable state, so one instance safely
    serves concurrent callers (mirrors `dashanan.domain.retrieval_fusion`'s
    equally stateless pure-function design, just packaged as a class here
    because the story's own title names it an "engine" and it composes five
    term functions plus a logging side effect, unlike the module-level
    functions `retrieval_fusion.py` exposes directly).
    """

    def score(
        self, inputs: ScoreInputs, weights: ScoreWeights | None = None
    ) -> MemoryScore:
        """Compute the composite `MemoryScore` for one item.

        Args:
            inputs: The item's raw term inputs.
            weights: The weight profile to use, or `None` to use
                `ScoreWeights.default()` (the Sprint 1 MVP `1/6` each).
                Passing a caller-supplied, non-default `ScoreWeights` here
                is exactly how AC-017 / NFR-009 ("operator-overridable
                without a code change") is satisfied -- this method never
                branches on whether `weights` is the default.

        Returns:
            The resulting immutable `MemoryScore`.

        Raises:
            ValueError: Propagated from `lambda_from_half_life`,
                `compute_frequency`, `ScoreTerms.__post_init__`, or
                `ScoreWeights.__post_init__` for any malformed input
                (negative `access_count`, `f_cap < 1`, a
                `provenance_confidence` outside `[0, 1]`, a non-default
                `weights` that does not sum to `1.0`, etc.).
        """
        if inputs.user_cosine is not None or inputs.task_cosine is not None:
            self._warn_if_degenerate_calibration(inputs.cos_min, inputs.cos_max)

        lambda_zone = lambda_from_half_life(inputs.half_life_seconds)
        terms = ScoreTerms(
            recency=compute_recency(inputs.dt_seconds, lambda_zone),
            frequency=compute_frequency(inputs.access_count, inputs.f_cap),
            importance=compute_importance(inputs.importance_tag),
            user_affinity=compute_user_affinity(
                inputs.user_cosine, inputs.cos_min, inputs.cos_max
            ),
            task_relevance=compute_task_relevance(
                inputs.task_cosine, inputs.cos_min, inputs.cos_max
            ),
            provenance_confidence=inputs.provenance_confidence,
        )
        return MemoryScore.compute(terms, weights or ScoreWeights.default())

    def _warn_if_degenerate_calibration(self, cos_min: float, cos_max: float) -> None:
        """HLD Section 12G: "fall back to the (cosine+1)/2 mapping and log a
        warning" -- this is the one place in the engine that performs I/O;
        `is_degenerate_calibration` (domain, pure) makes the decision, this
        method is only responsible for the logging side effect.
        """
        if is_degenerate_calibration(cos_min, cos_max):
            logger.warning(
                "memory score engine: degenerate cosine calibration, "
                "falling back to (cosine+1)/2 mapping",
                extra={"cos_min": cos_min, "cos_max": cos_max},
            )
