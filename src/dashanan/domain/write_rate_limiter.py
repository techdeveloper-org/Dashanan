"""Per-tenant write-throughput token-bucket admission control (HLD Threat D-1).

DSHN-60 remediation (MEDIUM): HLD Threat D-1 documents two halves --
per-zone write-volume capacity quotas (the Zone 2 capacity/MaxAge backstop,
`domain.zone2_capacity_backstop`) and per-tenant write-THROUGHPUT admission
control. Only the first half had a concrete implementation; nothing bounded
how many `ProvenanceWriteGate.submit_write` calls a single tenant could
issue per second, so an adversarial caller with valid credentials could
flood the write path (and, downstream, the journal/persistence layer) with
no structural limit.

This module holds pure domain logic only -- no I/O, no wall-clock reads --
mirroring `write_gate.py`'s own "pure types/functions here, orchestration
in `application.provenance_write_gate`" split. `TokenBucketState` is
immutable; every operation here takes a state in and returns a new state
out, so the caller (the write gate, which already owns a `Clock` and a
per-key lock) is the sole place that decides how state is stored and
serialized across calls -- this module has no opinion on storage.

Token-bucket, not a fixed window: HLD Threat D-1 asks for a bound on
"an adversarial per-tenant write burst," and a fixed-window counter allows
a caller to burst up to 2x the nominal rate across a window boundary (all
of the previous window's budget at its tail, all of the next window's
budget at its head). A token bucket has no such boundary-doubling effect --
capacity bounds the largest possible burst unconditionally, at any instant.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from dashanan.domain.exceptions import DashananError


class WriteRateLimiterConfigError(DashananError):
    """Raised when a `TokenBucketConfig` is constructed with invalid data.

    Kept local to this module rather than added to the shared
    `dashanan.domain.exceptions`, mirroring `write_gate.
    ProvenanceJournalPort` and `zone2_capacity_backstop.
    Zone2CapacityBackstopError`'s identical "kept local" choice.
    """


@dataclass(frozen=True, slots=True)
class TokenBucketConfig:
    """The operator-configurable capacity/refill-rate pair for one bucket.

    Attributes:
        capacity: The maximum number of tokens the bucket can hold --
            equivalently, the largest burst of writes a tenant may submit
            with no inter-arrival delay at all before being throttled.
            Must be positive.
        refill_per_second: How many tokens are added back per second of
            elapsed time, up to `capacity` -- equivalently, the sustained
            steady-state write rate this bucket allows. Must be positive.
    """

    capacity: float
    refill_per_second: float

    def __post_init__(self) -> None:
        """Raises:
        WriteRateLimiterConfigError: If `capacity` or `refill_per_second`
            is not positive -- a non-positive value here would either
            reject every write unconditionally (capacity <= 0) or never
            refill (refill_per_second <= 0), neither of which is a valid
            operator-supplied throttling policy.
        """
        if self.capacity <= 0:
            raise WriteRateLimiterConfigError(
                f"TokenBucketConfig.capacity must be positive, got {self.capacity}"
            )
        if self.refill_per_second <= 0:
            raise WriteRateLimiterConfigError(
                "TokenBucketConfig.refill_per_second must be positive, "
                f"got {self.refill_per_second}"
            )


@dataclass(frozen=True, slots=True)
class TokenBucketState:
    """One tenant's current token count and the instant it was last refilled.

    Attributes:
        tokens: Tokens currently available, in `[0, capacity]` of whatever
            `TokenBucketConfig` this state is evaluated against.
        last_refilled_at: The instant `tokens` was last computed as of.
    """

    tokens: float
    last_refilled_at: datetime


def new_full_bucket(config: TokenBucketConfig, now: datetime) -> TokenBucketState:
    """A fresh bucket at full capacity -- the state a first-seen tenant starts from.

    Starting full (not empty) is deliberate: a tenant's first write ever
    must not be throttled merely because no tokens have accrued yet: the
    whole point of `capacity` is "the largest burst allowed with no prior
    history," and a brand-new tenant has, by definition, no prior history
    of abuse to justify starting them below that burst allowance.
    """
    return TokenBucketState(tokens=config.capacity, last_refilled_at=now)


def try_consume(
    state: TokenBucketState,
    config: TokenBucketConfig,
    now: datetime,
    cost: float = 1.0,
) -> tuple[TokenBucketState, bool]:
    """Refill `state` up to `now`, then attempt to spend `cost` tokens.

    Args:
        state: The bucket's state as of its last observed refill.
        config: This bucket's capacity/refill-rate policy.
        now: The current instant (the write gate's own injected `Clock`,
            never `datetime.now()` directly -- testing-core).
        cost: Tokens this attempt costs. Defaults to `1.0` (one write).

    Returns:
        A `(new_state, allowed)` pair. `new_state` reflects the refill
        that always happens regardless of outcome; `allowed` is `True`
        (and `cost` tokens have been deducted) only when the refilled
        balance was `>= cost`. A clock reading of `now` earlier than
        `state.last_refilled_at` (an out-of-order call) is treated as no
        elapsed time -- never a negative refill -- so `tokens` can only
        move toward `capacity`, never below `state.tokens`, from a clock
        anomaly alone.
    """
    elapsed_seconds = max(0.0, (now - state.last_refilled_at).total_seconds())
    refilled_tokens = min(
        config.capacity, state.tokens + elapsed_seconds * config.refill_per_second
    )
    if refilled_tokens >= cost:
        return (
            TokenBucketState(tokens=refilled_tokens - cost, last_refilled_at=now),
            True,
        )
    return TokenBucketState(tokens=refilled_tokens, last_refilled_at=now), False
