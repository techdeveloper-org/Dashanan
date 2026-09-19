"""Smoke assertions for DASH-STORY-003, inline per the dev subtask scope.

The formal pytest suite covering every AC (AC-002, AC-002-DPDP-1) is the QA
subtask's responsibility (see the story's `qa_prompt`). These checks only
confirm the package is importable, wired correctly, and that the
must-not-deviate structural properties (keyset-only, PK-only locate,
append-only, tenant_id-required) hold, ahead of that formal suite landing --
the exact scope split `tests/test_smoke.py` used for DASH-STORY-001.

Runtime assumptions recorded per rule 33/40 test-roadmap conventions:
  - clock: FakeClock, fixed at 2026-01-01T00:00:00+00:00 unless a test
    states otherwise -- deterministic, no wall-clock dependency.
  - tenant_id: "tenant-1" for all requests unless a test states otherwise.
  - No fixture seed is required; nothing in this suite is randomized.

PII NOTE: per the dev_prompt's PII constraint, no example conversational
content appears anywhere below -- payload fixtures use the literal
`<PII_EXAMPLE_REDACTED>` placeholder.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from dashanan.domain.episode import EPISODIC_LAMBDA_ZONE, Episode, EpisodeState
from dashanan.domain.episodic_query import EpisodicCursor
from dashanan.domain.exceptions import ZoneRepositoryError
from dashanan.domain.ports import ZoneQuery
from dashanan.infrastructure.sql_episodic_repository import (
    _FETCH_PAGE_SQL_NO_CURSOR,
    _FETCH_PAGE_SQL_WITH_CURSOR,
    _FETCH_RECENCY_SQL,
    _LOCATE_BY_PK_SQL,
    SqlEpisodicRepository,
)

_PLACEHOLDER_PAYLOAD = "<PII_EXAMPLE_REDACTED>"


class FakeClock:
    """Deterministic Clock double: fixed instant, injectable (testing-core DI)."""

    def __init__(self, fixed: datetime) -> None:
        self._fixed = fixed

    def now(self) -> datetime:
        return self._fixed


class RecordingCursor:
    """DB-API cursor double: records every execute() call, returns canned rows."""

    def __init__(self, rows: list[tuple[object, ...]] | None = None) -> None:
        self.executed: list[tuple[str, tuple[object, ...]]] = []
        self._rows = rows or []
        self._raise: Exception | None = None

    def execute(self, sql: str, params: Sequence[object]) -> None:
        self.executed.append((sql, tuple(params)))
        if self._raise is not None:
            raise self._raise

    def fetchall(self) -> list[tuple[object, ...]]:
        return list(self._rows)


class RecordingConnection:
    """DB-API connection double exposing one shared RecordingCursor."""

    def __init__(self, rows: list[tuple[object, ...]] | None = None) -> None:
        self.cursor_obj = RecordingCursor(rows)

    def cursor(self) -> RecordingCursor:
        return self.cursor_obj


def _episode(
    tenant_id: str = "tenant-1",
    session_id: str = "session-1",
    seq: int = 0,
    occurred_at: datetime | None = None,
    written_at: datetime | None = None,
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
        token_count=10,
        state=EpisodeState.ACTIVE,
        score_terms={},
    )


def _row_for(episode: Episode) -> tuple[object, ...]:
    return (
        episode.tenant_id,
        episode.session_id,
        episode.episode_id,
        episode.seq,
        episode.occurred_at,
        episode.written_at,
        episode.payload,
        list(episode.actors),
        episode.token_count,
        episode.state.value,
        dict(episode.score_terms),
    )


@pytest.fixture
def fixed_clock() -> FakeClock:
    return FakeClock(datetime(2026, 1, 1, tzinfo=UTC))


class TestPackageWiring:
    """Baseline: the adapter constructs and is importable, before any AC-level suite."""

    def test_repository_constructs_with_fake_connection_and_clock(
        self, fixed_clock: FakeClock
    ) -> None:
        repo = SqlEpisodicRepository(RecordingConnection(), fixed_clock, verify_privileges=False)
        assert repo is not None


class TestAC002DecayAttribute:
    """AC-002: each entry exposes its own decay attribute."""

    def test_decay_is_one_at_zero_elapsed_time(self) -> None:
        occurred = datetime(2026, 1, 1, tzinfo=UTC)
        episode = _episode(occurred_at=occurred)
        assert episode.decay(occurred) == pytest.approx(1.0)

    def test_decay_matches_documented_half_life(self) -> None:
        """HLD Section 12A: t_half = 2 days -- decay at exactly t_half must be ~0.5."""
        occurred = datetime(2026, 1, 1, tzinfo=UTC)
        episode = _episode(occurred_at=occurred)
        two_days_later = occurred + timedelta(days=2)

        assert episode.decay(two_days_later) == pytest.approx(0.5, rel=1e-3)

    def test_decay_uses_the_hld_cited_lambda_constant(self) -> None:
        assert EPISODIC_LAMBDA_ZONE == pytest.approx(4.01e-6, rel=1e-3)

    def test_decay_clamps_negative_elapsed_time_to_one(self) -> None:
        """Clock skew: as_of before occurred_at must not exceed decay=1.0."""
        occurred = datetime(2026, 1, 2, tzinfo=UTC)
        episode = _episode(occurred_at=occurred)
        before_occurred = occurred - timedelta(hours=1)

        assert episode.decay(before_occurred) == pytest.approx(1.0)

    def test_each_entry_computes_decay_independently_of_the_others(self) -> None:
        as_of = datetime(2026, 1, 5, tzinfo=UTC)
        older = _episode(seq=0, occurred_at=datetime(2026, 1, 1, tzinfo=UTC))
        newer = _episode(seq=1, occurred_at=datetime(2026, 1, 4, tzinfo=UTC))

        assert older.decay(as_of) < newer.decay(as_of)


class TestMustNotDeviateTenantIdRequired:
    """Must-not-deviate item 5: tenant_id present in every query, no exceptions."""

    def test_fetch_rejects_blank_tenant_id_before_querying(
        self, fixed_clock: FakeClock
    ) -> None:
        connection = RecordingConnection()
        repo = SqlEpisodicRepository(connection, fixed_clock, verify_privileges=False)
        query = ZoneQuery(
            tenant_id="",
            task="task",
            query_embedding=None,
            max_items=10,
            min_provenance_conf=0.0,
        )

        with pytest.raises(ValueError, match="tenant_id"):
            repo.fetch(query)
        assert connection.cursor_obj.executed == [], "must not query without tenant_id"

    def test_fetch_page_rejects_blank_tenant_id_before_querying(
        self, fixed_clock: FakeClock
    ) -> None:
        connection = RecordingConnection()
        repo = SqlEpisodicRepository(connection, fixed_clock, verify_privileges=False)

        with pytest.raises(ValueError, match="tenant_id"):
            repo.fetch_page(tenant_id="", session_id="session-1", page_size=10)
        assert connection.cursor_obj.executed == []

    def test_locate_rejects_blank_tenant_id_before_querying(
        self, fixed_clock: FakeClock
    ) -> None:
        connection = RecordingConnection()
        repo = SqlEpisodicRepository(connection, fixed_clock, verify_privileges=False)
        episode_id = Episode.episode_id_for("tenant-1", "session-1", 0)

        with pytest.raises(ValueError, match="tenant_id"):
            repo.locate(tenant_id="", episode_id=episode_id)
        assert connection.cursor_obj.executed == []


class TestMustNotDeviateKeysetNeverOffset:
    """Must-not-deviate item 2: keyset pagination, never OFFSET."""

    def test_no_repository_query_contains_offset(self) -> None:
        for sql in (
            _FETCH_RECENCY_SQL,
            _FETCH_PAGE_SQL_NO_CURSOR,
            _FETCH_PAGE_SQL_WITH_CURSOR,
            _LOCATE_BY_PK_SQL,
        ):
            assert "OFFSET" not in sql.upper()

    def test_second_page_query_uses_a_keyset_predicate_not_a_counted_skip(self) -> None:
        assert "< (%s, %s)" in _FETCH_PAGE_SQL_WITH_CURSOR
        assert "< (%s, %s)" not in _FETCH_PAGE_SQL_NO_CURSOR

    def test_fetch_page_first_page_has_no_cursor(
        self, fixed_clock: FakeClock
    ) -> None:
        episode = _episode(seq=0)
        connection = RecordingConnection(rows=[_row_for(episode)])
        repo = SqlEpisodicRepository(connection, fixed_clock, verify_privileges=False)

        page = repo.fetch_page(tenant_id="tenant-1", session_id="session-1", page_size=10)

        sql_used, params_used = connection.cursor_obj.executed[0]
        assert sql_used == _FETCH_PAGE_SQL_NO_CURSOR
        assert params_used == ("tenant-1", "session-1", 11)  # page_size + 1 over-fetch
        assert page.items[0].episode_id == episode.episode_id

    def test_fetch_page_with_cursor_uses_keyset_predicate_values(
        self, fixed_clock: FakeClock
    ) -> None:
        connection = RecordingConnection(rows=[])
        repo = SqlEpisodicRepository(connection, fixed_clock, verify_privileges=False)
        cursor = EpisodicCursor(
            after_occurred_at=datetime(2026, 1, 3, tzinfo=UTC), after_seq=7
        )

        repo.fetch_page(
            tenant_id="tenant-1", session_id="session-1", page_size=5, cursor=cursor
        )

        sql_used, params_used = connection.cursor_obj.executed[0]
        assert sql_used == _FETCH_PAGE_SQL_WITH_CURSOR
        assert params_used == (
            "tenant-1",
            "session-1",
            cursor.after_occurred_at,
            cursor.after_seq,
            6,
        )

    def test_fetch_page_reports_has_more_when_rows_exceed_page_size(
        self, fixed_clock: FakeClock
    ) -> None:
        rows = [
            _row_for(_episode(seq=i, occurred_at=datetime(2026, 1, 1 + i, tzinfo=UTC)))
            for i in range(3)
        ]  # page_size=2 requested, 3 rows returned (over-fetch signal)
        connection = RecordingConnection(rows=rows)
        repo = SqlEpisodicRepository(connection, fixed_clock, verify_privileges=False)

        page = repo.fetch_page(tenant_id="tenant-1", session_id="session-1", page_size=2)

        assert len(page.items) == 2
        assert page.has_more is True
        assert page.next_cursor is not None
        assert page.next_cursor.after_seq == page.items[-1].seq

    def test_fetch_page_reports_no_more_when_rows_fit_within_page_size(
        self, fixed_clock: FakeClock
    ) -> None:
        rows = [_row_for(_episode(seq=0))]
        connection = RecordingConnection(rows=rows)
        repo = SqlEpisodicRepository(connection, fixed_clock, verify_privileges=False)

        page = repo.fetch_page(tenant_id="tenant-1", session_id="session-1", page_size=5)

        assert len(page.items) == 1
        assert page.has_more is False
        assert page.next_cursor is None


class TestAC002DPDP1LocatableByPkOnly:
    """AC-002-DPDP-1: locatable via direct B-tree PK lookup, never a BRIN range scan."""

    def test_locate_query_where_clause_never_references_occurred_at(self) -> None:
        """The SELECT list carries occurred_at (Episode needs it); the WHERE
        predicate -- the part that decides which index Postgres can use --
        must not, so this can never become a BRIN range scan."""
        where_clause = _LOCATE_BY_PK_SQL.split("WHERE", 1)[1]
        assert "occurred_at" not in where_clause

    def test_locate_query_filters_on_exactly_the_pk_columns(self) -> None:
        assert "tenant_id = %s AND session_id = %s AND seq = %s" in _LOCATE_BY_PK_SQL

    def test_episode_id_round_trips_through_parse(self) -> None:
        episode_id = Episode.episode_id_for("tenant-1", "session-1", 42)
        assert Episode.parse_episode_id(episode_id) == ("tenant-1", "session-1", 42)

    def test_locate_resolves_episode_id_to_a_single_pk_equality_query(
        self, fixed_clock: FakeClock
    ) -> None:
        episode = _episode(seq=3)
        connection = RecordingConnection(rows=[_row_for(episode)])
        repo = SqlEpisodicRepository(connection, fixed_clock, verify_privileges=False)

        found = repo.locate(tenant_id="tenant-1", episode_id=episode.episode_id)

        sql_used, params_used = connection.cursor_obj.executed[0]
        assert sql_used == _LOCATE_BY_PK_SQL
        assert params_used == ("tenant-1", "session-1", 3)
        assert found is not None
        assert found.episode_id == episode.episode_id

    def test_locate_returns_none_when_no_row_matches(
        self, fixed_clock: FakeClock
    ) -> None:
        connection = RecordingConnection(rows=[])
        repo = SqlEpisodicRepository(connection, fixed_clock, verify_privileges=False)
        episode_id = Episode.episode_id_for("tenant-1", "session-1", 99)

        assert repo.locate(tenant_id="tenant-1", episode_id=episode_id) is None

    def test_locate_rejects_tenant_id_mismatched_with_parsed_episode_id(
        self, fixed_clock: FakeClock
    ) -> None:
        connection = RecordingConnection()
        repo = SqlEpisodicRepository(connection, fixed_clock, verify_privileges=False)
        other_tenants_episode_id = Episode.episode_id_for(
            "tenant-OTHER", "session-1", 0
        )

        with pytest.raises(ValueError, match="does not belong to tenant"):
            repo.locate(tenant_id="tenant-1", episode_id=other_tenants_episode_id)
        assert connection.cursor_obj.executed == []


class TestMustNotDeviateAppendOnly:
    """Must-not-deviate item 4: append-only."""

    def test_repository_exposes_no_update_or_delete_method(
        self, fixed_clock: FakeClock
    ) -> None:
        repo = SqlEpisodicRepository(RecordingConnection(), fixed_clock, verify_privileges=False)
        assert not hasattr(repo, "update")
        assert not hasattr(repo, "delete")

    def test_no_repository_query_string_contains_update_or_delete(self) -> None:
        for sql in (
            _FETCH_RECENCY_SQL,
            _FETCH_PAGE_SQL_NO_CURSOR,
            _FETCH_PAGE_SQL_WITH_CURSOR,
            _LOCATE_BY_PK_SQL,
        ):
            assert "UPDATE" not in sql.upper()
            assert "DELETE" not in sql.upper()

    def test_append_issues_a_single_insert_statement(
        self, fixed_clock: FakeClock
    ) -> None:
        connection = RecordingConnection()
        repo = SqlEpisodicRepository(connection, fixed_clock, verify_privileges=False)
        episode = _episode(seq=0)

        repo.append(episode)

        assert len(connection.cursor_obj.executed) == 1
        sql_used, params_used = connection.cursor_obj.executed[0]
        assert sql_used.strip().upper().startswith("INSERT INTO EPISODIC_ENTRIES")
        assert params_used[0] == "tenant-1"


class TestBoundaryAndNegativeCases:
    """Boundary/negative cases per rule 33/40 test-roadmap conventions."""

    def test_episode_rejects_written_at_before_occurred_at(self) -> None:
        occurred = datetime(2026, 1, 2, tzinfo=UTC)
        with pytest.raises(ValueError, match="written_at"):
            Episode(
                tenant_id="tenant-1",
                session_id="session-1",
                episode_id=Episode.episode_id_for("tenant-1", "session-1", 0),
                seq=0,
                occurred_at=occurred,
                written_at=occurred - timedelta(seconds=1),
                payload=_PLACEHOLDER_PAYLOAD,
                actors=(),
                token_count=1,
                state=EpisodeState.ACTIVE,
            )

    def test_episode_rejects_negative_seq(self) -> None:
        occurred = datetime(2026, 1, 1, tzinfo=UTC)
        with pytest.raises(ValueError, match="seq"):
            Episode(
                tenant_id="tenant-1",
                session_id="session-1",
                episode_id="tenant-1:session-1:0",
                seq=-1,
                occurred_at=occurred,
                written_at=occurred,
                payload=_PLACEHOLDER_PAYLOAD,
                actors=(),
                token_count=1,
                state=EpisodeState.ACTIVE,
            )

    def test_episode_rejects_zero_token_count(self) -> None:
        occurred = datetime(2026, 1, 1, tzinfo=UTC)
        with pytest.raises(ValueError, match="token_count"):
            Episode(
                tenant_id="tenant-1",
                session_id="session-1",
                episode_id="tenant-1:session-1:0",
                seq=0,
                occurred_at=occurred,
                written_at=occurred,
                payload=_PLACEHOLDER_PAYLOAD,
                actors=(),
                token_count=0,
                state=EpisodeState.ACTIVE,
            )

    def test_parse_episode_id_rejects_malformed_input(self) -> None:
        with pytest.raises(ValueError, match="not a valid"):
            Episode.parse_episode_id("not-enough-segments")

    def test_parse_episode_id_rejects_non_integer_seq(self) -> None:
        with pytest.raises(ValueError, match="non-integer"):
            Episode.parse_episode_id("tenant-1:session-1:not-a-number")

    def test_fetch_page_rejects_zero_page_size(self, fixed_clock: FakeClock) -> None:
        repo = SqlEpisodicRepository(RecordingConnection(), fixed_clock, verify_privileges=False)
        with pytest.raises(ValueError, match="page_size"):
            repo.fetch_page(tenant_id="tenant-1", session_id="session-1", page_size=0)

    def test_fetch_wraps_underlying_failure_as_zone_repository_error(
        self, fixed_clock: FakeClock
    ) -> None:
        connection = RecordingConnection()
        connection.cursor_obj._raise = RuntimeError("connection refused")
        repo = SqlEpisodicRepository(connection, fixed_clock, verify_privileges=False)
        query = ZoneQuery(
            tenant_id="tenant-1",
            task="task",
            query_embedding=None,
            max_items=10,
            min_provenance_conf=0.0,
        )

        with pytest.raises(ZoneRepositoryError):
            repo.fetch(query)
