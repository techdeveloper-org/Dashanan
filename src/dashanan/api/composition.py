"""AppContext: the composition root's real caller (AC-023-2, closing DSHN-60).

`CompositionRootConfigurationError`'s own attempt-3 remediation named the
exact still-open gap this module closes: "this codebase has no api/routes
module or host application anywhere that actually invokes this
composition root, or any other constructor, outside its test suite."

AC-023-2 (verbatim): every `MemoryOrchestrator`/`SqlProvenanceRepository`/
`SqlEpisodicRepository` this host constructs goes through
`dashanan.infrastructure.composition_root`'s own builder functions --
`build_memory_orchestrator`/`build_provenance_repository`/
`build_episodic_repository` -- NEVER a bare constructor call anywhere in
this module or in `app.py`. `EntityMemoryRepository`/`SqlSemanticRepository`
are NOT among the three AC-023-2 names; this module still builds them
through `composition_root.build_conflict_aware_semantic_repository`/
`build_conflict_aware_entity_memory_repository` wherever a Postgres
connection is available, matching `composition_root`'s own established
"caller composes the pre-constructed Shape A adapter, hands it in"
convention for `EntityMemoryRepository` (`build_conflict_aware_entity_
memory_repository`'s own docstring).

Shape A / Shape B split (must-not-deviate item 1, binding): this story
does not stand up real Postgres -- that is FR-015 (DASH-STORY-024)'s own
scope, which `docker-compose.yml` already provisions for local dev. This
module ATTEMPTS one real `build_postgres_connection()` at startup
(`composition_root`'s own real, non-embedded Shape B adapter path). When
Postgres is unreachable (`SettingsConfigurationError`, `psycopg.Error`),
this module does NOT fail the whole host: Zone 1 (Working, Shape A,
in-process) and Zone 4 (Procedural, Shape A, in-process) stay fully live,
and every Postgres-dependent operationId (`assembleContext` for Zone
2/3/5/7, `writeMemory`'s Zone 2/3/5 legs, `eraseSubject`'s Zone 8 leg --
Zone 8's own manifest is itself `SqlManifestRepository`-backed) degrades
to the documented `503 ServiceDegraded`/`WriteRejectedNotDurable`
response openapi.yaml already specifies for exactly this scenario
(HLD Section 8.4 per-dependency degradation) -- never a `501` placeholder
(AC-023-1) and never a silently faked success.

Superseded, `AR1-S3-G3` (DASH-STORY-028-DEV closes this gap): a single
shared `psycopg.Connection` used to be reused across Zone 2/3/7/8-manifest.
`psycopg.Connection` is not safe for concurrent use across threads without
its own pooling. This module now builds one real `psycopg_pool.
ConnectionPool` at startup (`composition_root.build_postgres_connection_
pool`) plus one `CircuitBreaker` wrapping pooled-connection acquisition
(`composition_root.build_postgres_circuit_breaker`, connection-pooling-
design.md Section 5.1), and every Zone 2/3/5/7/8-manifest repository/
service is now constructed FRESH, PER REQUEST, from a pool-checked-out
connection via the `make_*_dependency` factories below -- never once at
startup. `EntityMemoryRepository` (Zone 5's Shape A adapter) and Zone 8's
in-memory bookkeeping (`InMemorySubjectKeyStore`/
`InMemoryZone8SubjectIndex`/`InMemorySubjectErasureJobStore`/
`LocalObjectStore`) remain startup-constructed singletons on `AppContext`
-- they hold cross-request state (an erasure job created in one request is
polled in another) or are themselves in-process, not Postgres-connection-
bound, so per-request reconstruction would either lose that state or buy
nothing.
"""

from __future__ import annotations

import logging
import tempfile
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass
from pathlib import Path

import psycopg
from fastapi import Depends
from psycopg_pool import ConnectionPool, PoolTimeout

