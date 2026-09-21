"""SqlEpisodicRepository: the Zone 2 ZoneRepository adapter (HLD Section 3.3, FR-002).

Implements the domain's frozen `ZoneRepository` Protocol (AR1-G2) against a
SQL backend shaped by `episodic_schema.sql`. Every method here validates
`tenant_id` before issuing a query (must-not-deviate item 5, AR1-003) and
every query is parameterized (application-security-core: never
string-concatenate user input into SQL).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, cast, runtime_checkable

from dashanan.domain.episode import Episode, EpisodeState
from dashanan.domain.episodic_query import EpisodicCursor, EpisodicPage
from dashanan.domain.exceptions import ZoneRepositoryError
from dashanan.domain.memory_item import MemoryItem
from dashanan.domain.ports import Clock, ZoneQuery
from dashanan.domain.zone import ZoneId
from dashanan.infrastructure.append_only_privilege_guard import (
    verify_append_only_role_is_safe,
)

logger = logging.getLogger(__name__)

_SELECT_COLUMNS = (
    "tenant_id, session_id, episode_id, seq, occurred_at, written_at, "
    "payload, actors, token_count, state, score_terms"
)

_FETCH_RECENCY_SQL = f"""
SELECT {_SELECT_COLUMNS}
FROM episodic_entries
WHERE tenant_id = %s
  AND (%s::timestamptz IS NULL OR occurred_at <= %s)
ORDER BY occurred_at DESC, session_id DESC, seq DESC
LIMIT %s
"""

_FETCH_PAGE_SQL_NO_CURSOR = f"""
SELECT {_SELECT_COLUMNS}
FROM episodic_entries
WHERE tenant_id = %s
  AND session_id = %s
ORDER BY occurred_at DESC, seq DESC
LIMIT %s
"""

_FETCH_PAGE_SQL_WITH_CURSOR = f"""
SELECT {_SELECT_COLUMNS}
FROM episodic_entries
WHERE tenant_id = %s
  AND session_id = %s
  AND (occurred_at, seq) < (%s, %s)
ORDER BY occurred_at DESC, seq DESC
LIMIT %s
"""

# Deliberately the ONLY query in this module that may appear in the same
# statement as occurred_at without an ORDER BY/range clause driving it --
# this predicate is PK equality (tenant_id, session_id, seq), the B-tree
# path AC-002-DPDP-1 requires. It must never gain a BETWEEN/range/ORDER BY
# on occurred_at; test_smoke_episodic.py asserts on this literal string.
_LOCATE_BY_PK_SQL = f"""
SELECT {_SELECT_COLUMNS}
FROM episodic_entries
WHERE tenant_id = %s AND session_id = %s AND seq = %s
"""

_APPEND_SQL = """
INSERT INTO episodic_entries
    (tenant_id, session_id, episode_id, seq, occurred_at, written_at,
     payload, actors, token_count, state, score_terms)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


@runtime_checkable
class SqlCursor(Protocol):
    """The minimal DB-API 2.0 (PEP 249) cursor surface this adapter needs."""

    def execute(self, sql: str, params: Sequence[object]) -> object:
        """Execute a parameterized statement. Must never receive interpolated SQL."""
        ...

    def fetchall(self) -> list[tuple[object, ...]]:
        """Return every row produced by the last `execute` call."""
        ...


@runtime_checkable
class SqlConnection(Protocol):
    """The minimal DB-API 2.0 connection surface this adapter needs.

    Kept local to this infrastructure module rather than added to the
    frozen `dashanan.domain.ports` (AR1-G2) -- it is an adapter
    implementation seam, not a cross-cutting application port. The
    composition root wires a real driver's connection (e.g. psycopg2)
    against Postgres; that wiring is out of this story's scope (no
    database driver dependency is added by DASH-STORY-003).
    """

    def cursor(self) -> SqlCursor:
        """Return a new cursor bound to this connection."""
        ...


