"""Smoke assertions for DASH-STORY-001, inline per the dev subtask scope.

The formal pytest suite covering every AC is the QA subtask's
responsibility. These checks only confirm the package is importable and
wired correctly before that suite lands.
"""

from __future__ import annotations

from dashanan.application.context_assembly_request import ContextAssemblyRequest
from dashanan.application.memory_orchestrator import MemoryOrchestrator
from dashanan.domain.zone import ZoneId
from dashanan.infrastructure.noop_event_bus import NoOpEventBus
from dashanan.infrastructure.system_clock import SystemClock


def test_assemble_context_degrades_when_no_zones_registered() -> None:
    """AC-009-SUPP-1: an unimplemented zone degrades instead of raising."""
    orchestrator = MemoryOrchestrator(
        zone_repositories={},
        event_bus=NoOpEventBus(),
        clock=SystemClock(),
        tenant_credential_signing_key=None,
    )
    request = ContextAssemblyRequest(
        tenant_id="tenant-1",
        session_id="session-1",
        task="what did we discuss last time",
        token_budget=1000,
    )

    result = orchestrator.assemble_context(request)

    assert result.degraded is True
    assert set(result.zones_unavailable) == set(ZoneId)
    assert result.items == []


def test_assemble_context_always_carries_unique_ids() -> None:
    """AC-009-R1-1: every response carries a unique assembly_id and trace_id."""
    orchestrator = MemoryOrchestrator(
        zone_repositories={},
        event_bus=NoOpEventBus(),
        clock=SystemClock(),
        tenant_credential_signing_key=None,
    )
    request = ContextAssemblyRequest(
        tenant_id="tenant-1",
        session_id="session-1",
        task="recall the last order",
        token_budget=500,
    )

    first = orchestrator.assemble_context(request)
    second = orchestrator.assemble_context(request)

    assert first.assembly_id and first.trace_id
    assert second.assembly_id and second.trace_id
    assert first.assembly_id != second.assembly_id
    assert first.trace_id != second.trace_id


def test_assemble_context_does_not_require_zone_level_knowledge() -> None:
    """AC-009: the host omits `zones` and still gets a routed response."""
    orchestrator = MemoryOrchestrator(
        zone_repositories={},
        event_bus=NoOpEventBus(),
        clock=SystemClock(),
        tenant_credential_signing_key=None,
    )
    request = ContextAssemblyRequest(
        tenant_id="tenant-1",
        session_id="session-1",
        task="anything relevant",
        token_budget=200,
    )

    result = orchestrator.assemble_context(request)

    assert result.token_budget == 200
    assert result.token_count_total == 0
