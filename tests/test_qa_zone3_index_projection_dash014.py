"""Formal QA suite for DASH-STORY-014 (Zone 3 -> Zone 6 retrieval-index
projection, async event emission only), tracing to FR-003.

FR-003 (SRS.md, verbatim): "The system SHALL provide a Semantic Memory
zone holding distilled, deduplicated cross-entity relationships and
general facts not owned by any single entity record, per the locked
Zone 3/Zone 5 ownership ADR."

This is the QA subtask (25% of story points, backlog_draft.json) for
DASH-STORY-014, per the story's own qa_prompt
(docs/phase-7-routing/sprint2_implementation_execution_plan.json). It
supersedes tests/test_smoke_zone3_index_projection_dash014.py's dev-side
smoke coverage with a formal suite enumerating happy-path, boundary and
adverse scenarios for every acceptance criterion under test, plus one
golden regression test per AC.

ACCEPTANCE CRITERIA UNDER TEST:
  AC-003-PROJ-1: Given a SemanticEdge or GeneralFact is durably written to
      Zone 3, a projection event is published to the EventBus for Zone 6's
      index worker to consume.
  AC-003-PROJ-2: Zone 3 makes no synchronous call into Zone 6 and carries
      no recorded dependency on it, per HLD Section 3.11.
  AC-003-PROJ-3: When Zone 3 cannot publish a projection event (EventBus
      unavailable), the underlying durable write is NOT rolled back or
      rejected, per HLD Section 3.7's "recall degradation, not a
      data-loss event" framing (OAQ-16).

TEST SCENARIO ENUMERATION (per AC, as the qa_prompt requires before any
test code -- "let's think step by step: enumerate every AC ... THEN write
test code"):

AC-003-PROJ-1 (projection event published):
  - Happy path (edge): one insert_edge -> exactly one publish, correct
    event_type and envelope. (test_insert_edge_publishes_...)
  - Happy path (fact): one insert_general_fact -> exactly one publish,
    correct event_type and envelope. (test_insert_general_fact_publishes_...)
  - Boundary: two consecutive writes publish two independent, correctly
    ordered events -- no event coalescing or loss.
    (test_two_consecutive_inserts_publish_two_independent_events)
  - Boundary: single-character tenant_id/item_id (the minimum non-blank
    value the domain entity allows) still produces a well-formed
    envelope; a blank tenant_id is rejected by the domain entity itself
    before the projector ever runs.
    (test_single_character_tenant_and_item_ids_..., and
     test_blank_tenant_id_is_rejected_by_the_domain_before_projection)
  - Adverse: malformed/mismatched event bus (missing publish method
    entirely) is swallowed the same as any other publish failure --
    the durable write still succeeds (documented, not a defect).
    (test_event_bus_missing_publish_method_is_swallowed_like_any_publish_failure)
  - Golden: the exact envelope shape for a semantic edge is pinned
    (regression guard for any later story touching this component).
    (test_golden_semantic_edge_projection_envelope)

AC-003-PROJ-2 (no synchronous Zone 6 dependency):
  - Static: module source contains no import of any Zone 6 module.
    (test_module_imports_no_zone6_module)
  - Structural: constructor's only two parameters are the sink and event
    bus ports (Protocol-typed), never a Zone 6 type.
    (test_constructor_collaborators_are_exactly_sink_and_event_bus)
  - Dependency-integrity: the module's import list is exactly the
    expected small set (dependency_integrity_check, must-not-deviate) --
    no accidental transitive Zone 6 import slipped in via another module.
    (test_dependency_integrity_no_mutation_outside_story_ownership)
  - Adverse: even when the injected EventBus double happens to also
    expose Zone-6-shaped methods (a hostile/over-broad fake), the
    projector never calls anything beyond `.publish(...)`.
    (test_projector_never_calls_anything_but_publish_on_event_bus)

AC-003-PROJ-3 (publish failure does not affect durable write):
  - Happy/adverse (edge): EventBus.publish raises RuntimeError -> sink
    write still recorded, no exception propagates, WARNING logged.
    (test_insert_edge_succeeds_when_event_bus_publish_raises)
  - Happy/adverse (fact): same, ConnectionError variant.
    (test_insert_general_fact_succeeds_when_event_bus_publish_raises)
  - Boundary: EventBus.publish raises on the exact last item of a batch
    of inserts -- prior successful writes are unaffected and the failing
    write still durably commits. (test_publish_failure_mid_batch_...)
  - Adverse: durable write itself fails (ZoneRepositoryError) -> no
    publish is attempted at all, and the exception propagates unchanged.
    (test_insert_edge_never_publishes_when_the_durable_write_itself_fails
     and the GeneralFact counterpart)
  - Adverse: EventBus.publish raises a non-Exception BaseException
    subclass is explicitly OUT OF SCOPE -- `except Exception` by design
    (per HLD OAQ-16 framing and Python convention) does not catch
    BaseException (e.g. KeyboardInterrupt, SystemExit); this is verified
    as a documented, deliberate boundary, not a defect.
    (test_publish_raising_keyboardinterrupt_propagates_by_design)
  - Golden: a NoOpEventBus (Shape A / offline deployment) never blocks a
    durable write -- regression guard for the offline-deployment
    contract. (test_golden_noop_event_bus_never_blocks_durable_write)

RUNTIME ASSUMPTIONS (recorded per rule 33/40 test-roadmap conventions):
  - clock: none required -- Zone3IndexProjector carries no timestamp
    field or clock dependency.
  - tenant_id: "tenant-1" for all requests unless a test states
    otherwise (e.g. the empty-string boundary test uses "").
  - fixture seed: none required -- nothing in this suite is randomized.

PII NOTE: per the dev_prompt's PII constraint, every illustration below
uses pseudonymized `<ENTITY_A>` / `<ENTITY_B>` placeholders or the
literal `<PII_EXAMPLE_REDACTED>` marker, never a realistic name.
"""

