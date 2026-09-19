"""Cross-zone integration fixture composition (DASHANAN, Part A + Part B).

This module was built in two sequential dispatches against the SAME file:

  Part A (python-backend-engineer persona): the skeleton -- wires
  MemoryOrchestrator, ProvenanceWriteGate, MemoryScoreEngine,
  RotationEngine + RotationDeadlineInvalidation,
  Zone2CapacityBackstopSweep, and CrossZoneDpdpErasureCascade together
  with Zone 1 (WorkingMemoryLRURepository) and Zone 6
  (HybridRetrievalIndexRepository) as REAL, non-mock Python objects at
  the domain/application layer.

  Part B (this pass, database-engineer persona): wires
  SqlEpisodicRepository (Zone 2) and SqlProvenanceRepository (Zone 7)
  for real, against the EXISTING `RecordingConnection` / `RecordingCursor`
  fake DB-API double already established in `tests/test_smoke_episodic.py`
  (imported from there, not re-implemented here, per DRY) -- no
  Testcontainers or Docker infrastructure exists anywhere in this repo
  and none was added. `SqlEpisodicRepository` and `SqlProvenanceRepository`
  are real Python objects exercising their real, parameterized SQL-
  generation logic against that fake; only the DB-API transport is a
  double, exactly like every other adapter test in this repo
  (`test_smoke_episodic.py` itself uses the identical double against the
  same two classes).

Both Zone 2 (`ZoneId.EPISODIC`, read via `MemoryOrchestrator` like every
other registered zone) and Zone 7 (read by `item_id` via a bespoke method,
never through `MemoryOrchestrator` -- see the `zone_repositories` fixture's
docstring) are now composed.

Composition-root choices this fixture makes (documented once here
rather than re-derived at every call site):

  - Clock: every component takes the SAME `SeedableClock` instance (a
    dependency-injected double per `dashanan.domain.ports.Clock`,
    testing-core section 19 -- never `SystemClock`/`datetime.now()`
    directly), so cross-component time is always internally consistent
    and a test can advance it explicitly to cross a TTL/rotation/MaxAge
    boundary deterministically.
  - EventBus: every component takes the SAME `RecordingEventBus`
    instance, so a cross-zone test can assert on the full, ordered
    event stream one scenario produced across every component that
    published to it (`memory.compressed`, `memory.evicted`,
    `context.assembled`), not just one component's own local view.
  - Every "no concrete production adapter exists yet" local Protocol
    (`RotationCandidatePort`/`RotationTransitionPort`,
    `Zone2BackstopConfigPort`/`Zone2CandidateSource`/`Zone2EvictionPort`,
    `ProvenanceJournalPort`) is backed by a small, real, stateful
    in-memory class below -- never `unittest.mock.Mock`/`MagicMock`.
    Each mirrors the "Shape A" in-memory adapters already established
    in `src/dashanan/infrastructure/` (`InMemoryVectorIndex`,
    `InMemoryLexicalIndex`, `InMemoryErasureObligationStore`): real
    state, real behavior, no network/disk I/O, swappable for a Shape B
    adapter later without changing any composed component's own code.
  - Zone 2 (`SqlEpisodicRepository`) and Zone 7 (`SqlProvenanceRepository`)
    are the one exception to "in-memory adapter": both are real, Shape-B-
    style SQL adapters (real parameterized-query generation, real
    row<->domain-object mapping), composed here against
    `RecordingConnection` -- a fake DB-API 2.0 transport, not an in-memory
    reimplementation of the adapter's own logic. This is deliberately the
    same fake `test_smoke_episodic.py` already uses to unit-test these
    same two classes, reused rather than duplicated.

Runtime assumptions recorded per rule 33/40 test-roadmap conventions:
  - clock: `SeedableClock`, seeded at `FIXED_EPOCH`
    (2026-01-01T00:00:00+00:00) unless a test explicitly seeds or
    advances it -- deterministic, no wall-clock dependency anywhere in
    this fixture tree.
  - tenant_id: `DEFAULT_TENANT_ID` ("tenant-1") is the default every
    seed helper uses when a test does not supply its own.
  - No fixture seed is random; nothing in this module calls `random` or
    generates a UUID for identifiers a test would need to match against.

PII NOTE: every seed helper and in-memory store below accepts only
control/ranking metadata (item_id, tenant_id, scores, states,
timestamps) or the literal `PLACEHOLDER_PAYLOAD` constant for the one
or two components that require a non-empty payload string -- mirroring
every `test_smoke_*.py` module's identical PII posture. No example
conversational content appears anywhere in this file.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from dashanan.application.memory_orchestrator import MemoryOrchestrator
from dashanan.application.memory_score_engine import MemoryScoreEngine
from dashanan.application.provenance_write_gate import ProvenanceWriteGate
from dashanan.application.rotation_deadline_invalidation import (
    RotationDeadlineInvalidationService,
)
from dashanan.application.rotation_engine import (
    RotationCandidateSnapshot,
    RotationCompressionResult,
    RotationEngine,
    RotationEnginePortError,
)
from dashanan.application.zone2_capacity_backstop_sweep import (
    Zone2CapacityBackstopSweep,
)
from dashanan.domain.ports import ZoneRepository
from dashanan.domain.rotation_state import RotationState
from dashanan.domain.write_gate import ProvenanceJournalEntry, ProvenanceJournalPort
from dashanan.domain.zone import ZoneId
from dashanan.domain.zone2_capacity_backstop import (
    EvictionCandidate,
    Zone2BackstopConfig,
)
from dashanan.infrastructure.dpdp_erasure_cascade import (
    CrossZoneDpdpErasureCascade,
    InMemoryErasureObligationStore,
    RetrievalIndexEviction,
)
from dashanan.infrastructure.hybrid_retrieval_index_repository import (
    HybridRetrievalIndexRepository,
)
from dashanan.infrastructure.in_memory_lexical_index import InMemoryLexicalIndex
from dashanan.infrastructure.in_memory_vector_index import InMemoryVectorIndex
from dashanan.infrastructure.sql_episodic_repository import SqlEpisodicRepository
from dashanan.infrastructure.sql_provenance_repository import SqlProvenanceRepository
from dashanan.infrastructure.working_memory_lru_repository import (
    WorkingMemoryLRURepository,
)

# Part B reuses (never re-implements, per DRY) the fake DB-API double
# `test_smoke_episodic.py` already established for exercising
# `SqlEpisodicRepository`/`SqlProvenanceRepository` without a real
# database -- see this module's own docstring above.
from tests.test_smoke_episodic import RecordingConnection

FIXED_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
"""The default instant every `SeedableClock` starts at, matching every
`test_smoke_*.py` module's own `FakeClock` convention across this repo."""