from dashanan.application.conflict_aware_zone_writes import (
    ConflictAwareEntityMemoryRepository,
    ConflictAwareSemanticRepository,
)
from dashanan.application.conflict_detection_sweep import ProvenanceRepositoryPort
from dashanan.application.memory_orchestrator import MemoryOrchestrator
from dashanan.application.subject_erasure_cascade import SubjectErasureCascadeService
from dashanan.application.unified_subject_erasure_orchestrator import (
    UnifiedSubjectErasureOrchestrator,
)
from dashanan.application.zone8_crypto_shredding_store import Zone8SubjectKeyedArchiver
from dashanan.application.zone8_consolidation_store import Zone8ConsolidationStore
from dashanan.application.zone8_erasure_durability import (
    DurableZone8ErasureCoordinator,
    InMemoryErasureMarkerStore,
    SqlErasureMarkerStore,
)
from dashanan.domain.ports import Clock, EventBus
from dashanan.domain.zone import ZoneId
from dashanan.infrastructure import composition_root
from dashanan.infrastructure.circuit_breaker import CircuitBreaker
from dashanan.infrastructure.entity_memory_repository import EntityMemoryRepository
from dashanan.infrastructure.in_memory_procedural_memory_repository import (
    InMemoryProceduralMemoryRepository,
)
from dashanan.infrastructure.in_memory_subject_erasure_job_store import (
    InMemorySubjectErasureJobStore,
)
from dashanan.infrastructure.in_memory_subject_key_store import InMemorySubjectKeyStore
from dashanan.infrastructure.in_memory_zone8_subject_index import InMemoryZone8SubjectIndex
from dashanan.infrastructure.local_object_store import LocalObjectStore
from dashanan.infrastructure.noop_event_bus import NoOpEventBus
from dashanan.infrastructure.settings import SettingsConfigurationError
from dashanan.infrastructure.sql_manifest_repository import SqlManifestRepository
from dashanan.infrastructure.sql_semantic_repository import SqlSemanticRepository
from dashanan.infrastructure.sql_subject_to_item_index_repository import (
    SqlSubjectToItemIndexRepository,
)
from dashanan.infrastructure.system_clock import SystemClock
from dashanan.infrastructure.working_memory_lru_repository import WorkingMemoryLRURepository

logger = logging.getLogger(__name__)

POOL_ACQUIRE_TIMEOUT_SECONDS = 2.0
"""connection-pooling-design.md Section 5's own recommended acquire timeout
(distinct from the load test's deliberately tight 250ms stress-test value,
Section 4.1)."""

POOL_TIMEOUT_RETRY_AFTER_SECONDS = 1
"""Section 5: PoolTimeout's `Retry-After` -- a fixed, short value, since pool
exhaustion is a short-lived saturation signal, NOT breaker-OPEN's
dynamically-computed value (Section 5.1.1)."""


class PostgresPoolExhaustedError(Exception):
    """Raised by `get_pooled_connection` when `PoolTimeout` occurs (Section 5).

    Maps to `503` + `Retry-After: 1` + `error.code=POOL_EXHAUSTED` (Section
    5's own decision, `api.app`'s registered exception handler) --
    distinct from `PostgresUnavailableError` below (Section 5.1.1).
    """


class PostgresUnavailableError(Exception):
    """Raised by `get_pooled_connection` when the circuit breaker is OPEN (Section 5.1).

    Carries `retry_after_seconds`, computed from the breaker's own
    `opened_at`/backoff state (Section 5.1.1's `max(1, ceil(opened_at +
    backoff_duration - now))` formula) -- never a fixed value, and never
    reusing `PostgresPoolExhaustedError`'s fixed 1-second value or
    `POOL_EXHAUSTED` error code (must-not-deviate, Section 5.1.1).
    """

    def __init__(self, retry_after_seconds: int) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__("Postgres circuit breaker is OPEN")


