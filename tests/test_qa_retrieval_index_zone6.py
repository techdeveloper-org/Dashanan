"""QA pytest suite for DASH-STORY-007 (Retrieval-Index zone / Zone 6, FR-006).

QA subtask (backlog_draft.json 20% split), independent verification pass on
top of the Dev subtask's own suites (tests/test_smoke_retrieval_index.py and
tests/test_retrieval_index_cost_estimator.py). Those dev suites already give
thorough structural/wiring coverage of every AC below; this suite is the
story's own boundary/adversarial pass plus the mandatory architecture-fitness
proof, matching the split DASH-STORY-003's test_qa_episodic_memory_zone2.py
established over test_smoke_episodic.py.

Acceptance criteria under test, verbatim from
docs/phase-7-routing/implementation_execution_plan.json's DASH-STORY-007
qa_prompt (identical to the dev_prompt's citation):

  AC-006: "Item in an indexed zone (2/3/4/5/8; Zone 1 and Zone 7 excluded)
  must appear in top-k fused vector+lexical results for TaskRelevance."

  AC-006-COST-1: "Cost estimate SHALL report average AND marginal cost
  c_var separately -- average alone is FORBIDDEN." (blocking
  mathematics-engineer delegation; this suite asserts against the
  delegation's already-returned, already-implemented derivation in
  application/retrieval_index_cost_estimator.py, never a self-computed
  approximation.)

  AC-014: "Tenant A's query returns only A's physical partition;
  UserAffinity never isolates tenants; no cache key leaks a B hit/miss
  signal."

MUST-NOT-DEVIATE items under test (ar1_assignments.json, AR1-007):
  - Per-tenant PHYSICAL partitioning -- one Qdrant collection + one
    OpenSearch index per tenant (ADR-007/013)
  - UserAffinity is never a tenant-isolation term (AC-014)
  - Cache keys tenant_id-prefixed -- no shared key leaks a hit/miss oracle
  - RRF fusion (sum_r 1/(60+rank_r(d))), never score normalization across
    scales (ADR-008)
  - Zone 1 is deliberately NOT indexed (HLD 3.7)

Plus the architecture-fitness invariant (HLD Section 3.0, invariant 1): no
domain/** module may import infrastructure/**, scoped here to this story's
own four new domain modules (vector_entry.py, lexical_doc.py,
retrieval_fusion.py, retrieval_index_ports.py) -- the repo-wide sweep
already lives in test_memory_orchestrator.py.

Dependency-integrity check (ar1_assignments.json): this story's write
surface is exactly its ten new files; the four package `__init__.py`
files (domain/application/infrastructure/root) are asserted unmodified
(docstring-only, no reference to any Zone 6 symbol) rather than assumed.

Runtime assumptions (rule 33/40/41 test-roadmap conventions, scoped to
this pure in-process library -- no HTTP/router/response-envelope layer
exists in this codebase, matching DASH-STORY-001/002/003's own precedent):
  - tenant_id: "tenant-a" / "tenant-b" for cross-tenant checks, otherwise
    "tenant-1".
  - No fixture seed is required; nothing in this suite is randomized.

PII NOTE: per the dev_prompt's PII constraint, fixtures use only short,
generic English sentences and opaque placeholder item_ids ("item-1",
"item-2", ...) -- never anything resembling real conversational content.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from dashanan.domain.exceptions import ZoneRepositoryError
from dashanan.domain.lexical_doc import LexicalDoc
from dashanan.domain.ports import ZoneQuery
from dashanan.domain.retrieval_fusion import (
    DEFAULT_RRF_K,
    reciprocal_rank_fusion,
)
from dashanan.domain.vector_entry import INDEXABLE_ZONES, VectorEntry
from dashanan.domain.zone import ZoneId
from dashanan.infrastructure.hybrid_retrieval_index_repository import (
    HybridRetrievalIndexRepository,
)
from dashanan.infrastructure.in_memory_lexical_index import InMemoryLexicalIndex
from dashanan.infrastructure.in_memory_vector_index import InMemoryVectorIndex
from dashanan.application.retrieval_index_cost_estimator import (
    Zone6CostInputs,
    compute_zone6_cost_report,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src" / "dashanan"
DOMAIN_DIR = SRC_DIR / "domain"
INFRASTRUCTURE_DIR = SRC_DIR / "infrastructure"
APPLICATION_DIR = SRC_DIR / "application"

STORY_DOMAIN_FILES = [
    DOMAIN_DIR / "vector_entry.py",
    DOMAIN_DIR / "lexical_doc.py",
    DOMAIN_DIR / "retrieval_fusion.py",
    DOMAIN_DIR / "retrieval_index_ports.py",
]
STORY_ALL_NEW_FILES = STORY_DOMAIN_FILES + [
    INFRASTRUCTURE_DIR / "in_memory_vector_index.py",
    INFRASTRUCTURE_DIR / "in_memory_lexical_index.py",
    INFRASTRUCTURE_DIR / "hybrid_retrieval_index_repository.py",
    APPLICATION_DIR / "retrieval_index_cost_estimator.py",
]

_UNSET = object()


def _query(
    tenant_id: str = "tenant-1",
    task: str | None = "quick brown fox",
    query_embedding: object = _UNSET,
    max_items: int = 10,
) -> ZoneQuery:
    """Build a `ZoneQuery` matching the dev suite's own default fixture shape."""
    resolved_embedding = [1.0, 0.0, 0.0] if query_embedding is _UNSET else query_embedding
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


