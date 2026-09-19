"""Dedicated unit tests for ContextAssemblyBuilder's greedy knapsack fill.

HLD Section 5 mandates a greedy value-density approximation (sort by
score/token_count descending, accept whatever still fits) over an exact
0/1 knapsack DP solve. These tests assert the greedy fill picks the
documented items under a token cap -- not merely that it runs -- per the
QA subtask requirement to test the knapsack's ordering logic itself.

Runtime assumptions: no clock/tenant dependency; deterministic inputs only.
"""

from __future__ import annotations

from dashanan.application.context_assembly_builder import ContextAssemblyBuilder
from dashanan.domain.memory_item import MemoryItem
from dashanan.domain.zone import ZoneId


def _item(item_id: str, token_count: int, score: float) -> MemoryItem:
    return MemoryItem(item_id, ZoneId.WORKING, "payload", token_count=token_count, score=score)


class TestGreedyKnapsackValueDensityOrdering:
    def test_selects_highest_density_items_first_under_budget(self) -> None:
        """density: A=1.0, B=0.9, C=0.8 -- all fit, so all are selected in density order."""
        items = [
            _item("A", token_count=6, score=6.0),
            _item("B", token_count=5, score=4.5),
            _item("C", token_count=4, score=3.2),
        ]
        builder = (
            ContextAssemblyBuilder()
            .with_token_budget(15)
            .with_identifiers("assembly-1", "trace-1")
            .add_candidates(items)
        )

        result = builder.build()

        assert [item.item_id for item in result.items] == ["A", "B", "C"]
        assert result.token_count_total == 15

    def test_skips_a_high_density_item_that_no_longer_fits_but_keeps_filling(self) -> None:
        """budget=10: A(density 1.0, tok 6) fits -> remaining 4; B(density 0.9, tok 5)
        no longer fits and is skipped; C(density 0.8, tok 4) fits the remainder.

        This is the case that distinguishes the greedy fill from a naive
        "stop at first non-fit" loop: the algorithm must continue past B
        to still pick up C.
        """
        items = [
            _item("A", token_count=6, score=6.0),
            _item("B", token_count=5, score=4.5),
            _item("C", token_count=4, score=3.2),
        ]
        builder = (
            ContextAssemblyBuilder()
            .with_token_budget(10)
            .with_identifiers("assembly-2", "trace-2")
            .add_candidates(items)
        )

        result = builder.build()

        assert [item.item_id for item in result.items] == ["A", "C"]
        assert result.token_count_total == 10

    def test_low_density_item_excluded_even_though_it_alone_would_fit(self) -> None:
        """budget=5: D has the lowest density and never gets a turn because A+B
        (checked first, in density order) already exhaust the budget it needed.
        """
        items = [
            _item("A", token_count=3, score=3.0),  # density 1.0
            _item("B", token_count=2, score=1.8),  # density 0.9
            _item("D", token_count=5, score=1.0),  # density 0.2, would fit alone
        ]
        builder = (
            ContextAssemblyBuilder()
            .with_token_budget(5)
            .with_identifiers("assembly-3", "trace-3")
            .add_candidates(items)
        )

        result = builder.build()

        assert [item.item_id for item in result.items] == ["A", "B"]
        assert result.token_count_total == 5

    def test_zero_remaining_budget_selects_nothing(self) -> None:
        items = [_item("A", token_count=1, score=1.0)]
        builder = (
            ContextAssemblyBuilder()
            .with_token_budget(0)
            .with_identifiers("assembly-4", "trace-4")
            .add_candidates(items)
        )

        result = builder.build()

        assert result.items == []
        assert result.token_count_total == 0

    def test_no_candidates_added_selects_nothing(self) -> None:
        builder = (
            ContextAssemblyBuilder()
            .with_token_budget(100)
            .with_identifiers("assembly-5", "trace-5")
        )

        result = builder.build()

        assert result.items == []
        assert result.token_count_total == 0
        assert result.token_budget == 100

    def test_candidates_from_multiple_zones_merge_before_ranking(self) -> None:
        working_item = _item("w1", token_count=2, score=1.0)  # density 0.5
        semantic_item = MemoryItem(
            "s1", ZoneId.SEMANTIC, "payload", token_count=2, score=3.0
        )  # density 1.5
        builder = (
            ContextAssemblyBuilder()
            .with_token_budget(4)
            .with_identifiers("assembly-6", "trace-6")
            .add_candidates([working_item])
            .add_candidates([semantic_item])
        )

        result = builder.build()

        assert [item.item_id for item in result.items] == ["s1", "w1"]
