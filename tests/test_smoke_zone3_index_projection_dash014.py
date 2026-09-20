"""Smoke assertions for DASH-STORY-014, inline per the dev subtask scope.

The formal pytest suite covering AC-003-PROJ-1 / AC-003-PROJ-2 /
AC-003-PROJ-3 in full is the QA subtask's responsibility (see the story's
`qa_prompt`, sprint2_implementation_execution_plan.json). These checks
confirm the package is importable, wired correctly, and that the
must-not-deviate structural properties (push/async-only projection, no
Zone 6 import/dependency, Zone 3 write durability independent of the
EventBus) hold, ahead of that formal suite landing -- the exact scope
split `tests/test_smoke_semantic.py` used for DASH-STORY-012.

Runtime assumptions recorded per rule 33/40 test-roadmap conventions:
  - clock: none needed -- `Zone3IndexProjector` carries no timestamp
    field or clock dependency.
  - tenant_id: "tenant-1" for all requests unless a test states otherwise.
  - No fixture seed is required; nothing in this suite is randomized.

PII NOTE: per the dev_prompt's PII constraint, every illustration below
uses pseudonymized `<ENTITY_A>` / `<ENTITY_B>` placeholders -- never a
realistic name.
"""

from __future__ import annotations

import inspect
import logging

import pytest

from dashanan.application.zone3_index_projection import (
    EVENT_TYPE_GENERAL_FACT_PROJECTED,
    EVENT_TYPE_SEMANTIC_EDGE_PROJECTED,
    Zone3IndexProjector,
)
from dashanan.domain.exceptions import ZoneRepositoryError
from dashanan.domain.semantic_memory import GeneralFact, SemanticEdge
from dashanan.domain.zone import ZoneId

import dashanan.application.zone3_index_projection as zone3_index_projection_module


class RecordingSink:
    """`SemanticWriteSink` double: records every call, optionally raises."""

    def __init__(self) -> None:
        self.edges_inserted: list[SemanticEdge] = []
        self.facts_inserted: list[GeneralFact] = []
        self._raise: Exception | None = None

    def insert_edge(self, edge: SemanticEdge) -> None:
        if self._raise is not None:
            raise self._raise
        self.edges_inserted.append(edge)

    def insert_general_fact(self, fact: GeneralFact) -> None:
        if self._raise is not None:
            raise self._raise
        self.facts_inserted.append(fact)


