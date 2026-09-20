"""Cross-zone integration fixture composition (DASHANAN, Sprint 2, Zones 3/4/5/8).

Mirrors `tests/integration/conftest.py`'s (Sprint 1) own composition-root
pattern exactly -- real adapters wherever a production adapter exists,
real hand-rolled stateful fakes (never `unittest.mock`) for the local
Protocols this codebase has not yet built a concrete adapter for, one
shared `SeedableClock` + `RecordingEventBus` per wired system, and the
same `RecordingConnection` fake DB-API 2.0 double Sprint 1 already
established (`tests.test_smoke_episodic.RecordingConnection`), reused
directly where its shape already fits (Zone 3, no `commit()` needed) and
mirrored with one added `commit()` method where a SQL adapter's own
contract requires it (Zone 8's `SqlManifestRepository`).

This module does NOT modify `tests/integration/conftest.py` (Sprint 1's
own file) in any way -- it only imports `SeedableClock`/`RecordingEventBus`
from it (module docstring, Sprint 1: "every composed component takes the
SAME instance") and otherwise stands alongside it as an ADDITIONAL,
independent fixture module. Because pytest only auto-discovers a file
literally named `conftest.py`, this module is registered as a plugin by
`tests/integration/test_smoke_fixture_wiring_sprint2.py`'s own
`pytest_plugins` declaration -- never by renaming this file, never by
editing Sprint 1's `conftest.py`.

Composition-root choices this fixture makes (documented once here,
mirroring Sprint 1's own docstring convention):

  - Zone 3 (`ZoneId.SEMANTIC`): `SqlSemanticRepository` -- a real,
    Shape-B-style SQL adapter -- composed against a plain
    `RecordingConnection` (Sprint 1's fake DB-API double, reused
    unmodified: `SqlSemanticRepository` never calls `connection.commit()`,
    so the Sprint 1 fake's shape is already schema-compatible, no
    Zone-3-specific connection double is needed), then wrapped by the real
    `Zone3IndexProjector` decorator (DASH-STORY-014) for its Zone 6
    best-effort projection-publish behavior.
  - Zone 4 (`ZoneId.PROCEDURAL`): `InMemoryProceduralMemoryRepository` --
    the real Shape A adapter -- plus real, hand-rolled stateful fakes for
    the four local Protocols `zone4_capacity_backstop_sweep.py` itself
    defines with no concrete production adapter yet
    (`Zone4BackstopConfigPort`, `Zone4CandidateSource`, `Zone4EvictionPort`,
    `Zone4RetrievalIndexEvictionPort`) -- the exact same situation Sprint 1
    had for Zone 2's own backstop ports in `conftest.py`
    (`InMemoryZone2BackstopConfigPort`/`InMemoryZone2Store`), mirrored here
    class-for-class rather than duplicated by inheritance (Zone 4's own
    candidate/config shapes are structurally distinct types).
  - Zone 5 (`ZoneId.ENTITY`): `EntityMemoryRepository` -- real, and (module
    docstring) already carries its own per-tenant `threading.Lock`
    internally, so no additional fake or wrapper is composed around it.
  - Zone 8 (`ZoneId.CONSOLIDATION`): `LocalObjectStore` (real, filesystem-
    backed, rooted under one `tmp_path` subdirectory per fixture) +
    `SqlManifestRepository` (real SQL adapter, composed against a NEW
    `RecordingConnectionWithCommit` -- Sprint 1's own `RecordingConnection`
    mirrored with one added `commit()` no-op, since `SqlManifestRepository.
    insert_batch` requires it and no existing Sprint 1 SQL adapter ever
    called it) -- both composed into the real `Zone8ConsolidationStore`
    Facade. `InMemorySubjectKeyStore` + `InMemoryZone8SubjectIndex` (real
    Shape A adapters) are then composed, with that Facade, into the real
    `Zone8SubjectKeyedArchiver` crypto-shredding decorator. `ArchiveEngine`
    (real) is wired to real, hand-rolled stateful fakes for
    `ArchiveCandidatePort`/`ArchiveTransitionPort` -- `archive_engine.py`'s
    own module docstring records no concrete production adapter exists yet
    for either Protocol. `LocalTenantBucketProvisioner` (real, filesystem-
    backed under its own `tmp_path` subdirectory) is composed into the
    real `TenantBucketProvisioningService` Facade.
    `InMemorySubjectErasureJobStore` (real Shape A adapter) is composed,
    with the crypto-shredding archiver, into the real
    `SubjectErasureCascadeService` Facade.

Runtime assumptions recorded per rule 33/40 test-roadmap conventions
(mirrors Sprint 1's own `conftest.py` docstring, identical values):
  - clock: `SeedableClock` (imported from Sprint 1's `conftest.py`),
    seeded at `FIXED_EPOCH` (2026-01-01T00:00:00+00:00) unless a test
    explicitly seeds or advances it.
  - tenant_id: `DEFAULT_TENANT_ID` ("tenant-1"), imported from Sprint 1's
    `conftest.py`, is the default every seed helper below uses.
  - No fixture seed is random; nothing in this module calls `random` or
    generates a UUID for an identifier a test would need to match against.

PII NOTE: every seed helper and in-memory store below accepts only
control/ranking/routing metadata (item_id, tenant_id, scores, states,
timestamps, byte offsets) or the literal `PLACEHOLDER_PAYLOAD` constant
(imported from Sprint 1's `conftest.py`) for the one or two components
that require a non-empty payload -- mirroring Sprint 1's own identical PII
posture. No example conversational content appears anywhere in this file.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pytest

from dashanan.application.archive_engine import (
    ArchiveCandidateSnapshot,
    ArchiveEngine,
    ArchiveEnginePortError,
    ArchiveMarkResult,
)
from dashanan.application.subject_erasure_cascade import SubjectErasureCascadeService
from dashanan.application.tenant_bucket_provisioner import TenantBucketProvisioningService
from dashanan.application.zone3_index_projection import Zone3IndexProjector
from dashanan.application.zone4_capacity_backstop_sweep import Zone4CapacityBackstopSweep
from dashanan.application.zone8_consolidation_store import Zone8ConsolidationStore
from dashanan.application.zone8_crypto_shredding_store import Zone8SubjectKeyedArchiver
from dashanan.domain.zone import ZoneId
from dashanan.domain.zone4_capacity_backstop import (
    ProcedureEvictionCandidate,
    Zone4BackstopConfig,
)
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
from dashanan.infrastructure.local_tenant_bucket_provisioner import (
    LocalTenantBucketProvisioner,
)
from dashanan.infrastructure.sql_manifest_repository import SqlManifestRepository
from dashanan.infrastructure.sql_semantic_repository import SqlSemanticRepository

from tests.integration.conftest import RecordingEventBus, SeedableClock
from tests.test_smoke_episodic import RecordingConnection


class RecordingCursorWithFetchone:
    """Sprint 1's `RecordingCursor` (`tests.test_smoke_episodic`), mirrored with `fetchone()`.

    `SqlManifestRepository.find_by_item_id` calls `cursor.fetchone()`
    (`sql_manifest_repository.SqlCursor`'s own Protocol -- a narrower
    single-row-return shape than every Sprint 1 SQL adapter's `fetchall()`-
    only `SqlCursor` Protocol), so Sprint 1's own `RecordingCursor` cannot
    satisfy it structurally. Mirrored here (not subclassed: Sprint 1's
    `RecordingConnection.__init__` hardcodes the concrete `RecordingCursor`
    type for its own `cursor_obj`, so there is no seam to override just the
    cursor class by inheritance alone) -- `execute`/`fetchall`/`executed`
    recording behavior is copied verbatim from Sprint 1's own class.
    """

    def __init__(self, rows: list[tuple[object, ...]] | None = None) -> None:
        self.executed: list[tuple[str, tuple[object, ...]]] = []
        self._rows = rows or []
        self._raise: Exception | None = None

    def execute(self, sql: str, params: object) -> None:
        """Record `(sql, params)`. Mirrors `RecordingCursor.execute` verbatim."""
        self.executed.append((sql, tuple(params)))
        if self._raise is not None:
            raise self._raise

    def fetchall(self) -> list[tuple[object, ...]]:
        """Return every canned row. Mirrors `RecordingCursor.fetchall` verbatim."""
        return list(self._rows)

    def fetchone(self) -> tuple[object, ...] | None:
        """Return the first canned row, or `None` -- `SqlManifestRepository`'s own need."""
        return self._rows[0] if self._rows else None


