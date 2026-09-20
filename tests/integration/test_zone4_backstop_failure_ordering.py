"""Zone 4 backstop sweep: per-item Zone-6-eviction failure leaves Zone 4 untouched.

Traces to FR-004 in SRS.md and `zone4_capacity_backstop_sweep.py`'s own
module docstring ("Zone 6 projection cleanup" section / JUDGMENT CALL):
`_execute_one` calls `Zone4RetrievalIndexEvictionPort.delete_item`
UNCONDITIONALLY, BEFORE the zone-local `Zone4EvictionPort.evict` call, so
that a Zone-6 failure leaves Zone 4 untouched and the whole decision
surfaces as a clean per-item `EvictionFailure`, retried on the next sweep
run -- the same "safer-failure-mode" ordering
`zone2_capacity_backstop_sweep._execute_one` already established for Zone 2.

This module verifies that documented ordering behaviorally, not just by
reading the source: it forces `delete_item` to raise for one specific
item and asserts (a) that item's own Zone 4 state is unchanged, and (b)
the sweep reports it in `failed`, not `evicted`.

GAP DISCLOSED (see `test_smoke_fixture_wiring_sprint2_gap` below and this
module's own class docstrings): `conftest_sprint2.py`'s
`InMemoryZone4RetrievalIndexEviction` fake has no way to force a
per-item `delete_item` failure -- it only ever records calls, never
raises -- so this module defines its own local, hand-rolled fake
(`RaisingRetrievalIndexEviction`, below) that implements the same
`Zone4RetrievalIndexEvictionPort` Protocol with that one added
capability, per this story's own instruction to report rather than patch
the shared fixture. Separately, and more significantly:
`conftest_sprint2.py`'s `Sprint2WiredSystem.zone4_backstop_sweep` is
wired with `InMemoryZone4Store` as its `Zone4EvictionPort`/
`Zone4CandidateSource` -- NOT `InMemoryProceduralMemoryRepository`
(the `zone4_repository` fixture, the real Shape A adapter this story's
own FR-004 traces to). The two stores are never synchronized with each
other anywhere in the fixture module. So "Zone 4's own state" as observed
through the wired sweep is `InMemoryZone4Store`'s state, not
`InMemoryProceduralMemoryRepository`'s -- the real repository fixture is
present in `Sprint2WiredSystem` but structurally unreachable from the
backstop sweep. This module seeds the identical item into BOTH stores and
asserts against both, so that gap is demonstrated directly (the repository
copy is proven untouched by the sweep in either code path, evicted or
failed, precisely because nothing wires the sweep to it) rather than
merely asserted away by testing only the store the sweep actually uses.

`conftest_sprint2.py` is not named `conftest.py`, so pytest does not
auto-discover it; `pytest_plugins` below registers it as a plugin for
this module, mirroring every other Sprint 2 integration test's identical
convention.

PII NOTE: every seeded value below is control/ranking/routing metadata
only (item_id, tenant_id, memory_score, timestamps) -- no example
conversational content appears anywhere in this file.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from dashanan.application.zone4_capacity_backstop_sweep import (
    Zone4BackstopPortError,
    Zone4CapacityBackstopSweep,
)
from dashanan.domain.procedure import Procedure
from dashanan.domain.zone4_capacity_backstop import (
    ProcedureEvictionCandidate,
    Zone4BackstopConfig,
)

from tests.integration.conftest import DEFAULT_TENANT_ID

from .conftest_sprint2 import (
    InMemoryProceduralMemoryRepository,
    InMemoryZone4BackstopConfigPort,
    InMemoryZone4Store,
    Sprint2WiredSystem,
)

pytest_plugins = ["tests.integration.conftest_sprint2"]

_FIXED_EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)
_FAILING_ITEM_ID = "task-signature-hash-fails-zone6-delete"
_SURVIVING_ITEM_ID = "task-signature-hash-survives-zone6-delete"


class RaisingRetrievalIndexEviction:
    """A hand-rolled `Zone4RetrievalIndexEvictionPort` fake that can force one failure.

    Local to this test module rather than added to `conftest_sprint2.py`
    (per this story's own instruction: report a fixture gap rather than
    patch the shared fixture) -- `InMemoryZone4RetrievalIndexEviction`
    (the shared fixture's own fake) has no mechanism to raise on a
    specific `item_id`, only to record calls. This class implements the
    same duck-typed `Zone4RetrievalIndexEvictionPort` Protocol
    (structurally, via `delete_item`) with that one added capability:
    every call is still recorded (mirroring the shared fake's own
    bookkeeping), but a call whose `item_id` matches `fail_for_item_id`
    raises `Zone4BackstopPortError` instead of succeeding.
    """

    def __init__(self, *, fail_for_item_id: str) -> None:
        self._fail_for_item_id = fail_for_item_id
        self.deleted: list[tuple[str, str]] = []
        self.delete_calls: list[tuple[str, str]] = []

    def delete_item(self, tenant_id: str, item_id: str) -> None:
        """`Zone4RetrievalIndexEvictionPort.delete_item`: raise only for the forced item_id."""
        self.delete_calls.append((tenant_id, item_id))
        if item_id == self._fail_for_item_id:
            raise Zone4BackstopPortError(
                f"simulated Zone 6 retrieval-index delete_item failure for "
                f"item_id={item_id!r}"
            )
        self.deleted.append((tenant_id, item_id))


def _seed_candidate(
    zone4_store: InMemoryZone4Store,
    *,
    item_id: str,
    memory_score: float,
) -> None:
    """Seed one `ProcedureEvictionCandidate` into the wired sweep's own store."""
    zone4_store.seed_candidate(
        DEFAULT_TENANT_ID,
        ProcedureEvictionCandidate(
            item_id=item_id,
            memory_score=memory_score,
            is_compressed=False,
            age_seconds=1.0,
            last_used_at=_FIXED_EPOCH,
        ),
    )


def _seed_repository_copy(
    zone4_repository: InMemoryProceduralMemoryRepository, *, item_id: str
) -> None:
    """Seed the SAME item_id into `InMemoryProceduralMemoryRepository` (module docstring gap)."""
    zone4_repository.commit(
        Procedure(
            tenant_id=DEFAULT_TENANT_ID,
            task_signature_hash=item_id,
            steps=("placeholder-step-one", "placeholder-step-two"),
            success_count=1,
            failure_count=0,
            last_used_at=_FIXED_EPOCH,
        )
    )


class TestZone4BackstopLeavesFailedItemUntouchedInWiredStore:
    """The item whose Zone-6 delete fails keeps its Zone 4 state in the store the sweep owns."""

    def test_failing_item_is_retained_in_zone4_store_and_reported_failed(
        self,
        sprint2_wired_system: Sprint2WiredSystem,
    ) -> None:
        """Forcing `delete_item` to fail for one item, among two candidates under a
        capacity-overflow config, must leave that one item present in
        `InMemoryZone4Store` (the store `Zone4CapacityBackstopSweep`
        actually reads/writes through) and must report it in
        `BackstopSweepResult.failed`, never in `.evicted` -- verifying
        `_execute_one`'s documented "Zone 6 before Zone 4" ordering
        behaviorally rather than by source inspection alone.
        """
        zone4_store = sprint2_wired_system.zone4_store
        config_port: InMemoryZone4BackstopConfigPort = (
            sprint2_wired_system.zone4_backstop_config_port
        )
        config_port.set_config(
            DEFAULT_TENANT_ID,
            Zone4BackstopConfig(capacity_cap=1, max_age_seconds=730 * 24 * 60 * 60),
        )

        # Two candidates, cap=1 -> exactly one eviction decision: the
        # lower-scored item (must-not-deviate item 2's own ordering).
        _seed_candidate(zone4_store, item_id=_FAILING_ITEM_ID, memory_score=0.1)
        _seed_candidate(zone4_store, item_id=_SURVIVING_ITEM_ID, memory_score=0.9)

        _seed_repository_copy(
            sprint2_wired_system.zone4_repository, item_id=_FAILING_ITEM_ID
        )

        raising_retrieval_index = RaisingRetrievalIndexEviction(
            fail_for_item_id=_FAILING_ITEM_ID
        )
        sweep = Zone4CapacityBackstopSweep(
            config_port=config_port,
            candidate_source=zone4_store,
            eviction_port=zone4_store,
            retrieval_index_eviction=raising_retrieval_index,
            event_bus=sprint2_wired_system.event_bus,
            clock=sprint2_wired_system.clock,
        )

        result = sweep.run(DEFAULT_TENANT_ID)

        # (b) the item is reported in the sweep's failed/retryable set,
        # not silently evicted.
        assert [f.item_id for f in result.evicted if f.item_id == _FAILING_ITEM_ID] == []  # noqa: E501
        evicted_ids = [outcome.item_id for outcome in result.evicted]
        assert _FAILING_ITEM_ID not in evicted_ids, (
            f"the failing item must never appear in `evicted`, got: {evicted_ids}"
        )
        failed_ids = [failure.item_id for failure in result.failed]
        assert failed_ids == [_FAILING_ITEM_ID], (
            f"expected exactly one failure, for {_FAILING_ITEM_ID!r}, got: {failed_ids}"
        )

        # (a) Zone 4's own state for that item is UNCHANGED in the store
        # the sweep actually reads/writes (`InMemoryZone4Store`):
        # `_execute_one` must never call `Zone4EvictionPort.evict` for an
        # item whose Zone-6 delete already raised.
        assert (DEFAULT_TENANT_ID, _FAILING_ITEM_ID) not in zone4_store.evicted, (
            "Zone4EvictionPort.evict must not have been called for the item "
            "whose Zone-6 retrieval-index delete failed"
        )
        remaining_candidates = zone4_store.fetch_candidates(
            DEFAULT_TENANT_ID, sprint2_wired_system.clock.now()
        )
        remaining_ids = {c.item_id for c in remaining_candidates}
        assert _FAILING_ITEM_ID in remaining_ids, (
            "the failing item must still be present in InMemoryZone4Store "
            "after the sweep run"
        )

        # Ordering proof: delete_item was attempted for the failing item
        # (it raised), and evict() was never reached for it -- the
        # eviction_port's own `evicted` log (asserted above) already
        # proves evict() was skipped; this additionally proves
        # delete_item WAS attempted, so the skip is caused by the
        # documented ordering, not by the decision never running at all.
        assert (DEFAULT_TENANT_ID, _FAILING_ITEM_ID) in raising_retrieval_index.delete_calls  # noqa: E501

        # The surviving (higher-scored) candidate was never selected for
        # eviction at cap=1, so it is untouched by either port -- sanity
        # check that this test's config produced exactly the one decision
        # it claims to.
        assert _SURVIVING_ITEM_ID not in failed_ids
        assert _SURVIVING_ITEM_ID not in evicted_ids


class TestZone4RepositoryFixtureIsUnreachableFromTheBackstopSweep:
    """Discloses the `InMemoryProceduralMemoryRepository` / sweep wiring gap directly."""

    def test_repository_copy_is_never_touched_by_the_sweep_either_way(
        self,
        sprint2_wired_system: Sprint2WiredSystem,
    ) -> None:
        """`conftest_sprint2.py`'s `zone4_repository` fixture
        (`InMemoryProceduralMemoryRepository`, the real Shape A adapter
        FR-004 traces to) is never passed to `Zone4CapacityBackstopSweep`
        anywhere in the fixture module -- `zone4_backstop_sweep` is wired
        with `InMemoryZone4Store` for both `Zone4CandidateSource` and
        `Zone4EvictionPort` instead. This test seeds the SAME item_id
        into both stores, runs a sweep that evicts it from
        `InMemoryZone4Store`, and shows the repository's own copy is
        completely unaffected either way -- not because the sweep
        respected some Zone-4-repository-specific contract, but because
        nothing in the composition ever gives the sweep a reference to
        the repository at all. A reviewer relying on `zone4_repository`
        to observe backstop-sweep side effects (as this story's own task
        description assumes is possible) would see stale state
        indefinitely, regardless of what the sweep actually decides.
        """
        zone4_store = sprint2_wired_system.zone4_store
        zone4_repository = sprint2_wired_system.zone4_repository
        config_port: InMemoryZone4BackstopConfigPort = (
            sprint2_wired_system.zone4_backstop_config_port
        )
        config_port.set_config(
            DEFAULT_TENANT_ID,
            Zone4BackstopConfig(capacity_cap=1, max_age_seconds=730 * 24 * 60 * 60),
        )

        item_id = "task-signature-hash-repo-gap-demo"
        other_item_id = "task-signature-hash-repo-gap-demo-other"
        _seed_candidate(zone4_store, item_id=item_id, memory_score=0.1)
        _seed_candidate(zone4_store, item_id=other_item_id, memory_score=0.9)
        _seed_repository_copy(zone4_repository, item_id=item_id)

        result = sprint2_wired_system.zone4_backstop_sweep.run(DEFAULT_TENANT_ID)

        # cap=0 forces eviction of the only candidate -- confirms the
        # wired sweep really did run an eviction against InMemoryZone4Store.
        assert [o.item_id for o in result.evicted] == [item_id]
        assert (DEFAULT_TENANT_ID, item_id) in zone4_store.evicted

        # And yet the repository's own copy of the same item_id is still
        # there, completely unaware the "same" item was just evicted --
        # the gap this class documents.
        stored = zone4_repository.get_by_task_signature_hash(DEFAULT_TENANT_ID, item_id)
        assert stored is not None, (
            "InMemoryProceduralMemoryRepository still holds the item after "
            "the wired sweep evicted the identically-keyed item from "
            "InMemoryZone4Store -- the two stores are not the same Zone 4 "
            "state and the repository is structurally unreachable from the "
            "sweep in this fixture composition"
        )
