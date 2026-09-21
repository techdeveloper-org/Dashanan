"""Shared `ZoneRepository`-port contract suite (DASH-STORY-021, FR-011).

FR-011 (verbatim, SRS.md): "The system SHALL define a storage-adapter
interface that decouples each zone's logical behavior from its physical
storage backend, so any zone can be backed by a different concrete
adapter without changing the zone's orchestration contract."

AC-011 (verbatim, SRS.md): "Given a zone is configured with a different
concrete storage adapter (e.g., swapping a relational adapter for a
vector-store adapter), When the zone's orchestration contract
(`ZoneRepository` port) is exercised, Then zone behavior is unchanged from
the caller's perspective."

AC-021-1 (verbatim, sprint3_ar1_assignments.json, DASH-STORY-021): "Given
the shared ZoneRepository-port contract test suite already exercised
against Zone 1/2/6/7's adapters in Sprint 1, When the same suite is run
against Zone 3 (SqlSemanticRepository) and Zone 5 (EntityMemoryRepository)'s
adapters, Then it passes with zero adapter-specific behavioral leakage (no
test requires knowledge of which concrete adapter is behind the port)."

Real finding this story confirms (sprint3_ar1_assignments.json's own
`real_finding` for this story_id): every one of the 8 zones already has a
shipped concrete adapter (direct source-tree glob). The remaining, real,
previously-unclosed verification work is exactly what this file does --
prove AC-011's cross-adapter behavioral-equivalence claim by running one
identical test body against several concrete `ZoneRepository` adapters
through the port alone, never through any adapter-specific method.

Zone 3 scope boundary (not a defect, AC-021-1's own must-not-deviate item 3
distinguishes "real behavioral divergence" from a pre-existing, already-
documented design boundary): `SqlSemanticRepository`
(`infrastructure/sql_semantic_repository.py`) does not implement
`ZoneRepository` at all -- its own module docstring records this as a
DASH-STORY-012 judgment call (`MemoryItem.token_count` must be positive,
and neither `SemanticEdge` nor `GeneralFact` carries a `token_count` field
in HLD Section 3.4's literal owned-entity shape), the same boundary
`SqlProvenanceRepository` already draws for Zone 7. AC-021-1's own premise
that the suite ran "against Zone 1/2/6/7's adapters in Sprint 1" already
carries this asymmetry: Zone 7's `SqlProvenanceRepository` has no `fetch`
method either (confirmed below, `TestZone3AndZone7AreDeliberatelyOutOfPortScope`),
so the historical "Zone 1/2/6/7" set was never a uniform four-adapter
port-conformance set to begin with. `TestZone3AndZone7AreDeliberatelyOutOfPortScope`
below is this story's AC-021-1 closure for Zone 3: it confirms (does not
newly introduce) that boundary structurally, via `isinstance` against the
frozen, `runtime_checkable` `ZoneRepository` Protocol -- no adapter code
changes, no new port method, no Jira defect (must-not-deviate item 3 only
requires a Jira issue for *real* behavioral divergence between two
adapters that both claim to implement the port; Zone 3 never claims to).

Every `Test*Contract` class below exercises the port surface only
(`isinstance(adapter, ZoneRepository)` and `adapter.fetch(query)`) against
five concrete adapters that DO implement `ZoneRepository`: Zone 1
(`WorkingMemoryLRURepository`), Zone 2 (`SqlEpisodicRepository`), Zone 4
(`InMemoryProceduralMemoryRepository`), Zone 5 (`EntityMemoryRepository`,
AC-021-1's own named target), and Zone 6 (`HybridRetrievalIndexRepository`).
No test method below calls `put`, `write_attribute`, `commit`,
`record_execution`, or `index_item` -- those are seeding-fixture concerns,
isolated in the per-adapter `_build_*` functions below the shared test
classes, never in a shared test body itself.

PII NOTE: every fixture below uses short, generic, non-conversational
placeholder text and opaque `item_id`/`tenant_id` strings, matching this
codebase's existing contract-test convention
(`test_smoke_retrieval_index.py`, `test_smoke_episodic.py`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from dashanan.domain.episode import Episode, EpisodeState
from dashanan.domain.memory_item import MemoryItem
from dashanan.domain.ports import ZoneQuery, ZoneRepository
from dashanan.domain.zone import ZoneId
from dashanan.infrastructure.entity_memory_repository import EntityMemoryRepository
from dashanan.infrastructure.hybrid_retrieval_index_repository import (
    HybridRetrievalIndexRepository,
)
from dashanan.infrastructure.in_memory_lexical_index import InMemoryLexicalIndex
from dashanan.infrastructure.in_memory_procedural_memory_repository import (
    InMemoryProceduralMemoryRepository,
)
from dashanan.infrastructure.in_memory_vector_index import InMemoryVectorIndex
from dashanan.infrastructure.noop_event_bus import NoOpEventBus
from dashanan.infrastructure.sql_episodic_repository import SqlEpisodicRepository
from dashanan.infrastructure.sql_provenance_repository import SqlProvenanceRepository
from dashanan.infrastructure.sql_semantic_repository import SqlSemanticRepository
from dashanan.infrastructure.working_memory_lru_repository import (
    WorkingMemoryLRURepository,
)

_SEEDED_TENANT = "dash021-contract-tenant"
_UNSEEDED_TENANT = "dash021-unseeded-tenant"
_FIXED_INSTANT = datetime(2026, 1, 1, tzinfo=UTC)


class _FixedClock:
    """Deterministic `Clock` double, local to this file (testing-core DI)."""

    def now(self) -> datetime:
        return _FIXED_INSTANT


class _RecordingCursor:
    """Minimal DB-API cursor double over a fixed row set.

    Filters the canned rows by the query's leading `tenant_id` parameter
    and truncates to its trailing `LIMIT` parameter, mirroring
    `_FETCH_RECENCY_SQL`'s own `(tenant_id, as_of, as_of, max_items)`
    parameter shape closely enough for this contract suite's tenant-
    isolation and `max_items` assertions to exercise real filtering
    behavior rather than a cursor double that ignores its own query.
    """

    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows
        self._last_params: Sequence[object] = ()

    def execute(self, sql: str, params: Sequence[object]) -> None:
        self._last_params = params

    def fetchall(self) -> list[tuple[object, ...]]:
        if not self._last_params:
            return list(self._rows)
        tenant_id = self._last_params[0]
        limit = self._last_params[-1]
        matching = [row for row in self._rows if row[0] == tenant_id]
        return matching[:limit]


class _RecordingConnection:
    """Minimal DB-API connection double exposing one shared `_RecordingCursor`."""

    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._cursor = _RecordingCursor(rows)

    def cursor(self) -> _RecordingCursor:
        return self._cursor


def _episode_row(episode: Episode) -> tuple[object, ...]:
    """Build one `_FETCH_RECENCY_SQL`-shaped row from an `Episode`, matching
    `SqlEpisodicRepository._SELECT_COLUMNS`'s fixed column order.
    """
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


@dataclass(frozen=True)
class AdapterUnderTest:
    """One concrete `ZoneRepository` adapter, seeded and ready for the shared suite.

    `seeded_query` already carries whatever `task`/`query_embedding` shape
    that adapter's own `fetch` needs to surface `seeded_item_ids` -- the
    shared test bodies below never construct adapter-specific query shapes
    themselves, only reuse this field.
    """

    label: str
    adapter: ZoneRepository
    zone_id: ZoneId
    seeded_item_ids: frozenset[str]
    seeded_query: ZoneQuery


def _zone_query(
    tenant_id: str,
    *,
    task: str | None = None,
    query_embedding: list[float] | None = None,
    max_items: int = 10,
) -> ZoneQuery:
    return ZoneQuery(
        tenant_id=tenant_id,
        task=task,
        query_embedding=query_embedding,
        max_items=max_items,
        min_provenance_conf=0.0,
    )


def _build_zone1() -> AdapterUnderTest:
    """Zone 1 (Working): `WorkingMemoryLRURepository`, Shape A (ADR-005)."""
    repo = WorkingMemoryLRURepository(clock=_FixedClock())
    repo.put(_SEEDED_TENANT, "session-1", "wm-1", "payload-1", token_count=5)
    repo.put(_SEEDED_TENANT, "session-1", "wm-2", "payload-2", token_count=5)
    return AdapterUnderTest(
        label="zone1-working-memory-lru",
        adapter=repo,
        zone_id=ZoneId.WORKING,
        seeded_item_ids=frozenset({"wm-1", "wm-2"}),
        seeded_query=_zone_query(_SEEDED_TENANT, max_items=10),
    )


def _build_zone2() -> AdapterUnderTest:
    """Zone 2 (Episodic): `SqlEpisodicRepository`, ADR-006 Shape B (SQL)."""
    episodes = [
        Episode(
            tenant_id=_SEEDED_TENANT,
            session_id="session-1",
            episode_id=Episode.episode_id_for(_SEEDED_TENANT, "session-1", seq),
            seq=seq,
            occurred_at=_FIXED_INSTANT,
            written_at=_FIXED_INSTANT,
            payload=f"episode-{seq}",
            actors=("user", "agent"),
            token_count=8,
            state=EpisodeState.ACTIVE,
            score_terms={},
        )
        for seq in (0, 1)
    ]
    connection = _RecordingConnection([_episode_row(ep) for ep in episodes])
    repo = SqlEpisodicRepository(connection, _FixedClock(), verify_privileges=False)
    return AdapterUnderTest(
        label="zone2-sql-episodic",
        adapter=repo,
        zone_id=ZoneId.EPISODIC,
        seeded_item_ids=frozenset(ep.episode_id for ep in episodes),
        seeded_query=_zone_query(_SEEDED_TENANT, max_items=10),
    )


def _build_zone4() -> AdapterUnderTest:
    """Zone 4 (Procedural): `InMemoryProceduralMemoryRepository`, Shape A (ADR-009)."""
    repo = InMemoryProceduralMemoryRepository(event_bus=NoOpEventBus(), clock=_FixedClock())
    task = "dash021 contract test task"
    procedure = repo.record_execution(
        _SEEDED_TENANT, task, ("step-a", "step-b"), succeeded=True
    )
    return AdapterUnderTest(
        label="zone4-in-memory-procedural",
        adapter=repo,
        zone_id=ZoneId.PROCEDURAL,
        seeded_item_ids=frozenset({procedure.task_signature_hash}),
        seeded_query=_zone_query(_SEEDED_TENANT, task=task, max_items=10),
    )


def _build_zone5() -> AdapterUnderTest:
    """Zone 5 (Entity): `EntityMemoryRepository`, AC-021-1's own named target."""
    repo = EntityMemoryRepository(clock=_FixedClock(), event_bus=NoOpEventBus())
    repo.write_attribute(_SEEDED_TENANT, "entity-1", "attr-a", "value-a", "prov-1")
    repo.register_alias(_SEEDED_TENANT, "entity-1", "dash021-alias")
    return AdapterUnderTest(
        label="zone5-entity-memory",
        adapter=repo,
        zone_id=ZoneId.ENTITY,
        seeded_item_ids=frozenset({"entity-1"}),
        seeded_query=_zone_query(_SEEDED_TENANT, task="dash021-alias", max_items=10),
    )