class RecordingConnectionWithCommit:
    """Sprint 1's `RecordingConnection` fake, mirrored with `commit()` + a fetchone-capable cursor.

    `SqlManifestRepository.insert_batch` (module docstring: "`commit` is
    required... `insert_batch` issues every row's INSERT on the same
    connection and commits once at the end") is the first Sprint 2 SQL
    adapter to call `connection.commit()`, and `find_by_item_id` is the
    first to call `cursor.fetchone()` -- neither exists on Sprint 1's own
    `RecordingConnection`/`RecordingCursor` pair (`tests.test_smoke_episodic`),
    so this class mirrors that pair's exact `cursor()`/`cursor_obj` shape
    (per DRY, this module's own docstring: reused where the shape already
    fits, mirrored -- never re-derived -- where it does not) with the two
    additions `SqlManifestRepository`'s own `SqlConnection`/`SqlCursor`
    Protocols require.
    """

    def __init__(self, rows: list[tuple[object, ...]] | None = None) -> None:
        self.cursor_obj = RecordingCursorWithFetchone(rows)
        self.commit_calls: int = 0

    def cursor(self) -> RecordingCursorWithFetchone:
        """Return the one shared cursor, mirrors `RecordingConnection.cursor` verbatim."""
        return self.cursor_obj

    def commit(self) -> None:
        """Record that a commit happened. No real transaction exists to commit."""
        self.commit_calls += 1


