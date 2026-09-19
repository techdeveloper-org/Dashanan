"""WorkingMemoryLRURepository: Shape A Zone 1 storage adapter (ADR-005, FR-001).

Shape A only (in-process, HashMap + doubly-linked list via `OrderedDict`,
per HLD Section 5's "Zone 1 Working" DSA row). Shape B (Redis hash +
sorted-set scoreboard, per ADR-005) is a separate adapter behind the
same read contract and is out of scope for this module -- see the
DASH-STORY-002 implementation report's `shared_file_request` /
judgment-call notes for why it is not attempted here.
"""

from __future__ import annotations

import threading
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

    Concurrency (ADR-018, closed by this class): this adapter is called
    from arbitrary caller threads -- `ProvenanceWriteGate.submit_write`
    invokes a `persist_fact` callback that may land here from any thread
    racing on a distinct idempotency key, so ADR-018's Shape A guidance
    of "a per-session lock" is implemented here as one
    `threading.Lock` per `(tenant_id, session_id)`, lazily created and
    cached in `_session_locks` under the short-held `_sessions_guard`
    (the same lazily-created-per-key-lock pattern
    `ProvenanceWriteGate._lock_for_idempotency_key` uses for its own
    concurrency guarantee). `threading.Lock`, not `asyncio.Lock`, because
    every call into this repository observed under real concurrent load
    (the DSHN-59 adversarial re-audit) arrives on a plain OS thread, not
    inside an event loop -- an `asyncio.Lock` provides no mutual
    exclusion across threads with no running loop. `put`, `get`,
    `evict_expired`, and `session_item_count` each hold one session's
    lock for their whole read-modify-write sequence against that
    session's `OrderedDict`; `fetch` acquires each matching session's
    lock in turn while reading it. Locks for different sessions are
    independent, so concurrent writes to different sessions are never
    serialized against each other. `_sessions_guard` is a second, always
    short-held lock that protects only structural changes to the outer
    `_sessions` dict itself (a new session being registered, or `fetch`
    safely snapshotting which sessions exist) -- it is never held while
    waiting on a session lock, so the two locks cannot deadlock against
    each other.
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
        self._sessions_guard = threading.Lock()
        self._session_locks: dict[tuple[str, str], threading.Lock] = {}

    def _lock_for_session(self, tenant_id: str, session_id: str) -> threading.Lock:
        """Return the one `Lock` serializing all access to one session's items.

        Lazily creates and caches one `threading.Lock` per `(tenant_id,
        session_id)` pair for this repository instance's lifetime,
        guarded by `_sessions_guard` against two threads racing to
        create the lock itself for the same key -- the same
        check-then-create race `ProvenanceWriteGate._lock_for_idempotency_key`
        closes for its own per-key locks (ADR-018).
        """
        key = (tenant_id, session_id)
        with self._sessions_guard:
            lock = self._session_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._session_locks[key] = lock
            return lock

    def _session_items_for(
        self, tenant_id: str, session_id: str
    ) -> OrderedDict[str, WorkingItem]:
        """Return this session's `OrderedDict`, creating it under `_sessions_guard`.

        Callers must already hold this session's lock (from
        `_lock_for_session`) before calling this -- creation of the
        `OrderedDict` and every subsequent mutation of it happen only
        while that lock is held, which is what makes the per-session
        lock a genuine mutual-exclusion guarantee over the structure
        `put`/`get`/`evict_expired` each read-modify-write.
        """
        key = (tenant_id, session_id)
        with self._sessions_guard:
            session_items = self._sessions.get(key)
            if session_items is None:
                session_items = OrderedDict()
                self._sessions[key] = session_items
            return session_items

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

        with self._lock_for_session(tenant_id, session_id):
            session_items = self._session_items_for(tenant_id, session_id)
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

        with self._lock_for_session(tenant_id, session_id):
            with self._sessions_guard:
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

        with self._lock_for_session(tenant_id, session_id):
            with self._sessions_guard:
                session_items = self._sessions.get((tenant_id, session_id))
            if session_items is None:
                return []
            return self._evict_expired_locked(session_items)

    def session_item_count(self, tenant_id: str, session_id: str) -> int:
        """Return the live item count for one session, after a lazy sweep."""
        with self._lock_for_session(tenant_id, session_id):
            with self._sessions_guard:
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
        with self._sessions_guard:
            matching_keys = [
                key for key in self._sessions if key[0] == query.tenant_id
            ]

        candidates: list[WorkingItem] = []
        for tenant_id, session_id in matching_keys:
            with self._lock_for_session(tenant_id, session_id):
                with self._sessions_guard:
                    session_items = self._sessions.get((tenant_id, session_id))
                if session_items is None:
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