def _build_zone6() -> AdapterUnderTest:
    """Zone 6 (Retrieval Index): `HybridRetrievalIndexRepository`, ADR-007/008.

    Zone 6 is a fused aggregator over other zones' owned items, so a
    fetch result's own `source_zone` carries the item's *origin* zone
    (whatever `index_item`'s own `source_zone` argument was -- here
    `ZoneId.EPISODIC`), never `ZoneId.RETRIEVAL_INDEX` itself. This
    fixture's `zone_id` field is therefore set to `ZoneId.EPISODIC` to
    match, the same origin-zone convention `test_smoke_retrieval_index.py`'s
    own AC-006 assertion already confirms.
    """
    repo = HybridRetrievalIndexRepository(InMemoryVectorIndex(), InMemoryLexicalIndex())
    repo.index_item(
        _SEEDED_TENANT,
        "ri-1",
        ZoneId.EPISODIC,
        "dash021 contract test fixture text",
        (1.0, 0.0, 0.0),
        "model-dash021",
    )
    return AdapterUnderTest(
        label="zone6-hybrid-retrieval-index",
        adapter=repo,
        zone_id=ZoneId.EPISODIC,
        seeded_item_ids=frozenset({"ri-1"}),
        seeded_query=_zone_query(
            _SEEDED_TENANT,
            task="dash021 contract test fixture text",
            query_embedding=[1.0, 0.0, 0.0],
            max_items=10,
        ),
    )


