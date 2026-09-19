"""SystemClock: the default production adapter for the Clock port."""

from __future__ import annotations

from datetime import UTC, datetime

from dashanan.domain.ports import Clock


class SystemClock(Clock):
    """Returns the real wall-clock time, timezone-aware in UTC.

    Kept separate from `MemoryOrchestrator` so tests can inject a fake
    `Clock` instead (python-core section 19, `testing-core`: dependency
    injection over patching `datetime.now` directly).
    """

    def now(self) -> datetime:
        """Return the current instant in UTC."""
        return datetime.now(UTC)
