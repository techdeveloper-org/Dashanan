"""HybridRetrievalIndexRepository: the Zone 6 `ZoneRepository` adapter (HLD 3.7, FR-006).

Implements the domain's frozen `ZoneRepository` Protocol (AR1-G2) by composing
a `VectorIndexPort` and a `LexicalIndexPort` (both Zone-6-scoped --
`retrieval_index_ports.py`) and fusing their candidate rankings with the
locked RRF formula (`retrieval_fusion.py`, ADR-008). This is Zone 6's own
realisation of HLD Section 5's DSA row: "HNSW graph ... BM25 inverted index
... RRF fusion: score = sum_r 1/(60 + rank_r(d))".

AC-006 (verbatim, ar1_assignments.json / SRS.md): "Item in an indexed zone
(2/3/4/5/8; Zone 1 and Zone 7 excluded) must appear in top-k fused
vector+lexical results for TaskRelevance."

AC-014 (verbatim): "Tenant A's query returns only A's physical partition;
UserAffinity never isolates tenants; no cache key leaks a B hit/miss signal."
This class never references `UserAffinity` anywhere in its fetch/fuse/rank
path (must-not-deviate item 2) and its own candidate cache (`_catalog`) is
keyed by a `tenant_id`-prefixed string (must-not-deviate item 3) -- see
`_catalog_key`.

REMEDIATION (P1, live-reproduced): the original `_catalog_key` built
`f'{tenant_id}:{item_id}'` with no delimiter escaping. Tenant "org" indexing
item_id="team:secret-report" produced the identical catalog key as tenant
"org:team" indexing item_id="secret-report" ("org:team:secret-report"),
letting one tenant's own `fetch()` -- searching strictly inside its own
physically-partitioned collection -- return another tenant's payload
verbatim. `_catalog_key` now backslash-escapes the separator (and any
literal backslash) inside each component before joining, so the join point
is always unambiguous regardless of what characters `tenant_id`/`item_id`
contain; identifiers that contain neither character (the common case)
produce byte-identical keys to the pre-fix scheme.

FOLLOW-UP (DASH-STORY-011 spike, re-confirmed Sprint 3): this class is a
Shape-A, in-memory `VectorIndexPort`/`LexicalIndexPort` composition -- it
imports no `qdrant-client` code today. Whoever replaces or extends this
class's `VectorIndexPort` with a real Qdrant-backed implementation (FR-015's
Shape B, once DASH-STORY-024's live server is the actual target) must pin
`qdrant-client` per `docs/spikes/dash-story-011-qdrant-client-compatibility-matrix.md`
rather than picking an arbitrary or "latest" version -- that document's
pinned-version recommendation is scoped to this repo's already-pinned
`qdrant/qdrant:v1.11.0` server image (`docker-compose.yml`, DASH-STORY-024)
and must be re-validated if that server image tag ever changes.
"""

from __future__ import annotations

from dashanan.domain.exceptions import ZoneRepositoryError
from dashanan.domain.lexical_doc import LexicalDoc
from dashanan.domain.memory_item import MemoryItem
from dashanan.domain.ports import ZoneQuery
from dashanan.domain.retrieval_fusion import (
    min_max_normalize_fused,
    reciprocal_rank_fusion,
)
from dashanan.domain.retrieval_index_ports import LexicalIndexPort, VectorIndexPort
from dashanan.domain.vector_entry import VectorEntry, VectorQuantization
from dashanan.domain.zone import ZoneId
from dashanan.infrastructure.in_memory_lexical_index import tokenize

DEFAULT_CANDIDATE_K = 100
"""Per-retriever candidate depth before fusion, independent of the caller's
final `max_items` -- fusing over a wider candidate pool than the final cap
is what lets an item that ranks, say, #3 lexically but only #40 by raw
vector rank still surface after RRF, instead of being cut before fusion
ever sees it."""

_CATALOG_KEY_SEPARATOR = ":"
_CATALOG_KEY_ESCAPE = "\\"


