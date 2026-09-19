"""WorkingMemoryLRURepository: Shape A Zone 1 storage adapter (ADR-005, FR-001).

Shape A only (in-process, HashMap + doubly-linked list via `OrderedDict`,
per HLD Section 5's "Zone 1 Working" DSA row). Shape B (Redis hash +
sorted-set scoreboard, per ADR-005) is a separate adapter behind the
same read contract and is out of scope for this module -- see the
DASH-STORY-002 implementation report's `shared_file_request` /
judgment-call notes for why it is not attempted here.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import replace
from datetime import timedelta

from dashanan.domain.memory_item import MemoryItem
from dashanan.domain.ports import Clock, ZoneQuery, ZoneRepository
from dashanan.domain.working_item import WorkingItem
from dashanan.domain.zone import ZoneId

DEFAULT_CAPACITY_PER_SESSION = 200
"""HLD 12A per-zone policy table, Zone 1 row: "200 items / session"."""

DEFAULT_IDLE_TTL_SECONDS = 1800.0
"""HLD 12A per-zone policy table, Zone 1 row: "TTL (30 min idle)"."""


class WorkingMemoryLRURepository(ZoneRepository):
    """Shape A `ZoneRepository` adapter for Zone 1 (HLD 3.2, ADR-005).

    Implements AC-001: an item whose idle TTL expires is evicted from
    Zone 1 without a caller ever issuing an explicit delete call, and
    the eviction touches only this repository's own state -- no other
    zone, port, or event bus is called as a side effect.

    Storage shape (HLD Section 5, "Zone 1 Working" row): one
    `collections.OrderedDict` per `(tenant_id, session_id)`, ordered by
    access recency (least-recently-used at the front). CPython
    implements `OrderedDict` as a hash table plus a doubly-linked list,
    which is exactly the HashMap + DLL structure the DSA choice
    mandates, giving O(1) amortized `get`/`put`/evict.

    Eviction ordering (HLD 12A, must-not-deviate list item 3): TTL-
    primary, never score-primary. Because `idle_ttl` is a single fixed
    value for the whole repository and every access moves an item to
    the end of its session's order (refreshing `last_access_at`), the
    front of that order is always the item soonest to cross the idle
    threshold. Sweeping from the front and stopping at the first
    not-yet-expired item is therefore sufficient to evict every
    currently-expired item in O(1) amortized time, without a full scan
    -- the sweep only ever touches items it actually evicts, plus one
    extra peek.

    Known scope gap (flagged, not silently absorbed): ADR-018 (Zone 1
    concurrency control -- WATCH/MULTI-EXEC or a Lua script for Shape
    B, a per-session `asyncio.Lock` for Shape A) is not implemented
    here. It was adopted 2026-09-18, after this story's routing prompt
    (ar1_assignments.json, dated 2026-09-17) fixed this story's context
    sources and must-not-deviate list, and ADR-018 does not appear in
    either. See the DASH-STORY-002 implementation report.
    """

    def __init__(
        self,
        clock: Clock,
        capacity_per_session: int = DEFAULT_CAPACITY_PER_SESSION,
        idle_ttl_seconds: float = DEFAULT_IDLE_TTL_SECONDS,
    ) -> None:
        """Compose the repository from its Clock port and capacity policy.

        Args:
            clock: Injectable time source (testing-core DI), matching
                the `SystemClock` / `Clock` convention already used by
                `MemoryOrchestrator`.
            capacity_per_session: Per-(tenant, session) item cap (HLD
                12A default: 200). Operator-configurable (NFR-009).
            idle_ttl_seconds: Idle TTL in seconds (HLD 12A default:
                1800 = 30 min). Operator-configurable (NFR-009).

        Raises:
            ValueError: If `capacity_per_session` or `idle_ttl_seconds`
                is not positive.
        """
        if capacity_per_session <= 0:
            raise ValueError(
                "capacity_per_session must be positive, got "
                f"{capacity_per_session}"
            )
        if idle_ttl_seconds <= 0:
            raise ValueError(
                f"idle_ttl_seconds must be positive, got {idle_ttl_seconds}"
            )
        self._clock = clock
        self._capacity_per_session = capacity_per_session
        self._idle_ttl = timedelta(seconds=idle_ttl_seconds)
        self._sessions: dict[tuple[str, str], OrderedDict[str, WorkingItem]] = {}

    def put(
        self,
        tenant_id: str,
        session_id: str,
        item_id: str,
        payload: str,
        token_count: int,
        score_terms: dict[str, float] | None = None,
    ) -> WorkingItem:
        """Write (or overwrite) one item, event-driven sweep first (HLD 12A).

        Args:
            tenant_id: Mandatory; enforced non-blank (HLD 3.0 invariant 2).
            session_id: Mandatory; enforced non-blank.
            item_id: Identifier of the item within the session.
            payload: Zone-owned content.
            token_count: Positive cost in tokens.
            score_terms: Optional placeholder score inputs (unused by
                this repository's own eviction ordering).

        Returns:
            The stored `WorkingItem`, with `written_at`/`last_access_at`
            set from the injected clock.

        Raises:
            ValueError: If `tenant_id`, `session_id`, or `item_id` is
                blank, propagated from `WorkingItem.__post_init__` for
                `item_id`/`token_count` and checked directly here for
                `tenant_id`/`session_id` before any lookup.
        """
        self._require_non_blank("tenant_id", tenant_id)
        self._require_non_blank("session_id", session_id)

        session_items = self._sessions.setdefault((tenant_id, session_id), OrderedDict())
        self._evict_expired_locked(session_items)

        now = self._clock.now()
        item = WorkingItem(
            tenant_id=tenant_id,
            session_id=session_id,
            item_id=item_id,
            payload=payload,
            token_count=token_count,
            written_at=now,
            last_access_at=now,
            score_terms=dict(score_terms) if score_terms else {},
        )
        session_items[item_id] = item
        session_items.move_to_end(item_id)

        while len(session_items) > self._capacity_per_session:
            session_items.popitem(last=False)

        return item

    def get(self, tenant_id: str, session_id: str, item_id: str) -> WorkingItem | None:
        """Read one item, refreshing its idle-TTL window (HLD 12A: "idle").

        A lazy expiry sweep runs first, so a `get` for an item that has
        already crossed its idle TTL returns `None` even if the
        event-driven write-time sweep has not yet reclaimed it.

        Args:
            tenant_id: Mandatory; enforced non-blank.
            session_id: Mandatory; enforced non-blank.
            item_id: Identifier of the item to read.

        Returns:
            The refreshed `WorkingItem`, or `None` if absent or expired.
        """
        self._require_non_blank("tenant_id", tenant_id)
        self._require_non_blank("session_id", session_id)

        session_items = self._sessions.get((tenant_id, session_id))
        if session_items is None:
            return None
        self._evict_expired_locked(session_items)

        item = session_items.get(item_id)
        if item is None:
            return None

        refreshed = replace(item, last_access_at=self._clock.now())
        session_items[item_id] = refreshed
        session_items.move_to_end(item_id)
        return refreshed

    def evict_expired(self, tenant_id: str, session_id: str) -> list[str]:
        """Force an eviction sweep for one session (AC-001's own mechanism).

        Exposed so a caller (a future rotation-sweep driver, or a test)
        can trigger the same automatic eviction the write path already
        runs -- this is the "without an explicit delete call" mechanism
        itself, not a host-issued delete.

        Args:
            tenant_id: Mandatory; enforced non-blank.
            session_id: Mandatory; enforced non-blank.

        Returns:
            The `item_id`s evicted by this sweep, oldest first. Empty
            if the session is unknown or nothing has expired.
        """
        self._require_non_blank("tenant_id", tenant_id)
        self._require_non_blank("session_id", session_id)

        session_items = self._sessions.get((tenant_id, session_id))
        if session_items is None:
            return []
        return self._evict_expired_locked(session_items)

    def session_item_count(self, tenant_id: str, session_id: str) -> int:
        """Return the live item count for one session, after a lazy sweep."""
        session_items = self._sessions.get((tenant_id, session_id))
        if session_items is None:
            return 0
        self._evict_expired_locked(session_items)
        return len(session_items)

    def fetch(self, query: ZoneQuery) -> list[MemoryItem]:
        """Serve the `ZoneRepository` read contract for Zone 1 (HLD Section 7.1).

        `ZoneQuery` carries no `session_id` (ports.py: the Orchestrator
        strips it before delegating, since Zone 1's own routing does
        not exist yet). This adapter therefore aggregates every session
        registered for `query.tenant_id`, most-recently-accessed first,
        capped at `query.max_items`. See the DASH-STORY-002
        implementation report's judgment-call note on this gap.

        Args:
            query: The stripped-down per-zone query from the Orchestrator.

        Returns:
            Up to `query.max_items` `MemoryItem`s for `query.tenant_id`,
            most-recently-accessed first, after a lazy expiry sweep of
            every touched session.
        """
        candidates: list[WorkingItem] = []
        for (tenant_id, _session_id), session_items in self._sessions.items():
            if tenant_id != query.tenant_id:
                continue
            self._evict_expired_locked(session_items)
            candidates.extend(session_items.values())

        candidates.sort(key=lambda wi: (wi.last_access_at, wi.item_id), reverse=True)
        return [
            MemoryItem(
                item_id=wi.item_id,
                source_zone=ZoneId.WORKING,
                payload=wi.payload,
                token_count=wi.token_count,
            )
            for wi in candidates[: query.max_items]
        ]

    def _evict_expired_locked(
        self, session_items: OrderedDict[str, WorkingItem]
    ) -> list[str]:
        """Pop every item at the front whose idle TTL has elapsed.

        Relies on the class docstring's ordering invariant: the front
        of `session_items` is always the least-recently-accessed item,
        which (under one fixed `idle_ttl`) is always the next one to
        expire. Stops at the first item that has not yet expired.
        """
        evicted: list[str] = []
        now = self._clock.now()
        while session_items:
            oldest_item_id = next(iter(session_items))
            oldest = session_items[oldest_item_id]
            if now - oldest.last_access_at < self._idle_ttl:
                break
            session_items.popitem(last=False)
            evicted.append(oldest_item_id)
        return evicted

    @staticmethod
    def _require_non_blank(name: str, value: str) -> None:
        """Enforce HLD 3.0 invariant 2 (mandatory tenant/session identifiers).

        Raises:
            ValueError: If `value` is blank.
        """
        if not value.strip():
            raise ValueError(f"{name} must not be blank")