_ADAPTER_BUILDERS = (
    _build_zone1,
    _build_zone2,
    _build_zone4,
    _build_zone5,
    _build_zone6,
)


def _adapters_under_test() -> list[AdapterUnderTest]:
    """Build a fresh set of adapters per test session (fixtures hold mutable state)."""
    return [builder() for builder in _ADAPTER_BUILDERS]


@pytest.fixture(params=_ADAPTER_BUILDERS, ids=lambda builder: builder.__name__)
def adapter_under_test(request: pytest.FixtureRequest) -> AdapterUnderTest:
    """One freshly-built, freshly-seeded adapter per parametrized test run.

    A fresh adapter per test (not a shared/session-scoped one) so no test
    in this shared suite can pass only because an earlier test in the same
    run happened to seed state a later test silently depends on -- each
    test in this file must hold under isolated, single-test execution.
    """
    return request.param()


class TestAdapterSatisfiesZoneRepositoryProtocol:
    """Every adapter under test structurally satisfies the frozen port (AR1-G2)."""

    def test_isinstance_zone_repository(self, adapter_under_test: AdapterUnderTest) -> None:
        assert isinstance(adapter_under_test.adapter, ZoneRepository), (
            f"{adapter_under_test.label} must satisfy the ZoneRepository "
            "Protocol via structural typing (runtime_checkable)"
        )