@dataclass(slots=True)
class AppContext:
    """Every real, composition-root-built collaborator this host's handlers call.

    `postgres_available` is the single flag that reflects whether Postgres
    is CONFIGURED at all (env present, pool constructed at startup) -- it is
    `True`/`False` for the lifetime of this `AppContext`, never toggled
    per-request. It is distinct from the per-request `PostgresUnavailable
    Error`/`PostgresPoolExhaustedError` exceptions the `make_*_dependency`
    factories below can raise on any given request even when
    `postgres_available` is `True` (a transiently OPEN breaker or an
    exhausted pool, Section 5/5.1) -- handlers that want the pre-existing
    "Postgres never configured" graceful-degradation response (e.g.
    `get_item_provenance` returning an empty list) still check
    `postgres_available`; the two new transient conditions are handled
    globally by `api.app`'s registered exception handlers instead, since
    they are not this-handler-specific degradations.

    `postgres_pool`/`postgres_breaker` are `None` exactly when
    `postgres_available` is `False`. Neither is ever read directly by a
    route handler -- only by the `make_*_dependency` factories in this
    module, which close over this `AppContext` (mirroring `api.app`'s own
    `_require_scope`-returns-a-bound-closure convention).
    """

    clock: Clock
    event_bus: EventBus
    tenant_credential_signing_key: bytes
    jwt_signing_key: bytes

    working_repository: WorkingMemoryLRURepository
    procedural_repository: InMemoryProceduralMemoryRepository
    entity_memory_repository: EntityMemoryRepository
    memory_orchestrator: MemoryOrchestrator

    postgres_available: bool
    postgres_pool: ConnectionPool | None
    postgres_breaker: CircuitBreaker | None

    # Zone 8 in-memory bookkeeping (Shape A, startup-constructed singletons --
    # NOT per-request; see module docstring's "Superseded, AR1-S3-G3" note).
    zone8_object_store: LocalObjectStore
    zone8_key_store: InMemorySubjectKeyStore
    zone8_subject_index: InMemoryZone8SubjectIndex
    zone8_job_store: InMemorySubjectErasureJobStore

    # DASH-STORY-027-DEV: the unified orchestrator's own combined job
    # store (SEPARATE from zone8_job_store, mirroring
    # UnifiedSubjectErasureOrchestrator's own docstring) and the Zone 8
    # erasure-durability write-ahead marker store. Both are Shape A,
    # startup-constructed singletons -- an orchestrator job/marker created
    # in one request is polled/recovered in another, the identical
    # cross-request-state rationale zone8_job_store above already
    # documents.
    unified_erasure_job_store: InMemorySubjectErasureJobStore
    zone8_erasure_marker_store: InMemoryErasureMarkerStore


def _try_build_postgres_pool() -> ConnectionPool | None:
    """Attempt `composition_root.build_postgres_connection_pool()`; `None` on any failure.

    Returns `None` -- never raises -- for `SettingsConfigurationError`
    (env not configured): Shape B is not available in this deployment
    right now, which this host must degrade gracefully for, not crash on
    (module docstring, Shape A/Shape B split). Unlike the old single-
    connection path, this does NOT catch `psycopg.Error` -- `psycopg_pool.
    ConnectionPool(open=True)` does not eagerly fail on an unreachable
    host the way a bare `psycopg.connect()` did (it retries `pool_min`
    connections in the background); reachability is instead surfaced
    per-request through `postgres_breaker`/`PostgresUnavailableError`,
    which is the whole point of DASH-STORY-028-DEV's breaker.
    """
    try:
        return composition_root.build_postgres_connection_pool()
    except SettingsConfigurationError as exc:
        logger.warning(
            "Postgres settings not configured; Zone 2/3/5/7/8-manifest "
            "operationIds will report degraded/503",
            extra={"reason": str(exc)},
        )
        return None


