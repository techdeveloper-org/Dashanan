"""QA pytest suite for DASH-STORY-003 (Episodic Memory zone / Zone 2, FR-002).

QA subtask (backlog_draft.json 30% split), independent verification pass on
top of the Dev subtask's own suite (tests/test_smoke_episodic.py). That dev
suite is explicitly scoped to structural/wiring smoke checks; this suite is
the story's formal AC-by-AC proof, matching the split DASH-STORY-001's
test_smoke.py vs test_memory_orchestrator.py already established and
DASH-STORY-002's test_smoke.py vs test_qa_working_memory_zone1.py repeated.

Unlike the dev suite -- which asserts against literal SQL text via a
recording cursor double -- this suite drives `SqlEpisodicRepository`
against a FAKE that actually *executes* each of the four query shapes'
real filter/sort/limit semantics over an in-memory row store. This proves
AC-002 and AC-002-DPDP-1 behaviorally (real chronological ordering, real
per-entry decay values, a real O(1) primary-key-indexed lookup) rather
than only proving the SQL text looks right.

Acceptance criteria under test, verbatim from
docs/phase-7-routing/implementation_execution_plan.json's DASH-STORY-003
dev_prompt (the qa_prompt cites the identical criteria):

  AC-002: "Given multiple Episodic entries with distinct timestamps, when
  queried by recency, then results return in strict chronological order
  and each entry exposes its own decay attribute."

  AC-002-DPDP-1 (REDACTED, no PII examples): "Given Zone 2 may hold PII, a
  DPDP erasure/purpose-limitation request SHALL be locatable and
  satisfiable for a stored entry (NFR-006, crypto-shredding per AC-013).
  'Locatable' is defined operationally: a fact is locatable for erasure
  purposes if its item_id (episode_id) resolves to its primary key
  (tenant_id, session_id, seq) via a direct B-tree PK lookup, never a
  full-zone BRIN range scan, within the Zone 2 episodic store (HLD
  3.3/12D)."

MUST-NOT-DEVIATE items under test (ar1_assignments.json, AR1-003):
  - BRIN on occurred_at, B-tree PK on (tenant, session, seq)
  - Keyset pagination, never OFFSET
  - Per-entry decay attribute exposed in the query result (AC-002)
  - Append-only
  - tenant_id in every query, no exceptions
  - "Locatable" means direct B-tree PK lookup, never a full-zone scan

Plus the architecture-fitness invariant (HLD Section 3.0, invariant 1):
no domain/** module may import infrastructure/**, scoped here to this
story's own two new domain modules (episode.py, episodic_query.py) --
the repo-wide sweep already lives in test_memory_orchestrator.py.

Runtime assumptions (rule 33/40/41 test-roadmap conventions, scoped to
this pure in-process library -- no HTTP/router/response-envelope layer
exists in this codebase, matching DASH-STORY-001/002's own precedent):
  - clock: FakeClock, fixed at 2026-01-01T00:00:00+00:00 unless a test
    states otherwise -- deterministic, no wall-clock dependency.
  - tenant_id: "tenant-1" for all requests unless a test states otherwise.
  - No fixture seed is required; nothing in this suite is randomized.

PII NOTE: per the dev_prompt's PII constraint, no example conversational
content appears anywhere below -- payload fixtures use the literal
`<PII_EXAMPLE_REDACTED>` placeholder, matching the dev subtask's own
convention.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from dashanan.domain.episode import Episode, EpisodeState
from dashanan.domain.episodic_query import EpisodicCursor
from dashanan.domain.ports import ZoneQuery
from dashanan.infrastructure.sql_episodic_repository import (
    _APPEND_SQL,
    _FETCH_PAGE_SQL_NO_CURSOR,
    _FETCH_PAGE_SQL_WITH_CURSOR,
    _FETCH_RECENCY_SQL,
    _LOCATE_BY_PK_SQL,
    SqlEpisodicRepository,
)

DOMAIN_DIR = Path(__file__).resolve().parents[1] / "src" / "dashanan" / "domain"
EPISODE_FILE = DOMAIN_DIR / "episode.py"
EPISODIC_QUERY_FILE = DOMAIN_DIR / "episodic_query.py"

_PLACEHOLDER_PAYLOAD = "<PII_EXAMPLE_REDACTED>"


class FakeClock:
    """Deterministic Clock double (testing-core DI), matching the story's own pattern."""

    def __init__(self, fixed: datetime) -> None:
        self._fixed = fixed

    def now(self) -> datetime:
        return self._fixed


