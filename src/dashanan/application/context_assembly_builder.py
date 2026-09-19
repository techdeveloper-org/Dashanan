"""ContextAssemblyBuilder: incremental, budget-validated AssemblyResult construction."""

from __future__ import annotations

from collections.abc import Iterable

from dashanan.application.assembly_result import AssemblyResult
from dashanan.domain.memory_item import MemoryItem
from dashanan.domain.zone import ZoneId


class ContextAssemblyBuilder:
    """Builder pattern for `AssemblyResult` (HLD Section 6, "Context assembly" row).

    A `ContextAssembly` is built incrementally from heterogeneous zone
    candidates, but the token-budget invariant -- never exceed
    `token_budget` -- must hold only at `build()`, not after each
    intermediate step. That is exactly the problem the Builder pattern
    solves over emitting a value object gradually.

    Fluent methods return `self` so `MemoryOrchestrator` composes one
    call chain per assembly.
    """

    def __init__(self) -> None:
        """Start an empty builder with no budget, candidates or identifiers set."""
        self._candidates: list[MemoryItem] = []
        self._zones_unavailable: list[ZoneId] = []
        self._token_budget: int = 0
        self._assembly_id: str = ""
        self._trace_id: str = ""

    def with_token_budget(self, token_budget: int) -> ContextAssemblyBuilder:
        """Set the knapsack capacity the greedy fill must respect."""
        self._token_budget = token_budget
        return self

    def with_identifiers(
        self, assembly_id: str, trace_id: str
    ) -> ContextAssemblyBuilder:
        """Bind the unique identifiers every response carries (AC-009-R1-1)."""
        self._assembly_id = assembly_id
        self._trace_id = trace_id
        return self

    def add_candidates(self, items: Iterable[MemoryItem]) -> ContextAssemblyBuilder:
        """Add items sourced from one successfully reached zone."""
        self._candidates.extend(items)
        return self

    def mark_zone_unavailable(self, zone: ZoneId) -> ContextAssemblyBuilder:
        """Record a zone that could not contribute (AC-009-SUPP-1)."""
        if zone not in self._zones_unavailable:
            self._zones_unavailable.append(zone)
        return self

    def build(self) -> AssemblyResult:
        """Validate identifiers and run the greedy fill to produce the result.

        Raises:
            ValueError: If `with_identifiers` was never called -- every
                assembly must carry an assembly_id and trace_id
                (AC-009-R1-1), so building without them is a programming
                error in the caller, not a degraded-but-valid outcome.
        """
        if not self._assembly_id or not self._trace_id:
            raise ValueError(
                "ContextAssemblyBuilder.build() requires with_identifiers() "
                "to have been called first"
            )
        selected = self._greedy_knapsack_fill()
        token_count_total = sum(item.token_count for item in selected)
        return AssemblyResult(
            items=selected,
            degraded=bool(self._zones_unavailable),
            zones_unavailable=list(self._zones_unavailable),
            assembly_id=self._assembly_id,
            trace_id=self._trace_id,
            token_count_total=token_count_total,
            token_budget=self._token_budget,
        )

    def _greedy_knapsack_fill(self) -> list[MemoryItem]:
        """Select items by descending value density under the token budget.

        HLD Section 5 mandates this approximation over an exact 0/1
        knapsack DP solve: sort candidates by score/token_count (value
        density) descending, then greedily accept each item that still
        fits. O(n log n) for the sort, O(n) for the fill -- O(n log n)
        overall, versus the DP's infeasible O(n * token_budget) on the
        synchronous read path.

        Returns:
            Selected items in descending value-density order, each
            confirmed to fit within the remaining budget at the time it
            was considered.
        """
        ranked = sorted(
            self._candidates,
            key=lambda item: item.score / item.token_count,
            reverse=True,
        )
        selected: list[MemoryItem] = []
        remaining_budget = self._token_budget
        for item in ranked:
            if item.token_count <= remaining_budget:
                selected.append(item)
                remaining_budget -= item.token_count
        return selected
