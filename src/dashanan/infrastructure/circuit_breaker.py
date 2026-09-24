"""Generic count-based ring-buffer circuit breaker (DASH-STORY-028-DEV, AC-028-DEV-4).

Flagged gap (never fabricated): HLD.md Section 5's "Circuit breakers" DSA row
and Section 8's vector-store (Qdrant) breaker table entry both *describe* this
pattern -- count-based ring buffer, N=20, failure-rate/slow-call-rate
thresholds, `30s * 2^(trips-1)` capped-at-300s half-open backoff, 5-probe
recovery -- but neither HLD.md nor any other module in this codebase contains
an actual implementation of it prior to this file. `connection-pooling-
design.md` Section 5.1 states this sub-task "reuses the same count-based
ring-buffer implementation HLD Section 5 already mandates ... this is not a
second, bespoke breaker design"; that statement is accurate as a description
of the DESIGN this module conforms to, but no prior CODE existed to import
from. This module is therefore the first concrete implementation of that
documented pattern in this codebase, written once, here, so that this
sub-task's Postgres breaker and any future adapter breaker (the vector-store
one HLD Section 8 already specifies) both depend on the SAME utility instead
of each writing their own -- closing the "second, bespoke breaker" concern
going forward rather than reusing code that did not yet exist.

Failure/success classification is the CALLER's responsibility (this module
has no opinion on what counts as a "connect failure" vs. a "query failure");
`connection_pool.py`'s `get_pooled_connection` dependency is what restricts
`record_failure`/`record_success` calls to connection-establishment outcomes
only, per `connection-pooling-design.md` Section 5.1's failure predicate.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from math import ceil

from dashanan.domain.ports import Clock

DEFAULT_WINDOW_SIZE = 20
"""HLD Section 5's "Circuit breakers" DSA row: count-based ring buffer, N=20."""

DEFAULT_FAILURE_RATE_THRESHOLD = 0.5
DEFAULT_SLOW_CALL_RATE_THRESHOLD = 0.3
DEFAULT_SLOW_CALL_MULTIPLIER = 2.0
"""A call counts as "slow" once it exceeds `DEFAULT_SLOW_CALL_MULTIPLIER` times
the breaker's own running p99 latency estimate over the current window."""

DEFAULT_BASE_BACKOFF_SECONDS = 30.0
DEFAULT_MAX_BACKOFF_SECONDS = 300.0
DEFAULT_HALF_OPEN_PROBE_COUNT = 5
"""HLD Section 8's vector-store breaker: `30s * 2^(trips-1)` capped at 300s,
5 probe attempts, all must succeed to close."""

DEFAULT_LATENCY_BASELINE_SIZE = 100
"""How many past successful-call latencies feed the running p99 estimate
(`_p99_latency_locked`). Deliberately LARGER than and INDEPENDENT of the
`window_size`-sized trip-detection ring: computing "2x p99" from the same
small window a slow-call burst is actively filling is self-defeating -- with
only `window_size` samples, the 99th percentile of the window is close to
its own maximum, so a burst of uniformly slow calls raises the threshold
right along with the calls meant to trip it. A larger, longer-lived baseline
means a genuine latency regression is judged against how the connection
behaved BEFORE the regression, not against itself."""


class BreakerState(Enum):
    """The three states this breaker's state machine moves through."""

    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


@dataclass(slots=True)
class _CallOutcome:
    success: bool
    latency_seconds: float