from __future__ import annotations

import inspect
import logging

import pytest

import dashanan.application.zone3_index_projection as zone3_index_projection_module
from dashanan.application.zone3_index_projection import (
    EVENT_TYPE_GENERAL_FACT_PROJECTED,
    EVENT_TYPE_SEMANTIC_EDGE_PROJECTED,
    SemanticWriteSink,
    Zone3IndexProjector,
)
from dashanan.domain.exceptions import ZoneRepositoryError
from dashanan.domain.ports import EventBus
from dashanan.domain.semantic_memory import GeneralFact, SemanticEdge
from dashanan.domain.zone import ZoneId

FORBIDDEN_ZONE6_MODULE_NAMES = (
    "retrieval_index_ports",
    "hybrid_retrieval_index_repository",
    "in_memory_vector_index",
    "in_memory_lexical_index",
)


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
        self._raise: BaseException | None = None

    def publish(self, event_type: str, payload: dict[str, object]) -> None:
        if self._raise is not None:
            raise self._raise
        self.published.append((event_type, dict(payload)))


class OverBroadEventBus:
    """Adverse double: exposes `.publish` plus extra, Zone-6-shaped methods.

    Verifies the projector calls nothing on its EventBus collaborator
    beyond `.publish(...)`, even when the injected object happens to
    also expose surface area a Zone 6 client might use.
    """

    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, object]]] = []
        self.forbidden_methods_called: list[str] = []

    def publish(self, event_type: str, payload: dict[str, object]) -> None:
        self.published.append((event_type, dict(payload)))

    def query_vector_index(self, *_args: object, **_kwargs: object) -> None:
        self.forbidden_methods_called.append("query_vector_index")

    def query_lexical_index(self, *_args: object, **_kwargs: object) -> None:
        self.forbidden_methods_called.append("query_lexical_index")


class EventBusMissingPublish:
    """Adverse double: does NOT implement `.publish` at all."""


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


class TestAC003Proj1ProjectionEventPublishedHappyPath:
    """AC-003-PROJ-1 happy path: a durable write is followed by one publish."""

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


class TestAC003Proj1ProjectionEventPublishedBoundary:
    """AC-003-PROJ-1 boundary scenarios."""

    def test_two_consecutive_inserts_publish_two_independent_events(self) -> None:
        sink = RecordingSink()
        bus = RecordingEventBus()
        projector = Zone3IndexProjector(sink, bus)

        projector.insert_edge(_edge(edge_id="edge-1"))
        projector.insert_edge(_edge(edge_id="edge-2"))

        assert len(bus.published) == 2
        first_type, first_payload = bus.published[0]
        second_type, second_payload = bus.published[1]
        assert first_type == second_type == EVENT_TYPE_SEMANTIC_EDGE_PROJECTED
        assert first_payload["item_id"] == "edge-1"
        assert second_payload["item_id"] == "edge-2"

    def test_single_character_tenant_and_item_ids_produce_well_formed_envelope(
        self,
    ) -> None:
        """Single-character ids are the minimum non-blank boundary
        `SemanticEdge.__post_init__` allows (it rejects blank
        tenant_id/edge_id at the domain layer, verified separately below);
        the projector must not special-case or reject this minimum."""
        sink = RecordingSink()
        bus = RecordingEventBus()
        projector = Zone3IndexProjector(sink, bus)

        projector.insert_edge(_edge(tenant_id="t", edge_id="e"))

        assert len(bus.published) == 1
        _, payload = bus.published[0]
        assert payload == {
            "tenant_id": "t",
            "item_id": "e",
            "zone": ZoneId.SEMANTIC.value,
        }

    def test_blank_tenant_id_is_rejected_by_the_domain_before_projection(
        self,
    ) -> None:
        """Boundary below the minimum: `SemanticEdge` itself enforces the
        non-blank invariant in `__post_init__`, so a blank tenant_id never
        reaches the projector's write-then-publish sequence at all -- the
        projector correctly delegates this validation to the domain
        entity rather than duplicating it."""
        sink = RecordingSink()
        bus = RecordingEventBus()
        projector = Zone3IndexProjector(sink, bus)

        with pytest.raises(ValueError, match="tenant_id must not be blank"):
            projector.insert_edge(_edge(tenant_id="", edge_id="e"))

        assert sink.edges_inserted == []
        assert bus.published == []


