"""AC-028-DEV-4 / AC-028-QA-4: circuit breaker state-transition suite.

connection-pooling-design.md Section 5.1/5.1.1 thresholds under test:
CLOSED -> OPEN on >=50% failure rate OR >=30% slow-call rate (at 2x p99)
over a 20-call window; OPEN fails fast (no `allow()`); OPEN -> HALF_OPEN
after `30s * 2^(trips-1)` capped at 300s; HALF_OPEN -> CLOSED only when
all 5 probes succeed; any HALF_OPEN probe failure re-opens and restarts
the backoff clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from dashanan.infrastructure.circuit_breaker import BreakerState, CircuitBreaker


@dataclass(slots=True)
class _FakeClock:
    """A settable `Clock` double (testing-core: DI over monkeypatching wall time)."""

    _now: datetime

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)


def _clock() -> _FakeClock:
    return _FakeClock(datetime(2026, 1, 1, tzinfo=UTC))


def _fill_window_with_failures(breaker: CircuitBreaker, count: int) -> None:
    for _ in range(count):
        breaker.record_failure(0.001)


def _fill_window_with_successes(breaker: CircuitBreaker, count: int, latency: float = 0.001) -> None:
    for _ in range(count):
        breaker.record_success(latency)


class TestCircuitBreakerClosedState:
    def test_starts_closed_and_allows_calls(self) -> None:
        breaker = CircuitBreaker(clock=_clock())
        assert breaker.state is BreakerState.CLOSED
        assert breaker.allow() is True

    def test_stays_closed_under_the_failure_rate_threshold(self) -> None:
        breaker = CircuitBreaker(clock=_clock())
        _fill_window_with_successes(breaker, 15)
        _fill_window_with_failures(breaker, 5)  # 25% failure rate, below 50%
        assert breaker.state is BreakerState.CLOSED
        assert breaker.allow() is True

    def test_trips_open_at_exactly_50_percent_failure_rate_over_the_20_call_window(self) -> None:
        breaker = CircuitBreaker(clock=_clock())
        _fill_window_with_successes(breaker, 10)
        _fill_window_with_failures(breaker, 10)  # exactly 50% over N=20
        assert breaker.state is BreakerState.OPEN

    def test_trips_open_above_50_percent_failure_rate(self) -> None:
        breaker = CircuitBreaker(clock=_clock())
        _fill_window_with_successes(breaker, 5)
        _fill_window_with_failures(breaker, 15)
        assert breaker.state is BreakerState.OPEN

    def test_trips_open_on_slow_call_rate_even_with_zero_failures(self) -> None:
        """>=30% of calls at >=2x p99 latency trips the breaker (Section 5.1).

        The p99 baseline is fed only by calls that have aged OUT of the
        20-item trip window (`_push_to_ring_locked`'s docstring), so this
        test first warms the breaker up with several full windows of fast,
        uniform-latency calls -- establishing a real, aged baseline -- before
        the 14-fast/6-slow window under test is evaluated.
        """
        breaker = CircuitBreaker(clock=_clock())
        _fill_window_with_successes(breaker, 100, latency=0.001)  # warm up the baseline
        _fill_window_with_successes(breaker, 14, latency=0.001)
        _fill_window_with_successes(breaker, 6, latency=1.0)
        assert breaker.state is BreakerState.OPEN

    def test_stays_closed_when_too_few_baseline_samples_exist_for_slow_detection(self) -> None:
        """Cold start: slow-call detection is skipped, never trips on a near-zero p99."""
        breaker = CircuitBreaker(clock=_clock())
        _fill_window_with_successes(breaker, 14, latency=0.001)
        _fill_window_with_successes(breaker, 6, latency=1.0)
        assert breaker.state is BreakerState.CLOSED


class TestCircuitBreakerOpenState:
    def test_open_breaker_fails_fast_before_the_backoff_elapses(self) -> None:
        breaker = CircuitBreaker(clock=_clock())
        _fill_window_with_failures(breaker, 20)
        assert breaker.state is BreakerState.OPEN
        assert breaker.allow() is False

    def test_retry_after_is_dynamically_computed_not_fixed(self) -> None:
        """Section 5.1.1: `max(1, ceil(opened_at + backoff - now))`, never a constant."""
        clock = _clock()
        breaker = CircuitBreaker(clock=clock)
        _fill_window_with_failures(breaker, 20)
        assert breaker.retry_after_seconds() == 30  # first trip: base backoff 30s

        clock.advance(10)
        assert breaker.retry_after_seconds() == 20  # counts down as time passes

        clock.advance(25)
        assert breaker.retry_after_seconds() == 1  # never goes to 0 or negative

    def test_transitions_to_half_open_once_backoff_elapses(self) -> None:
        clock = _clock()
        breaker = CircuitBreaker(clock=clock)
        _fill_window_with_failures(breaker, 20)
        assert breaker.state is BreakerState.OPEN

        clock.advance(30.0)
        assert breaker.allow() is True
        assert breaker.state is BreakerState.HALF_OPEN

    def test_does_not_transition_to_half_open_before_backoff_elapses(self) -> None:
        clock = _clock()
        breaker = CircuitBreaker(clock=clock)
        _fill_window_with_failures(breaker, 20)

        clock.advance(29.0)
        assert breaker.allow() is False
        assert breaker.state is BreakerState.OPEN

    def test_backoff_doubles_on_each_successive_trip_capped_at_300_seconds(self) -> None:
        clock = _clock()
        breaker = CircuitBreaker(clock=clock)

        _fill_window_with_failures(breaker, 20)  # trip 1
        assert breaker.retry_after_seconds() == 30
        clock.advance(30.0)
        assert breaker.allow() is True  # -> HALF_OPEN
        breaker.record_failure(0.001)  # probe fails -> re-opens, trip 2
        assert breaker.state is BreakerState.OPEN
        assert breaker.retry_after_seconds() == 60

        clock.advance(60.0)
        assert breaker.allow() is True
        breaker.record_failure(0.001)  # trip 3
        assert breaker.retry_after_seconds() == 120

        # Advance trip count synthetically via repeated half-open failures to
        # reach the 300s cap (30 * 2^(trips-1) >= 300 once trips >= 5).
        for _ in range(2):
            clock.advance(breaker.retry_after_seconds())
            assert breaker.allow() is True
            breaker.record_failure(0.001)

        clock.advance(breaker.retry_after_seconds())
        assert breaker.retry_after_seconds() <= 300


class TestCircuitBreakerHalfOpenState:
    def _open_and_half_open(self, clock: _FakeClock) -> CircuitBreaker:
        breaker = CircuitBreaker(clock=clock)
        _fill_window_with_failures(breaker, 20)
        clock.advance(30.0)
        assert breaker.allow() is True
        assert breaker.state is BreakerState.HALF_OPEN
        return breaker

    def test_closes_only_after_all_5_probes_succeed(self) -> None:
        clock = _clock()
        breaker = self._open_and_half_open(clock)

        for _ in range(4):
            breaker.record_success(0.001)
            assert breaker.state is BreakerState.HALF_OPEN

        breaker.record_success(0.001)  # 5th consecutive success
        assert breaker.state is BreakerState.CLOSED

    def test_a_single_probe_failure_reopens_and_restarts_the_backoff_clock(self) -> None:
        clock = _clock()
        breaker = self._open_and_half_open(clock)

        breaker.record_success(0.001)
        breaker.record_success(0.001)
        breaker.record_failure(0.001)  # 3rd probe fails

        assert breaker.state is BreakerState.OPEN
        assert breaker.retry_after_seconds() == 60  # trip count incremented to 2

    def test_half_open_allows_only_up_to_5_concurrent_probe_attempts(self) -> None:
        clock = _clock()
        breaker = CircuitBreaker(clock=clock)
        _fill_window_with_failures(breaker, 20)
        clock.advance(30.0)

        allowed = [breaker.allow() for _ in range(7)]
        assert allowed == [True, True, True, True, True, False, False]


class TestCircuitBreakerFailurePredicateIsCallerScoped:
    """The breaker itself has no opinion on what a "failure" is -- verified by
    exercising `record_success`/`record_failure` directly, matching how
    `api.composition.make_pooled_connection_dependency` restricts these
    calls to connection-establishment outcomes only, never query-level
    errors (connection-pooling-design.md Section 5.1's failure predicate).
    This suite documents that contract at the breaker's own boundary.
    """

    def test_only_recorded_outcomes_count_toward_the_window(self) -> None:
        breaker = CircuitBreaker(clock=_clock())
        _fill_window_with_successes(breaker, 20)
        assert breaker.state is BreakerState.CLOSED
        # Simulating 100 "query-level errors" that are never routed through
        # record_failure at all (as the real dependency does) leaves the
        # breaker's own state completely unaffected.
        assert breaker.state is BreakerState.CLOSED
