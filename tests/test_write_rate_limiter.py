"""Tests for the DSHN-60 MEDIUM remediation on HLD Threat D-1 (write-throughput admission control).

Covers `domain.write_rate_limiter`'s pure token-bucket logic and its
wiring into `ProvenanceWriteGate.submit_write` via `application.
provenance_write_gate.ProvenanceWriteGate`.

Runtime assumptions recorded per rule 33/40 test-roadmap conventions:
  - clock: FakeClock, fixed or manually advanced per test -- deterministic,
    no wall-clock dependency.
  - tenant_id: "tenant-1" for all requests unless a test states otherwise.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from dashanan.application.provenance_write_gate import (
    ERROR_RATE_LIMIT_EXCEEDED,
    ProvenanceWriteGate,
)
from dashanan.domain.write_gate import ProvenanceJournalEntry, WriteAccepted, WriteRejected, WriteRequest
from dashanan.domain.write_rate_limiter import (
    TokenBucketConfig,
    WriteRateLimiterConfigError,
    new_full_bucket,
    try_consume,
)
from dashanan.domain.zone import ZoneId

_PLACEHOLDER_CONTEXT_HASH = hashlib.sha256(b"<PII_EXAMPLE_REDACTED>").hexdigest()
_FIXED_TS = datetime(2026, 1, 1, tzinfo=UTC)
_SIGNING_KEY = b"rate-limiter-suite-test-only-user-turn-signing-key"


class FakeClock:
    def __init__(self, fixed: datetime) -> None:
        self._fixed = fixed

    def now(self) -> datetime:
        return self._fixed

    def advance(self, seconds: float) -> None:
        self._fixed = self._fixed + timedelta(seconds=seconds)


class RecordingJournal:
    def __init__(self) -> None:
        self.entries: list[ProvenanceJournalEntry] = []

    def append(self, entry: ProvenanceJournalEntry) -> None:
        self.entries.append(entry)

    def find_by_idempotency_key(
        self, tenant_id: str, idempotency_key: str
    ) -> ProvenanceJournalEntry | None:
        for entry in self.entries:
            if (
                entry.tenant_id == tenant_id
                and entry.idempotency_key == idempotency_key
            ):
                return entry
        return None


def _request(tenant_id: str = "tenant-1") -> WriteRequest:
    return WriteRequest(
        tenant_id=tenant_id,
        item_id=f"item-{uuid4()}",
        source_zone=ZoneId.EPISODIC,
        source_type_raw="tool_output",
        caller_identity="dashanan-orchestrator",
        retrieval_context_hash=_PLACEHOLDER_CONTEXT_HASH,
        idempotency_key=str(uuid4()),
    )


class TestTokenBucketConfigValidation:
    def test_non_positive_capacity_is_rejected(self) -> None:
        with pytest.raises(WriteRateLimiterConfigError):
            TokenBucketConfig(capacity=0, refill_per_second=1.0)

    def test_non_positive_refill_rate_is_rejected(self) -> None:
        with pytest.raises(WriteRateLimiterConfigError):
            TokenBucketConfig(capacity=10, refill_per_second=0.0)


class TestTryConsume:
    def test_a_fresh_full_bucket_allows_consumption_up_to_capacity(self) -> None:
        config = TokenBucketConfig(capacity=3, refill_per_second=1.0)
        state = new_full_bucket(config, _FIXED_TS)

        state, allowed_1 = try_consume(state, config, _FIXED_TS)
        state, allowed_2 = try_consume(state, config, _FIXED_TS)
        state, allowed_3 = try_consume(state, config, _FIXED_TS)
        state, allowed_4 = try_consume(state, config, _FIXED_TS)

        assert (allowed_1, allowed_2, allowed_3, allowed_4) == (True, True, True, False)

    def test_tokens_refill_over_elapsed_time_up_to_capacity(self) -> None:
        config = TokenBucketConfig(capacity=2, refill_per_second=1.0)
        state = new_full_bucket(config, _FIXED_TS)
        state, _ = try_consume(state, config, _FIXED_TS)
        state, _ = try_consume(state, config, _FIXED_TS)

        # Bucket is now empty; after 1 elapsed second, exactly 1 token refills.
        later = _FIXED_TS + timedelta(seconds=1)
        state, allowed = try_consume(state, config, later)
        assert allowed is True

        # Immediately after that, the bucket is empty again.
        state, allowed_again = try_consume(state, config, later)
        assert allowed_again is False

    def test_refill_never_exceeds_capacity(self) -> None:
        config = TokenBucketConfig(capacity=2, refill_per_second=100.0)
        state = new_full_bucket(config, _FIXED_TS)
        state, _ = try_consume(state, config, _FIXED_TS)

        much_later = _FIXED_TS + timedelta(seconds=1000)
        state, allowed_1 = try_consume(state, config, much_later)
        state, allowed_2 = try_consume(state, config, much_later)
        state, allowed_3 = try_consume(state, config, much_later)

        assert (allowed_1, allowed_2, allowed_3) == (True, True, False)

    def test_an_out_of_order_earlier_clock_reading_never_reduces_tokens(self) -> None:
        config = TokenBucketConfig(capacity=5, refill_per_second=1.0)
        state = new_full_bucket(config, _FIXED_TS)
        earlier = _FIXED_TS - timedelta(seconds=10)

        state, allowed = try_consume(state, config, earlier)
        assert allowed is True
        assert state.tokens == pytest.approx(4.0)


class TestProvenanceWriteGateRateLimiting:
    def test_a_burst_within_capacity_is_fully_accepted(self) -> None:
        gate = ProvenanceWriteGate(
            journal=RecordingJournal(),
            clock=FakeClock(_FIXED_TS),
            user_turn_signing_key=_SIGNING_KEY,
            rate_limit_config=TokenBucketConfig(capacity=5, refill_per_second=1.0),
        )
        results = [gate.submit_write(_request(), lambda: None) for _ in range(5)]
        assert all(isinstance(r, WriteAccepted) for r in results)

    def test_exceeding_capacity_rejects_with_429_and_does_not_journal(self) -> None:
        journal = RecordingJournal()
        gate = ProvenanceWriteGate(
            journal=journal,
            clock=FakeClock(_FIXED_TS),
            user_turn_signing_key=_SIGNING_KEY,
            rate_limit_config=TokenBucketConfig(capacity=2, refill_per_second=1.0),
        )
        persisted: list[bool] = []
        gate.submit_write(_request(), lambda: persisted.append(True))
        gate.submit_write(_request(), lambda: persisted.append(True))
        result = gate.submit_write(_request(), lambda: persisted.append(True))

        assert isinstance(result, WriteRejected)
        assert result.http_status == 429
        assert result.error_code == ERROR_RATE_LIMIT_EXCEEDED
        assert len(journal.entries) == 2
        assert persisted == [True, True]

    def test_the_bucket_is_scoped_per_tenant_not_global(self) -> None:
        gate = ProvenanceWriteGate(
            journal=RecordingJournal(),
            clock=FakeClock(_FIXED_TS),
            user_turn_signing_key=_SIGNING_KEY,
            rate_limit_config=TokenBucketConfig(capacity=1, refill_per_second=0.001),
        )
        result_a1 = gate.submit_write(_request(tenant_id="tenant-A"), lambda: None)
        result_b1 = gate.submit_write(_request(tenant_id="tenant-B"), lambda: None)
        result_a2 = gate.submit_write(_request(tenant_id="tenant-A"), lambda: None)

        assert isinstance(result_a1, WriteAccepted)
        assert isinstance(result_b1, WriteAccepted)
        assert isinstance(result_a2, WriteRejected)
        assert result_a2.error_code == ERROR_RATE_LIMIT_EXCEEDED

    def test_a_verbatim_replay_does_not_spend_a_token(self) -> None:
        """A replay returns the cached WriteAccepted without re-journaling
        or re-persisting -- it must also not count against the rate limit,
        since it performs no new write."""
        journal = RecordingJournal()
        gate = ProvenanceWriteGate(
            journal=journal,
            clock=FakeClock(_FIXED_TS),
            user_turn_signing_key=_SIGNING_KEY,
            rate_limit_config=TokenBucketConfig(capacity=1, refill_per_second=0.001),
        )
        request = _request()
        first = gate.submit_write(request, lambda: None)
        second = gate.submit_write(request, lambda: None)
        third = gate.submit_write(request, lambda: None)

        assert isinstance(first, WriteAccepted)
        assert isinstance(second, WriteAccepted)
        assert isinstance(third, WriteAccepted)
        assert second.write_id == first.write_id == third.write_id
        assert len(journal.entries) == 1

    def test_capacity_refills_over_time_allowing_further_writes(self) -> None:
        clock = FakeClock(_FIXED_TS)
        gate = ProvenanceWriteGate(
            journal=RecordingJournal(),
            clock=clock,
            user_turn_signing_key=_SIGNING_KEY,
            rate_limit_config=TokenBucketConfig(capacity=1, refill_per_second=1.0),
        )
        first = gate.submit_write(_request(), lambda: None)
        exhausted = gate.submit_write(_request(), lambda: None)
        clock.advance(1.0)
        refilled = gate.submit_write(_request(), lambda: None)

        assert isinstance(first, WriteAccepted)
        assert isinstance(exhausted, WriteRejected)
        assert isinstance(refilled, WriteAccepted)

    def test_default_rate_limit_config_does_not_throttle_typical_test_workloads(
        self,
    ) -> None:
        """The default configuration (1000 capacity) must comfortably
        absorb every existing suite's concurrent-write test volume (at
        most a few dozen writes per tenant) with no explicit config."""
        gate = ProvenanceWriteGate(
            journal=RecordingJournal(),
            clock=FakeClock(_FIXED_TS),
            user_turn_signing_key=_SIGNING_KEY,
        )
        results = [gate.submit_write(_request(), lambda: None) for _ in range(50)]
        assert all(isinstance(r, WriteAccepted) for r in results)