DEFAULT_TENANT_ID = "tenant-1"

PLACEHOLDER_PAYLOAD = "<PII_EXAMPLE_REDACTED>"
"""The one literal payload string this fixture tree ever writes, matching
`tests/test_smoke_episodic.py`'s identical placeholder (PII note above)."""

_TEST_ONLY_USER_TURN_SIGNING_KEY = b"integration-fixture-test-only-signing-key"
"""A fixed, non-secret key for `ProvenanceWriteGate` in tests only.

`ProvenanceWriteGate.__init__` requires a real, host-exclusive secret in
production (application-security-core: never hardcode secrets; the
composition root provisions it from a secrets manager or environment
variable). This literal is deliberately test-scoped -- it signs nothing
this fixture tree does not itself also verify -- and must never be
reused as a real deployment's signing key.
"""


class SeedableClock:
    """Deterministic, mutable, seedable `Clock` double (testing-core section 19).

    Every composed component in this fixture tree shares ONE instance of
    this class, so advancing it once advances "now" consistently for the
    whole wired system -- the mechanism a cross-zone test uses to cross a
    TTL (Zone 1), rotation deadline, or MaxAge (Zone 2 backstop) boundary
    deterministically, with no dependency on real wall-clock time and no
    flakiness from test execution speed.
    """

    def __init__(self, seed: datetime = FIXED_EPOCH) -> None:
        """Seed this clock at `seed` (default `FIXED_EPOCH`).

        Args:
            seed: The instant `now()` returns until the next `advance`/
                `set_to` call. Must be timezone-aware (every `Clock` port
                caller in this codebase requires it).
        """
        self._now = seed

    def now(self) -> datetime:
        """Return the seeded (or since-advanced) instant."""
        return self._now

    def advance(self, seconds: float) -> None:
        """Move `now()` forward by `seconds` (may be negative)."""
        self._now = self._now + timedelta(seconds=seconds)

    def set_to(self, instant: datetime) -> None:
        """Jump `now()` directly to `instant`."""
        self._now = instant