class RecordingEventBus:
    """`EventBus` double: records every publish call, optionally raises."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, object]]] = []
        self._raise: Exception | None = None

    def publish(self, event_type: str, payload: dict[str, object]) -> None:
        if self._raise is not None:
            raise self._raise
        self.published.append((event_type, dict(payload)))


def _edge(
    tenant_id: str = "tenant-1",
    edge_id: str = "edge-1",
    subject_ref: str = "entity-a",
    predicate: str = "related_to",
    object_ref: str = "entity-b",
) -> SemanticEdge:
    return SemanticEdge(
        tenant_id=tenant_id,
        edge_id=edge_id,
        subject_ref=subject_ref,
        predicate=predicate,
        object_ref=object_ref,
    )


def _fact(
    tenant_id: str = "tenant-1",
    fact_id: str = "fact-1",
    statement: str = "<PII_EXAMPLE_REDACTED>",
    subject_scope: str = "global",
) -> GeneralFact:
    return GeneralFact(
        tenant_id=tenant_id,
        fact_id=fact_id,
        statement=statement,
        subject_scope=subject_scope,
    )


class TestPackageWiring:
    """Baseline: the projector constructs and is importable, before any AC-level suite."""

    def test_projector_constructs_with_fake_collaborators(self) -> None:
        projector = Zone3IndexProjector(RecordingSink(), RecordingEventBus())
        assert projector is not None


class TestAC003Proj1ProjectionEventPublished:
    """AC-003-PROJ-1: a durable write is followed by one EventBus publish call."""

    def test_insert_edge_publishes_exactly_one_projection_event(self) -> None:
        sink = RecordingSink()
        bus = RecordingEventBus()
        projector = Zone3IndexProjector(sink, bus)
        edge = _edge()

        projector.insert_edge(edge)

        assert sink.edges_inserted == [edge]
        assert len(bus.published) == 1
        event_type, payload = bus.published[0]
        assert event_type == EVENT_TYPE_SEMANTIC_EDGE_PROJECTED
        assert payload == {
            "tenant_id": "tenant-1",
            "item_id": "edge-1",
            "zone": ZoneId.SEMANTIC.value,
        }

    def test_insert_general_fact_publishes_exactly_one_projection_event(self) -> None:
        sink = RecordingSink()
        bus = RecordingEventBus()
        projector = Zone3IndexProjector(sink, bus)
        fact = _fact()

        projector.insert_general_fact(fact)

        assert sink.facts_inserted == [fact]
        assert len(bus.published) == 1
        event_type, payload = bus.published[0]
        assert event_type == EVENT_TYPE_GENERAL_FACT_PROJECTED
        assert payload == {
            "tenant_id": "tenant-1",
            "item_id": "fact-1",
            "zone": ZoneId.SEMANTIC.value,
        }

    def test_projection_payload_never_carries_fact_content(self) -> None:
        """PII/must-not-deviate: the envelope is item_id/tenant_id/zone only."""
        sink = RecordingSink()
        bus = RecordingEventBus()
        projector = Zone3IndexProjector(sink, bus)

        projector.insert_edge(_edge(predicate="located_in"))

        _, payload = bus.published[0]
        assert set(payload.keys()) == {"tenant_id", "item_id", "zone"}
        assert "located_in" not in payload.values()


class TestAC003Proj2NoSynchronousZone6Dependency:
    """AC-003-PROJ-2: no synchronous call into, or import of, Zone 6."""

    def test_module_imports_no_zone6_module(self) -> None:
        source = inspect.getsource(zone3_index_projection_module)
        for forbidden in (
            "retrieval_index_ports",
            "hybrid_retrieval_index_repository",
            "in_memory_vector_index",
            "in_memory_lexical_index",
        ):
            assert forbidden not in source

    def test_constructor_collaborators_are_exactly_sink_and_event_bus(self) -> None:
        """Only collaborators are the injected SemanticWriteSink and EventBus ports."""
        params = inspect.signature(Zone3IndexProjector.__init__).parameters
        annotations = {
            name: param.annotation
            for name, param in params.items()
            if name != "self"
        }
        assert set(annotations) == {"sink", "event_bus"}
        assert annotations["sink"] == "SemanticWriteSink"
        assert annotations["event_bus"] == "EventBus"


class TestAC003Proj3PublishFailureDoesNotAffectDurableWrite:
    """AC-003-PROJ-3: EventBus unavailability never rolls back or rejects the write."""

    def test_insert_edge_succeeds_when_event_bus_publish_raises(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        sink = RecordingSink()
        bus = RecordingEventBus()
        bus._raise = RuntimeError("event bus unavailable")
        projector = Zone3IndexProjector(sink, bus)
        edge = _edge()

        with caplog.at_level(logging.WARNING):
            projector.insert_edge(edge)

        assert sink.edges_inserted == [edge]
        assert any(
            "projection publish failed" in record.message
            for record in caplog.records
        )

    def test_insert_general_fact_succeeds_when_event_bus_publish_raises(self) -> None:
        sink = RecordingSink()
        bus = RecordingEventBus()
        bus._raise = ConnectionError("broker unreachable")
        projector = Zone3IndexProjector(sink, bus)
        fact = _fact()

        projector.insert_general_fact(fact)

        assert sink.facts_inserted == [fact]

    def test_insert_edge_never_publishes_when_the_durable_write_itself_fails(
        self,
    ) -> None:
        """A write that never commits has nothing to project (defense-in-depth)."""
        sink = RecordingSink()
        sink._raise = ZoneRepositoryError(zone="semantic", reason="constraint violation")
        bus = RecordingEventBus()
        projector = Zone3IndexProjector(sink, bus)

        with pytest.raises(ZoneRepositoryError):
            projector.insert_edge(_edge())

        assert bus.published == []

    def test_insert_general_fact_never_publishes_when_the_durable_write_itself_fails(
        self,
    ) -> None:
        sink = RecordingSink()
        sink._raise = ZoneRepositoryError(zone="semantic", reason="duplicate fact_id")
        bus = RecordingEventBus()
        projector = Zone3IndexProjector(sink, bus)

        with pytest.raises(ZoneRepositoryError):
            projector.insert_general_fact(_fact())

        assert bus.published == []


class TestMustNotDeviateNoOpEventBusIsAValidCollaborator:
    """A Shape A / offline deployment (NoOpEventBus) must not block Zone 3 writes."""

    def test_noop_event_bus_satisfies_the_event_bus_port_and_never_raises(self) -> None:
        from dashanan.infrastructure.noop_event_bus import NoOpEventBus

        sink = RecordingSink()
        projector = Zone3IndexProjector(sink, NoOpEventBus())
        edge = _edge()

        projector.insert_edge(edge)

        assert sink.edges_inserted == [edge]