@pytest.fixture
def fixed_clock() -> FakeClock:
    return FakeClock(datetime(2026, 1, 10, tzinfo=UTC))


class FakeEpisodicCursor:
    """A DB-API cursor double that actually EXECUTES each query shape's
    real filter/sort/limit/lookup semantics against an in-memory row
    store, rather than only recording the SQL text.

    Rows are stored exactly as `SqlEpisodicRepository.append()` builds its
    INSERT params tuple -- `_SELECT_COLUMNS`' column order and the INSERT
    column order are identical in the real adapter, so a stored append
    row can be returned directly from a SELECT without any reshaping,
    exactly as a real driver round-trip would.

    A separate `_pk_index` dict backs `_LOCATE_BY_PK_SQL`: this is the
    behavioral proof for AC-002-DPDP-1's "direct B-tree PK lookup, never
    a full-zone scan" -- resolving `episode_id` never iterates
    `self.rows`, so its cost is independent of how many episodes exist in
    the zone, however large `self.rows` grows.
    """

    def __init__(self, store: "FakeEpisodicStore") -> None:
        self._store = store
        self.executed: list[tuple[str, tuple[object, ...]]] = []
        self._last_result: list[tuple[object, ...]] = []

    def execute(self, sql: str, params: Sequence[object]) -> None:
        params = tuple(params)
        self.executed.append((sql, params))

        if sql == _APPEND_SQL:
            self._store.insert(params)
            self._last_result = []
            return

        if sql == _FETCH_RECENCY_SQL:
            tenant_id, as_of_a, _as_of_b, max_items_raw = params
            max_items = cast(int, max_items_raw)
            self._store.record_full_scan()
            candidates = [row for row in self._store.rows if row[0] == tenant_id]
            if as_of_a is not None:
                as_of = cast(datetime, as_of_a)
                candidates = [
                    row for row in candidates if cast(datetime, row[4]) <= as_of
                ]
            candidates.sort(key=lambda row: (row[4], row[1], row[3]), reverse=True)
            self._last_result = candidates[:max_items]
            return

        if sql == _FETCH_PAGE_SQL_NO_CURSOR:
            tenant_id, session_id, limit_raw = params
            limit = cast(int, limit_raw)
            self._store.record_full_scan()
            candidates = [
                row
                for row in self._store.rows
                if row[0] == tenant_id and row[1] == session_id
            ]
            candidates.sort(key=lambda row: (row[4], row[3]), reverse=True)
            self._last_result = candidates[:limit]
            return

        if sql == _FETCH_PAGE_SQL_WITH_CURSOR:
            tenant_id, session_id, after_occurred_at, after_seq, limit_raw = params
            limit = cast(int, limit_raw)
            self._store.record_full_scan()
            candidates = [
                row
                for row in self._store.rows
                if row[0] == tenant_id
                and row[1] == session_id
                and (row[4], row[3]) < (after_occurred_at, after_seq)
            ]
            candidates.sort(key=lambda row: (row[4], row[3]), reverse=True)
            self._last_result = candidates[:limit]
            return

        if sql == _LOCATE_BY_PK_SQL:
            tenant_id, session_id, seq = params
            self._store.record_pk_lookup()
            row = self._store.pk_index.get((tenant_id, session_id, seq))
            self._last_result = [row] if row is not None else []
            return

        raise AssertionError(f"FakeEpisodicCursor received an unrecognized query: {sql!r}")

    def fetchall(self) -> list[tuple[object, ...]]:
        return list(self._last_result)


class FakeEpisodicStore:
    """The in-memory backing store `FakeEpisodicCursor` operates over.

    Tracks `full_scan_calls` (queries whose cost is proportional to
    `len(rows)`: the two recency/pagination paths) separately from
    `pk_lookup_calls` (the O(1) dict-indexed `locate` path), so a test
    can assert the two access patterns never converge as the store grows
    -- the operational core of AC-002-DPDP-1.
    """

    def __init__(self) -> None:
        self.rows: list[tuple[object, ...]] = []
        self.pk_index: dict[tuple[object, object, object], tuple[object, ...]] = {}
        self.full_scan_calls: int = 0
        self.pk_lookup_calls: int = 0

    def insert(self, row: tuple[object, ...]) -> None:
        self.rows.append(row)
        tenant_id, session_id, _episode_id, seq = row[0], row[1], row[2], row[3]
        self.pk_index[(tenant_id, session_id, seq)] = row

    def record_full_scan(self) -> None:
        self.full_scan_calls += 1

    def record_pk_lookup(self) -> None:
        self.pk_lookup_calls += 1


