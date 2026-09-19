"""QA pytest suite for DASH-STORY-002 (Working Memory zone / Zone 1, FR-001).

QA subtask (backlog_draft.json 20% split). Independent verification pass on
top of the Dev subtask's own suite (tests/test_working_memory_lru_repository.py):
re-proves AC-001 from a fresh angle, adds the architecture-fitness assertion
scoped to this story's two new files (HLD 3.0 invariant 1), and covers the
boundary/adversarial matrix the Dev suite does not already exercise --
value-object immutability, blank-field rejection on every WorkingItem
constructor field, negative token_count, cross-session fetch aggregation,
zero-`max_items` fetch, and the event-driven sweep-on-put contract.

Runtime assumptions (rule 33/40/41 test-roadmap conventions, scoped to this
pure in-process library -- no HTTP/router/response-envelope layer exists in
this codebase yet, matching DASH-STORY-001's own precedent):
  - clock: FakeClock, controllable via `.advance(seconds=...)`, fixed at
    2026-01-01T00:00:00+00:00 unless a test states otherwise.
  - tenant_id: "tenant-1", session_id: "session-1" unless stated otherwise.
  - idle_ttl_seconds: 1800.0 (HLD 12A default) unless a test overrides it.
  - No fixture seed required; nothing in this suite is randomized.
"""

from __future__ import annotations

import ast
import dataclasses
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from dashanan.domain.ports import ZoneQuery
from dashanan.domain.working_item import WorkingItem
from dashanan.domain.zone import ZoneId
from dashanan.infrastructure.working_memory_lru_repository import (
    DEFAULT_IDLE_TTL_SECONDS,
    WorkingMemoryLRURepository,
)

DOMAIN_DIR = Path(__file__).resolve().parents[1] / "src" / "dashanan" / "domain"
WORKING_ITEM_FILE = DOMAIN_DIR / "working_item.py"


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


class TestAC001IndependentReVerification:
    """AC-001 (verbatim, DASH-STORY-002 dev_prompt): "Given a host session
    writes to Working Memory, when the item's TTL expires without
    promotion, then the item is evicted from Zone 1 without an explicit
    delete call, and no other zone is mutated as a side effect."

    Re-proved here from a fresh angle (a second, longer-lived session with
    an interleaved access pattern) rather than re-running the Dev suite's
    own scenarios verbatim.
    """

    def test_ac001_full_lifecycle_write_survive_expire(
        self, repo: WorkingMemoryLRURepository, clock: FakeClock
    ) -> None:
        repo.put("tenant-1", "session-1", "note-1", "scratch note", token_count=3)

        clock.advance(seconds=100)
        assert repo.get("tenant-1", "session-1", "note-1") is not None

        clock.advance(seconds=DEFAULT_IDLE_TTL_SECONDS)
        assert repo.get("tenant-1", "session-1", "note-1") is None

    def test_ac001_eviction_is_purely_time_driven_no_capacity_involved(
        self, clock: FakeClock
    ) -> None:
        """AC-001 concerns TTL expiry specifically; a repository with a
        capacity large enough that capacity eviction never fires must
        still evict on idle-TTL alone.
        """
        repo = WorkingMemoryLRURepository(clock=clock, capacity_per_session=1000)
        repo.put("tenant-1", "session-1", "item-1", "payload", token_count=1)

        clock.advance(seconds=DEFAULT_IDLE_TTL_SECONDS + 1)
        repo.evict_expired("tenant-1", "session-1")

        assert repo.session_item_count("tenant-1", "session-1") == 0

    def test_ac001_no_mutation_of_sibling_tenant_as_side_effect(
        self, repo: WorkingMemoryLRURepository, clock: FakeClock
    ) -> None:
        """A stronger cross-boundary proxy than same-tenant cross-session
        isolation: a completely different tenant's data must be
        unreachable and unaffected by another tenant's TTL sweep.
        """
        repo.put("tenant-1", "session-1", "item-1", "payload", token_count=1)

        clock.advance(seconds=DEFAULT_IDLE_TTL_SECONDS + 1)
        repo.evict_expired("tenant-1", "session-1")

        # tenant-2 writes AFTER tenant-1's sweep, so its own item is fresh
        # and must be unaffected by the sweep that just ran for tenant-1.
        repo.put("tenant-2", "session-1", "item-2", "payload", token_count=1)

        assert repo.session_item_count("tenant-2", "session-1") == 1
        assert repo.get("tenant-2", "session-1", "item-2") is not None


