"""SqlSemanticRepository: the Zone 3 storage adapter (HLD Section 3.4, FR-003).

Zone 3 does NOT implement the generic `ZoneRepository` Protocol
(`domain/ports.py`, frozen per AR1-G2) -- mirroring `SqlProvenanceRepository`'s
identical choice for Zone 7 and for the same class of reason: `MemoryItem`
(DASH-STORY-001) requires a positive `token_count` on every candidate, and
neither `SemanticEdge` nor `GeneralFact` carries a `token_count` field in
HLD Section 3.4's literal owned-entity shape -- inventing one here would be
this schema-focused story assigning a value HLD never specifies, rather
than a real requirement. This adapter's own read shapes -- "edges touching
one subject_ref," "edges touching one object_ref" (HLD Section 5's
bounded-depth-BFS access pattern) and "one fact by fact_id" -- are
therefore bespoke methods, the same pattern `SqlProvenanceRepository.
find_by_item_id` already established for a query shape outside
`ZoneRepository`. See this story's dev report, judgment-call list, for the
full rationale and the boundary this leaves for a future context-assembly
integration story.

Every method validates `tenant_id` before issuing a query (HLD 3.0
invariant 2) and every query is parameterized (application-security-core:
never string-concatenate user input into SQL). Unlike
`SqlEpisodicRepository` / `SqlProvenanceRepository`, this adapter has no
`verify_privileges` constructor parameter: `append_only_privilege_guard`
checks specifically for append-only bypass (owner/SUPERUSER/CREATEROLE
disabling a REVOKE UPDATE/DELETE trigger), and `semantic_schema.sql`
deliberately has no such trigger -- Zone 3's tables are ordinarily
mutable (state transitions, score_terms updates), so that guard's
precondition does not hold here.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Protocol, cast, runtime_checkable

from dashanan.domain.exceptions import ZoneRepositoryError
from dashanan.domain.semantic_memory import GeneralFact, SemanticEdge, SemanticState
from dashanan.domain.zone import ZoneId

logger = logging.getLogger(__name__)

_EDGE_COLUMNS = (
    "tenant_id, edge_id, subject_ref, predicate, object_ref, qualifiers, "
    "state, score_terms"
)
_FACT_COLUMNS = "tenant_id, fact_id, statement, subject_scope, state, score_terms"

_FIND_EDGES_BY_SUBJECT_SQL = f"""
SELECT {_EDGE_COLUMNS}
FROM semantic_edges
WHERE tenant_id = %s AND subject_ref = %s
ORDER BY edge_id ASC
"""

_FIND_EDGES_BY_OBJECT_SQL = f"""
SELECT {_EDGE_COLUMNS}
FROM semantic_edges
WHERE tenant_id = %s AND object_ref = %s
ORDER BY edge_id ASC
"""

_FIND_FACT_BY_ID_SQL = f"""
SELECT {_FACT_COLUMNS}
FROM general_facts
WHERE tenant_id = %s AND fact_id = %s
"""

_DELETE_EDGES_BY_SUBJECT_SQL = """
DELETE FROM semantic_edges
WHERE tenant_id = %s AND subject_ref = %s
RETURNING edge_id
"""

_INSERT_EDGE_SQL = """
INSERT INTO semantic_edges
    (tenant_id, edge_id, subject_ref, predicate, object_ref, qualifiers, state, score_terms)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
"""

_INSERT_FACT_SQL = """
INSERT INTO general_facts
    (tenant_id, fact_id, statement, subject_scope, state, score_terms)