class HybridRetrievalIndexRepository:
    """Zone 6's `ZoneRepository` adapter: dual-index hybrid retrieval with RRF fusion.

    Tenant isolation (AC-014, ADR-013, must-not-deviate items 1-2): enforced
    structurally by delegating every search to `VectorIndexPort` /
    `LexicalIndexPort` implementations that themselves physically partition
    per tenant (see `InMemoryVectorIndex` / `InMemoryLexicalIndex`) -- this
    class never reads or writes any collection/index directly, and never
    computes or references `UserAffinity` anywhere in its own fetch/fuse/rank
    path (that score term belongs to the Scoring Service, not to zone-level
    isolation).
    """

    def __init__(
        self,
        vector_index: VectorIndexPort,
        lexical_index: LexicalIndexPort,
        candidate_k: int = DEFAULT_CANDIDATE_K,
    ) -> None:
        """Compose the adapter from its two index ports.

        Args:
            vector_index: The tenant-partitioned vector search port (ADR-007).
            lexical_index: The tenant-partitioned BM25 search port (ADR-008).
            candidate_k: Per-retriever candidate depth before RRF fusion.

        Raises:
            ValueError: If `candidate_k` is not positive.
        """
        if candidate_k <= 0:
            raise ValueError(f"candidate_k must be positive, got {candidate_k}")
        self._vector_index = vector_index
        self._lexical_index = lexical_index
        self._candidate_k = candidate_k
        self._catalog: dict[str, LexicalDoc] = {}

    def index_item(
        self,
        tenant_id: str,
        item_id: str,
        source_zone: ZoneId,
        text: str,
        vector: tuple[float, ...],
        model_id: str,
        quantization: VectorQuantization = VectorQuantization.FLOAT32,
    ) -> None:
        """Project one item from an owning zone into both Zone 6 surfaces (HLD 3.7).

        Writes a `VectorEntry` and a `LexicalDoc` for the same
        `(tenant_id, item_id)` pair. Zone 6 needs no independent durability
        (HLD 3.7: "fully rebuildable"), so a partial write here is
        recoverable by simply re-indexing from the owning zone, never by
        Zone 6 attempting its own compensating transaction.

        Args:
            tenant_id: Owning tenant, forwarded to both index ports.
            item_id: Identifier of the item within `source_zone`.
            source_zone: Must be one of `vector_entry.INDEXABLE_ZONES`
                (Zones 2/3/4/5/8); Zone 1 and Zone 7 are rejected.
            text: Tokenizable content for the lexical surface, and the value
                `fetch()` later returns as `MemoryItem.payload` for this
                item (Zone 6's only locally-held copy of the content it
                indexes -- see the module docstring's cross-reference to
                `lexical_doc.py`).
            vector: The embedding for the vector surface.
            model_id: Identifies which embedding adapter produced `vector`.
            quantization: Storage precision (HLD 12D). Defaults to float32.

        Raises:
            ValueError: If `source_zone` is not indexable, or any
                identifier/text/vector argument is invalid, propagated from
                `VectorEntry`/`LexicalDoc` construction.
        """
        vector_entry = VectorEntry(
            tenant_id=tenant_id,
            item_id=item_id,
            source_zone=source_zone,
            vector=vector,
            model_id=model_id,
            quantization=quantization,
        )
        lexical_doc = LexicalDoc(
            tenant_id=tenant_id,
            item_id=item_id,
            source_zone=source_zone,
            text=text,
        )
        self._vector_index.upsert(vector_entry)
        self._lexical_index.upsert(lexical_doc)
        self._catalog[self._catalog_key(tenant_id, item_id)] = lexical_doc

    def fetch(self, query: ZoneQuery) -> list[MemoryItem]:
        """Serve the `ZoneRepository` read contract for Zone 6 (AC-006).

        Runs both retrievers (vector, when `query.query_embedding` is
        supplied; lexical, when `query.task` is supplied) and fuses their
        rankings with `reciprocal_rank_fusion` (must-not-deviate item 4:
        never a raw-score blend). `MemoryItem.score` on the returned items
        is the min-max-normalized fused RANKING value for this one
        candidate set (ADR-008) -- per OAQ-5 this is valid for assembly-time
        ranking only and must never be read back as the persisted
        `TaskRelevance` term, which stays raw normalized cosine computed
        elsewhere (`retrieval_fusion.min_max_normalize_fused`'s docstring).

        Args:
            query: The stripped-down per-zone query from the Orchestrator.
                Neither `query_embedding` nor `task` is required, but at
                least one must be present for any candidate to surface -- a
                query with both absent always returns an empty list.

        Returns:
            Up to `query.max_items` `MemoryItem`s, fused-score descending.
            An item only surfaces if it was previously indexed via
            `index_item` for `query.tenant_id` -- a fused candidate whose
            catalog entry is missing (never written, or written for a
            different tenant) is silently excluded rather than raising,
            since the fusion and catalog lookups are two different systems
            of record by design (HLD 3.7: Zone 6 is a derived, rebuildable
            projection).

        Raises:
            ValueError: If `query.tenant_id` is blank (AC-014).
            dashanan.domain.exceptions.ZoneRepositoryError: If either
                underlying index search fails, so `MemoryOrchestrator` can
                degrade this zone rather than propagate (AC-009-SUPP-1).
        """
        if not query.tenant_id.strip():
            raise ValueError("fetch requires a non-blank tenant_id (AC-014)")

        try:
            vector_ranking = (
                self._vector_index.search(
                    query.tenant_id, tuple(query.query_embedding), self._candidate_k
                )
                if query.query_embedding
                else []
            )
            lexical_ranking = (
                self._lexical_index.search(query.tenant_id, query.task, self._candidate_k)
                if query.task
                else []
            )
        except Exception as exc:
            raise ZoneRepositoryError(
                zone=ZoneId.RETRIEVAL_INDEX.value, reason=str(exc)
            ) from exc

        fused = reciprocal_rank_fusion([vector_ranking, lexical_ranking])
        normalized = min_max_normalize_fused(fused)
        ranked_item_ids = sorted(
            normalized, key=lambda item_id: (-normalized[item_id], item_id)
        )

        items: list[MemoryItem] = []
        for item_id in ranked_item_ids[: query.max_items]:
            catalog_entry = self._catalog.get(self._catalog_key(query.tenant_id, item_id))
            if catalog_entry is None:
                continue
            token_count = max(1, len(tokenize(catalog_entry.text)))
            items.append(
                MemoryItem(
                    item_id=item_id,
                    source_zone=catalog_entry.source_zone,
                    payload=catalog_entry.text,
                    token_count=token_count,
                    score=normalized[item_id],
                )
            )
        return items

    def delete_item(self, tenant_id: str, item_id: str) -> None:
        """Remove one item from both Zone 6 surfaces and the local catalog.

        New port capability required by this remediation round (not part of
        the original fetch/upsert contract): DASH-STORY-004's own DPDP
        erasure cascade (DSHN-58) calls this for its Zone-6 leg once an item
        has been erased from its owning zone. Delegates to
        `VectorIndexPort.delete`/`LexicalIndexPort.delete` -- both
        tenant-partitioned, mirroring `upsert`'s per-port delegation -- and
        drops the matching `_catalog` entry.

        Idempotent: deleting a `(tenant_id, item_id)` pair that was never
        indexed here, or already deleted, is a silent no-op on every
        surface, mirroring the index ports' own idempotent delete semantics
        (and `upsert`'s tolerance of re-indexing).

        Args:
            tenant_id: Owning tenant, forwarded to both index ports.
            item_id: Identifier of the item to remove.

        Raises:
            ValueError: If `tenant_id` is blank (AC-014).
        """
        if not tenant_id.strip():
            raise ValueError("delete_item requires a non-blank tenant_id (AC-014)")
        self._vector_index.delete(tenant_id, item_id)
        self._lexical_index.delete(tenant_id, item_id)
        self._catalog.pop(self._catalog_key(tenant_id, item_id), None)

    @staticmethod
    def _escape_catalog_key_component(value: str) -> str:
        """Escape `_CATALOG_KEY_ESCAPE` and `_CATALOG_KEY_SEPARATOR` occurrences.

        Backslashes are escaped first (doubled), then any literal separator
        character is escaped -- the standard "escape then join" scheme that
        makes the join unambiguous: after escaping, the only unescaped
        `_CATALOG_KEY_SEPARATOR` character left in the final joined string
        is the one `_catalog_key` inserts between the two components, so a
        left-to-right, escape-aware scan always finds the same split point
        regardless of what `tenant_id`/`item_id` contain.
        """
        escaped_backslashes = value.replace(_CATALOG_KEY_ESCAPE, _CATALOG_KEY_ESCAPE * 2)
        return escaped_backslashes.replace(
            _CATALOG_KEY_SEPARATOR, _CATALOG_KEY_ESCAPE + _CATALOG_KEY_SEPARATOR
        )

    @staticmethod
    def _catalog_key(tenant_id: str, item_id: str) -> str:
        """Build a `tenant_id`-prefixed catalog key (must-not-deviate item 3, AC-014).

        No shared key can collide across tenants: two different tenants
        indexing the same `item_id` always produce two distinct keys, and
        -- P1 remediation -- a tenant_id/item_id pair that itself contains
        `_CATALOG_KEY_SEPARATOR` can never be reinterpreted as a different
        (tenant_id, item_id) split, because both components are escaped
        (`_escape_catalog_key_component`) before the fixed-position join.
        For example, tenant "org" + item_id "team:secret-report" now keys
        to "org:team\\:secret-report", never colliding with tenant
        "org:team" + item_id "secret-report", which keys to
        "org\\:team:secret-report" -- two distinct strings.
        """
        escaped_tenant_id = HybridRetrievalIndexRepository._escape_catalog_key_component(
            tenant_id
        )
        escaped_item_id = HybridRetrievalIndexRepository._escape_catalog_key_component(item_id)
        return f"{escaped_tenant_id}{_CATALOG_KEY_SEPARATOR}{escaped_item_id}"