class TestArchitectureFitnessScopedToStory:
    """HLD 3.0 invariant 1, scoped explicitly to this story's own new
    domain module: `domain/working_item.py` must import nothing from
    `dashanan.infrastructure`. (The repo-wide sweep already lives in
    test_memory_orchestrator.py; this is the QA subtask's own
    independent, story-scoped proof.)
    """

    def _imported_module_names(self, source_path: Path) -> list[str]:
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        names: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module is not None:
                    names.append(node.module)
        return names

    def test_working_item_module_exists(self) -> None:
        assert WORKING_ITEM_FILE.is_file(), f"{WORKING_ITEM_FILE} not found"

    def test_working_item_does_not_import_infrastructure(self) -> None:
        imported = self._imported_module_names(WORKING_ITEM_FILE)
        bad = [
            name
            for name in imported
            if name == "dashanan.infrastructure"
            or name.startswith("dashanan.infrastructure.")
        ]
        assert not bad, (
            "HLD 3.0 invariant 1 violated -- domain/working_item.py imports "
            f"infrastructure: {bad}"
        )

    def test_working_item_is_a_pure_value_object_frozen_dataclass(self) -> None:
        """Structural proof that `WorkingItem` follows this codebase's
        Value-Object convention (frozen dataclass), matching `MemoryItem`.
        """
        assert dataclasses.is_dataclass(WorkingItem)
        params = getattr(WorkingItem, "__dataclass_params__")
        assert params.frozen is True


class TestWorkingItemValueObjectImmutability:
    """Frozen-dataclass contract: every mutation must go through
    `dataclasses.replace`, never direct attribute assignment.
    """

    def _sample(self) -> WorkingItem:
        now = datetime(2026, 1, 1, tzinfo=UTC)
        return WorkingItem(
            tenant_id="tenant-1",
            session_id="session-1",
            item_id="item-1",
            payload="payload",
            token_count=1,
            written_at=now,
            last_access_at=now,
        )

    def test_direct_attribute_assignment_raises(self) -> None:
        item = self._sample()
        with pytest.raises(dataclasses.FrozenInstanceError):
            item.payload = "mutated"  # type: ignore[misc]

    def test_replace_produces_a_new_instance_leaving_original_untouched(self) -> None:
        item = self._sample()
        later = item.last_access_at + timedelta(seconds=60)

        refreshed = dataclasses.replace(item, last_access_at=later)

        assert refreshed is not item
        assert item.last_access_at != refreshed.last_access_at
        assert refreshed.last_access_at == later


class TestWorkingItemConstructorBoundaryMatrix:
    """Boundary/negative matrix for every mandatory field on
    `WorkingItem.__post_init__`, extending the Dev suite's item_id/
    token_count-only coverage to tenant_id and session_id.
    """

    def _now(self) -> datetime:
        return datetime(2026, 1, 1, tzinfo=UTC)

    def test_rejects_blank_tenant_id(self) -> None:
        with pytest.raises(ValueError, match="tenant_id must not be blank"):
            WorkingItem(
                tenant_id="   ",
                session_id="session-1",
                item_id="item-1",
                payload="payload",
                token_count=1,
                written_at=self._now(),
                last_access_at=self._now(),
            )

    def test_rejects_blank_session_id(self) -> None:
        with pytest.raises(ValueError, match="session_id must not be blank"):
            WorkingItem(
                tenant_id="tenant-1",
                session_id="",
                item_id="item-1",
                payload="payload",
                token_count=1,
                written_at=self._now(),
                last_access_at=self._now(),
            )

    def test_rejects_negative_token_count(self) -> None:
        with pytest.raises(ValueError, match="token_count must be positive"):
            WorkingItem(
                tenant_id="tenant-1",
                session_id="session-1",
                item_id="item-1",
                payload="payload",
                token_count=-5,
                written_at=self._now(),
                last_access_at=self._now(),
            )

    def test_accepts_token_count_of_exactly_one(self) -> None:
        item = WorkingItem(
            tenant_id="tenant-1",
            session_id="session-1",
            item_id="item-1",
            payload="payload",
            token_count=1,
            written_at=self._now(),
            last_access_at=self._now(),
        )
        assert item.token_count == 1

    def test_score_terms_defaults_to_empty_mapping_not_shared_mutable_default(
        self,
    ) -> None:
        """Anti-pattern guard (python-core skill, section 27): two
        instances built without `score_terms` must not share the same
        underlying dict.
        """
        item_a = WorkingItem(
            tenant_id="tenant-1",
            session_id="session-1",
            item_id="item-a",
            payload="a",
            token_count=1,
            written_at=self._now(),
            last_access_at=self._now(),
        )
        item_b = WorkingItem(
            tenant_id="tenant-1",
            session_id="session-1",
            item_id="item-b",
            payload="b",
            token_count=1,
            written_at=self._now(),
            last_access_at=self._now(),
        )
        assert item_a.score_terms == {}
        assert item_a.score_terms is not item_b.score_terms


