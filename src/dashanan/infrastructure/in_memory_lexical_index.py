"""InMemoryLexicalIndex: Shape A `LexicalIndexPort` adapter (ADR-008, HLD Section 5).

Shape A only -- an in-process BM25 inverted index, standing in for SQLite
FTS5 (ADR-008's actual Shape A choice) the same way `WorkingMemoryLRURepository`
stands in for Redis in Shape B: same read contract, no new database
dependency added by this story. A Shape B OpenSearch-backed adapter is a
separate adapter behind the same `LexicalIndexPort` and out of this story's
scope.

BM25 parameters (`k1=1.5`, `b=0.75`, `BM25_K1`/`BM25_B` below) are the
standard defaults from the original Okapi BM25 formulation. The HLD's DSA
Choices table (Section 5) names "BM25 scoring" as the locked algorithm but
does not pin `k1`/`b` to specific values the way RRF's `k=60` IS locked --
flagged as a judgment call in this story's dev report.

Physical partitioning (ADR-008, ADR-013, must-not-deviate item 1): each
tenant gets its own inverted index (`_TenantLexicalIndex` instance), exactly
mirroring `InMemoryVectorIndex`'s per-tenant collection structure.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

from dashanan.domain.lexical_doc import LexicalDoc

_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")

BM25_K1 = 1.5
BM25_B = 0.75


def tokenize(text: str) -> list[str]:
    """Lowercase, alphanumeric-run tokenization shared by indexing and querying."""
    return _TOKEN_PATTERN.findall(text.lower())


@dataclass
class _TenantLexicalIndex:
    """One tenant's own inverted index -- never shared across tenants."""

    docs: dict[str, LexicalDoc] = field(default_factory=dict)
    doc_term_counts: dict[str, Counter[str]] = field(default_factory=dict)
    doc_lengths: dict[str, int] = field(default_factory=dict)
    postings: dict[str, set[str]] = field(default_factory=dict)

    def upsert(self, doc: LexicalDoc) -> None:
        """Tokenize and (re)index one document, clearing its prior postings first."""
        self._remove(doc.item_id)
        terms = tokenize(doc.text)
        counts = Counter(terms)
        self.docs[doc.item_id] = doc
        self.doc_term_counts[doc.item_id] = counts
        self.doc_lengths[doc.item_id] = len(terms)
        for term in counts:
            self.postings.setdefault(term, set()).add(doc.item_id)

    def search(self, query_text: str, top_k: int) -> list[str]:
        """Score every candidate document against `query_text` by Okapi BM25."""
        query_terms = tokenize(query_text)
        if not query_terms or not self.docs:
            return []

        doc_count = len(self.docs)
        avgdl = sum(self.doc_lengths.values()) / doc_count if doc_count else 0.0
        scores: dict[str, float] = {}
        for term in set(query_terms):
            candidates = self.postings.get(term)
            if not candidates:
                continue
            document_frequency = len(candidates)
            idf = math.log(
                (doc_count - document_frequency + 0.5) / (document_frequency + 0.5) + 1.0
            )
            for item_id in candidates:
                term_frequency = self.doc_term_counts[item_id][term]
                doc_length = self.doc_lengths[item_id]
                length_norm = 1 - BM25_B + BM25_B * (doc_length / avgdl if avgdl else 0.0)
                denominator = term_frequency + BM25_K1 * length_norm
                contribution = (
                    idf * (term_frequency * (BM25_K1 + 1) / denominator)
                    if denominator
                    else 0.0
                )
                scores[item_id] = scores.get(item_id, 0.0) + contribution

        ranked = sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))
        return [item_id for item_id, _score in ranked[:top_k]]

    def _remove(self, item_id: str) -> None:
        self.docs.pop(item_id, None)
        counts = self.doc_term_counts.pop(item_id, None)
        self.doc_lengths.pop(item_id, None)
        if not counts:
            return
        for term in counts:
            postings = self.postings.get(term)
            if postings is None:
                continue
            postings.discard(item_id)
            if not postings:
                del self.postings[term]


class InMemoryLexicalIndex:
    """Shape A `LexicalIndexPort`: one BM25 inverted index per tenant."""

    def __init__(self) -> None:
        self._tenants: dict[str, _TenantLexicalIndex] = {}

    def upsert(self, doc: LexicalDoc) -> None:
        """Index or re-index one lexical document into its own tenant's index."""
        tenant_index = self._tenants.setdefault(doc.tenant_id, _TenantLexicalIndex())
        tenant_index.upsert(doc)

    def search(self, tenant_id: str, query_text: str, top_k: int) -> list[str]:
        """BM25 search over `tenant_id`'s own index only.

        Args:
            tenant_id: The tenant whose index to search. Never crosses into
                another tenant's index (ADR-008/ADR-013).
            query_text: The task/query text to score candidates against.
            top_k: Maximum number of `item_id`s to return.

        Returns:
            Up to `top_k` `item_id`s, BM25-score descending, ties broken by
            `item_id` ascending. Empty when `tenant_id` has no index yet or
            `query_text` tokenizes to nothing.

        Raises:
            ValueError: If `tenant_id` is blank or `top_k` is not positive.
        """
        if not tenant_id.strip():
            raise ValueError("InMemoryLexicalIndex.search requires a non-blank tenant_id")
        if top_k <= 0:
            raise ValueError(f"InMemoryLexicalIndex.search requires top_k > 0, got {top_k}")

        tenant_index = self._tenants.get(tenant_id)
        if tenant_index is None:
            return []
        return tenant_index.search(query_text, top_k)

    def delete(self, tenant_id: str, item_id: str) -> None:
        """Remove one lexical document from `tenant_id`'s own index, if present.

        New port capability (DASH-STORY-007 remediation): DASH-STORY-004's
        DPDP erasure cascade (DSHN-58) uses this for its Zone-6 leg. Never
        touches another tenant's index (ADR-008/ADR-013).

        Args:
            tenant_id: The tenant whose index to delete from.
            item_id: Identifier of the document to remove.

        Raises:
            ValueError: If `tenant_id` is blank.
        """
        if not tenant_id.strip():
            raise ValueError("InMemoryLexicalIndex.delete requires a non-blank tenant_id")
        tenant_index = self._tenants.get(tenant_id)
        if tenant_index is not None:
            tenant_index._remove(item_id)