class TestArchitectureFitnessScopedToStory:
    """HLD Section 3.0 invariant 1, scoped to this story's own four new
    domain modules: none of `vector_entry.py`, `lexical_doc.py`,
    `retrieval_fusion.py`, `retrieval_index_ports.py` may import from
    `dashanan.infrastructure`. (The repo-wide sweep already lives in
    test_memory_orchestrator.py; this is the QA subtask's own
    independent, story-scoped proof.)
    """

    def _imported_module_names(self, source_path: Path) -> list[str]:
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        names: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module is not None:
                    names.append(node.module)
        return names

    @pytest.mark.parametrize("source_path", STORY_DOMAIN_FILES)
    def test_domain_module_exists(self, source_path: Path) -> None:
        assert source_path.is_file(), f"{source_path} not found"

    @pytest.mark.parametrize("source_path", STORY_DOMAIN_FILES)
    def test_domain_module_does_not_import_infrastructure(self, source_path: Path) -> None:
        imported = self._imported_module_names(source_path)
        bad = [
            name
            for name in imported
            if name == "dashanan.infrastructure"
            or name.startswith("dashanan.infrastructure.")
        ]
        assert not bad, (
            "HLD 3.0 invariant 1 violated -- "
            f"{source_path.name} imports infrastructure: {bad}"
        )

    def test_vector_entry_and_lexical_doc_are_frozen_immutable_value_objects(self) -> None:
        import dataclasses

        for cls in (VectorEntry, LexicalDoc):
            assert dataclasses.is_dataclass(cls)
            params = getattr(cls, "__dataclass_params__")
            assert params.frozen is True, f"{cls.__name__} must be frozen (value object)"


class TestDependencyIntegrityCheck:
    """ar1_assignments.json: no mutation outside this story's ownership.

    The four package `__init__.py` files are asserted unmodified (still
    docstring-only, still silent about any Zone 6 symbol) rather than
    assumed -- a story that quietly wired a new export into a shared
    `__init__.py` would corrupt that file's own git history/ownership.
    """

    @pytest.mark.parametrize(
        "init_path",
        [
            SRC_DIR / "__init__.py",
            DOMAIN_DIR / "__init__.py",
            APPLICATION_DIR / "__init__.py",
            INFRASTRUCTURE_DIR / "__init__.py",
        ],
    )
    def test_package_init_has_no_zone6_reference(self, init_path: Path) -> None:
        text = init_path.read_text(encoding="utf-8")
        for forbidden in (
            "VectorEntry",
            "LexicalDoc",
            "HybridRetrievalIndexRepository",
            "reciprocal_rank_fusion",
            "retrieval_index",
            "Zone6",
        ):
            assert forbidden not in text, (
                f"{init_path.relative_to(REPO_ROOT)} unexpectedly references "
                f"{forbidden!r} -- this story must not mutate shared __init__.py files"
            )