class FakeEpisodicConnection:
    """DB-API connection double exposing one shared `FakeEpisodicCursor`."""

    def __init__(self) -> None:
        self.store = FakeEpisodicStore()
        self.cursor_obj = FakeEpisodicCursor(self.store)

    def cursor(self) -> FakeEpisodicCursor:
        return self.cursor_obj


def _episode(
    tenant_id: str = "tenant-1",
    session_id: str = "session-1",
    seq: int = 0,
    occurred_at: datetime | None = None,
    written_at: datetime | None = None,
    token_count: int = 10,
    state: EpisodeState = EpisodeState.ACTIVE,
) -> Episode:
    occurred = occurred_at or datetime(2026, 1, 1, tzinfo=UTC)
    return Episode(
        tenant_id=tenant_id,
        session_id=session_id,
        episode_id=Episode.episode_id_for(tenant_id, session_id, seq),
        seq=seq,
        occurred_at=occurred,
        written_at=written_at or occurred,
        payload=_PLACEHOLDER_PAYLOAD,
        actors=("user", "agent"),
        token_count=token_count,
        state=state,
        score_terms={},
    )


def _zone_query(**overrides: object) -> ZoneQuery:
    defaults: dict[str, object] = {
        "tenant_id": "tenant-1",
        "task": "recall recent episodic context",
        "query_embedding": None,
        "max_items": 100,
        "min_provenance_conf": 0.0,
    }
    defaults.update(overrides)
    return ZoneQuery(**defaults)  # type: ignore[arg-type]


@pytest.fixture
def connection() -> FakeEpisodicConnection:
    return FakeEpisodicConnection()


@pytest.fixture
def repo(
    connection: FakeEpisodicConnection, fixed_clock: FakeClock
) -> SqlEpisodicRepository:
    return SqlEpisodicRepository(connection, fixed_clock)


class TestAC002ChronologicalOrderRealBehavior:
    """AC-002 (verbatim): "when queried by recency, then results return
    in strict chronological order" -- proven here against a store that
    actually sorts, not only against a recorded SQL string.
    """

    def test_fetch_tenant_wide_recency_returns_strict_descending_order(
        self, repo: SqlEpisodicRepository, fixed_clock: FakeClock
    ) -> None:
        base = datetime(2026, 1, 1, tzinfo=UTC)
        # Appended deliberately OUT of chronological order.
        repo.append(_episode(seq=2, occurred_at=base + timedelta(days=5)))
        repo.append(_episode(seq=0, occurred_at=base))
        repo.append(_episode(seq=1, occurred_at=base + timedelta(days=2)))

        items = repo.fetch(_zone_query(max_items=10))

        assert [item.item_id for item in items] == [
            Episode.episode_id_for("tenant-1", "session-1", 2),
            Episode.episode_id_for("tenant-1", "session-1", 1),
            Episode.episode_id_for("tenant-1", "session-1", 0),
        ]

    def test_fetch_page_within_a_session_returns_strict_descending_order(
        self, repo: SqlEpisodicRepository
    ) -> None:
        base = datetime(2026, 1, 1, tzinfo=UTC)
        for seq, offset_days in ((0, 0), (1, 3), (2, 1), (3, 7)):
            repo.append(_episode(seq=seq, occurred_at=base + timedelta(days=offset_days)))

        page = repo.fetch_page(tenant_id="tenant-1", session_id="session-1", page_size=10)

        occurred_ats = [item.occurred_at for item in page.items]
        assert occurred_ats == sorted(occurred_ats, reverse=True), (
            "fetch_page must return strict chronological (most-recent-first) "
            f"order, got: {occurred_ats}"
        )
        assert [item.seq for item in page.items] == [3, 1, 2, 0]

    def test_recency_order_is_consistent_across_repeated_reads(
        self, repo: SqlEpisodicRepository
    ) -> None:
        """Determinism: re-reading the same zone state must never reorder
        entries -- "strict" chronological order excludes any nondeterminism.
        """
        base = datetime(2026, 1, 1, tzinfo=UTC)
        for seq in range(5):
            repo.append(_episode(seq=seq, occurred_at=base + timedelta(hours=seq)))

        first_read = [item.item_id for item in repo.fetch(_zone_query(max_items=10))]
        second_read = [item.item_id for item in repo.fetch(_zone_query(max_items=10))]

        assert first_read == second_read
        assert len(first_read) == 5

    def test_fetch_recency_respects_as_of_temporal_bound(
        self, repo: SqlEpisodicRepository
    ) -> None:
        base = datetime(2026, 1, 1, tzinfo=UTC)
        repo.append(_episode(seq=0, occurred_at=base))
        repo.append(_episode(seq=1, occurred_at=base + timedelta(days=10)))

        as_of = base + timedelta(days=1)
        items = repo.fetch(_zone_query(max_items=10, as_of=as_of))

        assert len(items) == 1
        assert items[0].item_id == Episode.episode_id_for("tenant-1", "session-1", 0)