def build_app_context(
    *,
    tenant_credential_signing_key: bytes,
    jwt_signing_key: bytes,
    zone8_object_store_dir: Path | None = None,
    postgres_pool: ConnectionPool | None | object = _try_build_postgres_pool,
) -> AppContext:
    """Build the one `AppContext` this host's `app.py` depends on (AC-023-2, AC-023-3).

    DASH-STORY-028-DEV: this function no longer eagerly constructs
    `SqlProvenanceRepository`/`SqlEpisodicRepository`/
    `ConflictAwareSemanticRepository`/`ConflictAwareEntityMemoryRepository`/
    `SqlManifestRepository`/`Zone8ConsolidationStore`/
    `Zone8SubjectKeyedArchiver`/`SubjectErasureCascadeService` -- it builds
    only the pool and the breaker. `api.app.create_app` binds this
    `AppContext` into the `make_*_dependency` factories below, which
    construct each of those fresh, per request, from a pool-checked-out
    connection.

    Args:
        tenant_credential_signing_key: Forwarded unchanged to
            `composition_root.build_memory_orchestrator` -- never
            resolved a second time here (single source of truth).
        jwt_signing_key: This host's own JWT verification key
            (`api.security`); unrelated to `tenant_credential_signing_key`
            (see `api.security`'s own "Key separation" docstring).
        zone8_object_store_dir: Where Zone 8's `LocalObjectStore` writes.
            Defaults to a fresh temp directory (testing-core DI seam).
        postgres_pool: Either a pre-built pool/pool-like double (testing-
            core DI -- a test passes a fake object exposing a
            `.connection(timeout=...)` context manager here to exercise
            the Postgres-available branch without real infrastructure),
            `None` to force the degraded branch, or the default sentinel
            callable, which calls `_try_build_postgres_pool()` for a real
            attempt.

    Returns:
        A fully composed `AppContext`. Every `MemoryOrchestrator` inside
        it was constructed by a `composition_root.py` builder call in
        THIS function -- grep-verifiable (AC-023-2). Zone 2/3/5/7/8-
        manifest repositories/services are NOT in this `AppContext` any
        more -- see `make_*_dependency` below for where they are now
        constructed (per-request), each still via a `composition_root.py`
        builder call.
    """
    clock = SystemClock()
    event_bus = NoOpEventBus()

    working_repository = WorkingMemoryLRURepository(clock=clock)
    procedural_repository = InMemoryProceduralMemoryRepository(event_bus=event_bus, clock=clock)
    entity_memory_repository = EntityMemoryRepository(clock=clock, event_bus=event_bus)

    zone_repositories: dict[ZoneId, object] = {
        ZoneId.WORKING: working_repository,
        ZoneId.PROCEDURAL: procedural_repository,
        ZoneId.ENTITY: entity_memory_repository,
    }
    memory_orchestrator = composition_root.build_memory_orchestrator(
        zone_repositories=zone_repositories,
        event_bus=event_bus,
        clock=clock,
        tenant_credential_signing_key=tenant_credential_signing_key,
    )

    # mypy note: `postgres_pool`'s testing-core DI seam accepts a duck-typed
    # pool double (any object exposing `.connection(timeout=...)`), not only
    # a real `ConnectionPool` -- mirrors the same pre-existing callable-union
    # pattern `_try_build_postgres_connection`'s predecessor used for
    # `postgres_connection` (see composition_root.py's own `SqlConnection`
    # Protocol precedent for the same "structural type, not nominal" choice).
    resolved_pool = postgres_pool() if callable(postgres_pool) else postgres_pool  # type: ignore[operator,misc]
    pool: ConnectionPool | None = resolved_pool  # type: ignore[assignment]
    postgres_available = pool is not None
    breaker = composition_root.build_postgres_circuit_breaker(clock) if postgres_available else None

    object_store_dir = zone8_object_store_dir or Path(tempfile.mkdtemp(prefix="dashanan-zone8-"))

    return AppContext(
        clock=clock,
        event_bus=event_bus,
        tenant_credential_signing_key=tenant_credential_signing_key,
        jwt_signing_key=jwt_signing_key,
        working_repository=working_repository,
        procedural_repository=procedural_repository,
        entity_memory_repository=entity_memory_repository,
        memory_orchestrator=memory_orchestrator,
        postgres_available=postgres_available,
        postgres_pool=pool,
        postgres_breaker=breaker,
        zone8_object_store=LocalObjectStore(object_store_dir),
        zone8_key_store=InMemorySubjectKeyStore(),
        zone8_subject_index=InMemoryZone8SubjectIndex(),
        zone8_job_store=InMemorySubjectErasureJobStore(),
        unified_erasure_job_store=InMemorySubjectErasureJobStore(),
        zone8_erasure_marker_store=InMemoryErasureMarkerStore(),
    )


