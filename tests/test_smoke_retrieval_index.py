"""Smoke assertions for DASH-STORY-007, inline per the dev subtask scope.

The formal pytest suite covering every AC (AC-006, AC-006-COST-1, AC-014) is
the QA subtask's responsibility (see the story's `qa_prompt`). These checks
confirm the package is importable, wired correctly, and that the
must-not-deviate structural properties hold, ahead of that formal suite
landing -- the exact scope split `test_smoke_episodic.py` used for
DASH-STORY-003.

PII NOTE: per the dev_prompt's PII constraint, this suite's fixtures use only
short, generic English sentences to exercise BM25/cosine ranking -- never
anything resembling real conversational content -- and every `item_id` is an
opaque placeholder like "item-1", never paired with example raw content.

Runtime assumptions recorded per rule 33/40 test-roadmap conventions:
  - tenant_id: "tenant-a" / "tenant-b" for cross-tenant checks, otherwise
    "tenant-1".
  - No fixture seed is required; nothing in this suite is randomized.
"""

from __future__ import annotations

import pytest

from dashanan.domain.exceptions import ZoneRepositoryError
from dashanan.domain.lexical_doc import LexicalDoc
from dashanan.domain.ports import ZoneQuery
from dashanan.domain.retrieval_fusion import (
    DEFAULT_RRF_K,
    min_max_normalize_fused,
    reciprocal_rank_fusion,
)
from dashanan.domain.vector_entry import INDEXABLE_ZONES, VectorEntry, cosine_similarity
from dashanan.domain.zone import ZoneId
from dashanan.infrastructure.hybrid_retrieval_index_repository import (
    HybridRetrievalIndexRepository,
)
from dashanan.infrastructure.in_memory_lexical_index import InMemoryLexicalIndex
from dashanan.infrastructure.in_memory_vector_index import InMemoryVectorIndex

_DEFAULT_QUERY_EMBEDDING = [1.0, 0.0, 0.0]
_UNSET = object()


def _query(
    tenant_id: str = "tenant-1",
    task: str | None = "quick brown fox",
    query_embedding: object = _UNSET,
    max_items: int = 10,
) -> ZoneQuery:
    resolved_embedding = (
        _DEFAULT_QUERY_EMBEDDING if query_embedding is _UNSET else query_embedding
    )
    return ZoneQuery(
        tenant_id=tenant_id,
        task=task,
        query_embedding=resolved_embedding,
        max_items=max_items,
        min_provenance_conf=0.0,
    )


@pytest.fixture
def repo() -> HybridRetrievalIndexRepository:
    return HybridRetrievalIndexRepository(InMemoryVectorIndex(), InMemoryLexicalIndex())


class TestPackageWiring:
    """Baseline: the adapter constructs and is importable, before any AC-level suite."""

    def test_repository_constructs_with_in_memory_ports(self) -> None:
        repo = HybridRetrievalIndexRepository(InMemoryVectorIndex(), InMemoryLexicalIndex())
        assert repo is not None

    def test_repository_satisfies_zone_repository_protocol(
        self, repo: HybridRetrievalIndexRepository
    ) -> None:
        from dashanan.domain.ports import ZoneRepository

        assert isinstance(repo, ZoneRepository)