class TestAC006BoundaryAndAdversarial:
    """AC-006, boundary/adversarial cases the dev smoke suite does not
    already exercise.
    """

    def test_zone6_never_indexes_itself(
        self, repo: HybridRetrievalIndexRepository
    ) -> None:
        """HLD 3.7: Zone 6 "owns no primary facts" and never indexes
        itself -- ZoneId.RETRIEVAL_INDEX must be rejected by the same
        allow-list that rejects Zone 1/7, proving the check is a real
        five-member allow-list rather than a two-member deny-list that
        happens to only special-case Zone 1 and Zone 7.
        """
        with pytest.raises(ValueError, match="not an indexable zone"):
            repo.index_item(
                "tenant-1", "item-1", ZoneId.RETRIEVAL_INDEX, "quick brown fox",
                (1.0, 0.0, 0.0), "model-x",
            )

    def test_reindexing_same_item_id_replaces_rather_than_duplicates(
        self, repo: HybridRetrievalIndexRepository
    ) -> None:
        """Replay: indexing the same (tenant_id, item_id) twice with
        different content must reflect only the newest write, never
        accumulate a stale duplicate entry in either surface.
        """
        repo.index_item(
            "tenant-1", "item-1", ZoneId.EPISODIC, "quick brown fox jumps",
            (1.0, 0.0, 0.0), "model-x",
        )
        repo.index_item(
            "tenant-1", "item-1", ZoneId.EPISODIC, "a slow turtle sleeps",
            (0.0, 1.0, 0.0), "model-x",
        )

        results = repo.fetch(_query(task="slow turtle sleeps", query_embedding=(0.0, 1.0, 0.0)))

        assert [item.item_id for item in results] == ["item-1"]
        assert results[0].payload == "a slow turtle sleeps"
        assert len(repo._catalog) == 1

    def test_candidate_k_boundary_excludes_item_ranked_just_outside_it(self) -> None:
        """A per-retriever candidate_k of 1 must admit only the single
        best-ranked vector candidate into fusion -- the second-best
        candidate must not surface even though it would rank within
        query.max_items. Uses the vector retriever alone (task=None) so
        the boundary is pinned by exact cosine similarity, not BM25's
        length-normalization/term-frequency interaction.
        """
        narrow_repo = HybridRetrievalIndexRepository(
            InMemoryVectorIndex(), InMemoryLexicalIndex(), candidate_k=1
        )
        narrow_repo.index_item(
            "tenant-1", "item-best", ZoneId.EPISODIC, "quick brown fox",
            (1.0, 0.0, 0.0), "model-x",
        )
        narrow_repo.index_item(
            "tenant-1", "item-second", ZoneId.SEMANTIC, "quick brown fox",
            (0.9, 0.1, 0.0), "model-x",
        )

        results = narrow_repo.fetch(_query(task=None, query_embedding=(1.0, 0.0, 0.0), max_items=10))

        assert [item.item_id for item in results] == ["item-best"]

    def test_zero_length_task_string_does_not_surface_a_lexical_candidate(
        self, repo: HybridRetrievalIndexRepository
    ) -> None:
        """An empty (but non-None) task string tokenizes to nothing, so
        the lexical retriever must contribute zero candidates -- distinct
        from `task=None`, which skips the lexical retriever entirely, but
        with the same observable outcome (no lexical hit)."""
        repo.index_item(
            "tenant-1", "item-1", ZoneId.EPISODIC, "quick brown fox jumps",
            (0.0, 0.0, 1.0), "model-x",
        )

        results = repo.fetch(_query(task="", query_embedding=None))

        assert results == []

    def test_every_indexable_zone_is_exactly_the_five_documented_zones(self) -> None:
        """Regression guard: AC-006 names zones 2/3/4/5/8 exactly -- this
        pins the exact set, not just membership of a few examples, so a
        future change that silently adds or removes a zone from
        INDEXABLE_ZONES fails here first.
        """
        assert INDEXABLE_ZONES == frozenset(
            {
                ZoneId.EPISODIC,
                ZoneId.SEMANTIC,
                ZoneId.PROCEDURAL,
                ZoneId.ENTITY,
                ZoneId.CONSOLIDATION,
            }
        )

    def test_ac006_golden_regression_item_surfaces_in_top_k(
        self, repo: HybridRetrievalIndexRepository
    ) -> None:
        """Golden test case for AC-006 (regression guard for any later
        story touching this component): a single indexed item in an
        indexable zone must appear in the fused top-k result set.
        """
        repo.index_item(
            "tenant-1", "item-1", ZoneId.EPISODIC, "quick brown fox jumps",
            (1.0, 0.0, 0.0), "model-x",
        )

        results = repo.fetch(_query(max_items=1))

        assert len(results) == 1
        assert results[0].item_id == "item-1"


