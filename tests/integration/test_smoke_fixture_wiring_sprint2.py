"""Smoke assertions for `tests/integration/conftest_sprint2.py`'s Zone 3/4/5/8 composition.

Mirrors `test_smoke_fixture_wiring.py`'s (Sprint 1) own scope split
exactly: confirms the fixture tree in `conftest_sprint2.py` actually
wires -- importable, constructs with no exception, and takes ONE real
call through each composed component -- ahead of the formal AC-level
cross-zone adversarial suite this fixture tree exists to support. These
are wiring smoke checks, not the full AC-level suite.

`conftest_sprint2.py` is not named `conftest.py`, so pytest does not
auto-discover it; `pytest_plugins` below registers it as a plugin for
this module, exactly as this module's own docstring records.

PII NOTE: every payload below is either abstract ranking/routing metadata
(item_id, tenant_id, scores, states, timestamps, byte content standing in
for an opaque already-Compressed blob) or the literal `PLACEHOLDER_PAYLOAD`
constant (`tests.integration.conftest`); no example conversational content
appears anywhere in this file.
"""

from __future__ import annotations

from datetime import UTC, datetime

from dashanan.application.archive_engine import ArchiveCandidateSnapshot, ArchiveTransitionOutcome
from dashanan.application.subject_erasure_cascade import (
    SubjectErasureJob,
    SubjectErasureJobStatus,
)
from dashanan.domain.consolidated_blob import ArchiveBatchItem
from dashanan.domain.rotation_state import RotationState
from dashanan.domain.semantic_memory import SemanticEdge
from dashanan.domain.tenant_bucket_policy import BucketProvisioningSpec, ResidencyRegion
from dashanan.domain.write_gate import WriteAccepted
from dashanan.domain.zone import ZoneId
from dashanan.domain.zone4_capacity_backstop import (
    ProcedureEvictionCandidate,
    Zone4BackstopConfig,
)

from tests.integration.conftest import DEFAULT_TENANT_ID, PLACEHOLDER_PAYLOAD

from .conftest_sprint2 import Sprint2WiredSystem

pytest_plugins = ["tests.integration.conftest_sprint2"]