class TestAC002DecayAttributePerEntryRealBehavior:
    """AC-002 (verbatim): "each entry exposes its own decay attribute" --
    proven against MemoryItem.score (the decay value `fetch()` computes
    per row) and against Episode.decay() directly, over episodes actually
    round-tripped through the repository.
    """

    def test_fetched_items_carry_distinct_decay_scores_by_recency(
        self, repo: SqlEpisodicRepository, fixed_clock: FakeClock
    ) -> None:
        older = fixed_clock.now() - timedelta(days=30)
        newer = fixed_clock.now() - timedelta(days=1)
        repo.append(_episode(seq=0, occurred_at=older))
        repo.append(_episode(seq=1, occurred_at=newer))

        items = repo.fetch(_zone_query(max_items=10))
        by_id = {item.item_id: item.score for item in items}

        newer_score = by_id[Episode.episode_id_for("tenant-1", "session-1", 1)]
        older_score = by_id[Episode.episode_id_for("tenant-1", "session-1", 0)]
        assert 0.0 < older_score < newer_score <= 1.0

    def test_every_fetched_item_has_its_own_independently_computed_score(
        self, repo: SqlEpisodicRepository
    ) -> None:
        base = datetime(2026, 1, 1, tzinfo=UTC)
        for seq in range(4):
            repo.append(_episode(seq=seq, occurred_at=base + timedelta(days=seq)))

        items = repo.fetch(_zone_query(max_items=10, as_of=base + timedelta(days=10)))
        scores = [item.score for item in items]

        assert len(scores) == len(set(scores)), (
            "each entry must expose its OWN decay attribute -- no two "
            f"episodes with different occurred_at may share a score: {scores}"
        )

    def test_fetch_page_items_each_support_independent_decay_computation(
        self, repo: SqlEpisodicRepository
    ) -> None:
        """`fetch_page` returns `Episode` objects (not `MemoryItem`), so
        AC-002's "own decay attribute" is proven by calling `.decay()` on
        each returned episode directly, per HLD's per-entry (not
        per-zone) decay design (episode.py's own decay() docstring).
        """
        base = datetime(2026, 1, 1, tzinfo=UTC)
        repo.append(_episode(seq=0, occurred_at=base))
        repo.append(_episode(seq=1, occurred_at=base + timedelta(days=4)))

        page = repo.fetch_page(tenant_id="tenant-1", session_id="session-1", page_size=10)
        as_of = base + timedelta(days=8)
        decays = [episode.decay(as_of) for episode in page.items]

        assert decays[0] != decays[1]
        assert all(0.0 < value <= 1.0 for value in decays)


