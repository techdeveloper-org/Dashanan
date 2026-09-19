"""InMemoryVectorIndex: Shape A `VectorIndexPort` adapter (ADR-007, HLD Section 5).

Shape A only -- flat, exact brute-force cosine search, which ADR-007 names as
"both exact and faster than HNSW's graph traversal overhead" and "the right
default" below n = 1e4 vectors ("Below n = 1e4 a flat index is both exact and
faster than HNSW's graph traversal overhead, and it avoids HNSW's build cost
entirely -- the right default for Profile A, where correctness-by-construction
beats asymptotics"). A Qdrant-backed Shape B adapter (HNSW, M=16 /
efConstruction=200 / efSearch=64, per-tenant collections) is a separate
adapter behind the same `VectorIndexPort` and is out of this story's scope --
see this story's dev report `judgment-call` notes for why it is not attempted
here (no Qdrant client dependency is added by this story, mirroring
DASH-STORY-002's precedent for not adding a Redis dependency for its own
Shape B adapter).

Physical partitioning (ADR-007, ADR-013, must-not-deviate item 1): each
tenant gets its OWN `dict` collection object, keyed by `tenant_id` at the
outer level only. There is no shared collection with a `tenant_id` filter
anywhere in this module -- an unscoped or cross-tenant search is
structurally unrepresentable, since `search` always resolves
`self._collections[tenant_id]` first and a tenant with no collection yet
simply has zero candidates, never another tenant's.
"""

from __future__ import annotations

from dashanan.domain.vector_entry import VectorEntry, cosine_similarity


class InMemoryVectorIndex:
    """Shape A `VectorIndexPort`: one exact flat cosine collection per tenant."""

    def __init__(self) -> None:
        self._collections: dict[str, dict[str, VectorEntry]] = {}

    def upsert(self, entry: VectorEntry) -> None:
        """Index or re-index one vector entry into its own tenant's collection."""
        collection = self._collections.setdefault(entry.tenant_id, {})
        collection[entry.item_id] = entry

    def search(
        self, tenant_id: str, query_vector: tuple[float, ...], top_k: int
    ) -> list[str]:
        """Exact flat cosine search over `tenant_id`'s own collection only.

        Args:
            tenant_id: The tenant whose collection to search. Never crosses
                into another tenant's collection (ADR-007/ADR-013).
            query_vector: The query embedding.
            top_k: Maximum number of `item_id`s to return.

        Returns:
            Up to `top_k` `item_id`s, cosine-similarity descending, ties
            broken by `item_id` ascending for deterministic ordering.

        Raises:
            ValueError: If `tenant_id` is blank, `query_vector` is empty, or
                `top_k` is not positive.
        """
        if not tenant_id.strip():
            raise ValueError("InMemoryVectorIndex.search requires a non-blank tenant_id")
        if not query_vector:
            raise ValueError("InMemoryVectorIndex.search requires a non-empty query_vector")
        if top_k <= 0:
            raise ValueError(f"InMemoryVectorIndex.search requires top_k > 0, got {top_k}")

        collection = self._collections.get(tenant_id, {})
        scored = [
            (cosine_similarity(query_vector, entry.vector), item_id)
            for item_id, entry in collection.items()
        ]
        scored.sort(key=lambda pair: (-pair[0], pair[1]))
        return [item_id for _score, item_id in scored[:top_k]]

    def delete(self, tenant_id: str, item_id: str) -> None:
        """Remove one vector entry from `tenant_id`'s own collection, if present.

        New port capability (DASH-STORY-007 remediation): DASH-STORY-004's
        DPDP erasure cascade (DSHN-58) uses this for its Zone-6 leg. Never
        touches another tenant's collection (ADR-007/ADR-013).

        Args:
            tenant_id: The tenant whose collection to delete from.
            item_id: Identifier of the entry to remove.

        Raises:
            ValueError: If `tenant_id` is blank.
        """
        if not tenant_id.strip():
            raise ValueError("InMemoryVectorIndex.delete requires a non-blank tenant_id")
        collection = self._collections.get(tenant_id)
        if collection is not None:
            collection.pop(item_id, None)