class TestAC006ItemAppearsInFusedResults:
    """AC-006 (verbatim): item in an indexed zone must appear in top-k fused results."""

    def test_indexed_item_surfaces_via_vector_and_lexical_agreement(
        self, repo: HybridRetrievalIndexRepository
    ) -> None:
        repo.index_item(
            "tenant-1", "item-1", ZoneId.EPISODIC, "the quick brown fox jumps",
            (1.0, 0.0, 0.0), "model-x",
        )

        results = repo.fetch(_query())

        assert [item.item_id for item in results] == ["item-1"]
        assert results[0].source_zone == ZoneId.EPISODIC

    def test_item_surfaces_from_lexical_match_alone_when_vector_absent(
        self, repo: HybridRetrievalIndexRepository
    ) -> None:
        repo.index_item(
            "tenant-1", "item-1", ZoneId.SEMANTIC, "the quick brown fox jumps",
            (0.0, 0.0, 1.0), "model-x",
        )

        results = repo.fetch(_query(query_embedding=None))

        assert [item.item_id for item in results] == ["item-1"]

    def test_item_surfaces_from_vector_match_alone_when_task_absent(
        self, repo: HybridRetrievalIndexRepository
    ) -> None:
        repo.index_item(
            "tenant-1", "item-1", ZoneId.ENTITY, "unrelated lexical content here",
            (1.0, 0.0, 0.0), "model-x",
        )

        results = repo.fetch(_query(task=None))

        assert [item.item_id for item in results] == ["item-1"]

    @pytest.mark.parametrize("indexed_zone", sorted(INDEXABLE_ZONES, key=lambda z: z.value))
    def test_every_indexable_zone_surfaces_a_hit(
        self, repo: HybridRetrievalIndexRepository, indexed_zone: ZoneId
    ) -> None:
        repo.index_item(
            "tenant-1", "item-1", indexed_zone, "quick brown fox content",
            (1.0, 0.0, 0.0), "model-x",
        )

        results = repo.fetch(_query())

        assert [item.item_id for item in results] == ["item-1"]
        assert results[0].source_zone == indexed_zone

    def test_no_candidate_when_neither_task_nor_embedding_supplied(
        self, repo: HybridRetrievalIndexRepository
    ) -> None:
        repo.index_item(
            "tenant-1", "item-1", ZoneId.EPISODIC, "quick brown fox",
            (1.0, 0.0, 0.0), "model-x",
        )

        results = repo.fetch(_query(task=None, query_embedding=None))

        assert results == []

    def test_results_are_capped_at_max_items(
        self, repo: HybridRetrievalIndexRepository
    ) -> None:
        for i in range(5):
            repo.index_item(
                "tenant-1", f"item-{i}", ZoneId.EPISODIC, "quick brown fox jumps",
                (1.0, 0.0, 0.0), "model-x",
            )

        results = repo.fetch(_query(max_items=2))

        assert len(results) == 2


class TestMustNotDeviateZone1AndZone7Excluded:
    """Must-not-deviate item 5 (AR1-007): Zone 1 excluded; Zone 7 never a source."""

    def test_index_item_rejects_zone1_working(
        self, repo: HybridRetrievalIndexRepository
    ) -> None:
        with pytest.raises(ValueError, match="not an indexable zone"):
            repo.index_item(
                "tenant-1", "item-1", ZoneId.WORKING, "text", (1.0,), "model-x"
            )

    def test_index_item_rejects_zone7_provenance(
        self, repo: HybridRetrievalIndexRepository
    ) -> None:
        with pytest.raises(ValueError, match="not an indexable zone"):
            repo.index_item(
                "tenant-1", "item-1", ZoneId.PROVENANCE, "text", (1.0,), "model-x"
            )

    def test_vector_entry_rejects_zone1_directly(self) -> None:
        with pytest.raises(ValueError, match="not an indexable zone"):
            VectorEntry(
                tenant_id="tenant-1", item_id="item-1", source_zone=ZoneId.WORKING,
                vector=(1.0,), model_id="model-x",
            )

    def test_lexical_doc_rejects_zone7_directly(self) -> None:
        with pytest.raises(ValueError, match="not an indexable zone"):
            LexicalDoc(
                tenant_id="tenant-1", item_id="item-1", source_zone=ZoneId.PROVENANCE,
                text="text",
            )

    def test_working_and_provenance_are_absent_from_indexable_zones(self) -> None:
        assert ZoneId.WORKING not in INDEXABLE_ZONES
        assert ZoneId.PROVENANCE not in INDEXABLE_ZONES
        assert ZoneId.RETRIEVAL_INDEX not in INDEXABLE_ZONES