VALUES (%s, %s, %s, %s, %s, %s)
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
    implementation seam, not a cross-cutting application port, mirroring
    `SqlEpisodicRepository`'s and `SqlProvenanceRepository`'s identical
    local-Protocol choice. The composition root wires a real driver's
    connection (e.g. psycopg2) against Postgres; that wiring is out of
    this story's scope (no database driver dependency is added by
    DASH-STORY-012).
    """

    def cursor(self) -> SqlCursor:
        """Return a new cursor bound to this connection."""
        ...


class SqlSemanticRepository:
    """Zone 3's storage adapter over `semantic_edges` and `general_facts`.

    Exposes exactly the read/write shapes `semantic_schema.sql`'s indexes
    serve directly: subject-scoped and object-scoped edge lookups (the
    two-sided B-tree, HLD Section 5), single-fact lookup by `fact_id`, and
    one insert per owned entity type. There is no `update`/`delete` method
    on this class yet -- state-machine transitions and score-term
    recomputation are the Rotation Engine's and Scoring Service's own
    write paths (out of this schema-focused story's scope, see the dev
    report's judgment-call list), not this adapter's.
    """

    def __init__(self, connection: SqlConnection) -> None:
        """Bind the adapter to its SQL connection.

        Args:
            connection: The DB-API connection this adapter issues
                parameterized queries against.
        """
        self._connection = connection

    def find_edges_by_subject(
        self, tenant_id: str, subject_ref: str
    ) -> list[SemanticEdge]:
        """Return every edge whose `subject_ref` matches (HLD Section 5's forward traversal).

        The one-hop starting point for a bounded-depth BFS from a known
        Zone 5 entity -- the dominant Zone 3 access pattern (HLD Section
        5, DSA row 3).

        Raises:
            ValueError: If `tenant_id` or `subject_ref` is blank.
            dashanan.domain.exceptions.ZoneRepositoryError: If the
                underlying query fails.
        """
        self._require_tenant(tenant_id)
        if not subject_ref.strip():
            raise ValueError(
                "find_edges_by_subject requires a non-blank subject_ref"
            )
        rows = self._execute_or_raise(
            _FIND_EDGES_BY_SUBJECT_SQL, (tenant_id, subject_ref)
        )
        return [self._row_to_edge(row) for row in rows]

    def find_edges_by_object(
        self, tenant_id: str, object_ref: str
    ) -> list[SemanticEdge]:
        """Return every edge whose `object_ref` matches (HLD's reverse-traversal case).

        Serves HLD Section 3.4's own worked example's `index_reverse=true`
        query shape ("who/what points at this entity?"), pseudonymized
        here per the dev_prompt's PII constraint.

        Raises:
            ValueError: If `tenant_id` or `object_ref` is blank.
            dashanan.domain.exceptions.ZoneRepositoryError: If the
                underlying query fails.
        """
        self._require_tenant(tenant_id)
        if not object_ref.strip():
            raise ValueError("find_edges_by_object requires a non-blank object_ref")
        rows = self._execute_or_raise(
            _FIND_EDGES_BY_OBJECT_SQL, (tenant_id, object_ref)
        )
        return [self._row_to_edge(row) for row in rows]

    def find_fact_by_id(self, tenant_id: str, fact_id: str) -> GeneralFact | None:
        """Return one `GeneralFact` by its primary key, or `None` if absent.

        Raises:
            ValueError: If `tenant_id` or `fact_id` is blank.
            dashanan.domain.exceptions.ZoneRepositoryError: If the
                underlying query fails.
        """
        self._require_tenant(tenant_id)
        if not fact_id.strip():
            raise ValueError("find_fact_by_id requires a non-blank fact_id")
        rows = self._execute_or_raise(_FIND_FACT_BY_ID_SQL, (tenant_id, fact_id))
        if not rows:
            return None
        return self._row_to_fact(rows[0])

    def delete_edges_by_subject(
        self, tenant_id: str, subject_ref: str
    ) -> tuple[str, ...]:
        """DASH-STORY-025's Zone 3 DPDP erasure leg: remove every edge naming `subject_ref`.

        AC-025-1's own real erasure mechanism (must-not-deviate item 2 of
        DASH-STORY-025: a plain, ordinary `DELETE` -- the same class of
        mechanism SRS.md Section 4.1 already documents Zone 2/6 use, never
        crypto-shredding key-management infrastructure). Issues a single
        `DELETE ... RETURNING edge_id` statement so the affected-item
        evidence returned to the caller is exactly what this call actually
        deleted -- not a separate `SELECT` taken before the `DELETE` runs,
        which would let a concurrent write between the two unlocked
        statements desync the reported evidence from the real deleted
        rows.

        Args:
            tenant_id: Owning tenant. Never blank.
            subject_ref: The Zone 5 `entity_id` reference to erase every
                edge for. Never blank.

        Returns:
            The `edge_id` of every `SemanticEdge` actually removed by this
            call, in `edge_id ASC` order (mirroring
            `find_edges_by_subject`'s own ordering). An empty tuple is a
            clean no-op -- no edge existed for `subject_ref` -- not an
            error.

        Raises:
            ValueError: If `tenant_id` or `subject_ref` is blank.
            dashanan.domain.exceptions.ZoneRepositoryError: If the delete
                query fails.
        """
        self._require_tenant(tenant_id)
        if not subject_ref.strip():
            raise ValueError(
                "delete_edges_by_subject requires a non-blank subject_ref"
            )
        rows = self._execute_or_raise(
            _DELETE_EDGES_BY_SUBJECT_SQL, (tenant_id, subject_ref)
        )
        if not rows:
            return ()
        return tuple(sorted(cast(str, row[0]) for row in rows))

    def insert_edge(self, edge: SemanticEdge) -> None:
        """Insert one `SemanticEdge`. `object_ref` is DB-CHECK-enforced non-null (AC-003-SCHEMA-1).

        Raises:
            dashanan.domain.exceptions.ZoneRepositoryError: If the insert
                fails -- including a `chk_semantic_edges_object_ref_not_null`
                violation, which this method does not itself pre-validate
                beyond `SemanticEdge.__post_init__`'s own domain-level
                guard (must-not-deviate item 2: the database constraint is
                the binding enforcement, this adapter does not duplicate
                it as an additional application-level gate).
        """
        self._require_tenant(edge.tenant_id)
        params = (
            edge.tenant_id,
            edge.edge_id,
            edge.subject_ref,
            edge.predicate,
            edge.object_ref,
            json.dumps(dict(edge.qualifiers)),
            edge.state.value,
            json.dumps(dict(edge.score_terms)),
        )
        self._execute_or_raise(_INSERT_EDGE_SQL, params)

    def insert_general_fact(self, fact: GeneralFact) -> None:
        """Insert one `GeneralFact` (AC-003-GF-1: the `|subjects|==0` routing target).

        Raises:
            dashanan.domain.exceptions.ZoneRepositoryError: If the insert
                fails (e.g. a `fact_id` primary-key collision).
        """
        self._require_tenant(fact.tenant_id)
        params = (
            fact.tenant_id,
            fact.fact_id,
            fact.statement,
            fact.subject_scope,
            fact.state.value,
            json.dumps(dict(fact.score_terms)),
        )
        self._execute_or_raise(_INSERT_FACT_SQL, params)

    def _require_tenant(self, tenant_id: str) -> None:
        """Guard every query with a mandatory tenant_id (HLD 3.0 invariant 2)."""
        if not tenant_id.strip():
            raise ValueError("tenant_id is required for every Semantic zone query")

    def _execute_or_raise(
        self, sql: str, params: Sequence[object]
    ) -> list[tuple[object, ...]]:
        """Run one parameterized statement, converting any failure to a domain error."""
        try:
            cursor = self._connection.cursor()
            cursor.execute(sql, params)
            return cursor.fetchall()
        except Exception as exc:  # noqa: BLE001 -- converted to typed domain error below
            logger.warning(
                "Zone 3 (Semantic) query failed",
                extra={
                    "zone": ZoneId.SEMANTIC.value,
                    "error_type": type(exc).__name__,
                    "reason": str(exc),
                },
            )
            raise ZoneRepositoryError(
                zone=ZoneId.SEMANTIC.value, reason=str(exc)
            ) from exc

    def _row_to_edge(self, row: tuple[object, ...]) -> SemanticEdge:
        """Map one result row back into the domain `SemanticEdge` shape.

        Every field is explicitly `cast` from the DB-API boundary's opaque
        `object` type -- `_EDGE_COLUMNS`' fixed column order is this
        method's only contract with the driver.
        """
        (
            tenant_id,
            edge_id,
            subject_ref,
            predicate,
            object_ref,
            qualifiers,
            state,
            score_terms,
        ) = row
        return SemanticEdge(
            tenant_id=cast(str, tenant_id),
            edge_id=cast(str, edge_id),
            subject_ref=cast(str, subject_ref),
            predicate=cast(str, predicate),
            object_ref=cast(str, object_ref),
            qualifiers=self._as_json_object(qualifiers),
            state=SemanticState(cast(str, state)),
            score_terms=self._as_score_terms(score_terms),
        )

    def _row_to_fact(self, row: tuple[object, ...]) -> GeneralFact:
        """Map one result row back into the domain `GeneralFact` shape."""
        (tenant_id, fact_id, statement, subject_scope, state, score_terms) = row
        return GeneralFact(
            tenant_id=cast(str, tenant_id),
            fact_id=cast(str, fact_id),
            statement=cast(str, statement),
            subject_scope=cast(str, subject_scope),
            state=SemanticState(cast(str, state)),
            score_terms=self._as_score_terms(score_terms),
        )

    @staticmethod
    def _as_json_object(value: object) -> dict[str, str]:
        """Accept either a driver-native dict (JSONB auto-decode) or a JSON string."""
        if isinstance(value, dict):
            return cast(dict[str, str], value)
        if value is None:
            return {}
        return cast(dict[str, str], json.loads(str(value)))

    @staticmethod
    def _as_score_terms(value: object) -> dict[str, float]:
        """Accept either a driver-native dict (JSONB auto-decode) or a JSON string."""
        if isinstance(value, dict):
            return cast(dict[str, float], value)
        if value is None:
            return {}
        return cast(dict[str, float], json.loads(str(value)))