class TestAC003Proj1ProjectionEventPublishedAdverse:
    """AC-003-PROJ-1 adverse scenarios."""

    def test_event_bus_missing_publish_method_is_swallowed_like_any_publish_failure(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A malformed EventBus collaborator (no `.publish`) raises
        `AttributeError` inside `_publish_projection`'s `try` block. By
        design (OAQ-16: any publish failure, including a broken adapter,
        must never affect the already-durable write) this is caught by
        the same `except Exception` as a broker outage would be -- the
        durable write still succeeds and a WARNING is logged. This is
        documented here as intended behavior, not a defect: a
        non-conforming `EventBus` adapter degrades recall, it does not
        break Zone 3 durability."""
        sink = RecordingSink()
        bus = EventBusMissingPublish()
        projector = Zone3IndexProjector(sink, bus)  # type: ignore[arg-type]
        edge = _edge()

        with caplog.at_level(logging.WARNING):
            projector.insert_edge(edge)

        assert sink.edges_inserted == [edge]
        assert any(
            "projection publish failed" in record.message
            for record in caplog.records
        )


class TestAC003Proj1GoldenRegression:
    """AC-003-PROJ-1 golden test: pins the exact envelope shape for
    regression detection by any later story touching this component."""

    def test_golden_semantic_edge_projection_envelope(self) -> None:
        sink = RecordingSink()
        bus = RecordingEventBus()
        projector = Zone3IndexProjector(sink, bus)

        projector.insert_edge(
            _edge(tenant_id="tenant-golden", edge_id="edge-golden")
        )

        assert bus.published == [
            (
                "zone3.semantic_edge.projected",
                {
                    "tenant_id": "tenant-golden",
                    "item_id": "edge-golden",
                    "zone": "semantic",
                },
            )
        ]


class TestAC003Proj2NoSynchronousZone6Dependency:
    """AC-003-PROJ-2: no synchronous call into, or import of, Zone 6."""

    def test_module_imports_no_zone6_module(self) -> None:
        source = inspect.getsource(zone3_index_projection_module)
        for forbidden in FORBIDDEN_ZONE6_MODULE_NAMES:
            assert forbidden not in source

    def test_constructor_collaborators_are_exactly_sink_and_event_bus(self) -> None:
        params = inspect.signature(Zone3IndexProjector.__init__).parameters
        annotations = {
            name: param.annotation
            for name, param in params.items()
            if name != "self"
        }
        assert set(annotations) == {"sink", "event_bus"}
        assert annotations["sink"] == "SemanticWriteSink"
        assert annotations["event_bus"] == "EventBus"

    def test_dependency_integrity_no_mutation_outside_story_ownership(self) -> None:
        """dependency_integrity_check (sprint2_ar1_assignments.json): the
        module's actual `import` targets are exactly the small, expected
        set -- no accidental transitive Zone 6 (or unrelated zone) import
        slipped in."""
        source = inspect.getsource(zone3_index_projection_module)
        import ast

        tree = ast.parse(source)
        imported_modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imported_modules.add(alias.name)

        allowed_prefixes = (
            "__future__",
            "logging",
            "typing",
            "dashanan.domain.ports",
            "dashanan.domain.semantic_memory",
            "dashanan.domain.zone",
        )
        for module_name in imported_modules:
            assert any(
                module_name == prefix or module_name.startswith(prefix + ".")
                for prefix in allowed_prefixes
            ), f"Unexpected import '{module_name}' outside story ownership"

    def test_projector_never_calls_anything_but_publish_on_event_bus(self) -> None:
        """Even with an over-broad EventBus double exposing Zone-6-shaped
        methods, the projector must call only `.publish(...)`."""
        sink = RecordingSink()
        bus = OverBroadEventBus()
        projector = Zone3IndexProjector(sink, bus)  # type: ignore[arg-type]

        projector.insert_edge(_edge())
        projector.insert_general_fact(_fact())

        assert bus.forbidden_methods_called == []
        assert len(bus.published) == 2

    def test_semantic_write_sink_protocol_is_runtime_checkable_and_local(self) -> None:
        """`SemanticWriteSink` is a local, structurally-typed seam (not
        added to the frozen `dashanan.domain.ports`, AR1-G2)."""
        assert issubclass(SemanticWriteSink, EventBus) is False
        sink = RecordingSink()
        assert isinstance(sink, SemanticWriteSink)


class TestAC003Proj3PublishFailureDoesNotAffectDurableWrite:
    """AC-003-PROJ-3: EventBus unavailability never rolls back or rejects
    the write. Happy/adverse cases."""

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
        assert all(record.levelno == logging.WARNING for record in caplog.records)

    def test_insert_general_fact_succeeds_when_event_bus_publish_raises(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        sink = RecordingSink()
        bus = RecordingEventBus()
        bus._raise = ConnectionError("broker unreachable")
        projector = Zone3IndexProjector(sink, bus)
        fact = _fact()

        with caplog.at_level(logging.WARNING):
            projector.insert_general_fact(fact)

        assert sink.facts_inserted == [fact]
        assert any(
            "projection publish failed" in record.message
            for record in caplog.records
        )

    def test_warning_log_extras_carry_event_type_tenant_and_item_but_no_fact_content(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The WARNING log's structured extras must identify the failed
        publish without leaking fact content (PII constraint)."""
        sink = RecordingSink()
        bus = RecordingEventBus()
        bus._raise = RuntimeError("event bus unavailable")
        projector = Zone3IndexProjector(sink, bus)

        with caplog.at_level(logging.WARNING):
            projector.insert_edge(_edge(edge_id="edge-42", predicate="located_in"))

        record = next(
            r for r in caplog.records if "projection publish failed" in r.message
        )
        assert record.event_type == EVENT_TYPE_SEMANTIC_EDGE_PROJECTED
        assert record.tenant_id == "tenant-1"
        assert record.item_id == "edge-42"
        assert "located_in" not in str(record.__dict__)


class TestAC003Proj3PublishFailureBoundary:
    """AC-003-PROJ-3 boundary: a mid-batch publish failure does not
    affect prior or subsequent durable writes."""

    def test_publish_failure_mid_batch_does_not_affect_other_writes(self) -> None:
        sink = RecordingSink()
        bus = RecordingEventBus()
        projector = Zone3IndexProjector(sink, bus)

        projector.insert_edge(_edge(edge_id="edge-1"))
        bus._raise = RuntimeError("transient broker outage")
        projector.insert_edge(_edge(edge_id="edge-2"))
        bus._raise = None
        projector.insert_edge(_edge(edge_id="edge-3"))

        assert [e.edge_id for e in sink.edges_inserted] == [
            "edge-1",
            "edge-2",
            "edge-3",
        ]
        published_ids = [payload["item_id"] for _, payload in bus.published]
        assert published_ids == ["edge-1", "edge-3"]


class TestAC003Proj3DurableWriteFailureAdverse:
    """AC-003-PROJ-3 adverse: the durable write itself failing means no
    projection is ever attempted (defense-in-depth, not the OAQ-16 case)."""

    def test_insert_edge_never_publishes_when_the_durable_write_itself_fails(
        self,
    ) -> None:
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

    def test_publish_raising_keyboardinterrupt_propagates_by_design(self) -> None:
        """Documented, deliberate boundary: `except Exception` does not
        catch `BaseException` subclasses (KeyboardInterrupt, SystemExit).
        This is intentional -- OAQ-16's "recall degradation, not data
        loss" framing covers publish *failures*, not process-level
        interrupts -- and is asserted here so it is never mistaken for a
        defect by a later story."""
        sink = RecordingSink()
        bus = RecordingEventBus()
        bus._raise = KeyboardInterrupt()
        projector = Zone3IndexProjector(sink, bus)

        with pytest.raises(KeyboardInterrupt):
            projector.insert_edge(_edge())

        assert sink.edges_inserted == [_edge()]


class TestAC003Proj3GoldenRegression:
    """AC-003-PROJ-3 golden test: a Shape A / offline deployment
    (NoOpEventBus) must never block a durable Zone 3 write."""

    def test_golden_noop_event_bus_never_blocks_durable_write(self) -> None:
        from dashanan.infrastructure.noop_event_bus import NoOpEventBus

        sink = RecordingSink()
        projector = Zone3IndexProjector(sink, NoOpEventBus())
        edge = _edge()

        projector.insert_edge(edge)

        assert sink.edges_inserted == [edge]


class TestPackageWiring:
    """Baseline: the projector constructs and is importable."""

    def test_projector_constructs_with_fake_collaborators(self) -> None:
        projector = Zone3IndexProjector(RecordingSink(), RecordingEventBus())
        assert projector is not None

    def test_event_type_constants_are_distinct(self) -> None:
        assert EVENT_TYPE_SEMANTIC_EDGE_PROJECTED != EVENT_TYPE_GENERAL_FACT_PROJECTED