class TestAC014TenantIsolation:
    """AC-014 (verbatim): tenant A's query returns only A's physical partition."""

    def test_fetch_never_returns_another_tenants_item(
        self, repo: HybridRetrievalIndexRepository
    ) -> None:
        repo.index_item(
            "tenant-a", "item-a", ZoneId.EPISODIC, "quick brown fox jumps",
            (1.0, 0.0, 0.0), "model-x",
        )
        repo.index_item(
            "tenant-b", "item-b", ZoneId.EPISODIC, "quick brown fox jumps",
            (1.0, 0.0, 0.0), "model-x",
        )

        results_a = repo.fetch(_query(tenant_id="tenant-a"))
        results_b = repo.fetch(_query(tenant_id="tenant-b"))

        assert [item.item_id for item in results_a] == ["item-a"]
        assert [item.item_id for item in results_b] == ["item-b"]

    def test_vector_index_partitions_are_physically_separate_dicts(self) -> None:
        """ADR-007: "one collection per tenant", never a shared index + filter."""
        vec_idx = InMemoryVectorIndex()
        vec_idx.upsert(
            VectorEntry(
                tenant_id="tenant-a", item_id="item-a", source_zone=ZoneId.EPISODIC,
                vector=(1.0, 0.0), model_id="m",
            )
        )
        assert "tenant-b" not in vec_idx._collections
        assert list(vec_idx._collections["tenant-a"].keys()) == ["item-a"]

    def test_fetch_rejects_blank_tenant_id(
        self, repo: HybridRetrievalIndexRepository
    ) -> None:
        with pytest.raises(ValueError, match="tenant_id"):
            repo.fetch(_query(tenant_id=""))

    def test_no_module_in_this_story_references_user_affinity(self) -> None:
        """Must-not-deviate item 2: UserAffinity is never a tenant-isolation term."""
        import dashanan.infrastructure.hybrid_retrieval_index_repository as module

        assert "UserAffinity" not in dir(module.HybridRetrievalIndexRepository)

    def test_catalog_keys_are_tenant_id_prefixed(
        self, repo: HybridRetrievalIndexRepository
    ) -> None:
        """Must-not-deviate item 3: cache keys are tenant_id-prefixed."""
        repo.index_item(
            "tenant-a", "item-1", ZoneId.EPISODIC, "quick brown fox",
            (1.0, 0.0, 0.0), "model-x",
        )
        assert list(repo._catalog.keys()) == ["tenant-a:item-1"]

    def test_same_item_id_different_tenants_never_collides_in_catalog(
        self, repo: HybridRetrievalIndexRepository
    ) -> None:
        repo.index_item(
            "tenant-a", "shared-item-id", ZoneId.EPISODIC, "quick brown fox",
            (1.0, 0.0, 0.0), "model-x",
        )
        repo.index_item(
            "tenant-b", "shared-item-id", ZoneId.EPISODIC, "a slow turtle sleeps",
            (0.0, 1.0, 0.0), "model-x",
        )
        assert len(repo._catalog) == 2
        assert repo._catalog["tenant-a:shared-item-id"].text != (
            repo._catalog["tenant-b:shared-item-id"].text
        )


class TestMustNotDeviateRRFFusion:
    """Must-not-deviate item 4: RRF fusion, never score normalization across scales."""

    def test_rrf_formula_matches_sum_one_over_k_plus_rank(self) -> None:
        fused = reciprocal_rank_fusion([["a", "b", "c"]], k=60)
        assert fused["a"] == pytest.approx(1.0 / 61)
        assert fused["b"] == pytest.approx(1.0 / 62)
        assert fused["c"] == pytest.approx(1.0 / 63)

    def test_default_k_is_locked_at_60(self) -> None:
        assert DEFAULT_RRF_K == 60

    def test_scores_from_two_retrievers_sum(self) -> None:
        fused = reciprocal_rank_fusion([["a", "b"], ["b", "a"]], k=60)
        assert fused["a"] == pytest.approx(1.0 / 61 + 1.0 / 62)
        assert fused["b"] == pytest.approx(1.0 / 62 + 1.0 / 61)
        assert fused["a"] == pytest.approx(fused["b"])

    def test_item_present_in_only_one_ranking_still_scores(self) -> None:
        fused = reciprocal_rank_fusion([["a"], []], k=60)
        assert fused == {"a": pytest.approx(1.0 / 61)}

    def test_rejects_duplicate_item_id_within_one_ranking(self) -> None:
        with pytest.raises(ValueError, match="duplicate"):
            reciprocal_rank_fusion([["a", "a"]])

    def test_rejects_non_positive_k(self) -> None:
        with pytest.raises(ValueError, match="k > 0"):
            reciprocal_rank_fusion([["a"]], k=0)

    def test_min_max_normalize_maps_to_unit_interval(self) -> None:
        normalized = min_max_normalize_fused({"a": 0.1, "b": 0.3, "c": 0.2})
        assert normalized["a"] == pytest.approx(0.0)
        assert normalized["b"] == pytest.approx(1.0)
        assert normalized["c"] == pytest.approx(0.5)

    def test_min_max_normalize_single_value_maps_to_one_not_nan(self) -> None:
        normalized = min_max_normalize_fused({"a": 0.5, "b": 0.5})
        assert normalized == {"a": 1.0, "b": 1.0}

    def test_min_max_normalize_empty_input_returns_empty(self) -> None:
        assert min_max_normalize_fused({}) == {}


