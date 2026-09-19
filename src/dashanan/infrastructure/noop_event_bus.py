"""NoOpEventBus: the Null Object adapter for the EventBus port (HLD Section 6)."""

from __future__ import annotations

import logging

from dashanan.domain.ports import EventBus

logger = logging.getLogger(__name__)


class NoOpEventBus(EventBus):
    """Satisfies the `EventBus` port with no broker bound.

    Lets Shape A (embedded, offline-capable per NFR-005) run the exact
    same `MemoryOrchestrator` code path as Shape B, with no conditional
    branching in the application layer for "is a broker configured."
    Every publish is logged at DEBUG so the absence of real delivery is
    observable, never silent.
    """

    def publish(self, event_type: str, payload: dict[str, object]) -> None:
        """Discard the event after logging it at DEBUG level."""
        logger.debug(
            "NoOpEventBus discarded event",
            extra={"event_type": event_type, "payload_keys": list(payload.keys())},
        )