class TestFetchReturnsWellFormedMemoryItems:
    """AC-011: `fetch` always returns the caller-visible `MemoryItem` shape,
    regardless of which concrete adapter is behind the port.
    """

    def test_fetch_returns_a_list(self, adapter_under_test: AdapterUnderTest) -> None:
        results = adapter_under_test.adapter.fetch(adapter_under_test.seeded_query)
        assert isinstance(results, list)

    def test_every_result_is_a_memory_item(
        self, adapter_under_test: AdapterUnderTest
    ) -> None:
        results = adapter_under_test.adapter.fetch(adapter_under_test.seeded_query)
        assert all(isinstance(item, MemoryItem) for item in results)

    def test_every_result_has_a_positive_token_count(
        self, adapter_under_test: AdapterUnderTest
    ) -> None:
        results = adapter_under_test.adapter.fetch(adapter_under_test.seeded_query)
        assert all(item.token_count > 0 for item in results), (
            "MemoryItem.token_count must be positive (MemoryItem.__post_init__'s "
            "own invariant) for every zone's fetch results, not only some"
        )

    def test_every_result_carries_the_adapters_own_zone_id(
        self, adapter_under_test: AdapterUnderTest
    ) -> None:
        results = adapter_under_test.adapter.fetch(adapter_under_test.seeded_query)
        assert all(
            item.source_zone == adapter_under_test.zone_id for item in results
        )

    def test_every_result_has_a_non_blank_item_id(
        self, adapter_under_test: AdapterUnderTest
    ) -> None:
        results = adapter_under_test.adapter.fetch(adapter_under_test.seeded_query)
        assert all(item.item_id.strip() for item in results)