class TestCosineSimilarity:
    def test_identical_vectors_are_similarity_one(self) -> None:
        assert cosine_similarity((1.0, 2.0, 3.0), (1.0, 2.0, 3.0)) == pytest.approx(1.0)

    def test_orthogonal_vectors_are_similarity_zero(self) -> None:
        assert cosine_similarity((1.0, 0.0), (0.0, 1.0)) == pytest.approx(0.0)

    def test_zero_vector_returns_zero_not_a_division_error(self) -> None:
        assert cosine_similarity((0.0, 0.0), (1.0, 1.0)) == 0.0

    def test_mismatched_lengths_raise(self) -> None:
        with pytest.raises(ValueError, match="equal-length"):
            cosine_similarity((1.0,), (1.0, 2.0))


class TestBoundaryAndNegativeCases:
    """Boundary/negative cases per rule 33/40 test-roadmap conventions."""

    def test_vector_entry_rejects_blank_tenant_id(self) -> None:
        with pytest.raises(ValueError, match="tenant_id"):
            VectorEntry(
                tenant_id="", item_id="item-1", source_zone=ZoneId.EPISODIC,
                vector=(1.0,), model_id="m",
            )

    def test_vector_entry_rejects_empty_vector(self) -> None:
        with pytest.raises(ValueError, match="vector"):
            VectorEntry(
                tenant_id="tenant-1", item_id="item-1", source_zone=ZoneId.EPISODIC,
                vector=(), model_id="m",
            )

    def test_lexical_doc_rejects_blank_text(self) -> None:
        with pytest.raises(ValueError, match="text"):
            LexicalDoc(
                tenant_id="tenant-1", item_id="item-1", source_zone=ZoneId.EPISODIC,
                text="   ",
            )

    def test_hybrid_repository_rejects_non_positive_candidate_k(self) -> None:
        with pytest.raises(ValueError, match="candidate_k"):
            HybridRetrievalIndexRepository(
                InMemoryVectorIndex(), InMemoryLexicalIndex(), candidate_k=0
            )

    def test_vector_index_search_rejects_blank_tenant_id(self) -> None:
        with pytest.raises(ValueError, match="tenant_id"):
            InMemoryVectorIndex().search("", (1.0,), 5)

    def test_lexical_index_search_rejects_non_positive_top_k(self) -> None:
        with pytest.raises(ValueError, match="top_k"):
            InMemoryLexicalIndex().search("tenant-1", "text", 0)

    def test_lexical_index_search_returns_empty_for_unknown_tenant(self) -> None:
        assert InMemoryLexicalIndex().search("tenant-1", "quick fox", 5) == []

    def test_fetch_excludes_fused_item_missing_from_catalog(
        self, repo: HybridRetrievalIndexRepository
    ) -> None:
        """A candidate the fusion surfaces but this tenant never indexed via
        `index_item` (e.g. written directly to a port bypassing the
        repository) must not raise or fabricate a payload."""
        repo._vector_index.upsert(
            VectorEntry(
                tenant_id="tenant-1", item_id="ghost", source_zone=ZoneId.EPISODIC,
                vector=(1.0, 0.0, 0.0), model_id="m",
            )
        )
        results = repo.fetch(_query(task=None))
        assert results == []

    def test_fetch_wraps_index_failure_as_zone_repository_error(
        self, repo: HybridRetrievalIndexRepository
    ) -> None:
        class _BrokenVectorIndex:
            def upsert(self, entry: object) -> None:  # pragma: no cover - unused
                raise NotImplementedError

            def search(self, tenant_id: str, query_vector: object, top_k: int) -> list[str]:
                raise RuntimeError("index unavailable")

        broken_repo = HybridRetrievalIndexRepository(_BrokenVectorIndex(), InMemoryLexicalIndex())

        with pytest.raises(ZoneRepositoryError):
            broken_repo.fetch(_query())