class InMemoryZone4BackstopConfigPort:
    """Real, in-memory `Zone4BackstopConfigPort`: per-tenant, live-mutable config.

    Mirrors `tests.integration.conftest.InMemoryZone2BackstopConfigPort`'s
    identical shape and rationale (own docstring: `Zone4CapacityBackstopSweep.
    run` calls `.load(tenant_id)` fresh on every sweep, must-not-deviate item
    1) -- a distinct class because `Zone4BackstopConfig`'s own HLD 12A
    defaults (cap 50,000 / MaxAge 730 days) differ from Zone 2's (cap
    100,000 / MaxAge 180 days), not because the shape itself differs.
    """

    _DEFAULT_CAPACITY_CAP = 50_000
    """HLD Section 12A Zone 4 row: "Capacity cap (per tenant): 50,000 procedures"."""

    _DEFAULT_MAX_AGE_SECONDS = 730 * 24 * 60 * 60
    """HLD Section 12A Zone 4 row: "MaxAge (forced archive): 730 days"."""

    def __init__(self) -> None:
        self._configs: dict[str, Zone4BackstopConfig] = {}
        self.load_calls: list[str] = []

    def set_config(self, tenant_id: str, config: Zone4BackstopConfig) -> None:
        """Set (or replace) `tenant_id`'s current cap/MaxAge config."""
        self._configs[tenant_id] = config

    def load(self, tenant_id: str) -> Zone4BackstopConfig:
        """`Zone4BackstopConfigPort.load`: the HLD 12A default unless overridden."""
        self.load_calls.append(tenant_id)
        return self._configs.get(
            tenant_id,
            Zone4BackstopConfig(
                capacity_cap=self._DEFAULT_CAPACITY_CAP,
                max_age_seconds=self._DEFAULT_MAX_AGE_SECONDS,
            ),
        )


class InMemoryZone4Store:
    """Real, in-memory `Zone4CandidateSource` + `Zone4EvictionPort` combined.

    Mirrors `tests.integration.conftest.InMemoryZone2Store`'s identical
    "one stateful store backs both related ports" shape, keyed on Zone 4's
    own `ProcedureEvictionCandidate` type rather than Zone 2's
    `EvictionCandidate` (deliberately decoupled types, per
    `zone4_capacity_backstop.py`'s own module docstring).
    """

    def __init__(self) -> None:
        self._candidates: dict[str, dict[str, ProcedureEvictionCandidate]] = {}
        self.fetch_calls: list[tuple[str, datetime]] = []
        self.evicted: list[tuple[str, str]] = []

    def seed_candidate(
        self, tenant_id: str, candidate: ProcedureEvictionCandidate
    ) -> None:
        """Add (or replace) one candidate for `tenant_id`, keyed by `candidate.item_id`."""
        self._candidates.setdefault(tenant_id, {})[candidate.item_id] = candidate

    def fetch_candidates(
        self, tenant_id: str, as_of: datetime
    ) -> list[ProcedureEvictionCandidate]:
        """`Zone4CandidateSource.fetch_candidates`: every currently-seeded candidate."""
        self.fetch_calls.append((tenant_id, as_of))
        return list(self._candidates.get(tenant_id, {}).values())

    def evict(self, tenant_id: str, item_id: str) -> None:
        """`Zone4EvictionPort.evict`: remove `item_id` from this store's own state."""
        self.evicted.append((tenant_id, item_id))
        self._candidates.get(tenant_id, {}).pop(item_id, None)