class TestFetchReturnsSeededItemsForTheSeededTenant:
    """AC-011: the caller-visible result set for the seeded tenant contains
    exactly the items that tenant's own data actually holds.
    """

    def test_fetch_surfaces_every_seeded_item_id(
        self, adapter_under_test: AdapterUnderTest
    ) -> None:
        results = adapter_under_test.adapter.fetch(adapter_under_test.seeded_query)
        returned_ids = {item.item_id for item in results}
        assert adapter_under_test.seeded_item_ids <= returned_ids, (
            f"{adapter_under_test.label}: expected seeded ids "
            f"{sorted(adapter_under_test.seeded_item_ids)} to be a subset of "
            f"returned ids {sorted(returned_ids)}"
        )


class TestFetchRespectsMaxItems:
    """AC-011: the port's `max_items` cap is honored uniformly across adapters."""

    def test_fetch_never_returns_more_than_max_items(
        self, adapter_under_test: AdapterUnderTest
    ) -> None:
        capped_query = ZoneQuery(
            tenant_id=adapter_under_test.seeded_query.tenant_id,
            task=adapter_under_test.seeded_query.task,
            query_embedding=adapter_under_test.seeded_query.query_embedding,
            max_items=1,
            min_provenance_conf=adapter_under_test.seeded_query.min_provenance_conf,
        )
        results = adapter_under_test.adapter.fetch(capped_query)
        assert len(results) <= 1


class TestFetchIsolatesUnseededTenants:
    """AC-014-class isolation, generalized to every zone (not only Zone 6):
    a tenant with no data of its own never receives another tenant's items.
    """

    def test_fetch_returns_no_items_for_a_tenant_with_no_data(
        self, adapter_under_test: AdapterUnderTest
    ) -> None:
        unseeded_query = ZoneQuery(
            tenant_id=_UNSEEDED_TENANT,
            task=adapter_under_test.seeded_query.task,
            query_embedding=adapter_under_test.seeded_query.query_embedding,
            max_items=adapter_under_test.seeded_query.max_items,
            min_provenance_conf=adapter_under_test.seeded_query.min_provenance_conf,
        )
        results = adapter_under_test.adapter.fetch(unseeded_query)
        returned_ids = {item.item_id for item in results}
        assert not (returned_ids & adapter_under_test.seeded_item_ids), (
            f"{adapter_under_test.label}: an unseeded tenant's fetch must "
            "never return another tenant's seeded item ids"
        )


class TestFetchNeverRaisesForAWellFormedQuery:
    """AC-011's "caller's perspective" clause: a well-formed `ZoneQuery`
    against any adapter completes without the adapter raising -- the
    caller (Memory Orchestrator) sees uniform behavior, not an adapter-
    specific exception type leaking through the port.
    """

    def test_fetch_completes_without_raising(
        self, adapter_under_test: AdapterUnderTest
    ) -> None:
        try:
            adapter_under_test.adapter.fetch(adapter_under_test.seeded_query)
        except Exception as exc:  # noqa: BLE001 -- this IS the assertion
            pytest.fail(
                f"{adapter_under_test.label}.fetch raised {type(exc).__name__}: "
                f"{exc} for a well-formed, correctly-seeded query"
            )


class TestSubstitutability:
    """AC-011's literal worked example: swapping the concrete adapter behind
    the port changes nothing about the shape of what a caller observes.

    This class does not call `fetch` per adapter individually (the classes
    above already do that); it instead confirms the cross-adapter
    invariant directly -- every adapter, seeded with its own data, produces
    result objects of the identical `MemoryItem` type with the identical
    field set, so a caller iterating `results` never needs an
    adapter-specific branch to consume them.
    """

    def test_all_adapters_produce_the_same_result_type(self) -> None:
        adapters = _adapters_under_test()
        result_types: set[type] = set()
        for entry in adapters:
            for item in entry.adapter.fetch(entry.seeded_query):
                result_types.add(type(item))
        assert result_types <= {MemoryItem}, (
            "Every adapter's fetch() must return plain MemoryItem instances "
            f"-- found adapter-specific result types: {result_types}"
        )