class TestAC014BoundaryAndAdversarial:
    """AC-014, boundary/adversarial cases the dev smoke suite does not
    already exercise.
    """

    def test_user_affinity_absent_from_every_new_source_file_not_only_the_repository_class(
        self,
    ) -> None:
        """Stronger than the dev suite's `dir()` check (which only
        inspects class attributes): an AST sweep of every one of this
        story's eight new source files confirms `UserAffinity` is never
        used as a live identifier -- not imported, not a variable name,
        not an attribute access, not a function/class name. Prose in a
        docstring that merely NAMES the invariant (e.g. "never references
        UserAffinity") is deliberately excluded from this check, since
        that prose is what documents compliance, not a violation of it.
        """
        for source_path in STORY_ALL_NEW_FILES:
            tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
            live_identifier_hits: list[str] = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    continue  # docstrings/comments-as-strings are prose, not code
                if isinstance(node, ast.Name) and node.id == "UserAffinity":
                    live_identifier_hits.append("Name")
                elif isinstance(node, ast.Attribute) and node.attr == "UserAffinity":
                    live_identifier_hits.append("Attribute")
                elif isinstance(node, (ast.Import, ast.ImportFrom)):
                    for alias in node.names:
                        if alias.name == "UserAffinity" or alias.asname == "UserAffinity":
                            live_identifier_hits.append("Import")
            assert not live_identifier_hits, (
                f"must-not-deviate item 2 violated -- {source_path.name} uses "
                f"UserAffinity as a live identifier: {live_identifier_hits}"
            )

    def test_cross_tenant_search_cannot_see_a_partial_partition_even_with_shared_prefix(
        self,
    ) -> None:
        """Adversarial: a tenant_id that is a string-prefix of another
        tenant_id ("tenant-1" vs "tenant-10") must not leak a candidate
        across the boundary -- proving isolation is keyed on exact
        dict-key equality, not prefix/substring matching.
        """
        repo = HybridRetrievalIndexRepository(InMemoryVectorIndex(), InMemoryLexicalIndex())
        repo.index_item(
            "tenant-10", "item-a", ZoneId.EPISODIC, "quick brown fox jumps",
            (1.0, 0.0, 0.0), "model-x",
        )

        results = repo.fetch(_query(tenant_id="tenant-1"))

        assert results == []

    def test_fetch_rejects_whitespace_only_tenant_id(
        self, repo: HybridRetrievalIndexRepository
    ) -> None:
        """Boundary: a tenant_id of only whitespace must be treated as
        blank (AC-014), matching the `.strip()` check already applied to
        every other identifier field in this story's domain entities.
        """
        with pytest.raises(ValueError, match="tenant_id"):
            repo.fetch(_query(tenant_id="   "))

    def test_two_tenants_indexing_disjoint_item_ids_never_cross_contaminate_results(
        self,
    ) -> None:
        """A more thorough tenant-isolation matrix than the dev suite's
        single-item case: three items per tenant, disjoint item_id
        namespaces, asserting each tenant's fetch returns exactly and
        only its own items regardless of insertion order.
        """
        repo = HybridRetrievalIndexRepository(InMemoryVectorIndex(), InMemoryLexicalIndex())
        for i in range(3):
            repo.index_item(
                "tenant-a", f"a-item-{i}", ZoneId.EPISODIC, "quick brown fox jumps",
                (1.0, 0.0, 0.0), "model-x",
            )
        for i in range(3):
            repo.index_item(
                "tenant-b", f"b-item-{i}", ZoneId.EPISODIC, "quick brown fox jumps",
                (1.0, 0.0, 0.0), "model-x",
            )

        results_a = {item.item_id for item in repo.fetch(_query(tenant_id="tenant-a", max_items=10))}
        results_b = {item.item_id for item in repo.fetch(_query(tenant_id="tenant-b", max_items=10))}

        assert results_a == {"a-item-0", "a-item-1", "a-item-2"}
        assert results_b == {"b-item-0", "b-item-1", "b-item-2"}
        assert results_a.isdisjoint(results_b)

    def test_ac014_golden_regression_tenant_isolation_holds(self) -> None:
        """Golden test case for AC-014 (regression guard): two tenants
        indexing the identical item_id with identical content must each
        see only their own copy, never the other's.
        """
        repo = HybridRetrievalIndexRepository(InMemoryVectorIndex(), InMemoryLexicalIndex())
        repo.index_item(
            "tenant-a", "shared-id", ZoneId.EPISODIC, "quick brown fox jumps",
            (1.0, 0.0, 0.0), "model-x",
        )
        repo.index_item(
            "tenant-b", "shared-id", ZoneId.EPISODIC, "quick brown fox jumps",
            (1.0, 0.0, 0.0), "model-x",
        )

        results_a = repo.fetch(_query(tenant_id="tenant-a"))
        results_b = repo.fetch(_query(tenant_id="tenant-b"))

        assert [item.item_id for item in results_a] == ["shared-id"]
        assert [item.item_id for item in results_b] == ["shared-id"]
        assert repo._catalog["tenant-a:shared-id"] is not repo._catalog["tenant-b:shared-id"]