class InMemoryZone4RetrievalIndexEviction:
    """Real, in-memory `Zone4RetrievalIndexEvictionPort` (Zone 6 projection cleanup).

    `zone4_capacity_backstop_sweep.py`'s own module docstring ("Zone 6
    projection cleanup" section) requires this port so an ordinary Zone 4
    eviction also reaches Zone 6 -- no concrete production adapter of this
    exact Protocol exists yet, so this fixture composes a small, real,
    stateful double (never `unittest.mock`) that simply records every
    `delete_item` call, idempotently.
    """

    def __init__(self) -> None:
        self.deleted: list[tuple[str, str]] = []

    def delete_item(self, tenant_id: str, item_id: str) -> None:
        """`Zone4RetrievalIndexEvictionPort.delete_item`: record the removal, idempotently."""
        self.deleted.append((tenant_id, item_id))


class InMemoryArchiveCandidateStore:
    """Real, in-memory `ArchiveCandidatePort` (`archive_engine.py`).

    `ArchiveEngine._evaluate_and_mark` calls `.current_state(...)` fresh
    per candidate, per sweep tick (AC-008-ROT-4's non-stale requirement) --
    this store lets a test seed one item's `(RotationState, memory_score,
    payload_tokens)` snapshot with `seed_item` and have the engine read
    exactly that snapshot back, mirroring
    `tests.integration.conftest.InMemoryRotationLifecycleStore`'s identical
    "seed then re-score" shape for the sibling `RotationEngine`.
    """

    def __init__(self) -> None:
        self._snapshots: dict[tuple[str, str, str], ArchiveCandidateSnapshot] = {}
        self.current_state_calls: list[tuple[str, ZoneId, str]] = []

    def seed_item(
        self,
        tenant_id: str,
        zone_id: ZoneId,
        item_id: str,
        *,
        snapshot: ArchiveCandidateSnapshot,
    ) -> None:
        """Set `(tenant_id, zone_id, item_id)`'s snapshot for the next `current_state` read."""
        self._snapshots[(tenant_id, zone_id.value, item_id)] = snapshot

    def current_state(
        self, tenant_id: str, zone_id: ZoneId, item_id: str
    ) -> ArchiveCandidateSnapshot:
        """`ArchiveCandidatePort.current_state`: return this item's seeded/current snapshot.

        Raises:
            ArchiveEnginePortError: If this exact key was never seeded --
                mirrors `InMemoryRotationLifecycleStore.current_state`'s
                identical "item no longer exists" operational failure.
        """
        self.current_state_calls.append((tenant_id, zone_id, item_id))
        key = (tenant_id, zone_id.value, item_id)
        if key not in self._snapshots:
            raise ArchiveEnginePortError(
                f"no archive candidate snapshot seeded for tenant={tenant_id!r} "
                f"zone={zone_id.value!r} item={item_id!r}"
            )
        return self._snapshots[key]


