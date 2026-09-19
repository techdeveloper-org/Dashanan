"""Zone 6-scoped ports for the vector/lexical index adapters (HLD 3.7, ADR-007/008).

Deliberately NOT added to the frozen `dashanan.domain.ports` module (AR1-G2:
the `ZoneRepository` port is frozen for every story after DASH-STORY-001).
These two ports are this story's own seam between the domain's fusion logic
(`retrieval_fusion.py`) and Zone 6's index adapters, mirroring how
`sql_episodic_repository.py` keeps its own `SqlConnection`/`SqlCursor`
Protocols local rather than widening the shared ports module -- the same
DASH-STORY-003 convention applied here.

`delete` on both ports is a DASH-STORY-007 remediation addition (not part of
the original fetch/upsert contract): DASH-STORY-004's own DPDP erasure
cascade (DSHN-58) depends on it to implement its Zone-6 leg. It is deliberately
minimal -- delete-by-`tenant_id`+`item_id` -- and structurally consistent with
`upsert`/`search`'s own per-tenant delegation shape.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from dashanan.domain.lexical_doc import LexicalDoc
from dashanan.domain.vector_entry import VectorEntry


@runtime_checkable
class VectorIndexPort(Protocol):
    """One tenant's physical vector collection (ADR-007: never a shared index)."""

    def upsert(self, entry: VectorEntry) -> None:
        """Index or re-index one vector entry into its own tenant's collection."""
        ...

    def search(
        self, tenant_id: str, query_vector: tuple[float, ...], top_k: int
    ) -> list[str]:
        """Return up to `top_k` `item_id`s for `tenant_id`, best-match-first.

        Must search ONLY `tenant_id`'s own collection (ADR-007/ADR-013,
        must-not-deviate item 1) -- never a shared index with a tenant
        filter.
        """
        ...

    def delete(self, tenant_id: str, item_id: str) -> None:
        """Remove one vector entry from `tenant_id`'s own collection, if present.

        Must touch ONLY `tenant_id`'s own collection, mirroring `search`'s
        isolation. Idempotent: deleting an `item_id` that is not indexed (or
        a `tenant_id` with no collection yet) is a silent no-op.
        """
        ...


@runtime_checkable
class LexicalIndexPort(Protocol):
    """One tenant's physical BM25 lexical index (ADR-008/ADR-013)."""

    def upsert(self, doc: LexicalDoc) -> None:
        """Index or re-index one lexical document into its own tenant's index."""
        ...

    def search(self, tenant_id: str, query_text: str, top_k: int) -> list[str]:
        """Return up to `top_k` `item_id`s for `tenant_id`, best-BM25-match-first.

        Must search ONLY `tenant_id`'s own index (ADR-007/ADR-013,
        must-not-deviate item 1) -- never a shared index with a tenant
        filter.
        """
        ...

    def delete(self, tenant_id: str, item_id: str) -> None:
        """Remove one lexical document from `tenant_id`'s own index, if present.

        Must touch ONLY `tenant_id`'s own index, mirroring `search`'s
        isolation. Idempotent: deleting an `item_id` that is not indexed (or
        a `tenant_id` with no index yet) is a silent no-op.
        """
        ...
