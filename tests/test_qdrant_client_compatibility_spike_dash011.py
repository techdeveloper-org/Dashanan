"""Verifies DASH-STORY-011's spike-exit deliverables exist (FR-006, spike re-confirmed Sprint 3).

FR-006 (verbatim, SRS.md): "The system SHALL provide a Retrieval-Index Memory zone
maintaining a vector + lexical index over all other zones for fast recall, and SHALL
expose the similarity primitives needed to compute the TaskRelevance and UserAffinity
terms of the Memory Score (FR-012)."

A spike's "AC" is its exit criterion, not a product behavior (backlog_draft.json's own
INVEST "testable" note for this story). This suite checks each of the four spike-exit
criteria as a concrete, automatable artifact rather than product code paths, since the
deliverable itself is documentation/dependency research, not a zone behavior:

  SPIKE-EXIT-1: the compatibility-matrix document exists and covers client version,
    server version, and Python 3.12+.
  SPIKE-EXIT-2: the document names a single pinned-version recommendation with rationale.
  SPIKE-EXIT-3: the recommendation is handed to DASH-STORY-007's Dev sub-task as a
    concrete follow-up inside that adapter's own module docstring, not left standalone.
  SPIKE-EXIT-4: the document does not reopen ADR-007's vendor decision anywhere in it.

This suite does not require network access or a live Qdrant server -- consistent with
this re-confirmation's own must-not-deviate scope (documentation-only research now, the
live-server half of validation stays blocked on FR-015's infrastructure).
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SPIKE_DOC = (
    _REPO_ROOT
    / "docs"
    / "spikes"
    / "dash-story-011-qdrant-client-compatibility-matrix.md"
)
_ZONE6_ADAPTER = (
    _REPO_ROOT
    / "src"
    / "dashanan"
    / "infrastructure"
    / "hybrid_retrieval_index_repository.py"
)


@pytest.fixture(scope="module")
def spike_doc_text() -> str:
    """Read the DASH-STORY-011 spike-exit compatibility-matrix document once per module."""
    return _SPIKE_DOC.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def zone6_adapter_text() -> str:
    """Read the Zone 6 adapter module DASH-STORY-011's follow-up is attached to."""
    return _ZONE6_ADAPTER.read_text(encoding="utf-8")


class TestSpikeExit1CompatibilityMatrix:
    """SPIKE-EXIT-1: client x server x Python 3.12+ compatibility matrix exists."""

    def test_spike_doc_file_exists(self) -> None:
        assert _SPIKE_DOC.is_file(), (
            f"Expected the DASH-STORY-011 spike-exit deliverable at {_SPIKE_DOC}, "
            "none found"
        )

    def test_spike_doc_names_the_pinned_server_version(self, spike_doc_text: str) -> None:
        assert "v1.11.0" in spike_doc_text, (
            "Matrix must cover this repo's actual pinned Qdrant server version "
            "(docker-compose.yml, DASH-STORY-024)"
        )

    def test_spike_doc_names_a_client_version(self, spike_doc_text: str) -> None:
        assert "qdrant-client" in spike_doc_text
        assert "1.19.1" in spike_doc_text or "1.11.3" in spike_doc_text, (
            "Matrix must name at least one concrete qdrant-client version"
        )

    def test_spike_doc_covers_python_312(self, spike_doc_text: str) -> None:
        assert "3.12" in spike_doc_text, (
            "Matrix must explicitly cover Python 3.12+ per the exit criterion "
            "and this project's requires-python >=3.12 (pyproject.toml)"
        )

    def test_spike_doc_cites_primary_sources(self, spike_doc_text: str) -> None:
        assert "pypi.org/pypi/qdrant-client/json" in spike_doc_text
        assert "qdrant.tech/documentation/upgrades" in spike_doc_text


class TestSpikeExit2PinnedRecommendation:
    """SPIKE-EXIT-2: breaking-change review and single pinned-version recommendation."""

    def test_spike_doc_states_a_single_recommendation(self, spike_doc_text: str) -> None:
        assert "Recommendation" in spike_doc_text
        assert "1.11.x" in spike_doc_text, (
            "The recommended pin must be stated explicitly, not left implicit"
        )

    def test_spike_doc_gives_rationale_for_the_pin(self, spike_doc_text: str) -> None:
        assert "Rationale" in spike_doc_text

    def test_spike_doc_reviews_breaking_changes(self, spike_doc_text: str) -> None:
        assert "breaking" in spike_doc_text.lower()


class TestSpikeExit3HandoffToDashStory007:
    """SPIKE-EXIT-3: recommendation handed to DASH-STORY-007's Dev sub-task, not orphaned."""

    def test_zone6_adapter_file_exists(self) -> None:
        assert _ZONE6_ADAPTER.is_file()

    def test_zone6_adapter_references_the_spike(self, zone6_adapter_text: str) -> None:
        assert "DASH-STORY-011" in zone6_adapter_text

    def test_zone6_adapter_references_the_spike_doc_path(
        self, zone6_adapter_text: str
    ) -> None:
        assert (
            "docs/spikes/dash-story-011-qdrant-client-compatibility-matrix.md"
            in zone6_adapter_text
        )

    def test_spike_doc_references_the_handoff_target(self, spike_doc_text: str) -> None:
        assert "DASH-STORY-007" in spike_doc_text
        assert "hybrid_retrieval_index_repository.py" in spike_doc_text


class TestSpikeExit4ScopeBoundary:
    """SPIKE-EXIT-4: ADR-007's vendor decision is not reopened anywhere in the deliverable."""

    def test_spike_doc_does_not_evaluate_alternative_vector_stores(
        self, spike_doc_text: str
    ) -> None:
        assert "not** re-evaluate Qdrant against pgvector" in spike_doc_text
        for competitor in ("pgvector", "Milvus"):
            mention_count = spike_doc_text.count(competitor)
            assert mention_count == 1, (
                f"'{competitor}' must appear at most once, in the explicit "
                f"out-of-scope declaration -- never as part of an actual "
                f"evaluation (found {mention_count} mentions)"
            )

    def test_spike_doc_states_adr007_is_locked(self, spike_doc_text: str) -> None:
        assert "ADR-007" in spike_doc_text
        assert "locked" in spike_doc_text.lower()

    def test_spike_doc_declares_its_own_scope_boundary(self, spike_doc_text: str) -> None:
        assert "SPIKE-EXIT-4" in spike_doc_text
