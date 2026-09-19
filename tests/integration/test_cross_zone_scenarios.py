"""Cross-zone integration scenarios (DASHANAN), composed-fixture pass.

Exercises the REAL, composed `wired_system` fixture from `conftest.py`
end-to-end across `MemoryOrchestrator`, `ProvenanceWriteGate`,
`MemoryScoreEngine`, `RotationEngine` + `RotationDeadlineInvalidationService`,
and `Zone2CapacityBackstopSweep` -- never a hand-rolled mock beyond the
existing `RecordingConnection`/`RecordingCursor` DB-API fake `conftest.py`
already composes Zone 2 (`SqlEpisodicRepository`) and Zone 7
(`SqlProvenanceRepository`) against (see `conftest.py`'s own module
docstring for why that one fake transport is reused rather than
duplicated).

Scope split (per the dispatch this suite was written under):
  - Scenarios 1, 2, 3, 5 (python-backend-engineer pass):
    Orchestrator / write-gate / rotation / score / tenant-isolation.
  - Scenario 4 (this pass, database-engineer persona): DPDP erasure
    cascade, corrected scope -- see
    `TestScenario4DpdpErasureCascadeRealZone6AndZone7Pointer`.

PII NOTE: every payload literal below is `PLACEHOLDER_PAYLOAD`
(`<PII_EXAMPLE_REDACTED>`), mirroring every other `test_smoke_*.py` /
`test_smoke_fixture_wiring.py` module in this repo. No example
conversational content appears anywhere in this file.
"""

from __future__ import annotations

import hashlib
import threading
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from dashanan.application.context_assembly_request import ContextAssemblyRequest
from dashanan.application.memory_score_engine import ScoreInputs
from dashanan.application.provenance_write_gate import (
    ERROR_IDEMPOTENCY_KEY_REUSED_FOR_DIFFERENT_REQUEST,
)
from dashanan.domain.episode import Episode, EpisodeState
from dashanan.domain.memory_score import ScoreWeights, compute_frequency
from dashanan.domain.ports import ZoneQuery
from dashanan.domain.provenance_record import ProvenanceRecord, SourceType, verify_chain
from dashanan.domain.rotation_deadline import weighted_non_recency_sum
from dashanan.domain.rotation_state import RotationState
from dashanan.domain.rotation_zone_policy import RotationZonePolicy
from dashanan.domain.write_gate import WriteAccepted, WriteRejected, WriteRequest
from dashanan.domain.zone import ZoneId
from dashanan.domain.zone2_capacity_backstop import (
    EvictionCandidate,
    EvictionReason,
    Zone2BackstopConfig,
)

from .conftest import DEFAULT_TENANT_ID, PLACEHOLDER_PAYLOAD, WiredSystem


