"""RotationTimerWheel: the per-zone hierarchical timer wheel (HLD Section 5, ADR-012).

Traces to FR-012 in SRS.md. HLD Section 5, "Rotation Engine" row: "Per-zone
hierarchical timer wheel (bucketed min-heap) keyed on predicted
threshold-crossing timestamp" / "Timer-wheel insert / re-insert: O(log N)
(bucketed min-heap)" / "Sweep tick: O(k log N) (`k` = items actually
crossing this tick, not N)".

MUST-NOT-DEVIATE (ar1_assignments.json AR1-009, binding):
  "O(k log N) sweep, not an O(N) scan" -- satisfied structurally: `pop_due`
  only ever pops heap entries whose key is `<= as_of`, and returns after
  the first entry with a later key (or an empty heap). It never iterates
  the full scheduled population.

MATH DELEGATION (mathematics-engineer, F6/D5, ar1_assignments.json
AR1-009): "A plain binary heap has no O(log N) arbitrary delete... Choose
one: (a) INDEXED HEAP... (b) LAZY DELETION: leave the stale entry, push a
new one with a monotonically increasing generation counter; on pop,
discard any entry whose generation is not current... (b) is simpler and
is the better fit given F5 implies a high mutation-to-schedule ratio."
This module implements (b) verbatim: `_generation` tracks the CURRENT
live generation per `item_id`; `schedule` bumps it and pushes a new heap
entry; `invalidate` removes it so any existing heap entry for that
`item_id` is orphaned; `pop_due` discards any popped entry whose
generation no longer matches `_generation.get(item_id)` before it is
returned as genuinely due. Per D5's amortized accounting: every heap push
is popped (and discarded or returned) at most once, so total sweep cost
over the wheel's lifetime is bounded by total schedules, not by the
number of ticks -- `T_sweep(k, N) = Theta(k log N)` holds with `N` the
live-scheduled count and the discarded-stale entries folded into `k`
(HLD Section 5's own complexity table; D5's two-sided bound `k*log2(N-k)
<= T_sweep <= 2k*log2(N) + c`).

This module holds pure domain logic only: no I/O, no Clock (the caller
always supplies `as_of` -- testing-core: dependency injection over reading
wall-clock time directly). One `RotationTimerWheel` instance is scoped to
exactly one zone (HLD Section 5: "Per-zone... timer wheel") -- the
application-layer `RotationEngine` (`application.rotation_engine`) owns
one instance per `ZoneId` it serves.

PII NOTE: every field here is a scheduling primitive -- an `item_id`
string, a `datetime` deadline, an integer generation counter -- never
item content, mirroring `dashanan.domain.rotation_state`'s identical PII
posture.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class ScheduledRotation:
    """One item's live scheduled entry, as returned by `pop_due`.

    Attributes:
        item_id: The item this deadline was scheduled for.
        due_at: The instant this entry became due (the value it was
            scheduled at, not the `as_of` the sweep popped it with).
    """

    item_id: str
    due_at: datetime


class RotationTimerWheel:
    """A single zone's bucketed min-heap of pending rotation deadlines.

    Implements the F6(b) lazy-deletion contract: `schedule` and
    `invalidate` are both O(log N) / O(1) respectively and never touch
    the heap's interior; `pop_due` is the only method that removes heap
    entries, and it removes exactly the entries it visits (due or stale),
    never more.
    """

    def __init__(self) -> None:
        self._heap: list[tuple[datetime, int, str]] = []
        self._generation: dict[str, int] = {}
        self._due_at_by_item: dict[str, datetime] = {}

    @property
    def size(self) -> int:
        """The number of currently LIVE scheduled entries (`N` in the HLD's complexity table).

        `O(1)` -- counts `_generation`, never the heap (which may also
        hold stale, not-yet-collected entries per the lazy-deletion
        contract).
        """
        return len(self._generation)

    def schedule(self, item_id: str, due_at: datetime) -> None:
        """Schedule (or reschedule) `item_id` to become due at `due_at`.

        `O(log N)`. If `item_id` already has a live entry, that entry is
        orphaned (its generation no longer matches) without being removed
        from the heap -- it is discarded lazily the next time `pop_due`
        visits it (F6(b)).

        Args:
            item_id: The item to schedule. Must not be blank.
            due_at: The instant at which this item becomes due for
                re-evaluation by the rotation sweep.

        Raises:
            ValueError: If `item_id` is blank.
        """
        if not item_id.strip():
            raise ValueError(
                "RotationTimerWheel.schedule requires a non-blank item_id"
            )
        generation = self._generation.get(item_id, 0) + 1
        self._generation[item_id] = generation
        self._due_at_by_item[item_id] = due_at
        heapq.heappush(self._heap, (due_at, generation, item_id))

    def invalidate(self, item_id: str) -> None:
        """Remove `item_id`'s live scheduled entry, if any.

        `O(1)`. Any existing heap entry for `item_id` is orphaned the
        same way `schedule` orphans a superseded entry -- it is discarded
        lazily by `pop_due`, never removed from the heap directly (F6(b)
        does not require an indexed heap).

        Idempotent: invalidating an `item_id` with no live entry (e.g. a
        pinned item, `dashanan.domain.rotation_deadline.
        compute_rotation_deadline` returned `None`) is a no-op, since
        "pinned is a hot path, not an edge case" (mathematics-engineer
        delegation F5) and the caller calls this unconditionally on every
        pin decision to guarantee no stale entry lingers from a prior,
        now-superseded schedule.
        """
        self._generation.pop(item_id, None)
        self._due_at_by_item.pop(item_id, None)

    def pop_due(self, as_of: datetime) -> list[ScheduledRotation]:
        """Pop and return every LIVE entry due at or before `as_of`.

        `O(k log N)` where `k` is the number of heap entries visited
        (genuinely due entries plus any stale entries discarded along the
        way) -- never a scan of the full `N` scheduled population
        (must-not-deviate: "O(k log N) sweep, not an O(N) scan"). Popping
        an entry removes it from `_generation` (it is no longer scheduled
        -- the caller, `application.rotation_engine`, is now responsible
        for re-scheduling it if the guarded transition it evaluates does
        not consume it).

        Args:
            as_of: The sweep's current instant. Every entry with
                `due_at <= as_of` is popped; the loop stops at the first
                entry (due or stale) with `due_at > as_of`, or when the
                heap is exhausted.

        Returns:
            Every genuinely due, still-live entry, in ascending `due_at`
            order (the heap's own pop order). Entries whose generation no
            longer matches `_generation` (superseded by a later
            `schedule`, or removed by `invalidate`) are silently
            discarded and never appear in the result.
        """
        due: list[ScheduledRotation] = []
        while self._heap and self._heap[0][0] <= as_of:
            due_at, generation, item_id = heapq.heappop(self._heap)
            if self._generation.get(item_id) != generation:
                continue
            del self._generation[item_id]
            self._due_at_by_item.pop(item_id, None)
            due.append(ScheduledRotation(item_id=item_id, due_at=due_at))
        return due