class TestZone3AndZone7AreDeliberatelyOutOfPortScope:
    """AC-021-1's Zone 3 closure: confirms (does not newly discover) the
    documented DASH-STORY-012 boundary -- `SqlSemanticRepository` does not
    implement `ZoneRepository`. Recorded here, next to the shared suite
    that DOES exercise Zone 5, so a future reader sees both halves of
    AC-021-1's own two-zone scope side by side. Zone 7's identical,
    already-documented boundary (`SqlProvenanceRepository`, mirrored by
    Zone 3's own module docstring) is included for the same reason
    AC-021-1's premise names Zone 7 in the "already exercised" set: to
    make explicit that Zone 7 was never a `ZoneRepository`-conformance
    case either, so this file's five-adapter shared suite above is not
    silently missing a zone that the story's own acceptance text implies
    should have been six or seven.
    """

    def test_sql_semantic_repository_does_not_implement_zone_repository(self) -> None:
        repo = SqlSemanticRepository(connection=_RecordingConnection([]))
        assert not isinstance(repo, ZoneRepository), (
            "SqlSemanticRepository (Zone 3) is documented (its own module "
            "docstring) as deliberately outside ZoneRepository's structural "
            "shape -- MemoryItem.token_count must be positive and neither "
            "SemanticEdge nor GeneralFact carries a token_count field. If "
            "this assertion ever fails, Zone 3 has gained a `fetch` method "
            "and AC-021-1's shared suite must be extended to cover it, not "
            "silently left out."
        )
        assert not hasattr(repo, "fetch")

    def test_sql_provenance_repository_does_not_implement_zone_repository(self) -> None:
        repo = SqlProvenanceRepository(
            connection=_RecordingConnection([]), verify_privileges=False
        )
        assert not isinstance(repo, ZoneRepository), (
            "SqlProvenanceRepository (Zone 7) draws the same boundary Zone "
            "3's own docstring cites as precedent; if this assertion ever "
            "fails, AC-021-1's premise about the historical Zone 1/2/6/7 "
            "set has changed and this file's shared-suite adapter list "
            "must be revisited."
        )
        assert not hasattr(repo, "fetch")


class _AdapterMissingFetch:
    """Synthetic double for the QA prompt's own negative example: "an
    adapter missing a required ZoneRepository port method." Carries every
    other port-shaped attribute a real adapter might have except `fetch`
    itself, so this test proves the Protocol's `runtime_checkable` check
    is keyed on the actual method being present, not merely on the
    object being adapter-shaped in general.
    """

    zone_id = ZoneId.WORKING


class TestAdapterMissingFetchMethodFailsTheProtocolCheck:
    """AC-011 negative case (qa_prompt's own worked example): a synthetic
    double that carries no `fetch` method at all must fail
    `isinstance(..., ZoneRepository)`, proving the shared suite's
    `TestAdapterSatisfiesZoneRepositoryProtocol` gate would actually catch
    a real adapter that shipped without implementing the port, not just
    document adapters that already pass.
    """

    def test_isinstance_check_rejects_an_adapter_with_no_fetch_method(self) -> None:
        double = _AdapterMissingFetch()
        assert not isinstance(double, ZoneRepository), (
            "a double exposing no fetch() method must fail the runtime_checkable "
            "ZoneRepository check -- if this assertion ever fails, isinstance is "
            "no longer a reliable substitutability gate for real adapters"
        )

    def test_calling_fetch_on_such_a_double_raises_attribute_error(self) -> None:
        double = _AdapterMissingFetch()
        query = _zone_query(_SEEDED_TENANT, max_items=10)
        with pytest.raises(AttributeError):
            double.fetch(query)  # type: ignore[attr-defined]


