"""Smoke assertions for `tests/integration/conftest.py`'s Part-A composition.

Confirms the fixture tree in `conftest.py` actually wires (importable,
constructs with no exception, exercises one real call through each
composed component) ahead of the formal cross-zone integration suite
Part B's SQL wiring will make possible. Mirrors this repo's own
`test_smoke_*.py` scope split: these are wiring smoke checks, not the
full AC-level suite.

PII NOTE: every payload below is `PLACEHOLDER_PAYLOAD`
(`<PII_EXAMPLE_REDACTED>`); no example conversational content appears.
"""

from __future__ import annotations

from datetime import UTC, datetime

from dashanan.application.context_assembly_request import ContextAssemblyRequest
from dashanan.application.memory_score_engine import ScoreInputs
from dashanan.domain.episode import Episode, EpisodeState
from dashanan.domain.provenance_record import ProvenanceRecord, SourceType
from dashanan.domain.rotation_state import RotationState
from dashanan.domain.rotation_zone_policy import RotationZonePolicy
from dashanan.domain.write_gate import WriteRequest
from dashanan.domain.zone import ZoneId
from dashanan.domain.zone2_capacity_backstop import (
    EvictionCandidate,
    Zone2BackstopConfig,
)

from .conftest import DEFAULT_TENANT_ID, PLACEHOLDER_PAYLOAD, WiredSystem