class InMemoryArchiveTransitionStore:
    """Real, in-memory `ArchiveTransitionPort` (`archive_engine.py`).

    `ArchiveEngine._evaluate_and_mark` calls `.mark_archived(...)` once a
    candidate passes the eligibility + guarded-transition checks, and needs
    back the item's already-Compressed payload bytes plus its compression
    generation to hand to `Zone8ConsolidationStore.consolidate_batch`. This
    store lets a test seed those two values per item via `seed_payload`.
    """

    def __init__(self) -> None:
        self._payloads: dict[tuple[str, str, str], tuple[bytes, int]] = {}
        self.mark_archived_calls: list[tuple[str, ZoneId, str]] = []

    def seed_payload(
        self,
        tenant_id: str,
        zone_id: ZoneId,
        item_id: str,
        *,
        payload: bytes,
        generation: int = 0,
    ) -> None:
        """Set `(tenant_id, zone_id, item_id)`'s already-Compressed payload and generation."""
        self._payloads[(tenant_id, zone_id.value, item_id)] = (payload, generation)

    def mark_archived(
        self, tenant_id: str, zone_id: ZoneId, item_id: str
    ) -> ArchiveMarkResult:
        """`ArchiveTransitionPort.mark_archived`: return this item's seeded payload/generation.

        Raises:
            ArchiveEnginePortError: If this exact key was never seeded via
                `seed_payload`.
        """
        self.mark_archived_calls.append((tenant_id, zone_id, item_id))
        key = (tenant_id, zone_id.value, item_id)
        if key not in self._payloads:
            raise ArchiveEnginePortError(
                f"no payload seeded for tenant={tenant_id!r} zone={zone_id.value!r} "
                f"item={item_id!r}"
            )
        payload, generation = self._payloads[key]
        return ArchiveMarkResult(payload=payload, generation=generation)


@pytest.fixture
def clock() -> SeedableClock:
    """The one `Clock` every fixture below shares -- Sprint 1's own class, a fresh instance."""
    return SeedableClock()


@pytest.fixture
def event_bus() -> RecordingEventBus:
    """The one `EventBus` every fixture below shares -- Sprint 1's own class, a fresh instance."""
    return RecordingEventBus()


@pytest.fixture
def zone3_connection() -> RecordingConnection:
    """The fake DB-API 2.0 connection `SqlSemanticRepository` issues queries against.

    Reuses Sprint 1's own `RecordingConnection` (`tests.test_smoke_episodic`)
    directly, unmodified: `SqlSemanticRepository` never calls `connection.
    commit()` (module docstring: no such requirement on its local
    `SqlConnection` Protocol), so Sprint 1's fake is already schema-
    compatible and no Zone-3-specific connection double is needed.
    """
    return RecordingConnection()


@pytest.fixture
def zone3_semantic_repository(
    zone3_connection: RecordingConnection,
) -> SqlSemanticRepository:
    """Zone 3 (`ZoneId.SEMANTIC`): the real SQL adapter over `zone3_connection`."""
    return SqlSemanticRepository(connection=zone3_connection)


@pytest.fixture
def zone3_index_projector(
    zone3_semantic_repository: SqlSemanticRepository,
    event_bus: RecordingEventBus,
) -> Zone3IndexProjector:
    """Zone 3's Zone-6 projection decorator (DASH-STORY-014), wrapping the real repository."""
    return Zone3IndexProjector(sink=zone3_semantic_repository, event_bus=event_bus)


@pytest.fixture
def zone4_repository(
    event_bus: RecordingEventBus, clock: SeedableClock
) -> InMemoryProceduralMemoryRepository:
    """Zone 4 (`ZoneId.PROCEDURAL`): the real Shape A adapter (HLD 3.5, FR-004)."""
    return InMemoryProceduralMemoryRepository(event_bus=event_bus, clock=clock)


@pytest.fixture
def zone4_backstop_config_port() -> InMemoryZone4BackstopConfigPort:
    """The real, per-tenant-mutable cap/MaxAge config store (HLD 12A Zone 4 row)."""
    return InMemoryZone4BackstopConfigPort()


@pytest.fixture
def zone4_store() -> InMemoryZone4Store:
    """The real store backing both `Zone4CandidateSource` and `Zone4EvictionPort`."""
    return InMemoryZone4Store()


@pytest.fixture
def zone4_retrieval_index_eviction() -> InMemoryZone4RetrievalIndexEviction:
    """The real `Zone4RetrievalIndexEvictionPort` double (Zone 6 projection cleanup)."""
    return InMemoryZone4RetrievalIndexEviction()


@pytest.fixture
def zone4_backstop_sweep(
    zone4_backstop_config_port: InMemoryZone4BackstopConfigPort,
    zone4_store: InMemoryZone4Store,
    zone4_retrieval_index_eviction: InMemoryZone4RetrievalIndexEviction,
    event_bus: RecordingEventBus,
    clock: SeedableClock,
) -> Zone4CapacityBackstopSweep:
    """The Zone 4 OAQ-4-class capacity/MaxAge backstop sweep (DASH-STORY-016, AC-004-CAP-1)."""
    return Zone4CapacityBackstopSweep(
        config_port=zone4_backstop_config_port,
        candidate_source=zone4_store,
        eviction_port=zone4_store,
        retrieval_index_eviction=zone4_retrieval_index_eviction,
        event_bus=event_bus,
        clock=clock,
    )