def _episodic_row(episode: Episode) -> tuple[object, ...]:
    """Build one canned `episodic_entries` SELECT row for `episode`.

    Mirrors `SqlEpisodicRepository._SELECT_COLUMNS`' fixed column order and
    `test_smoke_episodic.py`'s own identical `_row_for` helper, kept as a
    local copy here (rather than importing that test module's private
    helper) since this suite already imports its `RecordingConnection`
    indirectly via `conftest.py`.
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


class TestScenario1WriteReadRoundTrip:
    """Scenario 1: write via the real `ProvenanceWriteGate`, read back for real.

    A single accepted write is required to do three things, all through
    real (non-mock) domain/application code: land in Zone 7 hash-chained
    (`ProvenanceRecord.create` + `SqlProvenanceRepository`), become
    retrievable through `MemoryOrchestrator.assemble_context()` querying
    the zone it targeted (Zone 1, a real in-process stateful adapter, so
    a write-then-read round trip is genuinely observable -- unlike Zone
    2/7's fake DB-API transport, whose canned rows do not automatically
    reflect an `INSERT`), and have a real `MemoryScore` computable for it.
    """

    def test_write_lands_in_zone7_and_becomes_retrievable_via_orchestrator(
        self, wired_system: WiredSystem
    ) -> None:
        ws = wired_system
        item_id = "scenario1-item"
        session_id = "scenario1-session"
        retrieval_context_hash = hashlib.sha256(b"scenario-1-query").hexdigest()
        persisted: dict[str, ProvenanceRecord] = {}

        def persist_fact() -> None:
            """The caller's own zone-content write, gated by `submit_write`."""
            ws.working_memory_repository.put(
                tenant_id=DEFAULT_TENANT_ID,
                session_id=session_id,
                item_id=item_id,
                payload=PLACEHOLDER_PAYLOAD,
                token_count=12,
            )
            record = ProvenanceRecord.create(
                tenant_id=DEFAULT_TENANT_ID,
                provenance_id=str(uuid4()),
                item_id=item_id,
                source_zone=ZoneId.WORKING,
                source_type=SourceType.TOOL_OUTPUT,
                write_timestamp=ws.clock.now(),
                actor="dashanan-orchestrator",
                change="initial write",
                retrieval_context_hash=retrieval_context_hash,
            )
            ws.provenance_repository.append(record)
            persisted["record"] = record

        request = WriteRequest(
            tenant_id=DEFAULT_TENANT_ID,
            item_id=item_id,
            source_zone=ZoneId.WORKING,
            source_type_raw="tool_output",
            caller_identity="dashanan-orchestrator",
            retrieval_context_hash=retrieval_context_hash,
            idempotency_key=str(uuid4()),
        )

        result = ws.write_gate.submit_write(request, persist_fact)

        assert isinstance(result, WriteAccepted)
        assert "record" in persisted

        # --- lands in real Zone 7, hash-chained ---
        insert_sql, insert_params = ws.provenance_connection.cursor_obj.executed[-1]
        assert insert_sql.strip().upper().startswith("INSERT INTO PROVENANCE_RECORDS")
        assert insert_params[0] == DEFAULT_TENANT_ID
        # The fake DB-API transport (conftest.py's own documented scope) has
        # no live backing store, so simulate the row this real INSERT would
        # now SELECT back -- feeding the real params straight into the real
        # SELECT-row shape (`_SELECT_COLUMNS`' order == `_APPEND_SQL`'s own
        # param order) exercises the adapter's real row<->domain mapping and
        # the real hash-chain verification, not a re-implementation of it.
        ws.provenance_connection.cursor_obj._rows = [insert_params]
        found = ws.provenance_repository.find_latest_by_item_id(
            DEFAULT_TENANT_ID, item_id
        )
        assert found is not None
        assert found.record_hash == persisted["record"].record_hash
        assert found.confidence == persisted["record"].confidence
        assert verify_chain([found]) is True

        # --- retrievable via MemoryOrchestrator, querying the real target zone ---
        assembly = ws.memory_orchestrator.assemble_context(
            ContextAssemblyRequest(
                tenant_id=DEFAULT_TENANT_ID,
                session_id=session_id,
                task="scenario-1 retrieval",
                token_budget=1000,
                zones=[ZoneId.WORKING],
            )
        )
        assert assembly.degraded is False
        matching = [item for item in assembly.items if item.item_id == item_id]
        assert len(matching) == 1
        assert matching[0].source_zone is ZoneId.WORKING

        # --- a real MemoryScore computed for it ---
        score = ws.memory_score_engine.score(
            ScoreInputs(
                dt_seconds=0.0,
                half_life_seconds=None,
                access_count=1,
                f_cap=10,
                importance_tag=None,
                provenance_confidence=persisted["record"].confidence,
                user_cosine=None,
                task_cosine=None,
                cos_min=-1.0,
                cos_max=1.0,
            )
        )
        assert 0.0 <= score.value <= 1.0
        assert score.terms.provenance_confidence == persisted["record"].confidence


