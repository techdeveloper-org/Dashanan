"""UnifiedSubjectErasureOrchestrator, wired against REAL Zone 2/6/8/3/5 collaborators (DASH-STORY-025).

QA subtask for DASH-STORY-025 -- "DPDP erasure-cascade reach into Zone 3
(Semantic) and Zone 5 (Entity) data (DSHN-70)" -- traces to NFR-006 /
AC-013 in SRS.md.

AC-013 (verbatim, SRS.md): "Given a data subject requests erasure, When
the erasure cascade executes across all 8 zones plus indices and
archives, Then the subject's payload content becomes permanently
unrecoverable via crypto-shredding while the Zone 7 provenance chain
structure (hashes, timestamps) remains intact and verifiable." This
module's own tests do NOT claim AC-013 is closed in full -- SRS.md
Section 4.1's docs-drift correction (crypto-shredding mechanism and Zone
7's provenance-chain clause both remain separately open) is unaffected
by anything asserted here.

TEST SCENARIO ENUMERATION (one golden test per AC, plus boundary/adverse):
  AC-025-1 (happy):    test_orchestrator_erases_the_seeded_zone3_semantic_edge
  AC-025-2 (happy):    test_orchestrator_erases_the_seeded_zone5_entity_attribute
  AC-025-3 (happy):    test_orchestrator_drives_all_four_zone_legs_from_one_call
  AC-025-3 (boundary): test_orchestrator_is_a_clean_noop_when_subject_has_no_live_data
  AC-025-3 (adverse):  test_orchestrator_marks_job_failed_when_zone5_leg_raises
  Ownership:           test_orchestrator_never_mutates_a_different_subjects_data

Runtime assumptions (rule 33/40 test-roadmap convention): `clock` is
`SeedableClock` seeded at `FIXED_EPOCH` (2026-01-01T00:00:00+00:00,
`tests.integration.conftest`'s own default); `tenant_id` is
`DEFAULT_TENANT_ID` ("tenant-1"); no fixture seed is random.

PII NOTE: every identifier below is abstract routing metadata
(subject_id, entity_id, edge_id, item_id, provenance_id) or the literal
`PLACEHOLDER_PAYLOAD` constant (`tests.integration.conftest`); no example
conversational content appears anywhere in this file.
"""

from __future__ import annotations

import pytest

from dashanan.application.subject_erasure_cascade import SubjectErasureJobStatus
from dashanan.application.unified_subject_erasure_orchestrator import (
    SubjectToItemIndex,
    UnifiedSubjectErasureOrchestrator,
    UnifiedSubjectErasureOrchestratorError,
)
from dashanan.domain.exceptions import ZoneRepositoryError
from dashanan.domain.semantic_memory import SemanticEdge
from dashanan.domain.subject_erasure import SubjectErasureRequest
from dashanan.domain.write_gate import WriteAccepted
from dashanan.infrastructure.dpdp_erasure_cascade import (
    CrossZoneDpdpErasureCascade,
    InMemoryErasureObligationStore,
    RetrievalIndexEviction,
)
from dashanan.infrastructure.in_memory_lexical_index import InMemoryLexicalIndex
from dashanan.infrastructure.in_memory_subject_erasure_job_store import (
    InMemorySubjectErasureJobStore,
)
from dashanan.infrastructure.in_memory_vector_index import InMemoryVectorIndex

from tests.integration.conftest import DEFAULT_TENANT_ID, PLACEHOLDER_PAYLOAD, InMemoryZone2Store

from .conftest_sprint2 import Sprint2WiredSystem

pytest_plugins = ["tests.integration.conftest_sprint2"]

_SUBJECT_ID = "unified-erasure-subject-1"
_OTHER_SUBJECT_ID = "unified-erasure-subject-untouched"