class CircuitBreaker:
    """Count-based ring-buffer breaker (HLD Section 5/8's documented pattern).

    Thread-safe: every method acquires an internal lock, since FastAPI's
    synchronous host (connection-pooling-design.md Section 2) serves
    concurrent requests from a thread pool, and this breaker's state is
    shared across all of them.
    """

    def __init__(
        self,
        clock: Clock,
        *,
        window_size: int = DEFAULT_WINDOW_SIZE,
        failure_rate_threshold: float = DEFAULT_FAILURE_RATE_THRESHOLD,
        slow_call_rate_threshold: float = DEFAULT_SLOW_CALL_RATE_THRESHOLD,
        slow_call_multiplier: float = DEFAULT_SLOW_CALL_MULTIPLIER,
        base_backoff_seconds: float = DEFAULT_BASE_BACKOFF_SECONDS,
        max_backoff_seconds: float = DEFAULT_MAX_BACKOFF_SECONDS,
        half_open_probe_count: int = DEFAULT_HALF_OPEN_PROBE_COUNT,
        latency_baseline_size: int = DEFAULT_LATENCY_BASELINE_SIZE,
    ) -> None:
        self._clock = clock
        self._window_size = window_size
        self._failure_rate_threshold = failure_rate_threshold
        self._slow_call_rate_threshold = slow_call_rate_threshold
        self._slow_call_multiplier = slow_call_multiplier
        self._base_backoff_seconds = base_backoff_seconds
        self._max_backoff_seconds = max_backoff_seconds
        self._half_open_probe_count = half_open_probe_count

        self._lock = threading.Lock()
        self._ring: deque[_CallOutcome] = deque(maxlen=window_size)
        self._latency_baseline: deque[float] = deque(maxlen=latency_baseline_size)
        self._state = BreakerState.CLOSED
        self._trip_count = 0
        self._opened_at: datetime | None = None
        self._half_open_attempts = 0
        self._half_open_successes = 0

    @property
    def state(self) -> BreakerState:
        with self._lock:
            return self._state

    def allow(self) -> bool:
        """Return whether a new call may proceed, transitioning OPEN->HALF_OPEN if due.

        Called BEFORE `pool.connection(...)` (connection-pooling-design.md
        Section 5.1's "checked before `pool.connection(...)` is called at
        all" requirement) -- `False` means fail fast with no pool
        acquisition attempted at all.
        """
        with self._lock:
            if self._state is BreakerState.CLOSED:
                return True
            if self._state is BreakerState.OPEN:
                if self._backoff_elapsed_locked():
                    self._state = BreakerState.HALF_OPEN
                    self._half_open_attempts = 1  # this call itself is probe #1
                    self._half_open_successes = 0
                    return True
                return False
            # HALF_OPEN: allow up to `_half_open_probe_count` concurrent probes.
            if self._half_open_attempts < self._half_open_probe_count:
                self._half_open_attempts += 1
                return True
            return False

    def record_success(self, latency_seconds: float) -> None:
        """Record a successful connection-establishment outcome."""
        with self._lock:
            self._push_to_ring_locked(_CallOutcome(success=True, latency_seconds=latency_seconds))
            if self._state is BreakerState.HALF_OPEN:
                self._half_open_successes += 1
                if self._half_open_successes >= self._half_open_probe_count:
                    self._close_locked()
                return
            self._evaluate_locked()

    def record_failure(self, latency_seconds: float = 0.0) -> None:
        """Record a failed connection-establishment outcome (never a query-level error)."""
        with self._lock:
            self._push_to_ring_locked(_CallOutcome(success=False, latency_seconds=latency_seconds))
            if self._state is BreakerState.HALF_OPEN:
                self._trip_locked()
                return
            self._evaluate_locked()

    def _push_to_ring_locked(self, outcome: _CallOutcome) -> None:
        """Append to the trip-detection ring, feeding an evicted item into the latency baseline.

        `self._latency_baseline` therefore holds only latencies that have
        already aged OUT of the current `window_size`-sized trip window --
        never the window's own contents. This is what keeps `_p99_latency_
        locked`'s threshold from being self-referential: a burst of slow
        calls filling the window cannot simultaneously inflate the baseline
        it is being judged against, since those calls have not aged out of
        the window yet (module docstring: `DEFAULT_LATENCY_BASELINE_SIZE`).
        """
        if len(self._ring) == self._ring.maxlen:
            evicted = self._ring[0]
            if evicted.success:
                self._latency_baseline.append(evicted.latency_seconds)
        self._ring.append(outcome)

    def retry_after_seconds(self) -> int:
        """`max(1, ceil(opened_at + backoff_duration - now))` (Section 5.1.1).

        Meaningful only while OPEN; returns `1` in any other state (callers
        only invoke this after `allow()` returned `False`, which implies
        OPEN, so this is a defensive fallback, not a documented contract for
        other states).
        """
        with self._lock:
            if self._state is not BreakerState.OPEN or self._opened_at is None:
                return 1
            backoff = self._current_backoff_seconds_locked()
            remaining = (
                self._opened_at.timestamp() + backoff - self._clock.now().timestamp()
            )
            return max(1, ceil(remaining))

    _MIN_BASELINE_SAMPLES = 5
    """Below this many aged-out latency samples, slow-call detection is
    skipped entirely (failure-rate detection still applies) rather than
    tripping on a meaningless near-zero p99 estimate during cold start --
    a fresh breaker (or one just after a `_close_locked`/`_trip_locked`
    reset, both of which clear the ring but NOT the baseline) has not yet
    aged any calls out of its very first window."""

    def _evaluate_locked(self) -> None:
        if len(self._ring) < self._window_size:
            return
        failures = sum(1 for c in self._ring if not c.success)
        failure_rate = failures / len(self._ring)
        slow_rate = 0.0
        p99_latency = self._p99_latency_locked()
        if p99_latency is not None:
            slow_calls = sum(
                1
                for c in self._ring
                if c.success and c.latency_seconds > p99_latency * self._slow_call_multiplier
            )
            slow_rate = slow_calls / len(self._ring)
        if failure_rate >= self._failure_rate_threshold or slow_rate >= self._slow_call_rate_threshold:
            self._trip_locked()

    def _p99_latency_locked(self) -> float | None:
        """The baseline's 99th-percentile latency, or `None` during cold start.

        Computed from `self._latency_baseline` (calls already aged out of
        the trip window), never from `self._ring` itself -- see
        `_push_to_ring_locked`'s docstring for why.
        """
        if len(self._latency_baseline) < self._MIN_BASELINE_SAMPLES:
            return None
        baseline = sorted(self._latency_baseline)
        index = min(len(baseline) - 1, ceil(0.99 * len(baseline)) - 1)
        return baseline[max(index, 0)]

    def _trip_locked(self) -> None:
        self._trip_count += 1
        self._state = BreakerState.OPEN
        self._opened_at = self._clock.now()
        self._ring.clear()

    def _close_locked(self) -> None:
        self._state = BreakerState.CLOSED
        self._trip_count = 0
        self._opened_at = None
        self._ring.clear()

    def _current_backoff_seconds_locked(self) -> float:
        exponent = max(0, self._trip_count - 1)
        backoff = self._base_backoff_seconds * float(2**exponent)
        return min(backoff, self._max_backoff_seconds)

    def _backoff_elapsed_locked(self) -> bool:
        if self._opened_at is None:
            return True
        backoff = self._current_backoff_seconds_locked()
        return self._clock.now().timestamp() >= self._opened_at.timestamp() + backoff