@pytest.fixture
def zone5_entity_repository(
    clock: SeedableClock, event_bus: RecordingEventBus
) -> EntityMemoryRepository:
    """Zone 5 (`ZoneId.ENTITY`): the real Shape A adapter (HLD 3.6, FR-005).

    Carries its own per-tenant `threading.Lock` internally (module
    docstring) -- no additional fake or wrapper is composed around it.
    """
    return EntityMemoryRepository(clock=clock, event_bus=event_bus)


@pytest.fixture
def zone8_object_store_dir(tmp_path: Path) -> Path:
    """The filesystem root `LocalObjectStore` writes blobs under, isolated per test."""
    return tmp_path / "zone8-objects"


@pytest.fixture
def zone8_object_store(zone8_object_store_dir: Path) -> LocalObjectStore:
    """Zone 8's real, filesystem-backed `ObjectStorePort` adapter (ADR-009)."""
    return LocalObjectStore(root_dir=zone8_object_store_dir)


@pytest.fixture
def zone8_manifest_connection() -> RecordingConnectionWithCommit:
    """The fake DB-API 2.0 connection `SqlManifestRepository` issues queries against.

    A NEW connection double, distinct from `zone3_connection` (Sprint 1's
    own `conftest.py` docstring rationale, mirrored here: separate
    connections avoid one fixture's canned rows/executed-statement log
    leaking into another's) -- `RecordingConnectionWithCommit` because
    `SqlManifestRepository.insert_batch` requires `connection.commit()`.
    """
    return RecordingConnectionWithCommit()


@pytest.fixture
def zone8_manifest_repository(
    zone8_manifest_connection: RecordingConnectionWithCommit,
) -> SqlManifestRepository:
    """Zone 8's real hot-manifest-index SQL adapter (ADR-009), over `zone8_manifest_connection`."""
    return SqlManifestRepository(connection=zone8_manifest_connection)


@pytest.fixture
def zone8_consolidation_store(
    zone8_object_store: LocalObjectStore,
    zone8_manifest_repository: SqlManifestRepository,
    clock: SeedableClock,
) -> Zone8ConsolidationStore:
    """Zone 8 (`ZoneId.CONSOLIDATION`): the real object-store + hot-manifest Facade (ADR-009)."""
    return Zone8ConsolidationStore(
        object_store=zone8_object_store,
        manifest=zone8_manifest_repository,
        clock=clock,
    )


@pytest.fixture
def zone8_subject_key_store() -> InMemorySubjectKeyStore:
    """The real Shape A `SubjectKeyStorePort` adapter (DPDP-2 crypto-shredding)."""
    return InMemorySubjectKeyStore()


@pytest.fixture
def zone8_subject_index() -> InMemoryZone8SubjectIndex:
    """The real Shape A `Zone8SubjectIndexPort` adapter (ADR-006's subject_id index)."""
    return InMemoryZone8SubjectIndex()


@pytest.fixture
def zone8_subject_keyed_archiver(
    zone8_consolidation_store: Zone8ConsolidationStore,
    zone8_subject_key_store: InMemorySubjectKeyStore,
    zone8_subject_index: InMemoryZone8SubjectIndex,
) -> Zone8SubjectKeyedArchiver:
    """The real crypto-shredding decorator (DASH-STORY-020) wrapping `zone8_consolidation_store`."""
    return Zone8SubjectKeyedArchiver(
        zone8_store=zone8_consolidation_store,
        key_store=zone8_subject_key_store,
        subject_index=zone8_subject_index,
    )


@pytest.fixture
def zone8_archive_candidate_store() -> InMemoryArchiveCandidateStore:
    """The real, stateful `ArchiveCandidatePort` double `ArchiveEngine` re-scores against."""
    return InMemoryArchiveCandidateStore()


@pytest.fixture
def zone8_archive_transition_store() -> InMemoryArchiveTransitionStore:
    """The real, stateful `ArchiveTransitionPort` double `ArchiveEngine` marks items through."""
    return InMemoryArchiveTransitionStore()