class TestScenario2MultiZoneDegradedAssembly:
    """Scenario 2: 3 real zones + 1 deliberately-unregistered zone, one Orchestrator.

    `wired_system.memory_orchestrator` registers exactly Zones 1, 2 and 6
    (module docstring). Requesting Zone `SEMANTIC` alongside them exercises
    AC-009-SUPP-1 with real adapters for the 3 registered zones -- no fake
    beyond the existing Zone-2 DB-API double.
    """

    def test_three_real_zones_aggregate_and_the_fourth_reports_unavailable(
        self, wired_system: WiredSystem
    ) -> None:
        ws = wired_system
        session_id = "scenario2-session"

        ws.working_memory_repository.put(
            tenant_id=DEFAULT_TENANT_ID,
            session_id=session_id,
            item_id="scenario2-working",
            payload=PLACEHOLDER_PAYLOAD,
            token_count=5,
        )
        ws.retrieval_index_repository.index_item(
            tenant_id=DEFAULT_TENANT_ID,
            item_id="scenario2-index",
            source_zone=ZoneId.EPISODIC,
            text="alpha beta gamma",
            vector=(1.0, 0.0),
            model_id="test-model",
        )
        episode = Episode(
            tenant_id=DEFAULT_TENANT_ID,
            session_id=session_id,
            episode_id=Episode.episode_id_for(DEFAULT_TENANT_ID, session_id, 0),
            seq=0,
            occurred_at=ws.clock.now(),
            written_at=ws.clock.now(),
            payload=PLACEHOLDER_PAYLOAD,
            actors=("user", "agent"),
            token_count=8,
            state=EpisodeState.ACTIVE,
            score_terms={},
        )
        ws.episodic_connection.cursor_obj._rows = [_episodic_row(episode)]

        result = ws.memory_orchestrator.assemble_context(
            ContextAssemblyRequest(
                tenant_id=DEFAULT_TENANT_ID,
                session_id=session_id,
                task="alpha beta gamma",
                token_budget=1000,
                query_embedding=[1.0, 0.0],
                zones=[
                    ZoneId.WORKING,
                    ZoneId.RETRIEVAL_INDEX,
                    ZoneId.EPISODIC,
                    ZoneId.SEMANTIC,
                ],
            )
        )

        assert result.degraded is True
        assert result.zones_unavailable == [ZoneId.SEMANTIC]

        # `MemoryItem.source_zone` reflects the item's OWNING zone, not
        # which `ZoneRepository` fetched it (Zone 6's `HybridRetrieval
        # IndexRepository.fetch` returns `catalog_entry.source_zone`, the
        # zone the indexed content actually belongs to -- HLD 3.7). Two of
        # these three real items are therefore both `EPISODIC`-owned by
        # design (the raw episode plus the item Zone 6 indexed from it),
        # so assert by `item_id` -- the one identifier unique per item --
        # rather than by `source_zone`.
        item_ids = {item.item_id for item in result.items}
        assert len(result.items) == 3
        assert item_ids == {
            "scenario2-working",
            "scenario2-index",
            episode.episode_id,
        }


