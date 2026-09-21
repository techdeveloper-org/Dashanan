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

Known, flagged gap (never fabricated, `AR1-S3-G3`): a single shared
`psycopg.Connection` is reused across Zone 2/3/7/8-manifest for this
story's scope. `psycopg.Connection` is not safe for concurrent use across
threads without its own pooling; a Shape B production deployment needs a
real connection pool (`psycopg_pool`), which is explicitly FR-015 scope,
not this story's. This module's own single-connection choice is
sufficient to prove AC-023-1/AC-023-2/AC-023-3 for Sprint 3 and is named
here rather than silently presented as production-ready.
"""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path

import psycopg

from dashanan.application.conflict_aware_zone_writes import (
    ConflictAwareEntityMemoryRepository,
    ConflictAwareSemanticRepository,
)
from dashanan.application.memory_orchestrator import MemoryOrchestrator
from dashanan.application.subject_erasure_cascade import SubjectErasureCascadeService
from dashanan.application.zone8_crypto_shredding_store import Zone8SubjectKeyedArchiver
from dashanan.application.zone8_consolidation_store import Zone8ConsolidationStore
from dashanan.domain.ports import Clock, EventBus
from dashanan.domain.zone import ZoneId
from dashanan.infrastructure import composition_root
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
from dashanan.infrastructure.sql_provenance_repository import SqlProvenanceRepository
from dashanan.infrastructure.system_clock import SystemClock
from dashanan.infrastructure.working_memory_lru_repository import WorkingMemoryLRURepository

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AppContext:
    """Every real, composition-root-built collaborator this host's handlers call.

    `postgres_available` is the single flag every Postgres-dependent
    handler checks before touching `provenance_repository`/
    `episodic_repository`/`semantic_repository`/`subject_erasure_service`
    (each `None` when `postgres_available` is `False`) -- handlers never
    guess; they check this flag and return the documented degraded
    response when it is `False`.
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
    provenance_repository: SqlProvenanceRepository | None
    episodic_repository: composition_root.SqlEpisodicRepository | None
    semantic_repository: ConflictAwareSemanticRepository | None
    conflict_aware_entity_repository: ConflictAwareEntityMemoryRepository | None
    subject_erasure_service: SubjectErasureCascadeService | None


def _try_build_postgres_connection() -> psycopg.Connection | None:
    """Attempt `composition_root.build_postgres_connection()`; `None` on any failure.

    Returns `None` -- never raises -- for `SettingsConfigurationError`
    (env not configured) or `psycopg.Error` (host unreachable, auth
    rejected, database absent): both mean "Shape B is not available in
    this deployment right now," which this host must degrade gracefully
    for, not crash on (module docstring, Shape A/Shape B split).
    """
    try:
        return composition_root.build_postgres_connection()
    except SettingsConfigurationError as exc:
        logger.warning(
            "Postgres settings not configured; Zone 2/3/7/8-manifest "
            "operationIds will report degraded/503",
            extra={"reason": str(exc)},
        )
        return None
    except psycopg.Error as exc:
        logger.warning(
            "Postgres unreachable; Zone 2/3/7/8-manifest operationIds "
            "will report degraded/503",
            extra={"reason": str(exc)},
        )
        return None


def build_app_context(
    *,
    tenant_credential_signing_key: bytes,
    jwt_signing_key: bytes,
    zone8_object_store_dir: Path | None = None,
    postgres_connection: psycopg.Connection | None | object = _try_build_postgres_connection,
) -> AppContext:
    """Build the one `AppContext` this host's `app.py` depends on (AC-023-2, AC-023-3).

    Args:
        tenant_credential_signing_key: Forwarded unchanged to
            `composition_root.build_memory_orchestrator` -- never
            resolved a second time here (single source of truth).
        jwt_signing_key: This host's own JWT verification key
            (`api.security`); unrelated to `tenant_credential_signing_key`
            (see `api.security`'s own "Key separation" docstring).
        zone8_object_store_dir: Where Zone 8's `LocalObjectStore` writes.
            Defaults to a fresh temp directory (testing-core DI seam).
        postgres_connection: Either a pre-built connection (testing-core
            DI -- a test passes a fake `SqlConnection` double here to
            exercise the Postgres-available branch without real
            infrastructure), `None` to force the degraded branch, or the
            default sentinel callable, which calls
            `_try_build_postgres_connection()` for a real attempt.

    Returns:
        A fully composed `AppContext`. Every `MemoryOrchestrator`/
        `SqlProvenanceRepository`/`SqlEpisodicRepository` inside it was
        constructed by a `composition_root.py` builder call in THIS
        function -- grep-verifiable (AC-023-2).
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

    connection = (
        postgres_connection() if callable(postgres_connection) else postgres_connection
    )

    provenance_repository: SqlProvenanceRepository | None = None
    episodic_repository: composition_root.SqlEpisodicRepository | None = None
    semantic_repository: ConflictAwareSemanticRepository | None = None
    conflict_aware_entity_repository: ConflictAwareEntityMemoryRepository | None = None
    subject_erasure_service: SubjectErasureCascadeService | None = None
    postgres_available = connection is not None

    if postgres_available:
        provenance_repository = composition_root.build_provenance_repository(
            connection, clock, detect_conflicts=False
        )
        episodic_repository = composition_root.build_episodic_repository(connection, clock)
        conflict_aware_semantic = composition_root.build_conflict_aware_semantic_repository(
            connection, connection, clock, detect_conflicts=True
        )
        semantic_repository = conflict_aware_semantic
        conflict_aware_entity_repository = (
            composition_root.build_conflict_aware_entity_memory_repository(
                entity_memory_repository, connection, clock, detect_conflicts=True,
            )
        )

        object_store_dir = zone8_object_store_dir or Path(tempfile.mkdtemp(prefix="dashanan-zone8-"))
        object_store = LocalObjectStore(object_store_dir)
        manifest_repository = SqlManifestRepository(connection=connection)
        zone8_store = Zone8ConsolidationStore(
            object_store=object_store, manifest=manifest_repository, clock=clock
        )
        archiver = Zone8SubjectKeyedArchiver(
            zone8_store=zone8_store,
            key_store=InMemorySubjectKeyStore(),
            subject_index=InMemoryZone8SubjectIndex(),
        )
        subject_erasure_service = SubjectErasureCascadeService(
            archiver=archiver,
            job_store=InMemorySubjectErasureJobStore(),
            clock=clock,
        )

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
        provenance_repository=provenance_repository,
        episodic_repository=episodic_repository,
        semantic_repository=semantic_repository,
        conflict_aware_entity_repository=conflict_aware_entity_repository,
        subject_erasure_service=subject_erasure_service,
    )
