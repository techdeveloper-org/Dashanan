"""Test suite for DASH-STORY-009's lazy-deletion timer wheel (HLD Section 5, F6).

Traces to FR-012 in SRS.md. Covers the must-not-deviate item "O(k log N)
sweep, not an O(N) scan" (ar1_assignments.json AR1-009) at the
`RotationTimerWheel` level: `pop_due` must return only genuinely due
entries and never visit live entries that are not yet due.

Also exercises the mathematics-engineer F6(b) lazy-deletion contract this
module implements verbatim: `schedule` orphans any prior live entry for
the same `item_id` without touching the heap directly; `invalidate` does
the same; `pop_due` discards orphaned (stale-generation) entries silently.

Runtime assumptions (rule 33/40 test-roadmap conventions):
  - `_FIXED_TS = datetime(2026, 1, 1, tzinfo=UTC)` is the anchor instant
    for every test below; deadlines are expressed as `_FIXED_TS +
    timedelta(...)` for readability. No fixture seed is required --
    nothing here is randomized.

PII NOTE: every field is a scheduling primitive (`item_id` string,
`datetime`) -- never item content, matching this story's PII posture.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from dashanan.domain.rotation_timer_wheel import RotationTimerWheel

_FIXED_TS = datetime(2026, 1, 1, tzinfo=UTC)


class TestScheduleAndPopDueBasics:
    def test_pop_due_returns_empty_list_for_empty_wheel(self) -> None:
        wheel = RotationTimerWheel()
        assert wheel.pop_due(_FIXED_TS) == []

    def test_scheduled_item_not_yet_due_is_not_popped(self) -> None:
        wheel = RotationTimerWheel()
        wheel.schedule("item-1", _FIXED_TS + timedelta(seconds=100))
        due = wheel.pop_due(_FIXED_TS)
        assert due == []
        assert wheel.size == 1

    def test_scheduled_item_exactly_at_boundary_is_due(self) -> None:
        """`due_at <= as_of` -- the boundary instant itself is due, not excluded."""
        wheel = RotationTimerWheel()
        due_at = _FIXED_TS + timedelta(seconds=100)
        wheel.schedule("item-1", due_at)
        due = wheel.pop_due(due_at)
        assert [d.item_id for d in due] == ["item-1"]

    def test_scheduled_item_past_due_is_popped(self) -> None:
        wheel = RotationTimerWheel()
        wheel.schedule("item-1", _FIXED_TS - timedelta(seconds=1))
        due = wheel.pop_due(_FIXED_TS)
        assert [d.item_id for d in due] == ["item-1"]

    def test_popped_item_is_removed_from_live_size(self) -> None:
        wheel = RotationTimerWheel()
        wheel.schedule("item-1", _FIXED_TS - timedelta(seconds=1))
        wheel.pop_due(_FIXED_TS)
        assert wheel.size == 0

    def test_schedule_rejects_blank_item_id(self) -> None:
        import pytest

        wheel = RotationTimerWheel()
        with pytest.raises(ValueError, match="item_id"):
            wheel.schedule("   ", _FIXED_TS)


class TestPopDueOrderingAndScope:
    """Must-not-deviate: `O(k log N)` -- only genuinely due entries are visited/returned."""

    def test_only_due_items_are_returned_others_stay_scheduled(self) -> None:
        wheel = RotationTimerWheel()
        wheel.schedule("due-1", _FIXED_TS - timedelta(seconds=10))
        wheel.schedule("due-2", _FIXED_TS - timedelta(seconds=5))
        wheel.schedule("not-due", _FIXED_TS + timedelta(seconds=3600))

        due = wheel.pop_due(_FIXED_TS)

        assert {d.item_id for d in due} == {"due-1", "due-2"}
        assert wheel.size == 1

    def test_due_items_are_returned_in_ascending_due_at_order(self) -> None:
        wheel = RotationTimerWheel()
        wheel.schedule("later", _FIXED_TS - timedelta(seconds=5))
        wheel.schedule("earlier", _FIXED_TS - timedelta(seconds=50))
        wheel.schedule("middle", _FIXED_TS - timedelta(seconds=20))

        due = wheel.pop_due(_FIXED_TS)

        assert [d.item_id for d in due] == ["earlier", "middle", "later"]

    def test_large_zone_only_pops_the_k_due_items_not_the_full_n(self) -> None:
        """Proxy for the `O(k log N)` bound: N=1000 scheduled, only k=3 due."""
        wheel = RotationTimerWheel()
        for i in range(1000):
            wheel.schedule(f"future-{i}", _FIXED_TS + timedelta(hours=1, seconds=i))
        wheel.schedule("due-a", _FIXED_TS - timedelta(seconds=1))
        wheel.schedule("due-b", _FIXED_TS - timedelta(seconds=2))
        wheel.schedule("due-c", _FIXED_TS - timedelta(seconds=3))

        due = wheel.pop_due(_FIXED_TS)

        assert len(due) == 3
        assert wheel.size == 1000

    def test_second_pop_due_call_returns_nothing_already_popped(self) -> None:
        wheel = RotationTimerWheel()
        wheel.schedule("item-1", _FIXED_TS - timedelta(seconds=1))
        first = wheel.pop_due(_FIXED_TS)
        second = wheel.pop_due(_FIXED_TS)
        assert len(first) == 1
        assert second == []


class TestLazyDeletionReschedule:
    """F6(b): `schedule` on an already-scheduled item orphans the OLD entry, never mutates it in place."""

    def test_rescheduling_an_item_moves_its_due_at(self) -> None:
        wheel = RotationTimerWheel()
        wheel.schedule("item-1", _FIXED_TS + timedelta(seconds=3600))
        wheel.schedule("item-1", _FIXED_TS - timedelta(seconds=1))

        due = wheel.pop_due(_FIXED_TS)

        assert [d.item_id for d in due] == ["item-1"]

    def test_rescheduling_does_not_double_count_size(self) -> None:
        wheel = RotationTimerWheel()
        wheel.schedule("item-1", _FIXED_TS + timedelta(seconds=3600))
        wheel.schedule("item-1", _FIXED_TS + timedelta(seconds=7200))
        assert wheel.size == 1

    def test_stale_orphaned_entry_from_an_earlier_schedule_is_discarded_silently(
        self,
    ) -> None:
        """The FIRST (now-orphaned) heap entry must never resurface as a due item."""
        wheel = RotationTimerWheel()
        wheel.schedule("item-1", _FIXED_TS - timedelta(seconds=100))
        wheel.schedule("item-1", _FIXED_TS + timedelta(seconds=3600))

        due = wheel.pop_due(_FIXED_TS)

        assert due == []
        assert wheel.size == 1

    def test_rescheduled_item_pops_only_once_at_its_new_due_at(self) -> None:
        wheel = RotationTimerWheel()
        wheel.schedule("item-1", _FIXED_TS - timedelta(seconds=100))
        wheel.schedule("item-1", _FIXED_TS - timedelta(seconds=1))

        due = wheel.pop_due(_FIXED_TS)

        assert len(due) == 1
        assert due[0].item_id == "item-1"


class TestInvalidate:
    """F5: pinning invalidates any existing entry, never leaves a stale schedule alive."""

    def test_invalidate_removes_a_live_entry(self) -> None:
        wheel = RotationTimerWheel()
        wheel.schedule("item-1", _FIXED_TS - timedelta(seconds=1))
        wheel.invalidate("item-1")

        due = wheel.pop_due(_FIXED_TS)

        assert due == []
        assert wheel.size == 0

    def test_invalidate_on_unscheduled_item_is_a_no_op(self) -> None:
        """F5: pinning is unconditional -- invalidate must never raise for an absent item."""
        wheel = RotationTimerWheel()
        wheel.invalidate("never-scheduled")
        assert wheel.size == 0

    def test_invalidate_only_affects_the_named_item(self) -> None:
        wheel = RotationTimerWheel()
        wheel.schedule("item-1", _FIXED_TS - timedelta(seconds=1))
        wheel.schedule("item-2", _FIXED_TS - timedelta(seconds=1))
        wheel.invalidate("item-1")

        due = wheel.pop_due(_FIXED_TS)

        assert [d.item_id for d in due] == ["item-2"]

    def test_invalidate_then_reschedule_produces_a_fresh_live_entry(self) -> None:
        wheel = RotationTimerWheel()
        wheel.schedule("item-1", _FIXED_TS - timedelta(seconds=1))
        wheel.invalidate("item-1")
        wheel.schedule("item-1", _FIXED_TS - timedelta(seconds=1))

        due = wheel.pop_due(_FIXED_TS)

        assert [d.item_id for d in due] == ["item-1"]


class TestPerZoneScoping:
    """HLD Section 5: "Per-zone hierarchical timer wheel" -- each instance is independent."""

    def test_two_wheel_instances_do_not_share_state(self) -> None:
        wheel_a = RotationTimerWheel()
        wheel_b = RotationTimerWheel()
        wheel_a.schedule("item-1", _FIXED_TS - timedelta(seconds=1))

        assert wheel_a.size == 1
        assert wheel_b.size == 0
        assert wheel_b.pop_due(_FIXED_TS) == []