class TestAC002DPDP1LocatableRealBehavior:
    """AC-002-DPDP-1 (verbatim, operational definition): "locatable for
    erasure purposes if its item_id (episode_id) resolves to its primary
    key ... via a direct B-tree PK lookup, never a full-zone BRIN range
    scan." Proven here by an instrumented store that tracks full-scan
    calls separately from PK-indexed lookup calls, across a zone large
    enough that a real scan would be observable.
    """

    def test_locate_resolves_the_correct_episode_among_many(
        self, repo: SqlEpisodicRepository
    ) -> None:
        base = datetime(2026, 1, 1, tzinfo=UTC)
        for seq in range(50):
            repo.append(_episode(seq=seq, occurred_at=base + timedelta(hours=seq)))
        target_id = Episode.episode_id_for("tenant-1", "session-1", 37)

        found = repo.locate(tenant_id="tenant-1", episode_id=target_id)

        assert found is not None
        assert found.episode_id == target_id
        assert found.seq == 37

    def test_locate_cost_is_independent_of_zone_size_never_a_full_scan(
        self, repo: SqlEpisodicRepository, connection: FakeEpisodicConnection
    ) -> None:
        """The operational core of AC-002-DPDP-1: growing the zone from 1
        entry to 200 entries must not change `locate`'s access pattern --
        it stays a single PK-indexed lookup, never a scan proportional to
        `len(rows)`. This is what "never a full-zone BRIN range scan"
        means in behavioral, not just textual, terms.
        """
        base = datetime(2026, 1, 1, tzinfo=UTC)
        for seq in range(200):
            repo.append(_episode(seq=seq, occurred_at=base + timedelta(minutes=seq)))
        assert connection.store.full_scan_calls == 0, (
            "append() must never trigger the full-scan (recency/pagination) path"
        )

        target_id = Episode.episode_id_for("tenant-1", "session-1", 199)
        repo.locate(tenant_id="tenant-1", episode_id=target_id)

        assert connection.store.pk_lookup_calls == 1
        assert connection.store.full_scan_calls == 0, (
            "locate() must resolve via the PK index only -- a full-zone "
            "scan occurring alongside a locate() call violates "
            "AC-002-DPDP-1's operational 'locatable' definition"
        )

    def test_locate_and_recency_fetch_use_structurally_disjoint_access_paths(
        self, repo: SqlEpisodicRepository, connection: FakeEpisodicConnection
    ) -> None:
        """Contrast case: the recency path (`fetch`) DOES scan (that is
        BRIN's correct, intended access pattern per HLD Section 5), while
        `locate` never does -- the two paths must remain observably
        distinct, not merge into one code path over time.
        """
        base = datetime(2026, 1, 1, tzinfo=UTC)
        for seq in range(10):
            repo.append(_episode(seq=seq, occurred_at=base + timedelta(days=seq)))

        repo.fetch(_zone_query(max_items=5))
        assert connection.store.full_scan_calls == 1
        assert connection.store.pk_lookup_calls == 0

        repo.locate(
            tenant_id="tenant-1",
            episode_id=Episode.episode_id_for("tenant-1", "session-1", 3),
        )
        assert connection.store.pk_lookup_calls == 1
        assert connection.store.full_scan_calls == 1, (
            "locate() must not add to the full-scan counter"
        )

    def test_locate_returns_none_for_an_episode_id_that_was_never_appended(
        self, repo: SqlEpisodicRepository
    ) -> None:
        unseen_id = Episode.episode_id_for("tenant-1", "session-1", 999)
        assert repo.locate(tenant_id="tenant-1", episode_id=unseen_id) is None

    def test_locate_is_satisfiable_for_an_entry_written_via_append(
        self, repo: SqlEpisodicRepository
    ) -> None:
        """"Locatable and satisfiable for a stored entry" (AC-002-DPDP-1):
        an episode written through the real `append()` write path is then
        resolvable through the real `locate()` read path end-to-end.
        """
        episode = _episode(seq=7, occurred_at=datetime(2026, 3, 1, tzinfo=UTC))
        repo.append(episode)

        found = repo.locate(tenant_id="tenant-1", episode_id=episode.episode_id)

        assert found is not None
        assert found.tenant_id == episode.tenant_id
        assert found.session_id == episode.session_id
        assert found.seq == episode.seq
        assert found.occurred_at == episode.occurred_at