class RecordingEventBus:
    """`EventBus` double: records every `publish()` call in order, never raises.

    Shared by every composed component (module docstring): a cross-zone
    test reads `.published` for the full, ordered event stream the whole
    wired system produced, not one component's private view -- the same
    pattern `test_memory_orchestrator.py` and `test_rotation_engine_
    dash009.py` already use per-component, generalized here to span the
    whole composition.
    """

    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, object]]] = []

    def publish(self, event_type: str, payload: dict[str, object]) -> None:
        """Record `(event_type, payload)`. Copies `payload` so later mutation is safe."""
        self.published.append((event_type, dict(payload)))

    def events_of_type(self, event_type: str) -> list[dict[str, object]]:
        """Convenience: every recorded payload for `event_type`, in publish order."""
        return [payload for etype, payload in self.published if etype == event_type]


class InMemoryProvenanceJournal:
    """Real, in-memory `ProvenanceJournalPort`: the ADR-010 durability barrier for tests.

    Backs `ProvenanceWriteGate` with a genuine append + idempotency-key
    lookup implementation (dict-backed, not `unittest.mock`) -- mirrors
    `InMemoryErasureObligationStore`'s identical "Shape A now" posture.
    A future pass MAY swap this for `SqlWriteJournalRepository` bound to
    a `RecordingConnection` extended with a `commit()` no-op; that swap
    is optional and out of this fixture's current scope (the write
    journal's own schema is `write_journal_schema.sql`, distinct from
    both `episodic_schema.sql` (Zone 2) and `provenance_schema.sql`
    (Zone 7) Part B is scoped to).
    """

    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str], ProvenanceJournalEntry] = {}
        self.appended: list[ProvenanceJournalEntry] = []

    def append(self, entry: ProvenanceJournalEntry) -> None:
        """Durably (in-process) record `entry`, keyed by `(tenant_id, idempotency_key)`."""
        self._by_key[(entry.tenant_id, entry.idempotency_key)] = entry
        self.appended.append(entry)

    def find_by_idempotency_key(
        self, tenant_id: str, idempotency_key: str
    ) -> ProvenanceJournalEntry | None:
        """Return the journaled entry for this key, or `None` if never appended."""
        return self._by_key.get((tenant_id, idempotency_key))