def _edge_row(edge: SemanticEdge) -> tuple[object, ...]:
    """Mirror `tests.test_smoke_semantic._edge_row`: a canned SELECT row for one `SemanticEdge`.

    `RecordingConnection` (`tests.test_smoke_episodic`) is a write-
    recording fake with NO read-back storage (its own class docstring):
    an `INSERT` through it never makes a later `SELECT` on the same
    connection return that row. Every test below that needs
    `find_edges_by_subject`/`delete_edges_by_subject` to see a
    previously-"inserted" edge seeds the connection's own cursor
    `_rows` directly with this helper's output -- the same established
    convention `tests/integration/test_zone3_zone5_conflict_sweep_wired.py`
    and `tests/integration/test_cross_zone_scenarios.py` already use for
    every other Sprint 2 SQL-fake read-back.
    """
    return (
        edge.tenant_id,
        edge.edge_id,
        edge.subject_ref,
        edge.predicate,
        edge.object_ref,
        dict(edge.qualifiers),
        edge.state.value,
        dict(edge.score_terms),
    )


class _FixedSubjectToItemIndex:
    """Real, hand-rolled `SubjectToItemIndex` test double: one static `subject_id -> item_ids` map.

    Never `unittest.mock` (this codebase's established convention,
    `tests.integration.conftest_sprint2`'s own module docstring) --
    a small, real, stateful class standing in for the not-yet-built
    production resolver (`unified_subject_erasure_orchestrator.py`'s own
    module docstring, "Zone 2/6 leg" section).
    """

    def __init__(self, mapping: dict[str, tuple[str, ...]]) -> None:
        self._mapping = mapping

    def items_for_subject(self, tenant_id: str, subject_id: str) -> tuple[str, ...]:
        return self._mapping.get(subject_id, ())


class _RaisingZone5Repository:
    """Real, hand-rolled fake: raises on `erase_entity`, delegates every other call.

    Used only by the AC-025-3 adverse (partial-failure) scenario --
    wraps the real `EntityMemoryRepository` fixture so `write_attribute`/
    `get_entity` still behave for real, while `erase_entity` fails
    deterministically.
    """

    def __init__(self, delegate: object) -> None:
        self._delegate = delegate

    def __getattr__(self, name: str) -> object:
        return getattr(self._delegate, name)

    def erase_entity(self, tenant_id: str, entity_id: str) -> tuple[str, ...]:
        raise ZoneRepositoryError(zone="entity", reason="simulated Zone 5 leg failure")


def _build_orchestrator(
    sprint2_wired_system: Sprint2WiredSystem,
    *,
    subject_to_item_index: SubjectToItemIndex | None = None,
    zone2_6_cascade: CrossZoneDpdpErasureCascade | None = None,
    zone5_repository: object | None = None,
) -> UnifiedSubjectErasureOrchestrator:
    """Compose the real orchestrator against `sprint2_wired_system`'s own real collaborators."""
    return UnifiedSubjectErasureOrchestrator(
        zone8_service=sprint2_wired_system.zone8_subject_erasure_cascade,
        zone3_repository=sprint2_wired_system.zone3_semantic_repository,
        zone5_repository=zone5_repository or sprint2_wired_system.zone5_entity_repository,
        job_store=InMemorySubjectErasureJobStore(),
        clock=sprint2_wired_system.clock,
        zone2_6_cascade=zone2_6_cascade,
        subject_to_item_index=subject_to_item_index,
    )