class TestScenario3RotationDeadlineInvalidationToBackstopChain:
    """Scenario 3: reschedule (real term mutation) -> Compressed -> backstop eviction.

    Every step below is the real production component: `RotationEngine`
    (`schedule_or_pin`/`run_sweep`), `RotationDeadlineInvalidationService`
    (`on_access`), `dashanan.domain.rotation_state.guarded_transition`
    (via `RotationEngine`), and `Zone2CapacityBackstopSweep`. The "real
    term change" is a genuine `Frequency` recompute
    (`dashanan.domain.memory_score.compute_frequency`) fed through
    `weighted_non_recency_sum` -- never a hand-edited float standing in
    for a score mutation.
    """

    def test_reschedule_then_compress_then_backstop_picks_it_up(
        self, wired_system: WiredSystem
    ) -> None:
        ws = wired_system
        item_id = "scenario3-item"
        zone_id = ZoneId.EPISODIC
        weights = ScoreWeights.default()
        policy = RotationZonePolicy.for_zone(
            t_half_seconds=1000.0, compress_threshold=0.30, max_age_seconds=100_000.0
        )

        def c_weighted_for(access_count: int) -> float:
            """The real, recomputed weighted non-Recency sum for `access_count`."""
            frequency = compute_frequency(access_count, f_cap=10)
            return weighted_non_recency_sum(
                w_frequency=weights.w_frequency,
                frequency=frequency,
                w_importance=weights.w_importance,
                importance=0.5,
                w_user_affinity=weights.w_user_affinity,
                user_affinity=0.5,
                w_task_relevance=weights.w_task_relevance,
                task_relevance=0.0,
                w_provenance_confidence=weights.w_provenance_confidence,
                provenance_confidence=0.5,
            )

        # --- initial schedule (access_count = 0) ---
        outcome1 = ws.rotation_deadline_invalidation.on_access(
            zone_id=zone_id,
            item_id=item_id,
            policy=policy,
            w_recency=weights.w_recency,
            c_weighted=c_weighted_for(access_count=0),
            max_age_remaining_seconds=policy.max_age_seconds,
            cooldown_remaining_seconds=0.0,
        )
        assert outcome1.scheduled is True
        assert outcome1.due_at is not None

        # --- real term mutation: Frequency increases via a second access,
        # forcing RotationDeadlineInvalidationService to re-schedule ---
        c_weighted_2 = c_weighted_for(access_count=1)
        outcome2 = ws.rotation_deadline_invalidation.on_access(
            zone_id=zone_id,
            item_id=item_id,
            policy=policy,
            w_recency=weights.w_recency,
            c_weighted=c_weighted_2,
            max_age_remaining_seconds=policy.max_age_seconds,
            cooldown_remaining_seconds=0.0,
        )
        assert outcome2.scheduled is True
        assert outcome2.due_at is not None
        assert outcome2.due_at > outcome1.due_at, (
            "a higher c_weighted after the access must push the computed "
            "deadline later -- otherwise the mutation was not honored"
        )

        # --- confirm the ORIGINAL deadline is genuinely invalidated, not
        # merely superseded by a second live entry: sweeping at the stale
        # due_at must transition nothing ---
        ws.clock.set_to(outcome1.due_at)
        premature = ws.rotation_engine.run_sweep(
            tenant_id=DEFAULT_TENANT_ID, zone_id=zone_id, policy=policy
        )
        assert premature.transitioned == ()
        assert premature.skipped == ()
        assert premature.failed == ()

        # --- RotationEngine transitions Active -> Compressed at the
        # rescheduled (correct) deadline ---
        ws.rotation_lifecycle_store.seed_item(
            item_id, state=RotationState.ACTIVE, memory_score=0.30
        )
        ws.clock.set_to(outcome2.due_at)
        sweep_result = ws.rotation_engine.run_sweep(
            tenant_id=DEFAULT_TENANT_ID, zone_id=zone_id, policy=policy
        )
        assert [t.item_id for t in sweep_result.transitioned] == [item_id]
        compressed_events = ws.event_bus.events_of_type("memory.compressed")
        assert any(e["item_id"] == item_id for e in compressed_events)

        # --- Zone2CapacityBackstopSweep picks up the now-Compressed item
        # under MaxAge pressure ---
        ws.zone2_backstop_config_port.set_config(
            DEFAULT_TENANT_ID,
            Zone2BackstopConfig(capacity_cap=1_000_000, max_age_seconds=10.0),
        )
        ws.zone2_store.seed_candidate(
            DEFAULT_TENANT_ID,
            EvictionCandidate(
                item_id=item_id,
                memory_score=0.30,
                is_compressed=True,
                age_seconds=100.0,
                written_at=ws.clock.now(),
            ),
        )
        backstop_result = ws.zone2_backstop_sweep.run(DEFAULT_TENANT_ID)

        assert [e.item_id for e in backstop_result.evicted] == [item_id]
        assert backstop_result.evicted[0].reason is EvictionReason.MAX_AGE
        assert (DEFAULT_TENANT_ID, item_id) in ws.zone2_store.evicted
        evicted_events = ws.event_bus.events_of_type("memory.evicted")
        assert any(e["item_id"] == item_id for e in evicted_events)