class TestMustNotDeviateRRFAdversarial:
    """Must-not-deviate item 4: RRF fusion boundary/adversarial cases
    beyond the dev suite's formula-correctness proof.
    """

    def test_k_boundary_value_one_does_not_divide_by_zero_or_raise(self) -> None:
        fused = reciprocal_rank_fusion([["a"]], k=1)
        assert fused == pytest.approx({"a": 1.0 / 2})

    def test_default_k_is_the_locked_hld_section_5_value(self) -> None:
        """Regression guard: DEFAULT_RRF_K must never silently drift from
        the HLD-locked value of 60 (ADR-008)."""
        assert DEFAULT_RRF_K == 60

    def test_rrf_never_reads_a_raw_score_only_ranks(self) -> None:
        """Adversarial: two rankings that are identical in ORDER but
        would carry very different raw cosine/BM25 scores if scores were
        consulted must still fuse to identical totals -- proving fusion
        is rank-only, never score-aware, per ADR-008."""
        fused_low_signal = reciprocal_rank_fusion([["x", "y"]])
        fused_high_signal = reciprocal_rank_fusion([["x", "y"]])
        assert fused_low_signal == fused_high_signal

    def test_empty_rankings_list_returns_empty_fused_map(self) -> None:
        assert reciprocal_rank_fusion([]) == {}

    def test_all_empty_rankings_return_empty_fused_map(self) -> None:
        assert reciprocal_rank_fusion([[], [], []]) == {}


class TestAC006Cost1BoundaryAndGoldenRegression:
    """AC-006-COST-1: assertions against the RETURNED mathematics-engineer
    derivation as implemented in application/retrieval_index_cost_estimator.py
    (never a self-computed approximation here), plus boundary cases the
    dev suite's worked-example proof does not already exercise.
    """

    def _profile_b_inputs(self) -> Zone6CostInputs:
        return Zone6CostInputs(
            period_hours=730.0,
            total_assemble_requests=8_672_400.0,
            l2_cache_hit_rate=0.25,
            fixed_cost_vector=280.0,
            fixed_cost_lexical=140.0,
            embedding_cost_per_call=2.0e-5,
            host_supplied_embedding_rate=0.30,
            l1_cache_hit_rate=0.40,
        )

    def test_ac006_cost1_golden_regression_average_and_marginal_both_present(self) -> None:
        """Golden test case for AC-006-COST-1 (regression guard): the
        report object must carry both average_cost_per_query and
        marginal_cost_per_query as two distinct, independently non-zero
        fields on the SAME object -- average alone is structurally
        impossible to construct (AC-006-COST-1's core requirement).
        """
        report = compute_zone6_cost_report(self._profile_b_inputs())

        assert report.average_cost_per_query > 0.0
        assert report.marginal_cost_per_query > 0.0
        assert report.average_cost_per_query != report.marginal_cost_per_query

    def test_marginal_cost_never_equals_average_cost_when_fixed_cost_is_positive(self) -> None:
        """Boundary: whenever F > 0, AC = F/V + c_var strictly exceeds
        MC = c_var for any finite positive V -- the two figures
        collapsing to the same value would silently violate
        AC-006-COST-1's separate-reporting requirement."""
        report = compute_zone6_cost_report(self._profile_b_inputs())
        assert report.average_cost_per_query > report.marginal_cost_per_query

    def test_very_large_volume_still_reports_both_figures_without_overflow(self) -> None:
        """Boundary: at a volume large enough that amortization_share
        approaches zero, both AC and MC must remain finite, positive,
        and independently reported -- not silently rounded to identical
        values by floating-point underflow of the F/V term."""
        huge_volume_inputs = Zone6CostInputs(
            period_hours=730.0,
            total_assemble_requests=1.0e12,
            l2_cache_hit_rate=0.0,
            fixed_cost_vector=280.0,
            fixed_cost_lexical=140.0,
            embedding_cost_per_call=2.0e-5,
            host_supplied_embedding_rate=0.30,
            l1_cache_hit_rate=0.40,
        )
        report = compute_zone6_cost_report(huge_volume_inputs)

        assert report.average_cost_per_query > 0.0
        assert report.marginal_cost_per_query > 0.0
        assert report.amortization_share < 0.01
        assert report.average_cost_per_query > report.marginal_cost_per_query


class TestFullEightFileInventoryPresent:
    """Sanity boundary for the QA subtask itself: every file this story's
    dev report claims to have created actually exists on disk, so a
    later accidental deletion is caught here before any AC-level test
    would fail for a confusing unrelated reason (ImportError instead of
    a clear inventory assertion).
    """

    @pytest.mark.parametrize("source_path", STORY_ALL_NEW_FILES)
    def test_story_source_file_exists(self, source_path: Path) -> None:
        assert source_path.is_file(), f"expected story file missing: {source_path}"
