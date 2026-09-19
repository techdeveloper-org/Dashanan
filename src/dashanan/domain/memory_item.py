"""MemoryItem: the aggregate root shared by every zone (HLD Section 3.0)."""

from __future__ import annotations

from dataclasses import dataclass

from dashanan.domain.zone import ZoneId


@dataclass(frozen=True, slots=True)
class MemoryItem:
    """A single unit of memory sourced from one zone.

    This is the minimal shape the Memory Orchestrator needs to route,
    score-rank and budget-fit candidates during context assembly
    (HLD Section 5, "Context assembler" row). Per-zone stories extend
    zone-specific payload semantics without changing this contract, since
    `payload` is intentionally opaque to the Orchestrator.

    Attributes:
        item_id: Stable identifier of the item within its source zone.
        source_zone: Which of the eight zones produced this item.
        payload: Zone-owned content. The Orchestrator never inspects it.
        token_count: Cost of including this item in an assembled context,
            used by the greedy knapsack value-density fill.
        score: Placeholder for the MemoryScore compound value (HLD Section
            12G). Defaults to 0.0 until the Scoring Service story lands.
    """

    item_id: str
    source_zone: ZoneId
    payload: str
    token_count: int
    score: float = 0.0

    def __post_init__(self) -> None:
        """Enforce the invariants a downstream knapsack fill depends on.

        Raises:
            ValueError: If `token_count` is not positive, or `item_id` is
                blank -- both would corrupt the value-density ordering.
        """
        if not self.item_id.strip():
            raise ValueError("MemoryItem.item_id must not be blank")
        if self.token_count <= 0:
            raise ValueError(
                f"MemoryItem.token_count must be positive, got {self.token_count}"
            )