class TestScenario5CrossTenantIsolation:
    """Scenario 5: two tenants, all real zones, one `MemoryOrchestrator` instance.

    Zone 1 (`WorkingMemoryLRURepository`) and Zone 6
    (`HybridRetrievalIndexRepository`) are REAL, structurally
    tenant-partitioned adapters (a per-`(tenant_id, session_id)` dict, and
    a per-`tenant_id` vector/lexical collection, respectively) -- both
    tenants' data is seeded into the SAME repository instances and each
    tenant's `assemble_context()` call is asserted to return only its own
    items. Zone 2 (`SqlEpisodicRepository`) is backed by the fake
    DB-API transport (`conftest.py`'s documented scope): its own
    `_require_tenant` guard and parameterized-query generation are real,
    non-mock code, so this asserts the real adapter always issues the
    query scoped to exactly the calling tenant's own `tenant_id` -- the
    same guarantee that, against the real Postgres backend this fake
    stands in for, is what prevents one tenant's rows from ever being
    selected by another tenant's read.
    """

    def test_zero_cross_tenant_leakage_across_working_and_retrieval_index(
        self, wired_system: WiredSystem
    ) -> None:
        ws = wired_system
        tenant_a = "tenant-A"
        tenant_b = "tenant-B"

        ws.working_memory_repository.put(
            tenant_id=tenant_a,
            session_id="session-a",
            item_id="working-a",
            payload=PLACEHOLDER_PAYLOAD,
            token_count=5,
        )
        ws.working_memory_repository.put(
            tenant_id=tenant_b,
            session_id="session-b",
            item_id="working-b",
            payload=PLACEHOLDER_PAYLOAD,
            token_count=5,
        )
        ws.retrieval_index_repository.index_item(
            tenant_id=tenant_a,
            item_id="index-a",
            source_zone=ZoneId.EPISODIC,
            text="shared query terms",
            vector=(1.0, 0.0),
            model_id="test-model",
        )
        ws.retrieval_index_repository.index_item(
            tenant_id=tenant_b,
            item_id="index-b",
            source_zone=ZoneId.EPISODIC,
            text="shared query terms",
            vector=(1.0, 0.0),
            model_id="test-model",
        )

        for tenant_id, own_ids, other_ids in (
            (tenant_a, {"working-a", "index-a"}, {"working-b", "index-b"}),
            (tenant_b, {"working-b", "index-b"}, {"working-a", "index-a"}),
        ):
            result = ws.memory_orchestrator.assemble_context(
                ContextAssemblyRequest(
                    tenant_id=tenant_id,
                    session_id="isolation-check-session",
                    task="shared query terms",
                    token_budget=1000,
                    query_embedding=[1.0, 0.0],
                    zones=[ZoneId.WORKING, ZoneId.RETRIEVAL_INDEX],
                )
            )

            item_ids = {item.item_id for item in result.items}
            assert own_ids <= item_ids, (
                f"tenant {tenant_id!r} must see its own items {own_ids}, "
                f"got {item_ids}"
            )
            assert not (other_ids & item_ids), (
                f"tenant {tenant_id!r} must NEVER see another tenant's "
                f"items {other_ids}, leaked: {other_ids & item_ids}"
            )

    def test_zone2_query_is_always_parameterized_with_the_calling_tenant_only(
        self, wired_system: WiredSystem
    ) -> None:
        """Zone 2 (fake DB-API transport): the real adapter never omits, nor
        substitutes, the calling tenant's own `tenant_id` in its query --
        the structural guarantee (must-not-deviate item 5, AR1-003) that,
        against the real Postgres backend, prevents cross-tenant rows from
        ever being selected.
        """
        ws = wired_system
        tenant_a = "tenant-A"
        tenant_b = "tenant-B"

        for tenant_id in (tenant_a, tenant_b):
            episode = Episode(
                tenant_id=tenant_id,
                session_id="isolation-session",
                episode_id=Episode.episode_id_for(tenant_id, "isolation-session", 0),
                seq=0,
                occurred_at=ws.clock.now(),
                written_at=ws.clock.now(),
                payload=PLACEHOLDER_PAYLOAD,
                actors=("user", "agent"),
                token_count=6,
                state=EpisodeState.ACTIVE,
                score_terms={},
            )
            ws.episodic_connection.cursor_obj._rows = [_episodic_row(episode)]
            ws.episodic_connection.cursor_obj.executed.clear()

            result = ws.memory_orchestrator.assemble_context(
                ContextAssemblyRequest(
                    tenant_id=tenant_id,
                    session_id="isolation-session",
                    task="isolation check",
                    token_budget=1000,
                    zones=[ZoneId.EPISODIC],
                )
            )

            assert result.degraded is False
            executed_sql, executed_params = ws.episodic_connection.cursor_obj.executed[-1]
            assert executed_sql.strip().upper().startswith("SELECT")
            assert executed_params[0] == tenant_id
            assert all(
                item.item_id == episode.episode_id for item in result.items
            )


