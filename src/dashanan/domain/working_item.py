"""WorkingItem: Zone 1 (Working Memory) domain entity (HLD Section 3.2, FR-001)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping


@dataclass(frozen=True, slots=True)
class WorkingItem:
    """A single scratch-context item held in Zone 1 (Working Memory).

    HLD 3.2 owned-entity shape: `WorkingItem(tenant_id, session_id,
    item_id, payload, token_count, score_terms, written_at,
    last_access_at)`. Zone 1 is TTL-primary and capacity-primary
    (HLD 3.2, 12A), never score-primary -- `score_terms` is carried
    through unevaluated pending the Scoring Service (DASH-STORY-008)
    and is never consulted by the repository's own eviction ordering.

    Frozen so every mutation (an access refreshing `last_access_at`, a
    re-write refreshing `payload`) goes through `dataclasses.replace`,
    matching this codebase's existing Value-Object-style entity
    convention (`MemoryItem`, HLD Section 6).

    Attributes:
        tenant_id: Owning tenant. Mandatory at every Zone 1 storage
            operation (HLD 3.0 invariant 2) -- never defaulted.
        session_id: The host session this scratch item belongs to.
        item_id: Identifier of the item within its session.
        payload: Zone-owned content. Opaque to the Orchestrator.
        token_count: Cost of including this item in an assembled
            context (HLD Section 5, "Context assembler" row).
        score_terms: Placeholder six-term inputs for the future
            MemoryScore (HLD Section 12G / DASH-STORY-008). Unused by
            Zone 1's own TTL-primary eviction.
        written_at: When this item was first written to Zone 1.
        last_access_at: When this item was last read or re-written.
            Zone 1's idle-TTL eviction (HLD 12A: "TTL (30 min idle)")
            measures elapsed time from this field, not `written_at`.
    """

    tenant_id: str
    session_id: str
    item_id: str
    payload: str
    token_count: int
    written_at: datetime
    last_access_at: datetime
    score_terms: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Enforce the invariants Zone 1's LRU repository depends on.

        Raises:
            ValueError: If `tenant_id`, `session_id`, or `item_id` is
                blank, or `token_count` is not positive.
        """
        if not self.tenant_id.strip():
            raise ValueError("WorkingItem.tenant_id must not be blank")
        if not self.session_id.strip():
            raise ValueError("WorkingItem.session_id must not be blank")
        if not self.item_id.strip():
            raise ValueError("WorkingItem.item_id must not be blank")
        if self.token_count <= 0:
            raise ValueError(
                f"WorkingItem.token_count must be positive, got {self.token_count}"
            )