def make_pooled_connection_dependency(
    ctx: AppContext,
) -> Callable[[], Generator[psycopg.Connection | None, None, None]]:
    """Build the `get_pooled_connection` FastAPI dependency, bound to `ctx` (Section 5/5.1).

    Mirrors `api.app._require_scope`'s own "returns a bound closure"
    convention. Yields `None` when `ctx.postgres_available` is `False`
    (Postgres never configured -- the pre-existing graceful-degradation
    branch, unchanged); every `make_*_dependency` provider below treats a
    `None` connection the same way the old code treated
    `ctx.postgres_available is False`.

    When Postgres IS configured, this is where the breaker's `allow()`
    check happens BEFORE `pool.connection(...)` (Section 5.1: "checked
    before `pool.connection(...)` is called at all"), and where
    `record_success`/`record_failure` are called -- scoped to ONLY the
    acquisition step itself (via manual `__enter__`/`__exit__`, not the
    whole `with` body), so a query-level error raised by caller code after
    the connection is yielded never reaches the breaker (Section 5.1's
    failure predicate: "never a query-level error on an already-open
    connection").

    Raises:
        PostgresUnavailableError: If the breaker is OPEN (Section 5.1).
        PostgresPoolExhaustedError: If `pool.connection(timeout=...)`
            raises `psycopg_pool.PoolTimeout` (Section 5).
    """

    def get_pooled_connection() -> Generator[psycopg.Connection | None, None, None]:
        if not ctx.postgres_available:
            yield None
            return

        pool = ctx.postgres_pool
        breaker = ctx.postgres_breaker
        assert pool is not None and breaker is not None  # invariant: see AppContext docstring

        if not breaker.allow():
            raise PostgresUnavailableError(retry_after_seconds=breaker.retry_after_seconds())

        started_at = time.perf_counter()
        try:
            connection_ctx = pool.connection(timeout=POOL_ACQUIRE_TIMEOUT_SECONDS)
            connection = connection_ctx.__enter__()
        except PoolTimeout as exc:
            raise PostgresPoolExhaustedError() from exc
        except psycopg.Error:
            breaker.record_failure(time.perf_counter() - started_at)
            raise
        else:
            breaker.record_success(time.perf_counter() - started_at)

        try:
            yield connection
        finally:
            connection_ctx.__exit__(None, None, None)

    return get_pooled_connection


def make_provenance_repository_dependency(
    get_pooled_connection: Callable[[], Generator[psycopg.Connection | None, None, None]],
    ctx: AppContext,
) -> Callable[..., ProvenanceRepositoryPort | None]:
    """Build the `get_provenance_repository` dependency (Zone 7, `detect_conflicts=False`).

    Matches the pre-existing `ctx.provenance_repository` construction
    exactly (`composition_root.build_provenance_repository(connection,
    clock, detect_conflicts=False)`) -- only WHEN it is constructed
    changed (per-request instead of at startup), never HOW. Returns
    `ProvenanceRepositoryPort` (not the concrete `SqlProvenanceRepository`)
    because `build_provenance_repository` returns the bare adapter OR a
    `ConflictDetectingProvenanceRepository`-wrapped one depending on
    `detect_conflicts` -- the same return type its own signature declares.
    """

    def get_provenance_repository(
        connection: psycopg.Connection | None = Depends(get_pooled_connection),
    ) -> ProvenanceRepositoryPort | None:
        if connection is None:
            return None
        return composition_root.build_provenance_repository(
            connection, ctx.clock, detect_conflicts=False
        )

    return get_provenance_repository


def make_episodic_repository_dependency(
    get_pooled_connection: Callable[[], Generator[psycopg.Connection | None, None, None]],
    ctx: AppContext,
) -> Callable[..., composition_root.SqlEpisodicRepository | None]:
    """Build the `get_episodic_repository` dependency (Zone 2).

    Matches the pre-existing `ctx.episodic_repository` construction
    exactly (`composition_root.build_episodic_repository(connection,
    clock)`).
    """

    def get_episodic_repository(
        connection: psycopg.Connection | None = Depends(get_pooled_connection),
    ) -> composition_root.SqlEpisodicRepository | None:
        if connection is None:
            return None
        return composition_root.build_episodic_repository(connection, ctx.clock)

    return get_episodic_repository


def make_semantic_repository_dependency(
    get_pooled_connection: Callable[[], Generator[psycopg.Connection | None, None, None]],
    ctx: AppContext,
) -> Callable[..., ConflictAwareSemanticRepository | None]:
    """Build the `get_semantic_repository` dependency (Zone 3, FR-013-swept).

    Matches the pre-existing `ctx.semantic_repository` construction
    exactly (`composition_root.build_conflict_aware_semantic_repository
    (connection, connection, clock, detect_conflicts=True)` -- the SAME
    pooled connection serves both Zone 3's own writes and the FR-013
    sweep's Zone 7 reads within one request, per this request's single
    checked-out connection, module docstring).
    """

    def get_semantic_repository(
        connection: psycopg.Connection | None = Depends(get_pooled_connection),
    ) -> ConflictAwareSemanticRepository | None:
        if connection is None:
            return None
        return composition_root.build_conflict_aware_semantic_repository(
            connection, connection, ctx.clock, detect_conflicts=True
        )

    return get_semantic_repository