@pytest.fixture
def zone8_archive_engine(
    zone8_archive_transition_store: InMemoryArchiveTransitionStore,
    zone8_archive_candidate_store: InMemoryArchiveCandidateStore,
    zone8_consolidation_store: Zone8ConsolidationStore,
    zone8_subject_index: InMemoryZone8SubjectIndex,
    event_bus: RecordingEventBus,
    clock: SeedableClock,
) -> ArchiveEngine:
    """The real `Compressed -> Archived` weekly sweep / OAQ-12 fast-track engine (DASH-STORY-019).

    Composed with the SAME `zone8_subject_index` instance
    `zone8_subject_keyed_archiver` uses (DSHN-69 fix): a weekly-sweep
    item that carries a derivable `subject_id`
    (`ArchiveCandidateSnapshot.subject_id`) is now registered into this
    shared index at archive time, so a later `erase_subject` call
    resolves it regardless of which of the two write paths archived it.
    """
    return ArchiveEngine(
        transition_port=zone8_archive_transition_store,
        candidate_port=zone8_archive_candidate_store,
        zone8_store=zone8_consolidation_store,
        event_bus=event_bus,
        clock=clock,
        subject_index=zone8_subject_index,
    )


@pytest.fixture
def zone8_bucket_root_dir(tmp_path: Path) -> Path:
    """The filesystem root `LocalTenantBucketProvisioner` writes bucket directories under."""
    return tmp_path / "zone8-buckets"


@pytest.fixture
def zone8_bucket_provisioner(
    zone8_bucket_root_dir: Path, clock: SeedableClock
) -> LocalTenantBucketProvisioner:
    """The real, filesystem-backed `BucketProvisioningPort` adapter (ADR-009 India Layer)."""
    return LocalTenantBucketProvisioner(root_dir=zone8_bucket_root_dir, clock=clock)


@pytest.fixture
def zone8_bucket_provisioning_service(
    zone8_bucket_provisioner: LocalTenantBucketProvisioner,
) -> TenantBucketProvisioningService:
    """The real Facade (AC-008-DPDP-2) over `zone8_bucket_provisioner`."""
    return TenantBucketProvisioningService(provisioning_port=zone8_bucket_provisioner)


@pytest.fixture
def zone8_subject_erasure_job_store() -> InMemorySubjectErasureJobStore:
    """The real Shape A `SubjectErasureJobStore` adapter (HLD 7.3 async admin-job pattern)."""
    return InMemorySubjectErasureJobStore()


@pytest.fixture
def zone8_subject_erasure_cascade(
    zone8_subject_keyed_archiver: Zone8SubjectKeyedArchiver,
    zone8_subject_erasure_job_store: InMemorySubjectErasureJobStore,
    clock: SeedableClock,
) -> SubjectErasureCascadeService:
    """The real DPDP admin-job Facade (AC-008-DPDP-1) over the crypto-shredding archiver."""
    return SubjectErasureCascadeService(
        archiver=zone8_subject_keyed_archiver,
        job_store=zone8_subject_erasure_job_store,
        clock=clock,
    )


@dataclass
class Sprint2WiredSystem:
    """Every composed Zone 3/4/5/8 component from this module, bundled for one test.

    Mirrors `tests.integration.conftest.WiredSystem`'s identical role and
    convention: a convenience aggregate, not a new abstraction -- every
    field here is exactly the object the matching same-named fixture above
    returns. Prefer depending on the individual fixtures directly when a
    test only needs one or two components; use `sprint2_wired_system` when
    a test genuinely exercises a cross-zone scenario touching most of the
    composition at once.
    """

    clock: SeedableClock
    event_bus: RecordingEventBus
    zone3_connection: RecordingConnection
    zone3_semantic_repository: SqlSemanticRepository
    zone3_index_projector: Zone3IndexProjector
    zone4_repository: InMemoryProceduralMemoryRepository
    zone4_backstop_config_port: InMemoryZone4BackstopConfigPort
    zone4_store: InMemoryZone4Store
    zone4_retrieval_index_eviction: InMemoryZone4RetrievalIndexEviction
    zone4_backstop_sweep: Zone4CapacityBackstopSweep
    zone5_entity_repository: EntityMemoryRepository
    zone8_object_store: LocalObjectStore
    zone8_manifest_connection: RecordingConnectionWithCommit
    zone8_manifest_repository: SqlManifestRepository
    zone8_consolidation_store: Zone8ConsolidationStore
    zone8_subject_key_store: InMemorySubjectKeyStore
    zone8_subject_index: InMemoryZone8SubjectIndex
    zone8_subject_keyed_archiver: Zone8SubjectKeyedArchiver
    zone8_archive_candidate_store: InMemoryArchiveCandidateStore
    zone8_archive_transition_store: InMemoryArchiveTransitionStore
    zone8_archive_engine: ArchiveEngine
    zone8_bucket_provisioner: LocalTenantBucketProvisioner
    zone8_bucket_provisioning_service: TenantBucketProvisioningService
    zone8_subject_erasure_job_store: InMemorySubjectErasureJobStore
    zone8_subject_erasure_cascade: SubjectErasureCascadeService


