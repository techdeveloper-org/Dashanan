"""MemoryOrchestrator: the Facade over Dashanan's eight memory zones (FR-009)."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from uuid import uuid4

from dashanan.application.assembly_result import AssemblyResult
from dashanan.application.context_assembly_builder import ContextAssemblyBuilder
from dashanan.application.context_assembly_request import ContextAssemblyRequest
from dashanan.domain.exceptions import ZoneRepositoryError
from dashanan.domain.ports import Clock, EventBus, ZoneQuery, ZoneRepository
from dashanan.domain.zone import ZoneId

logger = logging.getLogger(__name__)


class MemoryOrchestrator:
    """Single entry point for cross-zone reads (HLD Section 3.1, FR-009).

    FR-009 (verbatim): "The system SHALL provide a central Memory
    Orchestrator that routes reads and writes across all 8 zones and
    exposes a single, unified context-assembly API to the host AI
    system."

    This is the Facade named in HLD Section 6: a host calls
    `assemble_context` without naming an individual zone (AC-009), and
    the Orchestrator resolves which registered `ZoneRepository` adapters
    to query. A zone with no registered adapter -- every zone, in this
    story, since no zone story has landed yet -- is reported as
    unavailable in a typed `AssemblyResult`, never as an unhandled
    exception (AC-009-SUPP-1). Each zone story after this one registers
    its adapter with this constructor; the Facade itself does not change.
    """

    def __init__(
        self,
        zone_repositories: Mapping[ZoneId, ZoneRepository],
        event_bus: EventBus,
        clock: Clock,
    ) -> None:
        """Compose the Orchestrator from its ports.

        Args:
            zone_repositories: The zones this deployment can currently
                serve. An empty mapping is valid -- every zone then
                degrades per AC-009-SUPP-1 -- and is exactly the state
                of a fresh deployment before any zone story lands.
            event_bus: Where `context.assembled` lifecycle events are
                published. Inject `NoOpEventBus` for Shape A / offline
                deployments (HLD Section 6, "Embedded/offline mode" row).
            clock: Injectable time source, so assembly timestamps are
                deterministic under test (testing-core).
        """
        self._zone_repositories: dict[ZoneId, ZoneRepository] = dict(
            zone_repositories
        )
        self._event_bus = event_bus
        self._clock = clock

    def assemble_context(self, request: ContextAssemblyRequest) -> AssemblyResult:
        """Resolve and assemble context without the host naming a zone.

        Implements AC-009: routes to whichever zones are registered,
        without the caller needing zone-level knowledge. Implements
        AC-009-SUPP-1: a zone with no registered adapter, or whose
        adapter raises `ZoneRepositoryError`, is recorded in
        `zones_unavailable` and never surfaces as an unhandled exception.
        Implements AC-009-R1-1: every returned `AssemblyResult`, success
        or degraded, carries a fresh `assembly_id` and `trace_id`. A
        duplicate `ZoneId` in `request.zones` -- `ContextAssemblyRequest`
        does not itself reject or dedupe the list -- is collapsed to a
        single fetch here, order preserved, so a repeated zone entry can
        never double-fetch and double-add the same items into the
        assembled result.

        Args:
            request: The host's context-assembly call.

        Returns:
            The budget-fitted `AssemblyResult`. `degraded` is True and
            `zones_unavailable` is non-empty whenever at least one
            targeted zone could not be reached.
        """
        assembly_id = str(uuid4())
        trace_id = str(uuid4())
        target_zones = (
            list(dict.fromkeys(request.zones))
            if request.zones is not None
            else list(ZoneId)
        )

        builder = (
            ContextAssemblyBuilder()
            .with_token_budget(request.token_budget)
            .with_identifiers(assembly_id, trace_id)
        )
        zone_query = ZoneQuery(
            tenant_id=request.tenant_id,
            task=request.task,
            query_embedding=request.query_embedding,
            max_items=request.max_items,
            min_provenance_conf=request.min_provenance_conf,
            as_of=request.as_of,
        )

        for zone in target_zones:
            self._fetch_zone(zone, zone_query, builder, assembly_id, trace_id)

        result = builder.build()
        self._publish_assembled_event(result)
        return result

    def _fetch_zone(
        self,
        zone: ZoneId,
        zone_query: ZoneQuery,
        builder: ContextAssemblyBuilder,
        assembly_id: str,
        trace_id: str,
    ) -> None:
        """Fetch one zone's candidates into `builder`, degrading on failure.

        Never raises: a missing adapter or a `ZoneRepositoryError` from
        an adapter that does exist both resolve to
        `builder.mark_zone_unavailable(zone)`, per AC-009-SUPP-1.
        """
        repository = self._zone_repositories.get(zone)
        if repository is None:
            logger.warning(
                "zone not available: no ZoneRepository registered",
                extra={
                    "zone": zone.value,
                    "assembly_id": assembly_id,
                    "trace_id": trace_id,
                },
            )
            builder.mark_zone_unavailable(zone)
            return

        try:
            candidates = repository.fetch(zone_query)
        except ZoneRepositoryError:
            logger.error(
                "zone repository fetch failed",
                exc_info=True,
                extra={
                    "zone": zone.value,
                    "assembly_id": assembly_id,
                    "trace_id": trace_id,
                },
            )
            builder.mark_zone_unavailable(zone)
            return

        builder.add_candidates(candidates)

    def _publish_assembled_event(self, result: AssemblyResult) -> None:
        """Publish a `context.assembled` event summarizing this assembly."""
        self._event_bus.publish(
            "context.assembled",
            {
                "assembly_id": result.assembly_id,
                "trace_id": result.trace_id,
                "degraded": result.degraded,
                "zones_unavailable": [
                    zone.value for zone in result.zones_unavailable
                ],
                "items_returned": len(result.items),
                "assembled_at": self._clock.now().isoformat(),
            },
        )