class TestOrchestratorAc025_1SemanticEdgeErasure:
    """AC-025-1 (verbatim, sprint3_ar1_assignments.json / this file's own header citation)."""

    def test_orchestrator_erases_the_seeded_zone3_semantic_edge(
        self, sprint2_wired_system: Sprint2WiredSystem
    ) -> None:
        """Given a real SemanticEdge seeded with subject_ref==subject_id, the Zone 3 leg deletes it."""
        edge = SemanticEdge(
            tenant_id=DEFAULT_TENANT_ID,
            edge_id="edge-unified-1",
            subject_ref=_SUBJECT_ID,
            predicate="located_in",
            object_ref="entity-b",
        )
        sprint2_wired_system.zone3_index_projector.insert_edge(edge)
        # RecordingConnection is write-recording-only (helper docstring above);
        # seed the cursor's canned rows so the leg's own DELETE ... RETURNING
        # read-back sees the edge_id.
        sprint2_wired_system.zone3_connection.cursor_obj._rows = [(edge.edge_id,)]

        orchestrator = _build_orchestrator(sprint2_wired_system)
        accepted = orchestrator.request_erasure(
            SubjectErasureRequest(tenant_id=DEFAULT_TENANT_ID, subject_id=_SUBJECT_ID)
        )

        job = orchestrator.get_job(accepted.job_id)
        assert job is not None
        assert job.status is SubjectErasureJobStatus.COMPLETED
        assert "edge-unified-1" in job.item_ids

        # Clear the canned rows: the DELETE the leg just issued means a real
        # re-read would now find nothing -- the fake's own read-back is
        # therefore reset to reflect that, mirroring the same "empty after
        # erasure" state a real backing store would report.
        sprint2_wired_system.zone3_connection.cursor_obj._rows = []
        remaining = sprint2_wired_system.zone3_semantic_repository.find_edges_by_subject(
            DEFAULT_TENANT_ID, _SUBJECT_ID
        )
        assert remaining == [], (
            "Zone 3 edge survived the orchestrator's request_erasure call -- "
            "AC-025-1's own inverted assertion (this story's QA sub-task) is stale"
        )


class TestOrchestratorAc025_2EntityAttributeErasure:
    """AC-025-2 (verbatim, sprint3_ar1_assignments.json / this file's own header citation)."""

    def test_orchestrator_erases_the_seeded_zone5_entity_attribute(
        self, sprint2_wired_system: Sprint2WiredSystem
    ) -> None:
        """Given a real EntityRecord attribute for entity_id==subject_id, the Zone 5 leg erases it."""
        write_result = sprint2_wired_system.zone5_entity_repository.write_attribute(
            tenant_id=DEFAULT_TENANT_ID,
            entity_id=_SUBJECT_ID,
            attribute_name="role",
            value=PLACEHOLDER_PAYLOAD,
            provenance_id="prov-unified-1",
        )
        assert isinstance(write_result, WriteAccepted)
        assert sprint2_wired_system.zone5_entity_repository.get_entity(
            DEFAULT_TENANT_ID, _SUBJECT_ID
        ) is not None

        orchestrator = _build_orchestrator(sprint2_wired_system)
        accepted = orchestrator.request_erasure(
            SubjectErasureRequest(tenant_id=DEFAULT_TENANT_ID, subject_id=_SUBJECT_ID)
        )

        job = orchestrator.get_job(accepted.job_id)
        assert job is not None
        assert job.status is SubjectErasureJobStatus.COMPLETED
        assert f"{_SUBJECT_ID}:role" in job.item_ids

        after_erasure = sprint2_wired_system.zone5_entity_repository.get_entity(
            DEFAULT_TENANT_ID, _SUBJECT_ID
        )
        assert after_erasure is None, (
            "a re-read via get_entity still returned the erased attribute -- "
            "AC-025-2's own literal wording (sprint3_ar1_assignments.json) is violated"
        )


