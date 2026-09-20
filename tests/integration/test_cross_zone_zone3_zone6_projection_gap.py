"""Cross-zone gap test: Zone 3's Zone-6 projection *event* has no real consumer.

Real gap under test: `Zone3IndexProjector` (DASH-STORY-014,
`src/dashanan/application/zone3_index_projection.py`) durably writes a
`SemanticEdge`/`GeneralFact` through its wrapped Zone 3 sink and then
best-effort publishes a `zone3.semantic_edge.projected` /
`zone3.general_fact.projected` event on the shared `EventBus` -- but that
module's own docstring is explicit: "It does NOT implement, call, or import
Zone 6's consumer/indexing logic ... that worker subscribes to the
`EventBus` independently, out of this story's build scope" (HLD Section
3.11: "zones are leaves by design"). A repo-wide search of `src/` for any
subscriber of either event type, or any other caller of
`HybridRetrievalIndexRepository.index_item` besides that class's own
docstring/tests, finds none:

    grep -rn "zone3[.]semantic_edge[.]projected|zone3[.]general_fact[.]projected|
      EVENT_TYPE_SEMANTIC_EDGE_PROJECTED|EVENT_TYPE_GENERAL_FACT_PROJECTED|
      index_item|[.]subscribe[(]" src/

only matches `zone3_index_projection.py` (the publisher itself) and
`hybrid_retrieval_index_repository.py`'s own `index_item` definition/
docstring -- no event-bus subscription of any kind exists anywhere in
`src/`, and nothing else in `src/` calls `index_item`. So a Zone 3 write
that is supposed to make an item retrievable through Zone 6 (HLD Section
3.10's Data Ownership Map: Zone 3 "PROJECTING INTO" Zone 6) never actually
reaches Zone 6's index in the real, wired system -- the projection event is
published into the void, and `HybridRetrievalIndexRepository.fetch`'s own
docstring already documents the consequence precisely: "a fused candidate
whose catalog entry is missing (never written ...) is silently excluded
rather than raising." This test proves that silent exclusion end-to-end: a
real Zone 3 write, a real event-bus publish, and a real Zone 6 query that
comes back empty.

This module does NOT modify `src/` or `tests/integration/conftest_sprint2.py`
-- it only composes `Sprint2WiredSystem`'s already-published Zone 3
fixtures together with Sprint 1's own `retrieval_index_repository` fixture
(`tests/integration/conftest.py`, auto-discovered for every test under
`tests/integration/` by its standard `conftest.py` filename -- no
`pytest_plugins` entry is needed for it, unlike `conftest_sprint2.py`),
and is registered as a pytest plugin exactly as
`tests/integration/test_smoke_fixture_wiring_sprint2.py` registers
`conftest_sprint2.py` itself.
"""

from __future__ import annotations

import pytest

from dashanan.application.zone3_index_projection import (
    EVENT_TYPE_SEMANTIC_EDGE_PROJECTED,
)
from dashanan.domain.ports import ZoneQuery
from dashanan.domain.semantic_memory import SemanticEdge
from dashanan.domain.zone import ZoneId
from dashanan.infrastructure.hybrid_retrieval_index_repository import (
    HybridRetrievalIndexRepository,
)

from tests.integration.conftest import DEFAULT_TENANT_ID
from tests.integration.conftest_sprint2 import Sprint2WiredSystem

pytest_plugins = ["tests.integration.conftest_sprint2"]


class TestZone3ProjectionEventHasNoZone6Consumer:
    """Documents the real gap: a Zone-6 projection event with nobody listening."""

    def test_projection_event_is_published_but_item_not_found_in_zone6(
        self,
        sprint2_wired_system: Sprint2WiredSystem,
        retrieval_index_repository: HybridRetrievalIndexRepository,
    ) -> None:
        """A real Zone 3 write publishes the projection event; Zone 6 never sees the item.

        Seeds one real `SemanticEdge` through `Zone3IndexProjector.insert_edge`
        (wrapping the real `SqlSemanticRepository` over Sprint 1's
        `RecordingConnection` fake, per `conftest_sprint2.py`'s own
        composition-root docstring). Confirms the projection event IS
        published on the shared `RecordingEventBus` with the edge's own
        `tenant_id`/`edge_id` in its envelope (AC-003-PROJ-1). Then queries
        Sprint 1's real, wired `HybridRetrievalIndexRepository` (Zone 6) for
        that exact item and asserts it is NOT found -- `index_item` was
        never called for it, because nothing in `src/` subscribes to the
        event this test just confirmed was published.
        """
        edge_id = "edge-projection-gap-001"
        subject_ref = "entity-subject-001"
        object_ref = "entity-object-001"
        predicate = "located_in"

        edge = SemanticEdge(
            tenant_id=DEFAULT_TENANT_ID,
            edge_id=edge_id,
            subject_ref=subject_ref,
            predicate=predicate,
            object_ref=object_ref,
        )

        sprint2_wired_system.zone3_index_projector.insert_edge(edge)

        published_edge_events = sprint2_wired_system.event_bus.events_of_type(
            EVENT_TYPE_SEMANTIC_EDGE_PROJECTED
        )
        assert len(published_edge_events) == 1, (
            "setup precondition failed: Zone3IndexProjector.insert_edge must "
            f"publish exactly one projection event, got: {published_edge_events}"
        )
        envelope = published_edge_events[0]
        assert envelope["tenant_id"] == DEFAULT_TENANT_ID
        assert envelope["item_id"] == edge_id
        assert envelope["zone"] == ZoneId.SEMANTIC.value

        query = ZoneQuery(
            tenant_id=DEFAULT_TENANT_ID,
            task=predicate,
            query_embedding=None,
            max_items=10,
            min_provenance_conf=0.0,
        )
        results = retrieval_index_repository.fetch(query)

        matching_results = [item for item in results if item.item_id == edge_id]
        assert matching_results == [], (
            "GAP CONFIRMED: the Zone-6 projection event for "
            f"item_id={edge_id!r} was published (envelope: {envelope!r}) but "
            "the item is NOT retrievable through the real, wired "
            "HybridRetrievalIndexRepository.fetch -- no consumer in src/ "
            "ever calls index_item for it (see this module's docstring for "
            f"the grep proving no subscriber exists). Got results: {results!r}"
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
