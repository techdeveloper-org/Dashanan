"""Reciprocal Rank Fusion for Zone 6 hybrid retrieval (HLD Section 5, ADR-008).

Locked formula, cited verbatim from the HLD's DSA Choices table (Section 5,
"Zone 6 Retrieval-Index" row) and ADR-008: `score = sum_r 1/(60 + rank_r(d))`,
`k = 60`. Must-not-deviate item 4 (ar1_assignments.json, AR1-007): "RRF fusion,
not score normalization across scales." This module fuses RANKS only -- it
never reads a raw cosine or BM25 score -- which is exactly the mechanism
ADR-008 chose to sidestep the cross-scale mixing problem it documents
(rejecting `alpha * cosine + (1-alpha) * bm25_normalized` for requiring
per-corpus tuning that silently misweights as corpus statistics drift).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

DEFAULT_RRF_K = 60
"""HLD Section 5 / ADR-008: `k = 60`, locked -- not a tunable default here."""


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[str]], k: int = DEFAULT_RRF_K
) -> dict[str, float]:
    """Fuse one or more rank-ordered candidate lists into one RRF score per item.

    Args:
        rankings: One ranked list of `item_id`s per retriever (e.g. one from
            the vector index, one from the lexical index), each already
            ordered best-first (rank 1 = best). An item absent from a
            ranking simply does not contribute that retriever's term to its
            sum -- this is reciprocal rank fusion's defined behaviour for a
            candidate only one retriever surfaced, not a gap this function
            needs to fill in.
        k: The RRF constant. Locked at 60 by ADR-008 / HLD Section 5; a
            caller overriding it is deviating from the locked formula.

    Returns:
        `item_id -> fused RRF score`, unsorted. Strictly positive for every
        item present in at least one input ranking.

    Raises:
        ValueError: If `k` is not positive, or any single ranking contains a
            duplicate `item_id` (rank would be ill-defined for that item).
    """
    if k <= 0:
        raise ValueError(f"reciprocal_rank_fusion requires k > 0, got {k}")

    fused: dict[str, float] = {}
    for ranking in rankings:
        seen: set[str] = set()
        for rank, item_id in enumerate(ranking, start=1):
            if item_id in seen:
                raise ValueError(
                    "reciprocal_rank_fusion received a ranking with duplicate "
                    f"item_id {item_id!r}; rank is ill-defined for a repeated item"
                )
            seen.add(item_id)
            fused[item_id] = fused.get(item_id, 0.0) + 1.0 / (k + rank)
    return fused


def min_max_normalize_fused(scores: Mapping[str, float]) -> dict[str, float]:
    """Min-max normalize an already-fused RRF score set to `[0, 1]` (ADR-008).

    Applies to the OUTPUT of `reciprocal_rank_fusion` only. Normalizing raw
    per-retriever cosine/BM25 scores onto a shared scale BEFORE fusion is
    exactly what must-not-deviate item 4 forbids (ADR-008's rejected "linear
    score combination" alternative) -- this function never sees a raw
    retriever score, only the fused RRF total.

    Per OAQ-5 (HLD Section 11): the result is relative to THIS candidate set
    and is valid for ranking during one assembly only -- it must never be
    persisted as the `TaskRelevance` score term, which stays raw normalized
    cosine computed elsewhere (`HybridRetrievalIndexRepository.fetch`'s
    docstring repeats this constraint at its call site).

    Args:
        scores: The `reciprocal_rank_fusion` output to normalize.

    Returns:
        `item_id -> normalized score in [0, 1]`. Every item maps to `1.0`
        when `scores` has exactly one distinct value (min == max), rather
        than dividing by zero. Empty input returns an empty mapping.
    """
    if not scores:
        return {}
    values = scores.values()
    lo, hi = min(values), max(values)
    spread = hi - lo
    if spread == 0.0:
        return {item_id: 1.0 for item_id in scores}
    return {item_id: (value - lo) / spread for item_id, value in scores.items()}