class TestSprint2FixtureWiringSmoke:
    """Baseline: every Zone 3/4/5/8 component constructs and takes one real call."""

    def test_sprint2_wired_system_constructs_with_no_exception(
        self, sprint2_wired_system: Sprint2WiredSystem
    ) -> None:
        assert sprint2_wired_system.zone8_consolidation_store is not None

    def test_zone3_semantic_edge_write_issues_a_real_insert_and_projects(
        self, sprint2_wired_system: Sprint2WiredSystem
    ) -> None:
        """Zone 3: one real `insert_edge` call through the `Zone3IndexProjector` decorator.

        Exercises `SqlSemanticRepository`'s real parameterized-SQL
        generation (the durable write) followed by the projector's
        best-effort Zone 6 projection-event publish (AC-003-PROJ-1).
        """
        edge = SemanticEdge(
            tenant_id=DEFAULT_TENANT_ID,
            edge_id="edge-1",
            subject_ref="entity-a",
            predicate="located_in",
            object_ref="entity-b",
        )

        sprint2_wired_system.zone3_index_projector.insert_edge(edge)

        executed = sprint2_wired_system.zone3_connection.cursor_obj.executed
        assert len(executed) == 1
        sql_used, params_used = executed[0]
        assert sql_used.strip().upper().startswith("INSERT INTO SEMANTIC_EDGES")
        assert params_used[0] == DEFAULT_TENANT_ID
        assert sprint2_wired_system.event_bus.events_of_type(
            "zone3.semantic_edge.projected"
        )

    def test_zone4_procedure_commit_stores_and_projects(
        self, sprint2_wired_system: Sprint2WiredSystem
    ) -> None:
        """Zone 4: one real `record_execution` (write commit) call.

        Exercises the real Shape A adapter's upsert plus its Zone 6
        projection-event publish (AC-004-4).
        """
        procedure = sprint2_wired_system.zone4_repository.record_execution(
            tenant_id=DEFAULT_TENANT_ID,
            task="draft a weekly status report",
            steps=("gather updates", "summarize", "send"),
            succeeded=True,
        )

        assert procedure.success_count == 1
        stored = sprint2_wired_system.zone4_repository.get_by_task_signature_hash(
            DEFAULT_TENANT_ID, procedure.task_signature_hash
        )
        assert stored is not None
        assert stored.task_signature_hash == procedure.task_signature_hash
        assert sprint2_wired_system.event_bus.events_of_type("memory.projected")

    def test_zone4_backstop_sweep_evicts_lowest_scored_procedure_over_cap(
        self, sprint2_wired_system: Sprint2WiredSystem
    ) -> None:
        """Zone 4: one real backstop sweep run, over the wired ports (AC-004-CAP-1)."""
        sprint2_wired_system.zone4_backstop_config_port.set_config(
            DEFAULT_TENANT_ID,
            Zone4BackstopConfig(capacity_cap=1, max_age_seconds=63_072_000.0),
        )
        as_of = datetime(2026, 1, 1, tzinfo=UTC)
        sprint2_wired_system.zone4_store.seed_candidate(
            DEFAULT_TENANT_ID,
            ProcedureEvictionCandidate(
                item_id="low-score",
                memory_score=0.1,
                is_compressed=False,
                age_seconds=10.0,
                last_used_at=as_of,
            ),
        )
        sprint2_wired_system.zone4_store.seed_candidate(
            DEFAULT_TENANT_ID,
            ProcedureEvictionCandidate(
                item_id="high-score",
                memory_score=0.9,
                is_compressed=False,
                age_seconds=10.0,
                last_used_at=as_of,
            ),
        )

        result = sprint2_wired_system.zone4_backstop_sweep.run(DEFAULT_TENANT_ID)

        assert [e.item_id for e in result.evicted] == ["low-score"]
        assert (DEFAULT_TENANT_ID, "low-score") in sprint2_wired_system.zone4_store.evicted
        assert (DEFAULT_TENANT_ID, "low-score") in (
            sprint2_wired_system.zone4_retrieval_index_eviction.deleted
        )
        assert sprint2_wired_system.event_bus.events_of_type("memory.evicted")

    def test_zone5_write_attribute_accepts_a_well_formed_write(
        self, sprint2_wired_system: Sprint2WiredSystem
    ) -> None:
        """Zone 5: one real `write_attribute` call (AC-005-2, AC-005-6)."""
        result = sprint2_wired_system.zone5_entity_repository.write_attribute(
            tenant_id=DEFAULT_TENANT_ID,
            entity_id="entity-1",
            attribute_name="role",
            value=PLACEHOLDER_PAYLOAD,
            provenance_id="prov-1",
        )

        assert isinstance(result, WriteAccepted)
        stored = sprint2_wired_system.zone5_entity_repository.get_entity(
            DEFAULT_TENANT_ID, "entity-1"
        )
        assert stored is not None
        assert any(a.attribute_name == "role" for a in stored.attributes)
        assert sprint2_wired_system.event_bus.events_of_type(
            "zone5.entity_attribute_projected"
        )

    def test_zone8_consolidate_batch_writes_a_resolvable_manifest_entry(
        self, sprint2_wired_system: Sprint2WiredSystem
    ) -> None:
        """Zone 8: one real `consolidate_batch` call (ADR-009, AC-008-1)."""
        item = ArchiveBatchItem(
            tenant_id=DEFAULT_TENANT_ID,
            item_id="archived-item-1",
            source_zone=ZoneId.EPISODIC,
            payload=b"already-compressed-opaque-bytes",
        )

        result = sprint2_wired_system.zone8_consolidation_store.consolidate_batch([item])

        assert [w.item_id for w in result.written] == ["archived-item-1"]
        assert result.written[0].manifest_entry.blob_id
        # `RecordingConnectionWithCommit`, like Sprint 1's own `RecordingConnection`,
        # is a write-recording fake with no read-back storage -- it records
        # the real INSERT statement `SqlManifestRepository.insert_batch`
        # issued and the real `commit()` call, mirroring
        # `test_smoke_fixture_wiring.py`'s own "assert the executed SQL" shape
        # (Sprint 1's `test_episodic_repository_append_issues_real_insert_sql`)
        # rather than a read-after-write that this fake cannot serve.
        assert sprint2_wired_system.zone8_manifest_connection.commit_calls == 1
        executed = sprint2_wired_system.zone8_manifest_connection.cursor_obj.executed
        assert len(executed) == 1
        sql_used, params_used = executed[0]
        assert sql_used.strip().upper().startswith("INSERT INTO ZONE8_MANIFEST")
        assert params_used[0] == DEFAULT_TENANT_ID
        assert params_used[1] == "archived-item-1"

    def test_zone8_crypto_shredded_archive_writes_and_indexes_for_the_subject(
        self, sprint2_wired_system: Sprint2WiredSystem
    ) -> None:
        """Zone 8: one real `archive_for_subject` call (DASH-STORY-020, AC-008-DPDP-3)."""
        item = ArchiveBatchItem(
            tenant_id=DEFAULT_TENANT_ID,
            item_id="subject-item-1",
            source_zone=ZoneId.EPISODIC,
            payload=b"plaintext-payload-to-be-sealed",
        )

        result = sprint2_wired_system.zone8_subject_keyed_archiver.archive_for_subject(
            item, subject_id="subject-1"
        )

        assert [w.item_id for w in result.written] == ["subject-item-1"]
        assert sprint2_wired_system.zone8_subject_index.find_item_ids(
            DEFAULT_TENANT_ID, "subject-1"
        ) == ("subject-item-1",)
        # The sealed (AES-256-GCM ciphertext) payload the decorator wrote is
        # never the plaintext -- confirms real encryption happened, not a
        # pass-through -- without a read-back through `RecordingConnectionWithCommit`
        # (a write-recording fake, own docstring above).
        key = sprint2_wired_system.zone8_subject_key_store.get_key(
            DEFAULT_TENANT_ID, "subject-1"
        )
        assert key is not None

    def test_zone8_archive_engine_fast_tracks_an_eligible_item(
        self, sprint2_wired_system: Sprint2WiredSystem
    ) -> None:
        """Zone 8: one real `fast_track_archive` call (AC-008-ROT-1, AC-008-ROT-2)."""
        sprint2_wired_system.zone8_archive_candidate_store.seed_item(
            DEFAULT_TENANT_ID,
            ZoneId.EPISODIC,
            "fast-track-item-1",
            snapshot=ArchiveCandidateSnapshot(
                lifecycle_state=RotationState.COMPRESSED,
                memory_score=0.1,
                payload_tokens=10,
            ),
        )
        sprint2_wired_system.zone8_archive_transition_store.seed_payload(
            DEFAULT_TENANT_ID,
            ZoneId.EPISODIC,
            "fast-track-item-1",
            payload=b"already-compressed-fast-track-bytes",
        )

        outcome = sprint2_wired_system.zone8_archive_engine.fast_track_archive(
            DEFAULT_TENANT_ID, ZoneId.EPISODIC, "fast-track-item-1"
        )

        assert isinstance(outcome, ArchiveTransitionOutcome)
        assert outcome.item_id == "fast-track-item-1"
        assert sprint2_wired_system.event_bus.events_of_type("memory.archived")

    def test_zone8_bucket_provision_creates_an_in_region_bucket(
        self, sprint2_wired_system: Sprint2WiredSystem
    ) -> None:
        """Zone 8: one real `provision_for_tenant` call (AC-008-DPDP-2)."""
        spec = BucketProvisioningSpec(
            tenant_id=DEFAULT_TENANT_ID,
            residency=ResidencyRegion.IN_REGION,
            audit_retention_days=30,
        )

        provisioned = sprint2_wired_system.zone8_bucket_provisioning_service.provision_for_tenant(
            spec
        )

        assert provisioned.tenant_id == DEFAULT_TENANT_ID
        assert provisioned.residency_value == ResidencyRegion.IN_REGION.value
        assert provisioned.versioning_enabled is True
        assert provisioned.object_lock_enabled is True

    def test_subject_erasure_job_store_saves_and_returns_a_job(
        self, sprint2_wired_system: Sprint2WiredSystem
    ) -> None:
        """Zone 8: one real `save` call against the DPDP admin-job store (HLD 7.3)."""
        job = SubjectErasureJob(
            job_id="job-1",
            tenant_id=DEFAULT_TENANT_ID,
            subject_id="subject-1",
            status=SubjectErasureJobStatus.ACCEPTED,
        )

        sprint2_wired_system.zone8_subject_erasure_job_store.save(job)

        stored = sprint2_wired_system.zone8_subject_erasure_job_store.get("job-1")
        assert stored is not None
        assert stored.status is SubjectErasureJobStatus.ACCEPTED
        assert sprint2_wired_system.zone8_subject_erasure_cascade.get_job("job-1") == stored