class TestScenario4DpdpErasureCascadeRealZone6AndZone7Pointer:
    """Scenario 4 (DPDP erasure cascade, corrected scope): database-engineer pass.

    A real PII-bearing item is indexed in real Zone 6
    (`HybridRetrievalIndexRepository`, both the vector and lexical
    surfaces) AND referenced by a real Zone 7 provenance pointer
    (`SqlProvenanceRepository.append`, via `ProvenanceRecord.create`'s
    real hashing/confidence logic -- the same technique Scenario 1 uses
    to make a write-then-read round trip observable against the fake
    DB-API transport). `CrossZoneDpdpErasureCascade.fulfil_pending_
    erasure` is then invoked for real, its own Zone-2 leg landing on the
    real, in-memory `Zone2EvictionPort` (`wired_system.zone2_store`) as
    the trigger this scenario asserts on -- never a mocked port.

    Corrected scope (per the dispatch this pass was written under):
    `dpdp_erasure_cascade.py`'s own module docstring rules a
    Postgres-level Zone 2 (`SqlEpisodicRepository`) deletion explicitly
    OUT of this cascade's scope -- `episodic_schema.sql`'s
    `trg_episodic_entries_append_only` trigger unconditionally rejects
    every UPDATE/DELETE, so the cascade depends on `Zone2EvictionPort`
    (dependency inversion) rather than deleting Zone 2 rows itself. This
    test therefore asserts the eviction-port call (`zone2_store.evicted`),
    never a `DELETE` issued against `episodic_connection`. Symmetrically,
    `SqlProvenanceRepository` (Zone 7) exposes no delete method anywhere
    in its own class -- append-only by construction, not merely by
    convention -- so this test's second half confirms the Zone 7
    provenance pointer this test wrote before the cascade ran is still
    present and byte-identical (same `record_hash`) afterward: the
    documented not-deletable/append-only state, not a silent orphan
    where the pointer survives while nothing records the cascade ever
    ran, and not a false claim that Zone 7 was itself touched.
    """

    def test_dpdp_erasure_cascade_removes_zone6_and_leaves_zone7_pointer_append_only(
        self, wired_system: WiredSystem
    ) -> None:
        ws = wired_system
        tenant_id = DEFAULT_TENANT_ID
        item_id = "scenario4-pii-item"
        vector = (1.0, 0.0)

        # --- a real PII-bearing item indexed in real Zone 6 ---
        ws.retrieval_index_repository.index_item(
            tenant_id=tenant_id,
            item_id=item_id,
            source_zone=ZoneId.EPISODIC,
            text=PLACEHOLDER_PAYLOAD,
            vector=vector,
            model_id="test-model",
        )
        assert item_id in ws.vector_index.search(tenant_id, vector, top_k=10)
        assert item_id in ws.lexical_index.search(tenant_id, PLACEHOLDER_PAYLOAD, top_k=10)

        # --- a real Zone 7 provenance pointer referencing the same item ---
        record = ProvenanceRecord.create(
            tenant_id=tenant_id,
            provenance_id="scenario4-prov",
            item_id=item_id,
            source_zone=ZoneId.EPISODIC,
            source_type=SourceType.SYSTEM_DERIVED,
            write_timestamp=datetime(2026, 1, 1, tzinfo=UTC),
            actor="dpdp-erasure-cascade-scenario",
            change="initial index",
            retrieval_context_hash="b" * 64,
        )
        ws.provenance_repository.append(record)
        insert_sql, insert_params = ws.provenance_connection.cursor_obj.executed[-1]
        assert insert_sql.strip().upper().startswith("INSERT INTO PROVENANCE_RECORDS")
        # The fake DB-API transport has no live backing store (module
        # docstring); feed the real INSERT params back as the row this
        # INSERT would now SELECT, exactly as Scenario 1 does, so the
        # "before" read below exercises the adapter's real row<->domain
        # mapping rather than a hand-rolled record.
        ws.provenance_connection.cursor_obj._rows = [insert_params]
        found_before = ws.provenance_repository.find_latest_by_item_id(tenant_id, item_id)
        assert found_before is not None
        assert found_before.record_hash == record.record_hash
        assert verify_chain([found_before]) is True

        # --- request erasure, then trigger the cascade for real ---
        ws.erasure_obligation_store.request_erasure(tenant_id, item_id)

        fulfilled = ws.dpdp_erasure_cascade.fulfil_pending_erasure(tenant_id, item_id)

        assert fulfilled is True
        assert not ws.erasure_obligation_store.has_pending_erasure(tenant_id, item_id)

        # --- the real, in-memory Zone2EvictionPort is the trigger this
        # scenario asserts on (corrected scope: never a Postgres-level
        # Zone 2 DELETE against `episodic_connection`) ---
        assert (tenant_id, item_id) in ws.zone2_store.evicted

        # --- Zone 6 removal confirmed for real, on both surfaces ---
        assert item_id not in ws.vector_index.search(tenant_id, vector, top_k=10)
        assert item_id not in ws.lexical_index.search(
            tenant_id, PLACEHOLDER_PAYLOAD, top_k=10
        )
        refetched = ws.retrieval_index_repository.fetch(
            ZoneQuery(
                tenant_id=tenant_id,
                task=PLACEHOLDER_PAYLOAD,
                query_embedding=list(vector),
                max_items=10,
                min_provenance_conf=0.0,
            )
        )
        assert all(item.item_id != item_id for item in refetched)

        # --- Zone 7 provenance pointer left append-only/not-deletable:
        # still present, byte-identical, never silently orphaned ---
        found_after = ws.provenance_repository.find_latest_by_item_id(tenant_id, item_id)
        assert found_after is not None
        assert found_after.item_id == item_id
        assert found_after.record_hash == found_before.record_hash
        assert verify_chain([found_after]) is True