def make_conflict_aware_entity_repository_dependency(
    get_pooled_connection: Callable[[], Generator[psycopg.Connection | None, None, None]],
    ctx: AppContext,
) -> Callable[..., ConflictAwareEntityMemoryRepository | None]:
    """Build the `get_conflict_aware_entity_repository` dependency (Zone 5, FR-013-swept).

    Matches the pre-existing `ctx.conflict_aware_entity_repository`
    construction exactly (`composition_root.
    build_conflict_aware_entity_memory_repository(entity_memory_
    repository, connection, clock, detect_conflicts=True)`) --
    `entity_memory_repository` remains the startup-constructed Shape A
    singleton (must-not-deviate: no zone repository's own constructor
    changes); only the FR-013 sweep's Zone 7 connection is per-request.
    """

    def get_conflict_aware_entity_repository(
        connection: psycopg.Connection | None = Depends(get_pooled_connection),
    ) -> ConflictAwareEntityMemoryRepository | None:
        if connection is None:
            return None
        return composition_root.build_conflict_aware_entity_memory_repository(
            ctx.entity_memory_repository, connection, ctx.clock, detect_conflicts=True
        )

    return get_conflict_aware_entity_repository


def make_manifest_repository_dependency(
    get_pooled_connection: Callable[[], Generator[psycopg.Connection | None, None, None]],
) -> Callable[..., SqlManifestRepository | None]:
    """Build the `get_manifest_repository` dependency (Zone 8's manifest table)."""

    def get_manifest_repository(
        connection: psycopg.Connection | None = Depends(get_pooled_connection),
    ) -> SqlManifestRepository | None:
        if connection is None:
            return None
        return SqlManifestRepository(connection=connection)

    return get_manifest_repository


def make_zone8_consolidation_store_dependency(
    get_manifest_repository: Callable[..., SqlManifestRepository | None],
    ctx: AppContext,
) -> Callable[..., Zone8ConsolidationStore | None]:
    """Build the `get_zone8_consolidation_store` dependency.

    `object_store` is `ctx.zone8_object_store` -- the startup-constructed
    singleton (module docstring: Zone 8's in-memory/on-disk bookkeeping is
    NOT per-request).
    """

    def get_zone8_consolidation_store(
        manifest: SqlManifestRepository | None = Depends(get_manifest_repository),
    ) -> Zone8ConsolidationStore | None:
        if manifest is None:
            return None
        return Zone8ConsolidationStore(
            object_store=ctx.zone8_object_store, manifest=manifest, clock=ctx.clock
        )

    return get_zone8_consolidation_store


def make_subject_erasure_service_dependency(
    get_zone8_consolidation_store: Callable[..., Zone8ConsolidationStore | None],
    ctx: AppContext,
) -> Callable[..., SubjectErasureCascadeService | None]:
    """Build the `get_subject_erasure_service` dependency (AC-023-3's Zone-8-only cascade).

    `key_store`/`subject_index`/`job_store` are `ctx.zone8_key_store`/
    `ctx.zone8_subject_index`/`ctx.zone8_job_store` -- startup-constructed
    singletons. `job_store` in particular MUST be a singleton, not
    per-request: an erasure job is created in one request and polled for
    status in a later one (module docstring).
    """

    def get_subject_erasure_service(
        zone8_store: Zone8ConsolidationStore | None = Depends(get_zone8_consolidation_store),
    ) -> SubjectErasureCascadeService | None:
        if zone8_store is None:
            return None
        archiver = Zone8SubjectKeyedArchiver(
            zone8_store=zone8_store,
            key_store=ctx.zone8_key_store,
            subject_index=ctx.zone8_subject_index,
        )
        return SubjectErasureCascadeService(
            archiver=archiver, job_store=ctx.zone8_job_store, clock=ctx.clock
        )

    return get_subject_erasure_service


