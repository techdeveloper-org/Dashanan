"""Domain ports (HLD Section 3.0): ZoneRepository, EventBus, Clock.

Every port is a `typing.Protocol` so infrastructure adapters satisfy it
through structural typing (Interface Segregation, Dependency Inversion)
without importing this module -- HLD invariant 1 forbids the reverse
import, not this direction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from dashanan.domain.memory_item import MemoryItem


@dataclass(frozen=True, slots=True)
class ZoneQuery:
    """Parameters a `ZoneRepository` needs to serve one assembly request.

    Mirrors the subset of the `/v1/context/assemble` request shape (HLD
    Section 7.1) that is meaningful at the per-zone fetch boundary; the
    Orchestrator strips `session_id`, `zones` and `include_provenance`
    before delegating, since those are routing concerns, not per-zone
    query concerns.
    """

    tenant_id: str
    task: str | None
    query_embedding: list[float] | None
    max_items: int
    min_provenance_conf: float
    as_of: datetime | None = None


@runtime_checkable
class ZoneRepository(Protocol):
    """Read port a zone implements to supply candidates for assembly.

    A zone story (DASH-STORY-002 onward) registers one concrete adapter
    per `ZoneId` with the Memory Orchestrator constructor; this Protocol
    is the frozen contract those adapters are built against (AR1-G2).
    """

    def fetch(self, query: ZoneQuery) -> list[MemoryItem]:
        """Return zero or more candidate items matching `query`.

        Raises:
            dashanan.domain.exceptions.ZoneRepositoryError: If the zone
                cannot serve the fetch. The Orchestrator maps this to a
                `zones_unavailable` entry rather than propagating it.
        """
        ...


@runtime_checkable
class EventBus(Protocol):
    """Publish port for zone-transition and assembly-lifecycle events.

    `NoOpEventBus` (infrastructure, Null Object pattern) satisfies this
    Protocol for Shape A / offline deployments with no broker bound.
    """

    def publish(self, event_type: str, payload: dict[str, object]) -> None:
        """Publish `payload` under `event_type`. Must not raise for I/O."""
        ...


@runtime_checkable
class Clock(Protocol):
    """Injectable time source so orchestrator behaviour is deterministic in tests."""

    def now(self) -> datetime:
        """Return the current timezone-aware instant."""
        ...