class InMemoryRotationLifecycleStore:
    """Real, in-memory `RotationCandidatePort` + `RotationTransitionPort` combined.

    `RotationEngine.run_sweep` re-scores each due item via
    `RotationCandidatePort.current_state` and, once a guarded transition
    is accepted, persists it via `RotationTransitionPort.compress` --
    both ports are satisfied by ONE stateful store here so a test can
    seed an item's `(RotationState, memory_score)` with `seed_item` and
    then observe the SAME store reflect `RotationState.COMPRESSED` after
    a sweep, exactly as a real zone adapter's own state would change.
    No concrete production adapter for either port exists yet in
    `src/dashanan/infrastructure/` (only per-test fakes did, e.g.
    `test_rotation_engine_dash009.FakeCandidatePort`); this class is
    this fixture's own real, reusable equivalent.
    """

    def __init__(self) -> None:
        self._states: dict[tuple[str, str], RotationState] = {}
        self._scores: dict[tuple[str, str], float] = {}
        self._generations: dict[tuple[str, str], int] = {}
        self.current_state_calls: list[tuple[str, ZoneId, str]] = []
        self.compress_calls: list[tuple[str, ZoneId, str]] = []

    def seed_item(
        self,
        item_id: str,
        *,
        state: RotationState = RotationState.ACTIVE,
        memory_score: float = 0.5,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> None:
        """Set `item_id`'s lifecycle state and score, as of the next sweep's re-score step."""
        self._states[(tenant_id, item_id)] = state
        self._scores[(tenant_id, item_id)] = memory_score

    def current_state(
        self, tenant_id: str, zone_id: ZoneId, item_id: str
    ) -> RotationCandidateSnapshot:
        """`RotationCandidatePort.current_state`: return this item's seeded/current snapshot.

        Raises:
            RotationEnginePortError: If `item_id` was never seeded for
                `tenant_id` -- mirrors a real adapter's "item no longer
                exists" operational failure (`RotationCandidatePort`'s
                own documented raise contract).
        """
        self.current_state_calls.append((tenant_id, zone_id, item_id))
        key = (tenant_id, item_id)
        if key not in self._states:
            raise RotationEnginePortError(
                f"no rotation lifecycle state seeded for tenant={tenant_id!r} "
                f"item={item_id!r}"
            )
        return RotationCandidateSnapshot(
            lifecycle_state=self._states[key], memory_score=self._scores[key]
        )

    def compress(
        self, tenant_id: str, zone_id: ZoneId, item_id: str
    ) -> RotationCompressionResult:
        """`RotationTransitionPort.compress`: durably transition `item_id` to `COMPRESSED`."""
        self.compress_calls.append((tenant_id, zone_id, item_id))
        key = (tenant_id, item_id)
        self._states[key] = RotationState.COMPRESSED
        generation = self._generations.get(key, 0) + 1
        self._generations[key] = generation
        return RotationCompressionResult(
            generation=generation, original_tokens=100, compressed_tokens=20
        )


class InMemoryZone2BackstopConfigPort:
    """Real, in-memory `Zone2BackstopConfigPort`: per-tenant, live-mutable config.

    `Zone2CapacityBackstopSweep.run` calls `.load(tenant_id)` fresh on
    every sweep and never caches it (must-not-deviate item 1 in
    `zone2_capacity_backstop_sweep.py`) -- this store's `set_config`
    lets a test change a tenant's cap/MaxAge between two `run()` calls
    and observe the second sweep immediately honor the new value, the
    exact behavior that must-not-deviate item exists to guarantee.
    """

    _DEFAULT_CAPACITY_CAP = 100_000
    """HLD Section 12A Sprint 1 default for Zone 2: "100,000 items"."""

    _DEFAULT_MAX_AGE_SECONDS = 180 * 24 * 60 * 60
    """HLD Section 12A Sprint 1 default for Zone 2: "180 days"."""

    def __init__(self) -> None:
        self._configs: dict[str, Zone2BackstopConfig] = {}
        self.load_calls: list[str] = []

    def set_config(self, tenant_id: str, config: Zone2BackstopConfig) -> None:
        """Set (or replace) `tenant_id`'s current cap/MaxAge config."""
        self._configs[tenant_id] = config

    def load(self, tenant_id: str) -> Zone2BackstopConfig:
        """`Zone2BackstopConfigPort.load`: the HLD 12A default unless overridden."""
        self.load_calls.append(tenant_id)
        return self._configs.get(
            tenant_id,
            Zone2BackstopConfig(
                capacity_cap=self._DEFAULT_CAPACITY_CAP,
                max_age_seconds=self._DEFAULT_MAX_AGE_SECONDS,
            ),
        )


class InMemoryZone2Store:
    """Real, in-memory `Zone2CandidateSource` + `Zone2EvictionPort` combined.

    Mirrors `InMemoryRotationLifecycleStore`'s "one stateful store backs
    both related ports" shape: `fetch_candidates` returns exactly the
    candidates a test seeded via `seed_candidate` for `tenant_id`, and
    `evict` actually removes the matching candidate from this store's
    own state, so a second `fetch_candidates` call after a sweep
    correctly reflects the eviction -- real state change, not a canned
    response. No concrete production adapter for either local Protocol
    exists yet (`zone2_capacity_backstop_sweep.py`'s own Protocols are
    unimplemented outside test fakes); this class is this fixture's real
    equivalent, ranking-only (never zone payload content, PII note
    above).
    """

    def __init__(self) -> None:
        self._candidates: dict[str, dict[str, EvictionCandidate]] = {}
        self.fetch_calls: list[tuple[str, datetime]] = []
        self.evicted: list[tuple[str, str]] = []

    def seed_candidate(self, tenant_id: str, candidate: EvictionCandidate) -> None:
        """Add (or replace) one candidate for `tenant_id`, keyed by `candidate.item_id`."""
        self._candidates.setdefault(tenant_id, {})[candidate.item_id] = candidate

    def fetch_candidates(
        self, tenant_id: str, as_of: datetime
    ) -> list[EvictionCandidate]:
        """`Zone2CandidateSource.fetch_candidates`: every currently-seeded candidate."""
        self.fetch_calls.append((tenant_id, as_of))
        return list(self._candidates.get(tenant_id, {}).values())

    def evict(self, tenant_id: str, item_id: str) -> None:
        """`Zone2EvictionPort.evict`: remove `item_id` from this store's own state."""
        self.evicted.append((tenant_id, item_id))
        self._candidates.get(tenant_id, {}).pop(item_id, None)


@pytest.fixture
def clock() -> SeedableClock:
    """The one `Clock` every fixture below shares (module docstring)."""
    return SeedableClock()


@pytest.fixture
def event_bus() -> RecordingEventBus:
    """The one `EventBus` every fixture below shares (module docstring)."""
    return RecordingEventBus()


@pytest.fixture
def working_memory_repository(clock: SeedableClock) -> WorkingMemoryLRURepository:
    """Zone 1 (`ZoneId.WORKING`): the real Shape A LRU adapter (ADR-005, FR-001)."""
    return WorkingMemoryLRURepository(clock=clock)


@pytest.fixture
def vector_index() -> InMemoryVectorIndex:
    """Zone 6's vector surface (ADR-007): exposed standalone so a test can assert
    directly on index state, in addition to going through `retrieval_index_repository`.
    """
    return InMemoryVectorIndex()


@pytest.fixture
def lexical_index() -> InMemoryLexicalIndex:
    """Zone 6's lexical surface (ADR-008): exposed standalone, mirrors `vector_index`."""
    return InMemoryLexicalIndex()


@pytest.fixture
def retrieval_index_repository(
    vector_index: InMemoryVectorIndex, lexical_index: InMemoryLexicalIndex
) -> HybridRetrievalIndexRepository:
    """Zone 6 (`ZoneId.RETRIEVAL_INDEX`): the real dual-index RRF-fusion adapter (HLD 3.7)."""
    return HybridRetrievalIndexRepository(
        vector_index=vector_index, lexical_index=lexical_index
    )


@pytest.fixture
def episodic_connection() -> RecordingConnection:
    """The fake DB-API 2.0 connection `SqlEpisodicRepository` issues queries against.

    Reuses `tests/test_smoke_episodic.py`'s own `RecordingConnection` /
    `RecordingCursor` double (module docstring, Part B) -- no
    Testcontainers/Docker anywhere in this repo. Exposed standalone (the
    same pattern `vector_index`/`lexical_index` use for Zone 6) so a test
    can assert directly on `.cursor_obj.executed` -- the exact SQL and
    params `SqlEpisodicRepository` issued -- in addition to going through
    `episodic_repository` itself.
    """
    return RecordingConnection()


@pytest.fixture
def episodic_repository(
    episodic_connection: RecordingConnection, clock: SeedableClock
) -> SqlEpisodicRepository:
    """Zone 2 (`ZoneId.EPISODIC`): the real `ZoneRepository` SQL adapter (Part B, FR-002).

    Real, non-mock `SqlEpisodicRepository` -- real parameterized-SQL
    generation, real row<->`Episode` mapping -- composed against the fake
    `episodic_connection` DB-API transport and the SAME shared `clock`
    every other component in this fixture tree uses, so `fetch()`'s
    decay computation (`ZoneQuery.as_of` unset) stays consistent with the
    rest of the composition (module docstring).
    """
    return SqlEpisodicRepository(connection=episodic_connection, clock=clock)


@pytest.fixture
def provenance_connection() -> RecordingConnection:
    """The fake DB-API 2.0 connection `SqlProvenanceRepository` issues queries against.

    A SEPARATE `RecordingConnection` instance from `episodic_connection`:
    Zone 2 and Zone 7 are distinct tables (`episodic_entries` vs.
    `provenance_records`) with distinct schemas, so sharing one fake
    connection's single canned-rows cursor between them would make one
    zone's seeded rows leak into the other's reads.
    """
    return RecordingConnection()


@pytest.fixture
def provenance_repository(
    provenance_connection: RecordingConnection,
) -> SqlProvenanceRepository:
    """Zone 7: the real storage adapter (Part B, FR-007, HLD Section 3.8).

    Real, non-mock `SqlProvenanceRepository` composed against the fake
    `provenance_connection` DB-API transport. Deliberately NOT registered
    in `zone_repositories` below -- `sql_provenance_repository.py`'s own
    module docstring states Zone 7 does not implement `ZoneRepository`
    (it is read by `item_id` via `find_by_item_id`/`find_latest_by_item_id`,
    never by the generic `ZoneQuery` every other zone answers), so it is a
    standalone fixture rather than a `zone_repositories` entry, exactly as
    `zone_repositories`' own docstring below states.
    """
    return SqlProvenanceRepository(connection=provenance_connection)


@pytest.fixture
def provenance_journal() -> InMemoryProvenanceJournal:
    """The real, in-memory ADR-010 durability barrier `ProvenanceWriteGate` journals to."""
    return InMemoryProvenanceJournal()


@pytest.fixture
def write_gate(
    provenance_journal: ProvenanceJournalPort, clock: SeedableClock
) -> ProvenanceWriteGate:
    """The cross-zone write-path gate every zone write is required to pass through (FR-010)."""
    return ProvenanceWriteGate(
        journal=provenance_journal,
        clock=clock,
        user_turn_signing_key=_TEST_ONLY_USER_TURN_SIGNING_KEY,
    )


@pytest.fixture
def memory_score_engine() -> MemoryScoreEngine:
    """The stateless six-term MemoryScore facade (FR-012). No ports to compose."""
    return MemoryScoreEngine()


@pytest.fixture
def rotation_lifecycle_store() -> InMemoryRotationLifecycleStore:
    """The real, stateful store backing both `RotationEngine` ports (module docstring)."""
    return InMemoryRotationLifecycleStore()


@pytest.fixture
def rotation_engine(
    rotation_lifecycle_store: InMemoryRotationLifecycleStore,
    event_bus: RecordingEventBus,
    clock: SeedableClock,
) -> RotationEngine:
    """The `Active -> Compressed` rotation scheduler + sweep (FR-012, ADR-012)."""
    return RotationEngine(
        transition_port=rotation_lifecycle_store,
        candidate_port=rotation_lifecycle_store,
        event_bus=event_bus,
        clock=clock,
    )


@pytest.fixture
def rotation_deadline_invalidation(
    rotation_engine: RotationEngine,
) -> RotationDeadlineInvalidationService:
    """The ADR-012 deadline-invalidation choke point (all three mutation paths, HLD 12C)."""
    return RotationDeadlineInvalidationService(rotation_engine=rotation_engine)


@pytest.fixture
def zone2_backstop_config_port() -> InMemoryZone2BackstopConfigPort:
    """The real, per-tenant-mutable cap/MaxAge config store (HLD 7.3, NFR-009)."""
    return InMemoryZone2BackstopConfigPort()


@pytest.fixture
def zone2_store() -> InMemoryZone2Store:
    """The real store backing both `Zone2CandidateSource` and `Zone2EvictionPort`."""
    return InMemoryZone2Store()


@pytest.fixture
def erasure_obligation_store() -> InMemoryErasureObligationStore:
    """The real DPDP pending-erasure tracker (DSHN-58 P1 remediation)."""
    return InMemoryErasureObligationStore()


@pytest.fixture
def retrieval_index_eviction(
    vector_index: InMemoryVectorIndex, lexical_index: InMemoryLexicalIndex
) -> RetrievalIndexEviction:
    """The `Zone2BackstopPortError`-translating Zone 6 deletion adapter (DSHN-58).

    Deliberately composed from the SAME `vector_index`/`lexical_index`
    instances `retrieval_index_repository` uses, per
    `dpdp_erasure_cascade.py`'s own docstring: this is a shared Zone-6
    removal mechanism, not a second implementation of "delete one item
    from Zone 6" that could drift from the read-path adapter's own view.
    """
    return RetrievalIndexEviction(vector_index=vector_index, lexical_index=lexical_index)


@pytest.fixture
def dpdp_erasure_cascade(
    erasure_obligation_store: InMemoryErasureObligationStore,
    zone2_store: InMemoryZone2Store,
    retrieval_index_eviction: RetrievalIndexEviction,
) -> CrossZoneDpdpErasureCascade:
    """The real, concrete `DpdpErasureCascadePort` adapter (DSHN-58 P1 remediation).

    `zone2_store` is reused here as the `Zone2EvictionPort` this cascade
    depends on (dependency inversion, `dpdp_erasure_cascade.py`'s own
    docstring: "the SAME seam `Zone2CapacityBackstopSweep` already uses
    for its own ordinary-eviction leg") -- one real Zone-2 removal
    mechanism, not two.
    """
    return CrossZoneDpdpErasureCascade(
        obligation_store=erasure_obligation_store,
        zone2_eviction=zone2_store,
        retrieval_index_eviction=retrieval_index_eviction,
    )


@pytest.fixture
def zone2_backstop_sweep(
    zone2_backstop_config_port: InMemoryZone2BackstopConfigPort,
    zone2_store: InMemoryZone2Store,
    dpdp_erasure_cascade: CrossZoneDpdpErasureCascade,
    retrieval_index_repository: HybridRetrievalIndexRepository,
    event_bus: RecordingEventBus,
    clock: SeedableClock,
) -> Zone2CapacityBackstopSweep:
    """The Zone 2 OAQ-4 capacity/MaxAge backstop sweep (FR-002, DASH-STORY-004).

    `retrieval_index_repository` (Zone 6's own read-path adapter) is
    passed directly as `retrieval_index_eviction` here -- per
    `zone2_capacity_backstop_sweep.RetrievalIndexEvictionPort`'s own
    docstring, "A `HybridRetrievalIndexRepository` instance already
    satisfies this Protocol structurally... no change to that class was
    required to compose it here." This is deliberately a DIFFERENT
    Zone-6 deletion path than `dpdp_erasure_cascade`'s own
    `RetrievalIndexEviction` wrapper (which exists specifically to
    translate failures into `Zone2BackstopPortError` for the DPDP
    cascade's own retry-safety contract) -- both are real, both call the
    same two underlying `vector_index`/`lexical_index` instances, so
    either ordering this sweep exercises is idempotent on Zone 6
    (`HybridRetrievalIndexRepository.delete_item`'s own idempotent
    contract).
    """
    return Zone2CapacityBackstopSweep(
        config_port=zone2_backstop_config_port,
        candidate_source=zone2_store,
        eviction_port=zone2_store,
        dpdp_port=dpdp_erasure_cascade,
        retrieval_index_eviction=retrieval_index_repository,
        event_bus=event_bus,
        clock=clock,
    )


@pytest.fixture
def zone_repositories(
    working_memory_repository: WorkingMemoryLRURepository,
    retrieval_index_repository: HybridRetrievalIndexRepository,
    episodic_repository: SqlEpisodicRepository,
) -> dict[ZoneId, ZoneRepository]:
    """The `MemoryOrchestrator` zone-routing table -- Zones 1, 2, and 6 wired for real.

    Every remaining `ZoneId` member (SEMANTIC, PROCEDURAL, ENTITY,
    CONSOLIDATION) has no adapter anywhere in `src/` yet -- `MemoryOrchestrator`
    degrades them via AC-009-SUPP-1 (`zones_unavailable`), which is itself
    real, in-scope, testable behavior this fixture already supports with
    no further wiring. `ZoneId.PROVENANCE` is deliberately NEVER registered
    here, in either dispatch: `sql_provenance_repository.py`'s own module
    docstring states Zone 7 does not implement `ZoneRepository` (it is read
    by `item_id` via a bespoke method, never by `ZoneQuery`), so
    `provenance_repository` above is a standalone fixture, not a member of
    this dict.
    """
    return {
        ZoneId.WORKING: working_memory_repository,
        ZoneId.RETRIEVAL_INDEX: retrieval_index_repository,
        ZoneId.EPISODIC: episodic_repository,
    }


@pytest.fixture
def memory_orchestrator(
    zone_repositories: dict[ZoneId, ZoneRepository],
    event_bus: RecordingEventBus,
    clock: SeedableClock,
) -> MemoryOrchestrator:
    """The Facade over Dashanan's eight memory zones (FR-009, HLD Section 3.1)."""
    return MemoryOrchestrator(
        zone_repositories=zone_repositories, event_bus=event_bus, clock=clock
    )


@dataclass
class WiredSystem:
    """Every composed component from this module, bundled for one cross-zone test.

    A convenience aggregate, not a new abstraction: every field here is
    exactly the object the matching same-named fixture above returns.
    Prefer depending on the individual fixtures directly when a test
    only needs one or two components (pytest's own fixture-injection
    still resolves each one exactly once per test either way); use
    `wired_system` when a test genuinely exercises a cross-zone
    scenario touching most of the composition at once.
    """

    clock: SeedableClock
    event_bus: RecordingEventBus
    working_memory_repository: WorkingMemoryLRURepository
    vector_index: InMemoryVectorIndex
    lexical_index: InMemoryLexicalIndex
    retrieval_index_repository: HybridRetrievalIndexRepository
    episodic_connection: RecordingConnection
    episodic_repository: SqlEpisodicRepository
    provenance_connection: RecordingConnection
    provenance_repository: SqlProvenanceRepository
    provenance_journal: InMemoryProvenanceJournal
    write_gate: ProvenanceWriteGate
    memory_score_engine: MemoryScoreEngine
    rotation_lifecycle_store: InMemoryRotationLifecycleStore
    rotation_engine: RotationEngine
    rotation_deadline_invalidation: RotationDeadlineInvalidationService
    zone2_backstop_config_port: InMemoryZone2BackstopConfigPort
    zone2_store: InMemoryZone2Store
    erasure_obligation_store: InMemoryErasureObligationStore
    retrieval_index_eviction: RetrievalIndexEviction
    dpdp_erasure_cascade: CrossZoneDpdpErasureCascade
    zone2_backstop_sweep: Zone2CapacityBackstopSweep
    zone_repositories: dict[ZoneId, ZoneRepository]
    memory_orchestrator: MemoryOrchestrator


@pytest.fixture
def wired_system(
    clock: SeedableClock,
    event_bus: RecordingEventBus,
    working_memory_repository: WorkingMemoryLRURepository,
    vector_index: InMemoryVectorIndex,
    lexical_index: InMemoryLexicalIndex,
    retrieval_index_repository: HybridRetrievalIndexRepository,
    episodic_connection: RecordingConnection,
    episodic_repository: SqlEpisodicRepository,
    provenance_connection: RecordingConnection,
    provenance_repository: SqlProvenanceRepository,
    provenance_journal: InMemoryProvenanceJournal,
    write_gate: ProvenanceWriteGate,
    memory_score_engine: MemoryScoreEngine,
    rotation_lifecycle_store: InMemoryRotationLifecycleStore,
    rotation_engine: RotationEngine,
    rotation_deadline_invalidation: RotationDeadlineInvalidationService,
    zone2_backstop_config_port: InMemoryZone2BackstopConfigPort,
    zone2_store: InMemoryZone2Store,
    erasure_obligation_store: InMemoryErasureObligationStore,
    retrieval_index_eviction: RetrievalIndexEviction,
    dpdp_erasure_cascade: CrossZoneDpdpErasureCascade,
    zone2_backstop_sweep: Zone2CapacityBackstopSweep,
    zone_repositories: dict[ZoneId, ZoneRepository],
    memory_orchestrator: MemoryOrchestrator,
) -> WiredSystem:
    """The full Part A + Part B composition, bundled (class docstring)."""
    return WiredSystem(
        clock=clock,
        event_bus=event_bus,
        working_memory_repository=working_memory_repository,
        vector_index=vector_index,
        lexical_index=lexical_index,
        retrieval_index_repository=retrieval_index_repository,
        episodic_connection=episodic_connection,
        episodic_repository=episodic_repository,
        provenance_connection=provenance_connection,
        provenance_repository=provenance_repository,
        provenance_journal=provenance_journal,
        write_gate=write_gate,
        memory_score_engine=memory_score_engine,
        rotation_lifecycle_store=rotation_lifecycle_store,
        rotation_engine=rotation_engine,
        rotation_deadline_invalidation=rotation_deadline_invalidation,
        zone2_backstop_config_port=zone2_backstop_config_port,
        zone2_store=zone2_store,
        erasure_obligation_store=erasure_obligation_store,
        retrieval_index_eviction=retrieval_index_eviction,
        dpdp_erasure_cascade=dpdp_erasure_cascade,
        zone2_backstop_sweep=zone2_backstop_sweep,
        zone_repositories=zone_repositories,
        memory_orchestrator=memory_orchestrator,
    )