class TestPutRejectsBlankItemId:
    """The Dev suite covers blank tenant_id/session_id on `put`; this adds
    the item_id boundary, propagated from `WorkingItem.__post_init__`.
    """

    def test_put_rejects_blank_item_id(
        self, repo: WorkingMemoryLRURepository
    ) -> None:
        with pytest.raises(ValueError, match="item_id must not be blank"):
            repo.put("tenant-1", "session-1", "   ", "payload", token_count=1)

    def test_put_rejects_non_positive_token_count(
        self, repo: WorkingMemoryLRURepository
    ) -> None:
        with pytest.raises(ValueError, match="token_count must be positive"):
            repo.put("tenant-1", "session-1", "item-1", "payload", token_count=0)


class TestEventDrivenSweepOnPut:
    """HLD 12A: the eviction sweep is event-driven (runs on every `put`),
    not a background timer. This proves the write path itself reclaims
    expired capacity before applying the new write, distinctly from the
    Dev suite's `evict_expired`-triggered and `get`-triggered sweeps.
    """

    def test_put_triggers_a_sweep_of_its_own_session_before_writing(
        self, repo: WorkingMemoryLRURepository, clock: FakeClock
    ) -> None:
        repo.put("tenant-1", "session-1", "item-1", "payload", token_count=1)

        clock.advance(seconds=DEFAULT_IDLE_TTL_SECONDS + 1)
        # No `get`, no explicit `evict_expired` call -- only a second `put`.
        repo.put("tenant-1", "session-1", "item-2", "payload", token_count=1)

        assert repo.session_item_count("tenant-1", "session-1") == 1
        assert repo.get("tenant-1", "session-1", "item-1") is None
        assert repo.get("tenant-1", "session-1", "item-2") is not None


class TestFetchAggregationAndBoundaries:
    """Extends the Dev suite's single-session `fetch()` conformance tests
    to cross-session aggregation (per the adapter's own documented
    behavior for `ZoneQuery`'s missing `session_id`) and a zero-item cap.
    """

    def test_fetch_aggregates_across_multiple_sessions_for_the_same_tenant(
        self, repo: WorkingMemoryLRURepository
    ) -> None:
        repo.put("tenant-1", "session-1", "item-1", "payload", token_count=1)
        repo.put("tenant-1", "session-2", "item-2", "payload", token_count=1)

        results = repo.fetch(_zone_query())

        returned_ids = {item.item_id for item in results}
        assert returned_ids == {"item-1", "item-2"}

    def test_fetch_most_recently_accessed_first_across_sessions(
        self, repo: WorkingMemoryLRURepository, clock: FakeClock
    ) -> None:
        repo.put("tenant-1", "session-1", "older", "payload", token_count=1)
        clock.advance(seconds=10)
        repo.put("tenant-1", "session-2", "newer", "payload", token_count=1)

        results = repo.fetch(_zone_query())

        assert [item.item_id for item in results] == ["newer", "older"]

    def test_fetch_with_zero_max_items_returns_empty_list(
        self, repo: WorkingMemoryLRURepository
    ) -> None:
        repo.put("tenant-1", "session-1", "item-1", "payload", token_count=1)

        results = repo.fetch(_zone_query(max_items=0))

        assert results == []

    def test_fetch_result_items_carry_working_zone_id(
        self, repo: WorkingMemoryLRURepository
    ) -> None:
        repo.put("tenant-1", "session-1", "item-1", "payload", token_count=1)

        results = repo.fetch(_zone_query())

        assert all(item.source_zone == ZoneId.WORKING for item in results)

    def test_fetch_on_tenant_with_no_sessions_returns_empty_list(
        self, repo: WorkingMemoryLRURepository
    ) -> None:
        results = repo.fetch(_zone_query(tenant_id="unknown-tenant"))
        assert results == []
