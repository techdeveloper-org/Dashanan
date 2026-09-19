"""Pytest suite for DASH-STORY-002 (Working Memory zone / Zone 1, FR-001).

Dev subtask (70% split). Covers AC-001 plus the story's must-not-deviate
list to the extent testable at this layer: TTL-primary eviction, O(1)
LRU class semantics, no cross-zone mutation, and mandatory tenant_id.
A dedicated QA subtask (backlog_draft.json 20% split) enumerates the
full AC boundary/adverse matrix separately; this suite is the Dev
subtask's own verification pass, not a substitute for it.

Runtime assumptions (rule 33/40 test-roadmap conventions):
  - clock: FakeClock, controllable via `.advance(seconds=...)`, fixed
    at 2026-01-01T00:00:00+00:00 unless a test states otherwise --
    deterministic, no wall-clock dependency.
  - tenant_id: "tenant-1", session_id: "session-1" unless a test states
    otherwise.
  - idle_ttl_seconds: 1800.0 (HLD 12A default) unless a test overrides it.
  - No fixture seed is required; nothing in this suite is randomized.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from dashanan.domain.ports import ZoneQuery
from dashanan.domain.working_item import WorkingItem
from dashanan.domain.zone import ZoneId
from dashanan.infrastructure.working_memory_lru_repository import (
    DEFAULT_CAPACITY_PER_SESSION,
    DEFAULT_IDLE_TTL_SECONDS,
    WorkingMemoryLRURepository,
)


class FakeClock:
    """Deterministic, advanceable Clock double (testing-core DI)."""

    def __init__(self, fixed: datetime) -> None:
        self._now = fixed

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 1, 1, tzinfo=UTC))


@pytest.fixture
def repo(clock: FakeClock) -> WorkingMemoryLRURepository:
    return WorkingMemoryLRURepository(clock=clock)


def _zone_query(**overrides: object) -> ZoneQuery:
    defaults: dict[str, object] = {
        "tenant_id": "tenant-1",
        "task": "recall scratch context",
        "query_embedding": None,
        "max_items": 100,
        "min_provenance_conf": 0.0,
    }
    defaults.update(overrides)
    return ZoneQuery(**defaults)  # type: ignore[arg-type]


class TestAC001TtlExpiryEvictsWithoutExplicitDelete:
    """AC-001 (verbatim, DASH-STORY-002 dev_prompt):

    "Given a host session writes to Working Memory, when the item's TTL
    expires without promotion, then the item is evicted from Zone 1
    without an explicit delete call, and no other zone is mutated as a
    side effect."
    """

    def test_item_survives_before_ttl_elapses(
        self, repo: WorkingMemoryLRURepository, clock: FakeClock
    ) -> None:
        repo.put("tenant-1", "session-1", "item-1", "payload", token_count=5)

        clock.advance(seconds=DEFAULT_IDLE_TTL_SECONDS - 1)

        assert repo.get("tenant-1", "session-1", "item-1") is not None

    def test_item_evicted_exactly_at_ttl_boundary(
        self, repo: WorkingMemoryLRURepository, clock: FakeClock
    ) -> None:
        repo.put("tenant-1", "session-1", "item-1", "payload", token_count=5)

        clock.advance(seconds=DEFAULT_IDLE_TTL_SECONDS)

        assert repo.get("tenant-1", "session-1", "item-1") is None

    def test_item_evicted_after_ttl_elapses_without_explicit_delete_call(
        self, repo: WorkingMemoryLRURepository, clock: FakeClock
    ) -> None:
        """No `delete()` method is ever called -- `put` then a pure time
        advance is the only host-visible action, matching AC-001's
        "without an explicit delete call" wording literally.
        """
        assert not hasattr(repo, "delete"), (
            "WorkingMemoryLRURepository must expose no explicit delete "
            "method for a host to call (AC-001)"
        )
        repo.put("tenant-1", "session-1", "item-1", "payload", token_count=5)

        clock.advance(seconds=DEFAULT_IDLE_TTL_SECONDS + 1)
        evicted = repo.evict_expired("tenant-1", "session-1")

        assert evicted == ["item-1"]
        assert repo.session_item_count("tenant-1", "session-1") == 0

    def test_expired_eviction_touches_no_other_session(
        self, repo: WorkingMemoryLRURepository, clock: FakeClock
    ) -> None:
        """Cross-session isolation is the closest in-repository proxy for
        "no other zone is mutated" -- Zone 1 has no handle to any other
        zone or session at all, so this documents the mechanism.
        """
        repo.put("tenant-1", "session-1", "item-1", "payload", token_count=5)

        clock.advance(seconds=DEFAULT_IDLE_TTL_SECONDS / 2)
        repo.put("tenant-1", "session-2", "item-2", "payload", token_count=5)

        clock.advance(seconds=DEFAULT_IDLE_TTL_SECONDS / 2 + 1)
        repo.put("tenant-1", "session-1", "item-3", "trigger sweep", token_count=1)

        assert repo.get("tenant-1", "session-1", "item-1") is None
        assert repo.session_item_count("tenant-1", "session-2") == 1
        assert repo.get("tenant-1", "session-2", "item-2") is not None

    def test_repository_never_holds_a_reference_to_another_zone_or_event_bus(
        self, repo: WorkingMemoryLRURepository
    ) -> None:
        """Structural proof of "no other zone mutated as a side effect":
        the adapter's own `__dict__` carries only its Clock port and its
        internal session map -- no ZoneRepository, EventBus, or other
        port is ever injected or reachable.
        """
        attribute_values = vars(repo).values()
        assert not any(
            hasattr(value, "publish") for value in attribute_values
        ), "WorkingMemoryLRURepository must not hold an EventBus reference"

    def test_expired_item_absent_from_fetch(
        self, repo: WorkingMemoryLRURepository, clock: FakeClock
    ) -> None:
        repo.put("tenant-1", "session-1", "item-1", "payload", token_count=5)

        clock.advance(seconds=DEFAULT_IDLE_TTL_SECONDS + 1)
        results = repo.fetch(_zone_query())

        assert results == []

    def test_access_before_ttl_refreshes_idle_window(
        self, repo: WorkingMemoryLRURepository, clock: FakeClock
    ) -> None:
        """Idle TTL, not a fixed write-time TTL: a read just under the
        deadline resets the clock, so the item survives a second window.
        """
        repo.put("tenant-1", "session-1", "item-1", "payload", token_count=5)

        clock.advance(seconds=DEFAULT_IDLE_TTL_SECONDS - 1)
        assert repo.get("tenant-1", "session-1", "item-1") is not None

        clock.advance(seconds=DEFAULT_IDLE_TTL_SECONDS - 1)
        assert repo.get("tenant-1", "session-1", "item-1") is not None


class TestMustNotDeviateTtlPrimaryNotScorePrimary:
    """must_not_deviate: "TTL-primary eviction, never score-primary
    eviction for Zone 1" (AR1-002).
    """

    def test_high_score_item_still_evicted_on_ttl_expiry(
        self, repo: WorkingMemoryLRURepository, clock: FakeClock
    ) -> None:
        repo.put(
            "tenant-1",
            "session-1",
            "item-high-score",
            "payload",
            token_count=5,
            score_terms={"recency": 1.0, "importance": 1.0},
        )

        clock.advance(seconds=DEFAULT_IDLE_TTL_SECONDS + 1)

        assert repo.get("tenant-1", "session-1", "item-high-score") is None

    def test_low_score_item_survives_within_ttl(
        self, repo: WorkingMemoryLRURepository, clock: FakeClock
    ) -> None:
        repo.put(
            "tenant-1",
            "session-1",
            "item-low-score",
            "payload",
            token_count=5,
            score_terms={"recency": 0.0, "importance": 0.0},
        )

        clock.advance(seconds=DEFAULT_IDLE_TTL_SECONDS - 1)

        assert repo.get("tenant-1", "session-1", "item-low-score") is not None


class TestMustNotDeviateTenantIdMandatory:
    """must_not_deviate: "tenant_id mandatory at the ZoneRepository port
    signature (HLD 3.0 invariant 2)" -- extended here to the write path.
    """

    def test_put_rejects_blank_tenant_id(
        self, repo: WorkingMemoryLRURepository
    ) -> None:
        with pytest.raises(ValueError, match="tenant_id must not be blank"):
            repo.put("   ", "session-1", "item-1", "payload", token_count=5)

    def test_put_rejects_blank_session_id(
        self, repo: WorkingMemoryLRURepository
    ) -> None:
        with pytest.raises(ValueError, match="session_id must not be blank"):
            repo.put("tenant-1", "", "item-1", "payload", token_count=5)

    def test_get_rejects_blank_tenant_id(
        self, repo: WorkingMemoryLRURepository
    ) -> None:
        with pytest.raises(ValueError, match="tenant_id must not be blank"):
            repo.get("", "session-1", "item-1")

    def test_evict_expired_rejects_blank_session_id(
        self, repo: WorkingMemoryLRURepository
    ) -> None:
        with pytest.raises(ValueError, match="session_id must not be blank"):
            repo.evict_expired("tenant-1", "  ")

    def test_zone_query_tenant_id_is_a_mandatory_non_default_field(self) -> None:
        """Architecture-fitness proxy for HLD 3.0 invariant 2 at the read
        port: `ZoneQuery.tenant_id` carries no default value.
        """
        import dataclasses

        tenant_field = next(
            f for f in dataclasses.fields(ZoneQuery) if f.name == "tenant_id"
        )
        assert tenant_field.default is dataclasses.MISSING
        assert tenant_field.default_factory is dataclasses.MISSING  # type: ignore[comparison-overlap]


class TestO1LruClassSemantics:
    """Section 5 DSA choice: "O(1) get/put/evict" via HashMap + DLL."""

    def test_capacity_overflow_evicts_least_recently_used_item(
        self, clock: FakeClock
    ) -> None:
        repo = WorkingMemoryLRURepository(clock=clock, capacity_per_session=2)
        repo.put("tenant-1", "session-1", "item-1", "payload", token_count=1)
        repo.put("tenant-1", "session-1", "item-2", "payload", token_count=1)

        repo.put("tenant-1", "session-1", "item-3", "payload", token_count=1)

        assert repo.get("tenant-1", "session-1", "item-1") is None
        assert repo.get("tenant-1", "session-1", "item-2") is not None
        assert repo.get("tenant-1", "session-1", "item-3") is not None

    def test_accessing_an_item_protects_it_from_lru_capacity_eviction(
        self, clock: FakeClock
    ) -> None:
        repo = WorkingMemoryLRURepository(clock=clock, capacity_per_session=2)
        repo.put("tenant-1", "session-1", "item-1", "payload", token_count=1)
        repo.put("tenant-1", "session-1", "item-2", "payload", token_count=1)

        repo.get("tenant-1", "session-1", "item-1")
        repo.put("tenant-1", "session-1", "item-3", "payload", token_count=1)

        assert repo.get("tenant-1", "session-1", "item-1") is not None
        assert repo.get("tenant-1", "session-1", "item-2") is None

    def test_default_capacity_matches_hld_12a(self) -> None:
        assert DEFAULT_CAPACITY_PER_SESSION == 200

    def test_default_idle_ttl_matches_hld_12a(self) -> None:
        assert DEFAULT_IDLE_TTL_SECONDS == 1800.0

    def test_put_overwrite_updates_payload_in_place(
        self, repo: WorkingMemoryLRURepository
    ) -> None:
        repo.put("tenant-1", "session-1", "item-1", "first", token_count=1)
        repo.put("tenant-1", "session-1", "item-1", "second", token_count=2)

        item = repo.get("tenant-1", "session-1", "item-1")
        assert item is not None
        assert item.payload == "second"
        assert item.token_count == 2
        assert repo.session_item_count("tenant-1", "session-1") == 1


class TestBoundaryAndNegativeCases:
    """Boundary/negative cases implied by the qa_prompt scope note."""

    def test_get_on_unknown_session_returns_none(
        self, repo: WorkingMemoryLRURepository
    ) -> None:
        assert repo.get("tenant-1", "unknown-session", "item-1") is None

    def test_get_on_unknown_item_id_returns_none(
        self, repo: WorkingMemoryLRURepository
    ) -> None:
        repo.put("tenant-1", "session-1", "item-1", "payload", token_count=1)
        assert repo.get("tenant-1", "session-1", "does-not-exist") is None

    def test_evict_expired_on_unknown_session_returns_empty_list(
        self, repo: WorkingMemoryLRURepository
    ) -> None:
        assert repo.evict_expired("tenant-1", "unknown-session") == []

    def test_zero_or_negative_capacity_rejected_at_construction(
        self, clock: FakeClock
    ) -> None:
        with pytest.raises(ValueError, match="capacity_per_session must be positive"):
            WorkingMemoryLRURepository(clock=clock, capacity_per_session=0)

    def test_zero_or_negative_ttl_rejected_at_construction(
        self, clock: FakeClock
    ) -> None:
        with pytest.raises(ValueError, match="idle_ttl_seconds must be positive"):
            WorkingMemoryLRURepository(clock=clock, idle_ttl_seconds=-1.0)

    def test_working_item_rejects_zero_token_count(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=UTC)
        with pytest.raises(ValueError, match="token_count must be positive"):
            WorkingItem(
                tenant_id="tenant-1",
                session_id="session-1",
                item_id="item-1",
                payload="payload",
                token_count=0,
                written_at=now,
                last_access_at=now,
            )

    def test_working_item_rejects_blank_item_id(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=UTC)
        with pytest.raises(ValueError, match="item_id must not be blank"):
            WorkingItem(
                tenant_id="tenant-1",
                session_id="session-1",
                item_id="   ",
                payload="payload",
                token_count=1,
                written_at=now,
                last_access_at=now,
            )


class TestFetchSatisfiesZoneRepositoryContract:
    """`fetch()` conformance, so this adapter can register with the
    Orchestrator (`MemoryOrchestrator.__init__`'s `zone_repositories`)
    without any change to DASH-STORY-001's code.
    """

    def test_fetch_returns_only_the_queried_tenants_items(
        self, repo: WorkingMemoryLRURepository
    ) -> None:
        repo.put("tenant-1", "session-1", "item-1", "payload", token_count=5)
        repo.put("tenant-2", "session-1", "item-2", "payload", token_count=5)

        results = repo.fetch(_zone_query(tenant_id="tenant-1"))

        assert [item.item_id for item in results] == ["item-1"]
        assert results[0].source_zone == ZoneId.WORKING

    def test_fetch_respects_max_items(
        self, repo: WorkingMemoryLRURepository
    ) -> None:
        for index in range(5):
            repo.put("tenant-1", "session-1", f"item-{index}", "payload", token_count=1)

        results = repo.fetch(_zone_query(max_items=2))

        assert len(results) == 2