def make_unified_subject_erasure_orchestrator_dependency(
    get_pooled_connection: Callable[[], Generator[psycopg.Connection | None, None, None]],
    get_zone8_consolidation_store: Callable[..., Zone8ConsolidationStore | None],
    ctx: AppContext,
) -> Callable[..., UnifiedSubjectErasureOrchestrator | None]:
    """Build the `get_unified_subject_erasure_orchestrator` dependency (DASH-STORY-027-DEV).

    Replaces `SubjectErasureCascadeService` as `eraseSubject`'s own
    dependency (AC-013's fuller cascade), without touching
    `make_subject_erasure_service_dependency`/`SubjectErasureCascadeService`
    themselves -- both remain fully intact and unregressed for any other
    caller (must-not-deviate, DEFINITION OF DONE).

    Composed per request from the SAME pooled connection every other
    per-request dependency above uses:
      - `zone3_repository`: a raw `SqlSemanticRepository(connection=...)`
        -- `SqlSemanticRepository`/`EntityMemoryRepository` are not among
        AC-023-2's three named types (module docstring), so a direct
        construction here (not through a `composition_root` builder)
        matches this module's own established precedent for those two
        types exactly.
      - `zone5_repository`: `ctx.entity_memory_repository`, the SAME
        startup-constructed Shape A singleton `make_conflict_aware_
        entity_repository_dependency` already uses -- one Zone 5 storage
        adapter, never a second instance.
      - `subject_to_item_index`: a real `SqlSubjectToItemIndexRepository
        (connection)` (replacing `NullSubjectToItemIndex`), giving the
        Zone 2/6 leg a genuine subject_id -> item_id[] resolution for the
        first time (AC-027-DEV-1).
      - `zone2_6_cascade`: left `None`. DISCLOSED GAP, pre-existing and
        not introduced by this story: this composition root wires no
        `VectorIndexPort`/`LexicalIndexPort` adapter anywhere (grep-
        verifiable), so `CrossZoneDpdpErasureCascade`'s own
        `RetrievalIndexEviction` collaborator has nothing real to compose
        against yet. `SqlZone2ComplianceEviction` (this story's own real,
        tested `Zone2EvictionPort` adapter) is ready to be composed into
        `CrossZoneDpdpErasureCascade` the moment a future story wires
        Zone 6's own ports here -- see this story's dev report,
        judgment-call list.
      - `zone1_erasure`: left `None` (`NullZone1ErasureLeg` default) --
        `Zone1ErasureCapable`'s own docstring explains the disclosed,
        unfabricated reason (`WorkingItem` carries no subject reference).
      - `zone8_durable_coordinator`: a `DurableZone8ErasureCoordinator`
        composed with the SAME `ctx.zone8_key_store`/`ctx.zone8_subject_
        index` singletons `Zone8SubjectKeyedArchiver` itself uses (no
        second destroy mechanism), `SqlErasureMarkerStore(connection)` for
        the write-ahead marker, and `connection` again for the finalize
        step's Zone 7 promotion + `subject_item_index` cleanup (design
        doc Section 7's "same finalize transaction").
      - `job_store`: `ctx.unified_erasure_job_store` -- a SEPARATE
        singleton from `ctx.zone8_job_store` (orchestrator module
        docstring's own "SEPARATE job from Zone 8's own internal job
        store" requirement).

    Returns `None` when Postgres is unavailable -- the pre-existing
    graceful-degradation branch every sibling `make_*_dependency` factory
    already implements; `eraseSubject` maps that to the documented
    `503 ServiceDegraded` response, unchanged.
    """

    def get_unified_subject_erasure_orchestrator(
        connection: psycopg.Connection | None = Depends(get_pooled_connection),
        zone8_store: Zone8ConsolidationStore | None = Depends(get_zone8_consolidation_store),
    ) -> UnifiedSubjectErasureOrchestrator | None:
        if connection is None or zone8_store is None:
            return None

        archiver = Zone8SubjectKeyedArchiver(
            zone8_store=zone8_store,
            key_store=ctx.zone8_key_store,
            subject_index=ctx.zone8_subject_index,
        )
        zone8_service = SubjectErasureCascadeService(
            archiver=archiver, job_store=ctx.zone8_job_store, clock=ctx.clock
        )
        durable_coordinator = DurableZone8ErasureCoordinator(
            key_store=ctx.zone8_key_store,
            subject_index=ctx.zone8_subject_index,
            marker_store=SqlErasureMarkerStore(connection),
            provenance_connection=connection,
            clock=ctx.clock,
        )
        return UnifiedSubjectErasureOrchestrator(
            zone8_service=zone8_service,
            zone3_repository=SqlSemanticRepository(connection=connection),
            zone5_repository=ctx.entity_memory_repository,
            job_store=ctx.unified_erasure_job_store,
            clock=ctx.clock,
            zone2_6_cascade=None,
            subject_to_item_index=SqlSubjectToItemIndexRepository(connection),
            zone8_durable_coordinator=durable_coordinator,
        )

    return get_unified_subject_erasure_orchestrator