class TestMustNotDeviateRealBehavior:
    """Real (not only structural) proof of AR1-003's binding items, over
    a full multi-page pagination flow and a genuinely growing zone.
    """

    def test_keyset_pagination_threads_correctly_across_three_real_pages(
        self, repo: SqlEpisodicRepository
    ) -> None:
        base = datetime(2026, 1, 1, tzinfo=UTC)
        for seq in range(7):
            repo.append(_episode(seq=seq, occurred_at=base + timedelta(days=seq)))

        seen_seqs: list[int] = []
        cursor: EpisodicCursor | None = None
        pages_fetched = 0
        while True:
            page = repo.fetch_page(
                tenant_id="tenant-1", session_id="session-1", page_size=3, cursor=cursor
            )
            seen_seqs.extend(episode.seq for episode in page.items)
            pages_fetched += 1
            if not page.has_more:
                break
            cursor = page.next_cursor
            assert cursor is not None
            assert pages_fetched <= 5, "pagination did not terminate as expected"

        assert seen_seqs == [6, 5, 4, 3, 2, 1, 0]
        assert pages_fetched == 3

    def test_append_only_the_zone_never_shrinks_or_mutates_across_reads(
        self, repo: SqlEpisodicRepository, connection: FakeEpisodicConnection
    ) -> None:
        for seq in range(5):
            repo.append(_episode(seq=seq))
        before = list(connection.store.rows)

        repo.fetch(_zone_query(max_items=100))
        repo.fetch_page(tenant_id="tenant-1", session_id="session-1", page_size=100)
        repo.locate(
            tenant_id="tenant-1", episode_id=Episode.episode_id_for("tenant-1", "session-1", 2)
        )

        assert connection.store.rows == before
        assert len(connection.store.rows) == 5

    def test_tenant_isolation_holds_under_a_real_mixed_tenant_store(
        self, repo: SqlEpisodicRepository
    ) -> None:
        base = datetime(2026, 1, 1, tzinfo=UTC)
        repo.append(
            Episode(
                tenant_id="tenant-A",
                session_id="session-1",
                episode_id=Episode.episode_id_for("tenant-A", "session-1", 0),
                seq=0,
                occurred_at=base,
                written_at=base,
                payload=_PLACEHOLDER_PAYLOAD,
                actors=(),
                token_count=1,
                state=EpisodeState.ACTIVE,
            )
        )
        repo.append(
            Episode(
                tenant_id="tenant-B",
                session_id="session-1",
                episode_id=Episode.episode_id_for("tenant-B", "session-1", 0),
                seq=0,
                occurred_at=base,
                written_at=base,
                payload=_PLACEHOLDER_PAYLOAD,
                actors=(),
                token_count=1,
                state=EpisodeState.ACTIVE,
            )
        )

        items_a = repo.fetch(_zone_query(tenant_id="tenant-A", max_items=100))
        items_b = repo.fetch(_zone_query(tenant_id="tenant-B", max_items=100))

        assert len(items_a) == 1
        assert len(items_b) == 1
        assert items_a[0].item_id != items_b[0].item_id