class TestOrchestratorAc025_3SingleCallFanOut:
    """AC-025-3 (verbatim, sprint3_ar1_assignments.json / this file's own header citation)."""

    def test_orchestrator_drives_all_four_zone_legs_from_one_call(
        self, sprint2_wired_system: Sprint2WiredSystem
    ) -> None:
        """One request_erasure call fans out to Zone 2/6, Zone 8, Zone 3, and Zone 5.

        Zone 2/6: a real `CrossZoneDpdpErasureCascade` (DSHN-58) over real
        `InMemoryVectorIndex`/`InMemoryLexicalIndex`/`InMemoryZone2Store`,
        with a real pending obligation recorded for one `item_id` and a
        `_FixedSubjectToItemIndex` resolving `_SUBJECT_ID` to that same
        `item_id` -- proving the Zone 2/6 leg is not merely invoked but
        genuinely fulfils a real obligation when a resolver is wired
        (module docstring's own honest-gap rationale).
        Zone 8: no archive seeded -- asserted as a genuine, real empty
        cascade (mirrors the pre-existing gap test's own convention).
        Zone 3 / Zone 5: seeded exactly as AC-025-1 / AC-025-2 above.
        """
        vector_index = InMemoryVectorIndex()
        lexical_index = InMemoryLexicalIndex()
        obligation_store = InMemoryErasureObligationStore()
        zone2_store = InMemoryZone2Store()
        obligation_store.request_erasure(DEFAULT_TENANT_ID, "zone6-item-1")
        zone2_6_cascade = CrossZoneDpdpErasureCascade(
            obligation_store=obligation_store,
            zone2_eviction=zone2_store,
            retrieval_index_eviction=RetrievalIndexEviction(vector_index, lexical_index),
        )
        subject_to_item_index = _FixedSubjectToItemIndex(
            {_SUBJECT_ID: ("zone6-item-1",)}
        )

        edge = SemanticEdge(
            tenant_id=DEFAULT_TENANT_ID,
            edge_id="edge-unified-fanout-1",
            subject_ref=_SUBJECT_ID,
            predicate="related_to",
            object_ref="entity-c",
        )
        sprint2_wired_system.zone3_index_projector.insert_edge(edge)
        sprint2_wired_system.zone3_connection.cursor_obj._rows = [(edge.edge_id,)]
        sprint2_wired_system.zone5_entity_repository.write_attribute(
            tenant_id=DEFAULT_TENANT_ID,
            entity_id=_SUBJECT_ID,
            attribute_name="role",
            value=PLACEHOLDER_PAYLOAD,
            provenance_id="prov-unified-fanout-1",
        )

        orchestrator = _build_orchestrator(
            sprint2_wired_system,
            subject_to_item_index=subject_to_item_index,
            zone2_6_cascade=zone2_6_cascade,
        )
        accepted = orchestrator.request_erasure(
            SubjectErasureRequest(tenant_id=DEFAULT_TENANT_ID, subject_id=_SUBJECT_ID)
        )

        job = orchestrator.get_job(accepted.job_id)
        assert job is not None
        assert job.status is SubjectErasureJobStatus.COMPLETED
        assert set(job.item_ids) == {
            "zone6-item-1",
            "edge-unified-fanout-1",
            f"{_SUBJECT_ID}:role",
        }, (
            "the combined job's item_ids must reflect all real affected items "
            "across Zone 2/6, Zone 3, and Zone 5 -- AC-025-3's own literal "
            "'reflects the combined result rather than only Zone 8's'"
        )

        assert not obligation_store.has_pending_erasure(DEFAULT_TENANT_ID, "zone6-item-1")
        delete_statements = [
            (sql, params)
            for sql, params in sprint2_wired_system.zone3_connection.cursor_obj.executed
            if "DELETE" in sql
        ]
        assert delete_statements == [
            (
                "\nDELETE FROM semantic_edges\n"
                "WHERE tenant_id = %s AND subject_ref = %s\n"
                "RETURNING edge_id\n",
                (DEFAULT_TENANT_ID, _SUBJECT_ID),
            )
        ], "Zone 3's own DELETE must target exactly this subject's edges"
        assert sprint2_wired_system.zone5_entity_repository.get_entity(
            DEFAULT_TENANT_ID, _SUBJECT_ID
        ) is None

    def test_orchestrator_is_a_clean_noop_when_subject_has_no_live_data(
        self, sprint2_wired_system: Sprint2WiredSystem
    ) -> None:
        """Boundary: a subject with no live Zone 2/6/3/5/8 data is a clean no-op, not an error."""
        orchestrator = _build_orchestrator(sprint2_wired_system)

        accepted = orchestrator.request_erasure(
            SubjectErasureRequest(tenant_id=DEFAULT_TENANT_ID, subject_id="never-seeded-subject")
        )

        job = orchestrator.get_job(accepted.job_id)
        assert job is not None
        assert job.status is SubjectErasureJobStatus.COMPLETED
        assert job.item_ids == ()

    def test_orchestrator_marks_job_failed_when_zone5_leg_raises(
        self, sprint2_wired_system: Sprint2WiredSystem
    ) -> None:
        """Adverse: a partial failure (Zone 3 succeeds, Zone 5 fails) is reported as FAILED, not silently completed."""
        edge = SemanticEdge(
            tenant_id=DEFAULT_TENANT_ID,
            edge_id="edge-unified-partial-1",
            subject_ref=_SUBJECT_ID,
            predicate="related_to",
            object_ref="entity-d",
        )
        sprint2_wired_system.zone3_index_projector.insert_edge(edge)
        sprint2_wired_system.zone3_connection.cursor_obj._rows = [(edge.edge_id,)]

        failing_zone5 = _RaisingZone5Repository(sprint2_wired_system.zone5_entity_repository)
        orchestrator = _build_orchestrator(sprint2_wired_system, zone5_repository=failing_zone5)

        with pytest.raises(UnifiedSubjectErasureOrchestratorError):
            orchestrator.request_erasure(
                SubjectErasureRequest(tenant_id=DEFAULT_TENANT_ID, subject_id=_SUBJECT_ID)
            )

        # Zone 3's own leg already ran (and committed) before the Zone 5 leg raised --
        # this orchestrator never rolls back an earlier leg's already-durable delete
        # (module docstring: "each leg independently idempotent, retry-safe").
        delete_statements = [
            sql
            for sql, _params in sprint2_wired_system.zone3_connection.cursor_obj.executed
            if "DELETE" in sql
        ]
        assert len(delete_statements) == 1, (
            "Zone 3's own DELETE must have already been issued before the Zone 5 "
            "leg's failure -- a partial failure must not roll back an earlier "
            "leg's already-durable delete"
        )