class SqlEpisodicRepository:
    """Zone 2's `ZoneRepository` adapter: append-only, keyset-paginated, PK-locatable.

    Composes a `SqlConnection` (the DB driver seam) and a `Clock` (so
    `decay()` has a deterministic `as_of` when the caller does not supply
    one via `ZoneQuery.as_of`, mirroring `MemoryOrchestrator`'s own
    injected-Clock pattern for testability, `testing-core`).
    """

    def __init__(
        self,
        connection: SqlConnection,
        clock: Clock,
        *,
        verify_privileges: bool,
    ) -> None:
        """Bind the adapter to its SQL connection and time source.

        Args:
            connection: The DB-API connection this adapter issues
                parameterized queries against.
            clock: Time source for `fetch()`'s decay computation when
                `ZoneQuery.as_of` is not supplied.
            verify_privileges: When True, immediately checks (via
                `append_only_privilege_guard`) that the connected role
                cannot bypass `episodic_entries`' append-only enforcement
                (SUPERUSER, CREATEROLE, or table ownership), raising
                `ZoneRepositoryError` if it can (DSHN-55 P1 re-review,
                attempt 2 -- see this module's docstring and
                `episodic_schema.sql` for why this is defense-in-depth,
                not the primary control).

                DSHN-60 remediation, attempt 3: this parameter carries NO
                default -- see `SqlProvenanceRepository.__init__`'s
                identical note for why. Attempt 2's `= False` default left
                the DSHN-55 check silently disabled at every real
                construction site in this codebase; a required
                keyword-only argument forces every caller to write
                `verify_privileges=False` explicitly to accept that, a
                visible and code-reviewable choice rather than an
                invisible one. Pass `False` for a test double with no
                `pg_roles`/`pg_class` catalog to query; pass `True` for
                any connection backed by a real Postgres role.
        """
        self._connection = connection
        self._clock = clock
        if verify_privileges:
            cursor = connection.cursor()
            verify_append_only_role_is_safe(
                cursor, "episodic_entries", ZoneId.EPISODIC.value
            )
        else:
            logger.warning(
                "SqlEpisodicRepository constructed with verify_privileges="
                "False -- the DSHN-55 defense-in-depth append-only "
                "privilege check is DISABLED for this connection. Pass "
                "verify_privileges=True at the composition root once a "
                "real production connection is wired."
            )

    def fetch(self, query: ZoneQuery) -> list[MemoryItem]:
        """Satisfy the `ZoneRepository` Protocol: tenant-wide recency read.

        `ZoneQuery` carries no `session_id` (the Orchestrator strips it --
        `domain/ports.py`), so this is Zone 2's cross-session recency
        query: every episode for `query.tenant_id`, most-recent-first,
        optionally bounded by `query.as_of` (HLD Section 7.1: "as_of ...
        temporal query against Episodic"), capped at `query.max_items`.
        This is the BRIN-accelerated path -- HLD Section 5 names exactly
        this shape ("Dominant query is a time range") as BRIN's
        justification.

        Raises:
            dashanan.domain.exceptions.ZoneRepositoryError: If the
                underlying query fails, so `MemoryOrchestrator` can
                degrade this zone rather than propagate (AC-009-SUPP-1).
        """
        self._require_tenant(query.tenant_id)
        as_of = query.as_of or self._clock.now()
        try:
            rows = self._execute(
                _FETCH_RECENCY_SQL,
                (query.tenant_id, as_of, as_of, query.max_items),
            )
        except Exception as exc:  # noqa: BLE001 -- converted to typed domain error below
            raise ZoneRepositoryError(
                zone=ZoneId.EPISODIC.value, reason=str(exc)
            ) from exc

        episodes = [self._row_to_episode(row) for row in rows]
        return [
            MemoryItem(
                item_id=episode.episode_id,
                source_zone=ZoneId.EPISODIC,
                payload=episode.payload,
                token_count=episode.token_count,
                score=episode.decay(as_of),
            )
            for episode in episodes
        ]

    def fetch_page(
        self,
        tenant_id: str,
        session_id: str,
        page_size: int,
        cursor: EpisodicCursor | None = None,
    ) -> EpisodicPage:
        """Keyset-paginate one session's episodes, most-recent-first (AC-002).

        Never uses OFFSET (must-not-deviate item 2): the second and later
        pages are served by the `(occurred_at, seq) < (cursor.after_occurred_at,
        cursor.after_seq)` predicate, an O(1)-per-page index-served
        comparison rather than a counted skip.

        Args:
            tenant_id: Required on every call (must-not-deviate item 5).
            session_id: Restricts the read to one session -- the
                dominant access pattern HLD Section 3.3 names.
            page_size: Maximum episodes to return in this page.
            cursor: `None` for the first page; otherwise the
                `next_cursor` from the previous `EpisodicPage`.

        Raises:
            ValueError: If `tenant_id`, `session_id` is blank, or
                `page_size` is not positive.
            dashanan.domain.exceptions.ZoneRepositoryError: If the
                underlying query fails.
        """
        self._require_tenant(tenant_id)
        if not session_id.strip():
            raise ValueError("fetch_page requires a non-blank session_id")
        if page_size <= 0:
            raise ValueError(f"fetch_page requires page_size > 0, got {page_size}")

        # Over-fetch by one to determine has_more without a second COUNT query.
        limit = page_size + 1
        try:
            if cursor is None:
                rows = self._execute(
                    _FETCH_PAGE_SQL_NO_CURSOR, (tenant_id, session_id, limit)
                )
            else:
                rows = self._execute(
                    _FETCH_PAGE_SQL_WITH_CURSOR,
                    (
                        tenant_id,
                        session_id,
                        cursor.after_occurred_at,
                        cursor.after_seq,
                        limit,
                    ),
                )
        except Exception as exc:  # noqa: BLE001 -- converted to typed domain error below
            raise ZoneRepositoryError(
                zone=ZoneId.EPISODIC.value, reason=str(exc)
            ) from exc

        episodes = [self._row_to_episode(row) for row in rows]
        has_more = len(episodes) > page_size
        page_items = episodes[:page_size]
        next_cursor = (
            EpisodicCursor(
                after_occurred_at=page_items[-1].occurred_at,
                after_seq=page_items[-1].seq,
            )
            if has_more and page_items
            else None
        )
        return EpisodicPage(
            items=page_items, next_cursor=next_cursor, has_more=has_more
        )

    def locate(self, tenant_id: str, episode_id: str) -> Episode | None:
        """Resolve `episode_id` to its row via a direct B-tree PK lookup only.

        This is AC-002-DPDP-1's "locatable" operational definition made
        concrete: `episode_id` is parsed into the PK triple
        (`Episode.parse_episode_id`, pure string parsing) and the ensuing
        query filters on `(tenant_id, session_id, seq)` equality --
        `_LOCATE_BY_PK_SQL` never references `occurred_at`, so it cannot
        become a BRIN range scan even accidentally.

        Args:
            tenant_id: Required and cross-checked against the parsed
                `episode_id` -- a caller cannot locate another tenant's
                episode by supplying a mismatched `tenant_id`.
            episode_id: The public identifier to resolve.

        Returns:
            The `Episode` if found, else `None`.

        Raises:
            ValueError: If `tenant_id` is blank, `episode_id` cannot be
                parsed, or the parsed `tenant_id` does not match the
                supplied `tenant_id`.
            dashanan.domain.exceptions.ZoneRepositoryError: If the
                underlying query fails.
        """
        self._require_tenant(tenant_id)
        parsed_tenant_id, session_id, seq = Episode.parse_episode_id(episode_id)
        if parsed_tenant_id != tenant_id:
            raise ValueError(
                f"episode_id {episode_id!r} does not belong to tenant "
                f"{tenant_id!r} (parsed tenant {parsed_tenant_id!r})"
            )

        try:
            rows = self._execute(_LOCATE_BY_PK_SQL, (tenant_id, session_id, seq))
        except Exception as exc:  # noqa: BLE001 -- converted to typed domain error below
            raise ZoneRepositoryError(
                zone=ZoneId.EPISODIC.value, reason=str(exc)
            ) from exc

        if not rows:
            return None
        return self._row_to_episode(rows[0])

    def append(self, episode: Episode) -> None:
        """Insert one episode. The only write this adapter exposes (append-only, item 4).

        There is deliberately no `update`/`delete` method on this class --
        the append-only invariant is a structural property of the API
        surface, not just a database grant (`episodic_schema.sql`'s
        `REVOKE UPDATE, DELETE`).

        Raises:
            dashanan.domain.exceptions.ZoneRepositoryError: If the insert
                fails (e.g. a PK or unique-constraint violation).
        """
        self._require_tenant(episode.tenant_id)
        try:
            # _APPEND_SQL is a plain INSERT with no RETURNING clause, so
            # (unlike this class's SELECT-issuing methods) it must not go
            # through the shared _execute() helper -- that helper's
            # cursor.fetchall() call raises psycopg.ProgrammingError
            # against a real driver when there is no result set to fetch.
            # The connection is opened with autocommit=False
            # (composition_root.build_postgres_connection), so the write
            # is explicitly committed here, mirroring
            # SqlManifestRepository.insert_batch's own commit-after-insert
            # precedent. This module's own `SqlConnection` Protocol
            # (unlike SqlManifestRepository's) does not declare `commit`,
            # and several existing tests pass a minimal double that
            # implements only `cursor()` -- `commit` is called only when
            # the connection actually exposes it, so a real driver
            # connection is durably committed while those doubles are
            # unaffected.
            cursor = self._connection.cursor()
            cursor.execute(
                _APPEND_SQL,
                (
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
                    json.dumps(dict(episode.score_terms)),
                ),
            )
            commit = getattr(self._connection, "commit", None)
            if commit is not None:
                commit()
        except Exception as exc:  # noqa: BLE001 -- converted to typed domain error below
            raise ZoneRepositoryError(
                zone=ZoneId.EPISODIC.value, reason=str(exc)
            ) from exc

    def _require_tenant(self, tenant_id: str) -> None:
        """Enforce must-not-deviate item 5: tenant_id present in every query, no exceptions."""
        if not tenant_id.strip():
            raise ValueError("tenant_id is required for every Episodic zone query")

    def _execute(self, sql: str, params: Sequence[object]) -> list[tuple[object, ...]]:
        """Run one parameterized statement and return every row."""
        cursor = self._connection.cursor()
        cursor.execute(sql, params)
        return cursor.fetchall()

    def _row_to_episode(self, row: tuple[object, ...]) -> Episode:
        """Map one result row back into the domain `Episode` shape.

        Every field is explicitly `cast` from the DB-API boundary's opaque
        `object` type -- `_SELECT_COLUMNS`' fixed column order is this
        method's only contract with the driver, so the casts document
        that contract rather than silencing a real type hazard.
        """
        (
            tenant_id,
            session_id,
            episode_id,
            seq,
            occurred_at,
            written_at,
            payload,
            actors,
            token_count,
            state,
            score_terms,
        ) = row
        return Episode(
            tenant_id=cast(str, tenant_id),
            session_id=cast(str, session_id),
            episode_id=cast(str, episode_id),
            seq=cast(int, seq),
            occurred_at=self._as_datetime(occurred_at),
            written_at=self._as_datetime(written_at),
            payload=cast(str, payload),
            actors=tuple(cast(Sequence[str], actors)) if actors is not None else (),
            token_count=cast(int, token_count),
            state=EpisodeState(cast(str, state)),
            score_terms=self._as_score_terms(score_terms),
        )

    @staticmethod
    def _as_datetime(value: object) -> datetime:
        """Accept either a driver-native `datetime` or an ISO-8601 string."""
        if isinstance(value, datetime):
            return value
        return datetime.fromisoformat(str(value))

    @staticmethod
    def _as_score_terms(value: object) -> dict[str, float]:
        """Accept either a driver-native dict (JSONB auto-decode) or a JSON string."""
        if isinstance(value, dict):
            return value
        if value is None:
            return {}
        return dict(json.loads(str(value)))