class TestArchitectureFitnessScopedToStory:
    """HLD Section 3.0 invariant 1, scoped to this story's own two new
    domain modules: neither `domain/episode.py` nor
    `domain/episodic_query.py` may import from `dashanan.infrastructure`.
    (The repo-wide sweep already lives in test_memory_orchestrator.py;
    this is the QA subtask's own independent, story-scoped proof,
    matching DASH-STORY-002's precedent in test_qa_working_memory_zone1.py.)
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

    @pytest.mark.parametrize("source_path", [EPISODE_FILE, EPISODIC_QUERY_FILE])
    def test_domain_module_exists(self, source_path: Path) -> None:
        assert source_path.is_file(), f"{source_path} not found"

    @pytest.mark.parametrize("source_path", [EPISODE_FILE, EPISODIC_QUERY_FILE])
    def test_domain_module_does_not_import_infrastructure(self, source_path: Path) -> None:
        imported = self._imported_module_names(source_path)
        bad = [
            name
            for name in imported
            if name == "dashanan.infrastructure"
            or name.startswith("dashanan.infrastructure.")
        ]
        assert not bad, (
            "HLD 3.0 invariant 1 violated -- "
            f"{source_path.name} imports infrastructure: {bad}"
        )

    def test_episode_is_a_frozen_immutable_value_object(self) -> None:
        import dataclasses

        assert dataclasses.is_dataclass(Episode)
        params = getattr(Episode, "__dataclass_params__")
        assert params.frozen is True


class TestBoundaryAndAdversarialMatrix:
    """The boundary/adversarial coverage the dev smoke suite does not
    already exercise, per this story's own AR1-003/QA-split convention.
    """

    def test_fetch_returns_empty_list_for_a_zone_with_no_episodes(
        self, repo: SqlEpisodicRepository
    ) -> None:
        items = repo.fetch(_zone_query(max_items=10))
        assert items == []

    def test_fetch_page_returns_empty_page_for_a_session_with_no_episodes(
        self, repo: SqlEpisodicRepository
    ) -> None:
        page = repo.fetch_page(tenant_id="tenant-1", session_id="session-1", page_size=10)
        assert page.items == []
        assert page.has_more is False
        assert page.next_cursor is None

    def test_fetch_respects_max_items_even_when_more_episodes_exist(
        self, repo: SqlEpisodicRepository
    ) -> None:
        base = datetime(2026, 1, 1, tzinfo=UTC)
        for seq in range(10):
            repo.append(_episode(seq=seq, occurred_at=base + timedelta(days=seq)))

        items = repo.fetch(_zone_query(max_items=3))

        assert len(items) == 3

    def test_fetch_page_rejects_blank_session_id(self, repo: SqlEpisodicRepository) -> None:
        with pytest.raises(ValueError, match="session_id"):
            repo.fetch_page(tenant_id="tenant-1", session_id="   ", page_size=10)

    def test_locate_rejects_a_malformed_episode_id_before_querying(
        self, repo: SqlEpisodicRepository, connection: FakeEpisodicConnection
    ) -> None:
        with pytest.raises(ValueError, match="not a valid"):
            repo.locate(tenant_id="tenant-1", episode_id="garbage")
        assert connection.cursor_obj.executed == []

    def test_append_persists_score_terms_round_trip_through_json_encoding(
        self, repo: SqlEpisodicRepository
    ) -> None:
        episode = Episode(
            tenant_id="tenant-1",
            session_id="session-1",
            episode_id=Episode.episode_id_for("tenant-1", "session-1", 0),
            seq=0,
            occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
            written_at=datetime(2026, 1, 1, tzinfo=UTC),
            payload=_PLACEHOLDER_PAYLOAD,
            actors=("user",),
            token_count=5,
            state=EpisodeState.ACTIVE,
            score_terms={"recency": 0.9, "relevance": 0.3},
        )
        repo.append(episode)

        found = repo.locate(tenant_id="tenant-1", episode_id=episode.episode_id)

        assert found is not None
        assert found.score_terms == {"recency": 0.9, "relevance": 0.3}

    def test_state_machine_value_round_trips_through_the_store(
        self, repo: SqlEpisodicRepository
    ) -> None:
        episode = _episode(seq=0, state=EpisodeState.COMPRESSED)
        repo.append(episode)

        found = repo.locate(tenant_id="tenant-1", episode_id=episode.episode_id)

        assert found is not None
        assert found.state is EpisodeState.COMPRESSED

    def test_two_episodes_at_the_identical_occurred_at_still_order_deterministically(
        self, repo: SqlEpisodicRepository
    ) -> None:
        """Tie-break coverage: (occurred_at, seq) as the keyset ordering
        key (episodic_query.py's own docstring) must resolve ties, not
        leave order ambiguous, when two entries share one instant.
        """
        same_instant = datetime(2026, 1, 5, tzinfo=UTC)
        repo.append(_episode(seq=0, occurred_at=same_instant))
        repo.append(_episode(seq=1, occurred_at=same_instant))

        page = repo.fetch_page(tenant_id="tenant-1", session_id="session-1", page_size=10)

        assert [episode.seq for episode in page.items] == [1, 0]


class TestJsonSerializationDidNotLeakPiiPlaceholderCorruption:
    """Sanity check that the DPDP-mandated `<PII_EXAMPLE_REDACTED>`
    placeholder used throughout this suite survives the JSON round-trip
    `append`/`locate` perform on `score_terms` without corruption -- a
    minimal, non-PII-bearing proof that the write/read path is faithful.
    """

    def test_payload_placeholder_is_preserved_exactly(
        self, repo: SqlEpisodicRepository
    ) -> None:
        episode = _episode(seq=0)
        repo.append(episode)

        found = repo.locate(tenant_id="tenant-1", episode_id=episode.episode_id)

        assert found is not None
        assert found.payload == _PLACEHOLDER_PAYLOAD
        assert json.dumps(found.payload)  # never raises: confirms plain JSON-safe string
