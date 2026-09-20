"""Zone3IndexProjector: async Zone 3 -> Zone 6 retrieval-index projection (DASH-STORY-014, FR-003).

FR-003 (SRS.md, verbatim): "The system SHALL provide a Semantic Memory
zone holding distilled, deduplicated cross-entity relationships and
general facts not owned by any single entity record, per the locked
Zone 3/Zone 5 ownership ADR."

HLD Section 3.10's Data Ownership Map records Zone 3 as PROJECTING INTO
Zone 6 (the derived retrieval-index tier, HLD Section 3.7) -- a one-way,
asynchronous relationship, never a synchronous read/write dependency (HLD
Section 3.11: zones are leaves by design). This module is that
projection's *publish* side only: it decorates a Zone 3 write sink
(`SqlSemanticRepository`, DASH-STORY-012, or any adapter with the same
shape) so that every durable `SemanticEdge`/`GeneralFact` insert is
followed by a best-effort, async projection-event publish for Zone 6's
own index worker to consume.

It does NOT implement, call, or import Zone 6's consumer/indexing logic
(DASH-STORY-007's scope, HLD Hard Rule 1: no two zones share a table) --
that worker subscribes to the `EventBus` independently, out of this
story's build scope.

Decorator pattern (`python-design-patterns-core`): this class satisfies
the exact same two-method write shape `SqlSemanticRepository` exposes, so
a caller can wrap the real repository with this projector with zero
change to any code that already depends on "the Zone 3 write sink" --
mirroring this codebase's established Proxy/Decorator convention (e.g.
`ProvenanceWriteGate` wrapping a caller's `persist_fact`) over
inheritance.

PII NOTE: the published event payload is strictly the event envelope --
`tenant_id`, `item_id`, `zone` -- never `SemanticEdge`/`GeneralFact`
content (`subject_ref`, `predicate`, `object_ref`, `qualifiers`,
`statement`, `subject_scope`). This module never reads those fields for
any purpose beyond the envelope identifiers already required to route the
event, per the dev_prompt's PII constraint.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from dashanan.domain.ports import EventBus
from dashanan.domain.semantic_memory import GeneralFact, SemanticEdge
from dashanan.domain.zone import ZoneId

logger = logging.getLogger(__name__)

EVENT_TYPE_SEMANTIC_EDGE_PROJECTED = "zone3.semantic_edge.projected"
EVENT_TYPE_GENERAL_FACT_PROJECTED = "zone3.general_fact.projected"


@runtime_checkable
class SemanticWriteSink(Protocol):
    """The two-method Zone 3 write shape this projector decorates.

    Kept local rather than added to the frozen `dashanan.domain.ports`
    module (AR1-G2) -- an adapter-composition seam, not a cross-cutting
    application port, mirroring `SqlSemanticRepository`'s own local
    `SqlConnection`/`SqlCursor` Protocol convention. `SqlSemanticRepository`
    (DASH-STORY-012) satisfies this Protocol structurally, with no import
    of this module required on its side.
    """

    def insert_edge(self, edge: SemanticEdge) -> None:
        """Durably persist one `SemanticEdge`. See `SqlSemanticRepository.insert_edge`."""
        ...

    def insert_general_fact(self, fact: GeneralFact) -> None:
        """Durably persist one `GeneralFact`. See `SqlSemanticRepository.insert_general_fact`."""
        ...


class Zone3IndexProjector:
    """Decorates a Zone 3 write sink with an async Zone 6 projection-event publish.

    AC-003-PROJ-1: every successful `insert_edge`/`insert_general_fact`
    call is followed by one `EventBus.publish` call carrying the
    projection envelope for Zone 6's index worker to consume.

    AC-003-PROJ-2: this class never imports, calls, or depends on any
    Zone 6 module -- its only collaborators are the injected
    `SemanticWriteSink` and `EventBus` ports, both satisfied structurally.
    No synchronous call into Zone 6 exists anywhere in this file, and no
    dependency edge to Zone 6 is recorded by this story (HLD Section
    3.11: zones are leaves).

    AC-003-PROJ-3: the durable write (`self._sink.insert_edge` /
    `insert_general_fact`) always runs to completion, and any exception
    it raises propagates unchanged, BEFORE the projection publish is even
    attempted -- there is nothing to project for a write that never
    committed. The publish itself runs in its own `try/except`: any
    exception the `EventBus` raises (broker unavailable, timeout, or any
    other publish failure) is logged and discarded, never re-raised and
    never used to roll back or reject the write that already durably
    committed -- HLD Section 3.7's "recall degradation, not a data-loss
    event" framing (OAQ-16). `EventBus.publish`'s own Protocol contract
    already promises "must not raise for I/O," so this `except Exception`
    is a defense-in-depth backstop against a non-conforming adapter, not
    a substitute for a compliant one.
    """

    def __init__(self, sink: SemanticWriteSink, event_bus: EventBus) -> None:
        """Compose the projector from the Zone 3 write sink it decorates and an EventBus.

        Args:
            sink: The real Zone 3 write sink (e.g. `SqlSemanticRepository`)
                this projector wraps. Every write is delegated here
                unchanged before any projection is attempted.
            event_bus: The publish port Zone 6's index worker subscribes
                to. `NoOpEventBus` is a valid, structurally-typed choice
                for a Shape A / offline deployment (HLD Section 6).
        """
        self._sink = sink
        self._event_bus = event_bus

    def insert_edge(self, edge: SemanticEdge) -> None:
        """Persist `edge` durably, then best-effort publish its projection event.

        Raises:
            Whatever `self._sink.insert_edge` raises (e.g.
                `dashanan.domain.exceptions.ZoneRepositoryError`) -- the
                projection step is never reached in that case.
        """
        self._sink.insert_edge(edge)
        self._publish_projection(
            EVENT_TYPE_SEMANTIC_EDGE_PROJECTED, edge.tenant_id, edge.edge_id
        )

    def insert_general_fact(self, fact: GeneralFact) -> None:
        """Persist `fact` durably, then best-effort publish its projection event.

        Raises:
            Whatever `self._sink.insert_general_fact` raises -- the
                projection step is never reached in that case.
        """
        self._sink.insert_general_fact(fact)
        self._publish_projection(
            EVENT_TYPE_GENERAL_FACT_PROJECTED, fact.tenant_id, fact.fact_id
        )

    def _publish_projection(
        self, event_type: str, tenant_id: str, item_id: str
    ) -> None:
        """Publish the projection envelope; swallow and log any publish failure.

        The envelope is deliberately minimal -- `tenant_id`, `item_id`,
        `zone` -- never the underlying fact content (module PII note).
        """
        payload: dict[str, object] = {
            "tenant_id": tenant_id,
            "item_id": item_id,
            "zone": ZoneId.SEMANTIC.value,
        }
        try:
            self._event_bus.publish(event_type, payload)
        except Exception as exc:  # noqa: BLE001 -- OAQ-16: publish failure must never affect the already-durable write
            logger.warning(
                "Zone 3 retrieval-index projection publish failed; the "
                "underlying durable write is unaffected (recall "
                "degradation, not data loss, HLD Section 3.7 OAQ-16)",
                extra={
                    "event_type": event_type,
                    "tenant_id": tenant_id,
                    "item_id": item_id,
                    "error": str(exc),
                },
                exc_info=True,
            )