class TestScenario6WriteGateConcurrencyAndIdempotencyMismatch:
    """Scenario 6 (DSHN-59 P1 remediation, attempt 1): the real, wired
    `ProvenanceWriteGate` + `InMemoryProvenanceJournal` this fixture
    composes, exercised against the two findings an adversarial
    concurrent-execution re-review raised.

    HIGH: `submit_write`'s idempotency check
    (`journal.find_by_idempotency_key`) and the subsequent journal
    append + `persist_fact()` call were not atomic and were protected by
    no lock anywhere in `ProvenanceWriteGate` or
    `InMemoryProvenanceJournal` -- two threads racing on the SAME
    `(tenant_id, idempotency_key)` could both observe a cache-miss
    before either had journaled, so `persist_fact` ran twice for one
    logical idempotency key. `ProvenanceWriteGate._lock_for_idempotency_key`
    now makes the whole check-through-persist sequence one atomic
    critical section per key; this test races real threads through the
    real, wired gate (never a synthetic single-threaded call sequence)
    to prove that guarantee holds under contention.

    MEDIUM: reusing an `idempotency_key` for a DIFFERENT `item_id` (same
    tenant) must be rejected, not silently answered with the first
    write's cached `WriteAccepted` -- see
    `ProvenanceWriteGate._is_verbatim_replay`.
    """

    def test_concurrent_submit_write_same_idempotency_key_persists_at_most_once(
        self, wired_system: WiredSystem
    ) -> None:
        ws = wired_system
        item_id = "dshn59-race-item"
        retrieval_context_hash = hashlib.sha256(b"dshn59-race-query").hexdigest()
        idempotency_key = "dshn59-race-nonce"
        thread_count = 8
        persist_calls: list[int] = []
        persist_calls_guard = threading.Lock()
        start_barrier = threading.Barrier(thread_count)
        results: list[object] = [None] * thread_count

        def persist_fact() -> None:
            with persist_calls_guard:
                persist_calls.append(1)

        def run(index: int) -> None:
            """One racing writer: build its own `WriteRequest`, wait for
            every other thread to reach the barrier, then all submit
            through the SAME shared `ws.write_gate` at once."""
            request = WriteRequest(
                tenant_id=DEFAULT_TENANT_ID,
                item_id=item_id,
                source_zone=ZoneId.WORKING,
                source_type_raw="tool_output",
                caller_identity="dashanan-orchestrator",
                retrieval_context_hash=retrieval_context_hash,
                idempotency_key=idempotency_key,
            )
            start_barrier.wait(timeout=5)
            results[index] = ws.write_gate.submit_write(request, persist_fact)

        threads = [
            threading.Thread(target=run, args=(index,)) for index in range(thread_count)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        assert all(isinstance(r, WriteAccepted) for r in results), results
        write_ids = {r.write_id for r in results}
        assert len(write_ids) == 1, (
            "every thread racing on the same idempotency_key must observe "
            f"the SAME write_id -- got distinct ids: {write_ids}"
        )
        assert len(persist_calls) == 1, (
            "persist_fact ran more than once for one logical idempotency_key "
            f"-- got {len(persist_calls)} calls (HIGH finding, DSHN-59)"
        )
        assert len(ws.provenance_journal.appended) == 1

    def test_idempotency_key_reused_for_different_item_id_is_rejected(
        self, wired_system: WiredSystem
    ) -> None:
        """The second, mismatched request's own zone-content write must
        never run -- the exact silent-data-loss the MEDIUM finding
        (DSHN-59) describes if it did."""
        ws = wired_system
        idempotency_key = "dshn59-mismatch-nonce"
        retrieval_context_hash = hashlib.sha256(b"dshn59-mismatch-query").hexdigest()
        persisted_items: list[str] = []

        def make_request(item_id: str) -> WriteRequest:
            return WriteRequest(
                tenant_id=DEFAULT_TENANT_ID,
                item_id=item_id,
                source_zone=ZoneId.WORKING,
                source_type_raw="tool_output",
                caller_identity="dashanan-orchestrator",
                retrieval_context_hash=retrieval_context_hash,
                idempotency_key=idempotency_key,
            )

        def persist_fact_for(item_id: str):
            """Bind `item_id` into a zero-arg `persist_fact` callback."""

            def _persist() -> None:
                persisted_items.append(item_id)

            return _persist

        first = ws.write_gate.submit_write(
            make_request("dshn59-item-original"),
            persist_fact_for("dshn59-item-original"),
        )
        second = ws.write_gate.submit_write(
            make_request("dshn59-item-mutated"),
            persist_fact_for("dshn59-item-mutated"),
        )

        assert isinstance(first, WriteAccepted)
        assert isinstance(second, WriteRejected)
        assert second.error_code == ERROR_IDEMPOTENCY_KEY_REUSED_FOR_DIFFERENT_REQUEST
        assert persisted_items == ["dshn59-item-original"]