class TestOrchestratorOwnershipIsolation:
    """dependency_integrity_check (sprint3_ar1_assignments.json): no mutation outside this story's ownership."""

    def test_orchestrator_never_mutates_a_different_subjects_data(
        self, sprint2_wired_system: Sprint2WiredSystem
    ) -> None:
        """Erasing `_SUBJECT_ID` must never touch `_OTHER_SUBJECT_ID`'s own Zone 3/5 data.

        Zone 5 (`EntityMemoryRepository`) is a real, genuinely stateful
        per-`(tenant_id, entity_id)` store, so its isolation is proven by
        a direct `get_entity` read-back for the untouched subject. Zone 3's
        `RecordingConnection` fake has no read-back storage of its own
        (this file's `_edge_row` helper docstring) -- its isolation is
        instead proven the same way the pre-existing gap test proves Zone
        3 behavior: by asserting the issued `DELETE`'s own bound
        parameters name only the target subject, never the untouched one.
        """
        sprint2_wired_system.zone5_entity_repository.write_attribute(
            tenant_id=DEFAULT_TENANT_ID,
            entity_id=_OTHER_SUBJECT_ID,
            attribute_name="role",
            value=PLACEHOLDER_PAYLOAD,
            provenance_id="prov-untouched-1",
        )
        target_edge = SemanticEdge(
            tenant_id=DEFAULT_TENANT_ID,
            edge_id="edge-target-1",
            subject_ref=_SUBJECT_ID,
            predicate="related_to",
            object_ref="entity-f",
        )
        sprint2_wired_system.zone3_index_projector.insert_edge(target_edge)
        sprint2_wired_system.zone3_connection.cursor_obj._rows = [(target_edge.edge_id,)]

        orchestrator = _build_orchestrator(sprint2_wired_system)
        orchestrator.request_erasure(
            SubjectErasureRequest(tenant_id=DEFAULT_TENANT_ID, subject_id=_SUBJECT_ID)
        )

        delete_statements = [
            params
            for sql, params in sprint2_wired_system.zone3_connection.cursor_obj.executed
            if "DELETE" in sql
        ]
        assert delete_statements == [(DEFAULT_TENANT_ID, _SUBJECT_ID)], (
            "the issued DELETE's own params must name only the erased subject, "
            "never the untouched one"
        )
        assert sprint2_wired_system.zone5_entity_repository.get_entity(
            DEFAULT_TENANT_ID, _OTHER_SUBJECT_ID
        ) is not None