@pytest.fixture
def sprint2_wired_system(
    clock: SeedableClock,
    event_bus: RecordingEventBus,
    zone3_connection: RecordingConnection,
    zone3_semantic_repository: SqlSemanticRepository,
    zone3_index_projector: Zone3IndexProjector,
    zone4_repository: InMemoryProceduralMemoryRepository,
    zone4_backstop_config_port: InMemoryZone4BackstopConfigPort,
    zone4_store: InMemoryZone4Store,
    zone4_retrieval_index_eviction: InMemoryZone4RetrievalIndexEviction,
    zone4_backstop_sweep: Zone4CapacityBackstopSweep,
    zone5_entity_repository: EntityMemoryRepository,
    zone8_object_store: LocalObjectStore,
    zone8_manifest_connection: RecordingConnectionWithCommit,
    zone8_manifest_repository: SqlManifestRepository,
    zone8_consolidation_store: Zone8ConsolidationStore,
    zone8_subject_key_store: InMemorySubjectKeyStore,
    zone8_subject_index: InMemoryZone8SubjectIndex,
    zone8_subject_keyed_archiver: Zone8SubjectKeyedArchiver,
    zone8_archive_candidate_store: InMemoryArchiveCandidateStore,
    zone8_archive_transition_store: InMemoryArchiveTransitionStore,
    zone8_archive_engine: ArchiveEngine,
    zone8_bucket_provisioner: LocalTenantBucketProvisioner,
    zone8_bucket_provisioning_service: TenantBucketProvisioningService,
    zone8_subject_erasure_job_store: InMemorySubjectErasureJobStore,
    zone8_subject_erasure_cascade: SubjectErasureCascadeService,
) -> Sprint2WiredSystem:
    """The full Zone 3/4/5/8 Sprint 2 composition, bundled (class docstring)."""
    return Sprint2WiredSystem(
        clock=clock,
        event_bus=event_bus,
        zone3_connection=zone3_connection,
        zone3_semantic_repository=zone3_semantic_repository,
        zone3_index_projector=zone3_index_projector,
        zone4_repository=zone4_repository,
        zone4_backstop_config_port=zone4_backstop_config_port,
        zone4_store=zone4_store,
        zone4_retrieval_index_eviction=zone4_retrieval_index_eviction,
        zone4_backstop_sweep=zone4_backstop_sweep,
        zone5_entity_repository=zone5_entity_repository,
        zone8_object_store=zone8_object_store,
        zone8_manifest_connection=zone8_manifest_connection,
        zone8_manifest_repository=zone8_manifest_repository,
        zone8_consolidation_store=zone8_consolidation_store,
        zone8_subject_key_store=zone8_subject_key_store,
        zone8_subject_index=zone8_subject_index,
        zone8_subject_keyed_archiver=zone8_subject_keyed_archiver,
        zone8_archive_candidate_store=zone8_archive_candidate_store,
        zone8_archive_transition_store=zone8_archive_transition_store,
        zone8_archive_engine=zone8_archive_engine,
        zone8_bucket_provisioner=zone8_bucket_provisioner,
        zone8_bucket_provisioning_service=zone8_bucket_provisioning_service,
        zone8_subject_erasure_job_store=zone8_subject_erasure_job_store,
        zone8_subject_erasure_cascade=zone8_subject_erasure_cascade,
    )
