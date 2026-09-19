"""AssemblyResult: the Result type returned by every context-assembly call."""

from __future__ import annotations

from dataclasses import dataclass, field

from dashanan.domain.memory_item import MemoryItem
from dashanan.domain.zone import ZoneId


@dataclass(frozen=True, slots=True)
class AssemblyResult:
    """Outcome of one `MemoryOrchestrator.assemble_context` call.

    A Result type (HLD Section 6, "Degraded responses" row): partial
    success is a normal, typed outcome rather than an exception, so every
    caller must read `degraded` and `zones_unavailable` instead of
    assuming a full response (error-handling-patterns section 8).

    Attributes:
        items: The budget-fitted items selected by the greedy knapsack
            fill, in selection order.
        degraded: True whenever at least one requested/default zone could
            not be reached. Never raises in that case (AC-009-SUPP-1).
        zones_unavailable: The zones that contributed no candidates,
            explicit rather than silent (HLD Section 7.1 response shape).
        assembly_id: A fresh UUID4 unique to this call, present even when
            `degraded` is True (AC-009-R1-1).
        trace_id: A fresh UUID4 unique to this call, for cross-service
            correlation, present even when `degraded` is True
            (AC-009-R1-1).
        token_count_total: Sum of `token_count` across `items`.
        token_budget: The budget the caller supplied, echoed back for the
            host's own bookkeeping.
    """

    items: list[MemoryItem] = field(default_factory=list)
    degraded: bool = False
    zones_unavailable: list[ZoneId] = field(default_factory=list)
    assembly_id: str = ""
    trace_id: str = ""
    token_count_total: int = 0
    token_budget: int = 0