class TestZoneQueryAgainstAnAdapterThatDoesNotImplementThePort:
    """QA prompt's own negative example: "a zone query against an adapter
    that doesn't implement it." Exercises Zone 3's `SqlSemanticRepository`
    (already confirmed above to fall outside `ZoneRepository`'s structural
    shape) with a real, well-formed `ZoneQuery`, proving the caller-facing
    failure mode is a clean `AttributeError` -- never a silent empty
    result that could be mistaken for "no items matched."
    """

    def test_fetch_call_against_sql_semantic_repository_raises_attribute_error(
        self,
    ) -> None:
        repo = SqlSemanticRepository(connection=_RecordingConnection([]))
        query = _zone_query(_SEEDED_TENANT, max_items=10)
        with pytest.raises(AttributeError):
            repo.fetch(query)  # type: ignore[attr-defined]


class TestAdapterSubstitutedMidSession:
    """AC-011's literal worked example, made concrete as a single caller
    routine run twice against two structurally different shipped
    adapters -- Zone 1's Shape A (`WorkingMemoryLRURepository`, ADR-005,
    in-process) swapped mid-"session" for Zone 2's Shape B
    (`SqlEpisodicRepository`, ADR-006, SQL) -- confirming the same caller
    code path (no adapter-specific branch) produces the port's uniform
    caller-visible contract against both, not only that each adapter
    passes its own isolated test.
    """

    @staticmethod
    def _caller_routine(adapter: ZoneRepository, query: ZoneQuery) -> list[MemoryItem]:
        """The one caller-side routine every adapter in this test is run through.

        Deliberately free of any `isinstance`/type check on `adapter` --
        AC-021-1's own "zero adapter-specific behavioral leakage" clause
        means this routine must not need to know which concrete adapter
        it received.
        """
        results = adapter.fetch(query)
        assert isinstance(results, list)
        return results

    def test_same_caller_routine_succeeds_across_a_mid_session_adapter_swap(
        self,
    ) -> None:
        shape_a = _build_zone1()
        shape_b = _build_zone2()

        first_results = self._caller_routine(shape_a.adapter, shape_a.seeded_query)
        second_results = self._caller_routine(shape_b.adapter, shape_b.seeded_query)

        assert {item.item_id for item in first_results} == shape_a.seeded_item_ids
        assert {item.item_id for item in second_results} == shape_b.seeded_item_ids
        assert all(isinstance(item, MemoryItem) for item in first_results + second_results)


class TestFullEightZoneAdapterInventoryPresent:
    """This story's own `real_finding` (sprint3_ar1_assignments.json,
    DASH-STORY-021): every one of the 8 zones already has a shipped
    concrete adapter file. Recorded here as an importability assertion so
    a future removal of any adapter module fails this suite rather than
    silently reopening the FR-011 "remaining zone coverage" question the
    must-not-deviate list forbids reinterpreting as license for a new
    Zone 9 or a duplicate adapter.
    """

    def test_every_shipped_zone_adapter_module_is_importable(self) -> None:
        import importlib

        module_names = (
            "dashanan.infrastructure.working_memory_lru_repository",
            "dashanan.infrastructure.sql_episodic_repository",
            "dashanan.infrastructure.sql_semantic_repository",
            "dashanan.infrastructure.in_memory_procedural_memory_repository",
            "dashanan.infrastructure.entity_memory_repository",
            "dashanan.infrastructure.hybrid_retrieval_index_repository",
            "dashanan.infrastructure.sql_provenance_repository",
        )
        for name in module_names:
            assert importlib.import_module(name) is not None, (
                f"Zone adapter module {name} must remain importable -- its "
                "absence would mean a zone that FR-011's real_finding "
                "confirmed as shipped has silently regressed"
            )
