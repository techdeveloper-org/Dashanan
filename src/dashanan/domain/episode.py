"""Episode: Zone 2's owned entity (HLD Section 3.3, FR-002)."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

EPISODIC_LAMBDA_ZONE: float = 4.01e-6
"""Zone 2's per-second exponential decay constant.

Cited verbatim from HLD Section 12A's own worked computation: `t_half` = 2
days, `lambda = ln(2) / t_half = 0.6931 / 172800 = 4.01e-6 s^-1` (HLD line
"Zone 2, 0.6931/172800 = 4.01e-6"). This is not derived here -- the HLD has
already done that derivation; this module only consumes the finalized
constant to satisfy AC-002's per-entry decay exposure. A later Scoring
Service story may promote this to a per-zone operator-configurable value on
the `/v1/zones/{zone}/config` surface (HLD Section 7.3); until then it is a
module-level constant scoped to this zone only.
"""


class EpisodeState(str, Enum):
    """The three states of the locked `Active -> Compressed -> Archived` state machine.

    Names taken directly from HLD Section 0 / the Phase 0 locked rotation
    state machine. An ENUM is appropriate here per database-engineer's
    schema-design rule ("ENUM types for fixed value sets only when the set
    truly never changes") -- this state machine is a Phase 0 locked input
    this HLD explicitly does not re-litigate.
    """

    ACTIVE = "active"
    COMPRESSED = "compressed"
    ARCHIVED = "archived"


_EPISODE_ID_SEPARATOR = ":"


@dataclass(frozen=True, slots=True)
class Episode:
    """Zone 2's owned entity: a chronological, time-anchored interaction record.

    Attributes mirror HLD Section 3.3's owned-entity shape exactly:
    `Episode(tenant_id, session_id, episode_id, seq, occurred_at,
    written_at, payload, actors[], token_count, state, score_terms)`.

    The primary key is the composite `(tenant_id, session_id, seq)` (HLD
    Section 5, DSA Choices row 2). `episode_id` is a derived, deterministic
    encoding of that triple (see `episode_id_for` / `parse_episode_id`) so
    that AC-002-DPDP-1's "item_id (episode_id) resolves to its primary key
    ... via a direct B-tree PK lookup" holds structurally: resolving an
    `episode_id` is pure string parsing, never a table scan.

    Attributes:
        tenant_id: Owning tenant. Present on every row -- no zone query may
            omit it (AR1-003 must-not-deviate item 5).
        session_id: The session this episode belongs to. Second PK column.
        episode_id: Deterministic `tenant_id:session_id:seq` encoding.
            Never hand-constructed by a caller -- always produced by
            `Episode.episode_id_for` at write time.
        seq: Monotonically increasing, session-scoped sequence number.
            Third and final PK column; disambiguates same-instant writes.
        occurred_at: When the interaction happened. The BRIN-indexed,
            naturally-ordered timestamp (HLD Section 5).
        written_at: When this record was durably written. Never earlier
            than `occurred_at` -- an episode cannot be recorded before it
            happens.
        payload: Zone-owned content, opaque to the Orchestrator (mirrors
            `MemoryItem.payload`'s existing contract).
        actors: Participant identifiers for this interaction (e.g. which
            party spoke), immutable tuple per the append-only invariant.
        token_count: Cost of including this episode in an assembled
            context. Must be positive (mirrors `MemoryItem`'s own
            invariant, `memory_item.py`).
        state: Current position in the locked rotation state machine.
        score_terms: Zone-computed partial MemoryScore contributions,
            keyed by term name. Recency/decay is *not* stored here -- it
            is computed on demand via `decay()` since it is a function of
            the read time, not a fixed write-time value.
    """

    tenant_id: str
    session_id: str
    episode_id: str
    seq: int
    occurred_at: datetime
    written_at: datetime
    payload: str
    actors: tuple[str, ...]
    token_count: int
    state: EpisodeState
    score_terms: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Enforce the invariants the keyset-pagination and PK-lookup paths depend on.

        Raises:
            ValueError: If any identifier is blank, `seq` is negative,
                `token_count` is not positive, or `written_at` precedes
                `occurred_at` (an episode cannot be written before it
                happened).
        """
        if not self.tenant_id.strip():
            raise ValueError("Episode.tenant_id must not be blank")
        if not self.session_id.strip():
            raise ValueError("Episode.session_id must not be blank")
        if not self.episode_id.strip():
            raise ValueError("Episode.episode_id must not be blank")
        if self.seq < 0:
            raise ValueError(f"Episode.seq must be non-negative, got {self.seq}")
        if self.token_count <= 0:
            raise ValueError(
                f"Episode.token_count must be positive, got {self.token_count}"
            )
        if self.written_at < self.occurred_at:
            raise ValueError(
                "Episode.written_at must not precede Episode.occurred_at "
                f"(occurred_at={self.occurred_at!r}, written_at={self.written_at!r})"
            )

    def decay(self, as_of: datetime) -> float:
        """Compute this episode's own Recency decay at `as_of` (AC-002).

        Implements the closed form HLD Section 5 assigns the Scoring
        service, `exp(-lambda * dt)`, using this zone's
        `EPISODIC_LAMBDA_ZONE`. Because it takes `as_of` as a parameter
        rather than reading a stored value, every entry always "exposes
        its own decay attribute" relative to the caller's read time,
        exactly as AC-002 requires, without this row needing an update on
        every read (which would violate the append-only invariant).

        Args:
            as_of: The instant to compute decay relative to.

        Returns:
            A value in `(0, 1]`. `dt` is clamped to zero when `as_of`
            precedes `occurred_at` (e.g. under clock skew across
            replicas), so a not-yet-elapsed read is treated as maximally
            fresh (`1.0`) rather than raising or exceeding `1.0`.
        """
        dt = max(0.0, (as_of - self.occurred_at).total_seconds())
        return math.exp(-EPISODIC_LAMBDA_ZONE * dt)

    @staticmethod
    def episode_id_for(tenant_id: str, session_id: str, seq: int) -> str:
        """Deterministically encode a PK triple as the public `episode_id`.

        Uses `rsplit`-safe encoding: `parse_episode_id` recovers the triple
        by splitting from the right, so a `tenant_id` containing the
        separator character is still recovered correctly as long as
        `session_id` does not also contain it (documented constraint, not
        HLD-mandated -- see the DASH-STORY-003 dev report's judgment-call
        list).
        """
        if not tenant_id.strip() or not session_id.strip():
            raise ValueError(
                "episode_id_for requires non-blank tenant_id and session_id"
            )
        if seq < 0:
            raise ValueError(f"episode_id_for requires seq >= 0, got {seq}")
        return f"{tenant_id}{_EPISODE_ID_SEPARATOR}{session_id}{_EPISODE_ID_SEPARATOR}{seq}"

    @staticmethod
    def parse_episode_id(episode_id: str) -> tuple[str, str, int]:
        """Recover `(tenant_id, session_id, seq)` from a public `episode_id`.

        This is the operation AC-002-DPDP-1 calls "resolves to its primary
        key": pure string parsing, O(1), never a database round-trip and
        never a BRIN range scan.

        Raises:
            ValueError: If `episode_id` does not have exactly three
                separator-delimited segments, or the trailing segment is
                not a valid non-negative integer.
        """
        parts = episode_id.rsplit(_EPISODE_ID_SEPARATOR, 2)
        if len(parts) != 3:
            raise ValueError(
                f"episode_id {episode_id!r} is not a valid "
                "'tenant_id:session_id:seq' encoding"
            )
        tenant_id, session_id, seq_text = parts
        if not tenant_id or not session_id:
            raise ValueError(
                f"episode_id {episode_id!r} has a blank tenant_id or session_id segment"
            )
        try:
            seq = int(seq_text)
        except ValueError as exc:
            raise ValueError(
                f"episode_id {episode_id!r} has a non-integer seq segment {seq_text!r}"
            ) from exc
        if seq < 0:
            raise ValueError(f"episode_id {episode_id!r} has a negative seq {seq}")
        return tenant_id, session_id, seq