class TestFixtureWiringSmoke:
    """Baseline: every Part-A component constructs and takes one real call."""

    def test_wired_system_constructs_with_no_exception(
        self, wired_system: WiredSystem
    ) -> None:
        assert wired_system.memory_orchestrator is not None

    def test_memory_orchestrator_assembles_context_from_zone1_and_zone6(
        self, wired_system: WiredSystem
    ) -> None:
        wired_system.working_memory_repository.put(
            tenant_id=DEFAULT_TENANT_ID,
            session_id="session-1",
            item_id="item-1",
            payload=PLACEHOLDER_PAYLOAD,
            token_count=5,
        )

        result = wired_system.memory_orchestrator.assemble_context(
            ContextAssemblyRequest(
                tenant_id=DEFAULT_TENANT_ID,
                session_id="session-1",
                task="recall the last order",
                token_budget=1000,
                max_items=10,
                min_provenance_conf=0.0,
            )
        )

        assert any(item.item_id == "item-1" for item in result.items)
        # Every unregistered ZoneId (SEMANTIC, PROCEDURAL, ENTITY, PROVENANCE,
        # CONSOLIDATION) degrades per AC-009-SUPP-1 -- never an unhandled
        # exception -- so `degraded` stays True even after Part B registers
        # Zone 2. PROVENANCE is deliberately never registered (module
        # docstring: read by item_id, never through MemoryOrchestrator), so
        # it is always present in `zones_unavailable`.
        assert result.degraded
        assert ZoneId.PROVENANCE in result.zones_unavailable
        # Part B: Zone 2 (EPISODIC) is now registered -- an empty
        # `RecordingConnection` (no seeded rows) is a real, successful
        # zero-result fetch, not an unavailable zone.
        assert ZoneId.EPISODIC not in result.zones_unavailable

    def test_write_gate_accepts_a_well_formed_write(
        self, wired_system: WiredSystem
    ) -> None:
        request = WriteRequest(
            tenant_id=DEFAULT_TENANT_ID,
            item_id="item-1",
            source_zone=ZoneId.WORKING,
            source_type_raw="system_derived",
            caller_identity="integration-fixture",
            retrieval_context_hash="a" * 64,
            idempotency_key="idem-1",
        )

        persisted: list[str] = []
        outcome = wired_system.write_gate.submit_write(
            request, persist_fact=lambda: persisted.append(request.item_id)
        )

        assert persisted == ["item-1"]
        assert wired_system.provenance_journal.find_by_idempotency_key(
            DEFAULT_TENANT_ID, "idem-1"
        ) is not None
        assert outcome.write_id

    def test_memory_score_engine_scores_one_item(
        self, wired_system: WiredSystem
    ) -> None:
        score = wired_system.memory_score_engine.score(
            ScoreInputs(
                dt_seconds=0.0,
                half_life_seconds=172_800.0,
                access_count=1,
                f_cap=10,
                importance_tag=None,
                provenance_confidence=0.8,
                user_cosine=None,
                task_cosine=None,
                cos_min=-1.0,
                cos_max=1.0,
            )
        )

        assert 0.0 <= score.value <= 1.0

    def test_rotation_engine_sweep_transitions_a_due_item(
        self, wired_system: WiredSystem
    ) -> None:
        policy = RotationZonePolicy.for_zone(
            t_half_seconds=172_800.0, compress_threshold=0.35, max_age_seconds=15_552_000.0
        )
        wired_system.rotation_lifecycle_store.seed_item(
            "item-1", state=RotationState.ACTIVE, memory_score=0.1
        )
        wired_system.rotation_engine.schedule_or_pin(
            zone_id=ZoneId.EPISODIC,
            item_id="item-1",
            policy=policy,
            w_recency=1.0 / 6.0,
            c_weighted=0.1,
            max_age_remaining_seconds=15_552_000.0,
            cooldown_remaining_seconds=0.0,
        )
        wired_system.clock.advance(seconds=1_000_000.0)

        result = wired_system.rotation_engine.run_sweep(
            tenant_id=DEFAULT_TENANT_ID, zone_id=ZoneId.EPISODIC, policy=policy
        )

        assert any(t.item_id == "item-1" for t in result.transitioned)
        assert wired_system.event_bus.events_of_type("memory.compressed")

    def test_zone2_backstop_sweep_evicts_lowest_scored_item_over_cap(
        self, wired_system: WiredSystem
    ) -> None:
        wired_system.zone2_backstop_config_port.set_config(
            DEFAULT_TENANT_ID,
            Zone2BackstopConfig(capacity_cap=1, max_age_seconds=15_552_000.0),
        )
        as_of = datetime(2026, 1, 1, tzinfo=UTC)
        wired_system.zone2_store.seed_candidate(
            DEFAULT_TENANT_ID,
            EvictionCandidate(
                item_id="low-score",
                memory_score=0.1,
                is_compressed=False,
                age_seconds=10.0,
                written_at=as_of,
            ),
        )
        wired_system.zone2_store.seed_candidate(
            DEFAULT_TENANT_ID,
            EvictionCandidate(
                item_id="high-score",
                memory_score=0.9,
                is_compressed=False,
                age_seconds=10.0,
                written_at=as_of,
            ),
        )

        result = wired_system.zone2_backstop_sweep.run(DEFAULT_TENANT_ID)

        assert [e.item_id for e in result.evicted] == ["low-score"]
        assert (DEFAULT_TENANT_ID, "low-score") in wired_system.zone2_store.evicted
        assert wired_system.event_bus.events_of_type("memory.evicted")

    def test_dpdp_erasure_cascade_reports_no_pending_obligation_by_default(
        self, wired_system: WiredSystem
    ) -> None:
        fulfilled = wired_system.dpdp_erasure_cascade.fulfil_pending_erasure(
            DEFAULT_TENANT_ID, "item-1"
        )

        assert fulfilled is False

    def test_dpdp_erasure_cascade_fulfils_a_pending_obligation_for_real(
        self, wired_system: WiredSystem
    ) -> None:
        as_of = datetime(2026, 1, 1, tzinfo=UTC)
        wired_system.zone2_store.seed_candidate(
            DEFAULT_TENANT_ID,
            EvictionCandidate(
                item_id="erase-me",
                memory_score=0.5,
                is_compressed=False,
                age_seconds=10.0,
                written_at=as_of,
            ),
        )
        wired_system.retrieval_index_repository.index_item(
            tenant_id=DEFAULT_TENANT_ID,
            item_id="erase-me",
            source_zone=ZoneId.EPISODIC,
            text=PLACEHOLDER_PAYLOAD,
            vector=(1.0, 0.0),
            model_id="test-model",
        )
        wired_system.erasure_obligation_store.request_erasure(
            DEFAULT_TENANT_ID, "erase-me"
        )

        fulfilled = wired_system.dpdp_erasure_cascade.fulfil_pending_erasure(
            DEFAULT_TENANT_ID, "erase-me"
        )

        assert fulfilled is True
        assert (DEFAULT_TENANT_ID, "erase-me") in wired_system.zone2_store.evicted
        assert not wired_system.erasure_obligation_store.has_pending_erasure(
            DEFAULT_TENANT_ID, "erase-me"
        )

    def test_episodic_repository_append_issues_real_insert_sql(
        self, wired_system: WiredSystem
    ) -> None:
        """Zone 2 (Part B): a real `SqlEpisodicRepository.append` call.

        Drives the adapter's real parameterized-SQL generation against the
        fake `episodic_connection` DB-API double and confirms the exact
        statement/params recorded -- the same assertion shape
        `test_smoke_episodic.py` itself uses for this class.
        """
        occurred = datetime(2026, 1, 1, tzinfo=UTC)
        episode = Episode(
            tenant_id=DEFAULT_TENANT_ID,
            session_id="session-1",
            episode_id=Episode.episode_id_for(DEFAULT_TENANT_ID, "session-1", 0),
            seq=0,
            occurred_at=occurred,
            written_at=occurred,
            payload=PLACEHOLDER_PAYLOAD,
            actors=("user", "agent"),
            token_count=10,
            state=EpisodeState.ACTIVE,
            score_terms={},
        )

        wired_system.episodic_repository.append(episode)

        assert len(wired_system.episodic_connection.cursor_obj.executed) == 1
        sql_used, params_used = wired_system.episodic_connection.cursor_obj.executed[0]
        assert sql_used.strip().upper().startswith("INSERT INTO EPISODIC_ENTRIES")
        assert params_used[0] == DEFAULT_TENANT_ID

    def test_episodic_repository_is_registered_and_readable_via_orchestrator(
        self, wired_system: WiredSystem
    ) -> None:
        """Zone 2 (Part B): reachable through `MemoryOrchestrator`, not only directly.

        An empty `episodic_connection` (no seeded rows) is a real,
        successful zero-result fetch -- `ZoneId.EPISODIC` must therefore
        route through `episodic_repository` and never be reported
        unavailable, confirming `zone_repositories` actually registered it.
        """
        result = wired_system.memory_orchestrator.assemble_context(
            ContextAssemblyRequest(
                tenant_id=DEFAULT_TENANT_ID,
                session_id="session-1",
                task="recall the last order",
                token_budget=1000,
                max_items=10,
                min_provenance_conf=0.0,
            )
        )

        assert ZoneId.EPISODIC not in result.zones_unavailable
        executed = wired_system.episodic_connection.cursor_obj.executed
        assert len(executed) == 1
        sql_used, params_used = executed[0]
        assert sql_used.strip().upper().startswith("SELECT")
        assert params_used[0] == DEFAULT_TENANT_ID

    def test_provenance_repository_append_issues_real_insert_sql(
        self, wired_system: WiredSystem
    ) -> None:
        """Zone 7 (Part B): a real `SqlProvenanceRepository.append` call.

        `ProvenanceRecord.create` is the only sanctioned constructor (its
        own class docstring) -- it computes `confidence` and `record_hash`
        for real, so this exercises the full real hashing/confidence logic,
        not just the SQL layer, before the record reaches the adapter.
        """
        record = ProvenanceRecord.create(
            tenant_id=DEFAULT_TENANT_ID,
            provenance_id="prov-1",
            item_id="item-1",
            source_zone=ZoneId.WORKING,
            source_type=SourceType.SYSTEM_DERIVED,
            write_timestamp=datetime(2026, 1, 1, tzinfo=UTC),
            actor="integration-fixture",
            change="initial write",
            retrieval_context_hash="a" * 64,
        )

        wired_system.provenance_repository.append(record)

        assert len(wired_system.provenance_connection.cursor_obj.executed) == 1
        sql_used, params_used = wired_system.provenance_connection.cursor_obj.executed[0]
        assert sql_used.strip().upper().startswith("INSERT INTO PROVENANCE_RECORDS")
        assert params_used[0] == DEFAULT_TENANT_ID

    def test_provenance_repository_find_by_item_id_issues_real_select_sql(
        self, wired_system: WiredSystem
    ) -> None:
        """Zone 7 (Part B): a real `SqlProvenanceRepository.find_by_item_id` call.

        An empty `provenance_connection` (no seeded rows) is a real,
        successful empty-lineage read -- confirms the read path's SQL
        generation and tenant_id guard, mirroring
        `SqlEpisodicRepository`'s equivalent smoke coverage above.
        """
        records = wired_system.provenance_repository.find_by_item_id(
            DEFAULT_TENANT_ID, "item-1"
        )

        assert records == []
        sql_used, params_used = wired_system.provenance_connection.cursor_obj.executed[0]
        assert sql_used.strip().upper().startswith("SELECT")
        assert params_used == (DEFAULT_TENANT_ID, "item-1")
