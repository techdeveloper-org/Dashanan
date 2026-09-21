"""Cross-zone DPDP erasure scope: `SubjectErasureCascadeService` reaches only Zone 8 (DASH-STORY-025).

HISTORY (DASH-STORY-025 / DSHN-70 update): this module originally
documented a gap -- `SubjectErasureCascadeService` was "the Zone-8-reaching
HALF" of HLD Section 7.4's `DELETE /v1/tenants/{id}/subjects/{subject_id}`
cascade, and a real `SemanticEdge`/`EntityRecord` for the erased
`subject_id` survived a `request_erasure` call untouched. AC-025-1 and
AC-025-2 (sprint3_ar1_assignments.json, DASH-STORY-025) require this
module's own two survival assertions to be inverted -- this file's own
citation of that requirement. `SubjectErasureCascadeService` itself is
UNCHANGED by DASH-STORY-025 (it remains the Zone-8-reaching half); what
changed is that Zone 3 (`SqlSemanticRepository.delete_edges_by_subject`)
and Zone 5 (`EntityMemoryRepository.erase_entity`) now each have their own
real erasure leg, exercised directly here (not through
`SubjectErasureCascadeService`, which still does not compose them --
`UnifiedSubjectErasureOrchestrator`, DASH-STORY-025's own new Facade, is
the component that composes all four legs behind one call; see
`tests/integration/test_zone3_zone5_conflict_sweep_wired.py`'s sibling
`test_unified_subject_erasure_orchestrator_dash025.py` for that
single-call fan-out coverage, AC-025-3).

This module still seeds real data for one `subject_id` across every Zone
3/4/5 component this codebase's real schemas can actually link to a
subject, still calls `SubjectErasureCascadeService.request_erasure` for
that same subject (documenting that THIS service alone still does not
reach Zone 3/5 -- it never claimed to), and now ALSO calls each zone's own
new erasure leg directly and asserts non-survival -- AC-025-1's and
AC-025-2's own required inversion of this file's prior assertions.

Zone 4 (`Procedure`, HLD Section 3.5) is a documented EXCLUSION, not an
oversight: `Procedure`'s real, current field set --
`(tenant_id, task_signature_hash, steps, success_count, failure_count,
last_used_at, state, score_terms)` (`dashanan.domain.procedure.Procedure`) --
carries no `subject_id`, `entity_id`, or any other subject-linkable
reference at all. `procedure.py`'s own docstring records that a
`procedure_id` field was deliberately omitted as out of this codebase's
current scope, and no other field on the entity stands in for a subject
reference either. There is therefore no real seed this test can construct
for Zone 4 without inventing a field the domain model does not have; this
test seeds Zone 3 and Zone 5 only and documents this Zone 4 exclusion here
instead of forcing an artificial one.

Uses `Sprint2WiredSystem` (`tests/integration/conftest_sprint2.py`) exactly
as `test_smoke_fixture_wiring_sprint2.py` does -- real adapters throughout,
registered as a plugin via `pytest_plugins` below since pytest does not
auto-discover a file not literally named `conftest.py`. This module does
not modify `conftest_sprint2.py` or `src/` in any way.

PII NOTE: every seeded value below is either abstract routing/ranking
metadata (subject_id, edge_id, predicate, entity_id, attribute_name) or the
literal `PLACEHOLDER_PAYLOAD` constant (`tests.integration.conftest`); no
example conversational content appears anywhere in this file.
"""

from __future__ import annotations

from dashanan.application.subject_erasure_cascade import SubjectErasureJobStatus
from dashanan.domain.semantic_memory import SemanticEdge
from dashanan.domain.subject_erasure import SubjectErasureRequest
from dashanan.domain.write_gate import WriteAccepted

from tests.integration.conftest import DEFAULT_TENANT_ID, PLACEHOLDER_PAYLOAD

from .conftest_sprint2 import Sprint2WiredSystem

pytest_plugins = ["tests.integration.conftest_sprint2"]

_SUBJECT_ID = "cross-zone-subject-1"
"""The one `subject_id` seeded across every zone this test exercises."""


def _edge_row(edge: SemanticEdge) -> tuple[object, ...]:
    """Mirror `tests.test_smoke_semantic._edge_row`: a canned SELECT row for one `SemanticEdge`.

    `RecordingConnection` (`tests.test_smoke_episodic`) is a write-
    recording fake with no read-back storage: an `INSERT` through it
    never makes a later `SELECT` on the same connection return that row.
    `delete_edges_by_subject`'s own internal read (`find_edges_by_subject`)
    needs the cursor's canned rows seeded directly with this helper's
    output before it is called, mirroring the same established
    `cursor_obj._rows = [...]` convention already used by
    `tests/integration/test_zone3_zone5_conflict_sweep_wired.py` and
    `tests/integration/test_cross_zone_scenarios.py`.
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


class TestCrossZoneDpdpErasureScopeGap:
    """Documents which zones a real `request_erasure`/zone-leg call actually reaches today."""

    def test_zone3_semantic_edge_does_not_survive_zone3_erasure_leg(
        self, sprint2_wired_system: Sprint2WiredSystem
    ) -> None:
        """AC-025-1: `SqlSemanticRepository.delete_edges_by_subject` removes the seeded edge.

        Inverts this file's prior `test_zone3_semantic_edge_survives_
        subject_erasure_cascade` (module docstring, HISTORY): that test's
        own `executed`-statement-log proof technique is reused here, only
        with the expectation flipped -- a `DELETE` statement (this story's
        own `_DELETE_EDGES_BY_SUBJECT_SQL`) is now expected to have been
        issued against Zone 3's connection after the leg runs, on top of
        the original `INSERT`.

        `SubjectErasureCascadeService.request_erasure` itself still never
        reaches Zone 3 (asserted first, below, unchanged from before this
        story) -- AC-025-1 requires the ZONE 3 LEG itself
        (`delete_edges_by_subject`) to issue a real state-transition or
        delete statement, not that `SubjectErasureCascadeService` grew a
        Zone 3 dependency it never had.
        """
        edge = SemanticEdge(
            tenant_id=DEFAULT_TENANT_ID,
            edge_id="edge-cross-zone-1",
            subject_ref=_SUBJECT_ID,
            predicate="located_in",
            object_ref="entity-b",
        )
        sprint2_wired_system.zone3_index_projector.insert_edge(edge)
        executed_before = list(
            sprint2_wired_system.zone3_connection.cursor_obj.executed
        )
        assert len(executed_before) == 1

        sprint2_wired_system.zone8_subject_erasure_cascade.request_erasure(
            SubjectErasureRequest(tenant_id=DEFAULT_TENANT_ID, subject_id=_SUBJECT_ID)
        )
        executed_after_zone8_only = sprint2_wired_system.zone3_connection.cursor_obj.executed
        assert executed_after_zone8_only == executed_before, (
            "SubjectErasureCascadeService issued SQL against Zone 3's connection -- "
            "it still must not: Zone 3's own erasure leg is a separate component "
            "(delete_edges_by_subject), never reached through SubjectErasureCascadeService"
        )

        sprint2_wired_system.zone3_connection.cursor_obj._rows = [
            (edge.edge_id,)
        ]
        erased_edge_ids = sprint2_wired_system.zone3_semantic_repository.delete_edges_by_subject(
            DEFAULT_TENANT_ID, _SUBJECT_ID
        )

        assert erased_edge_ids == ("edge-cross-zone-1",), (
            "delete_edges_by_subject did not report the seeded edge as erased -- "
            "if this fires, AC-025-1's own inverted assertion is stale"
        )
        executed_after_zone3_leg = sprint2_wired_system.zone3_connection.cursor_obj.executed
        assert len(executed_after_zone3_leg) == 2, (
            "Zone 3's own erasure leg must issue exactly one additional "
            "statement (a single DELETE ... RETURNING edge_id) against the "
            "connection, on top of the original INSERT -- DSHN-70's fix "
            "collapses the previous separate SELECT + DELETE into one "
            "statement so the reported evidence is exactly what was deleted"
        )
        assert "DELETE" in executed_after_zone3_leg[-1][0]
        assert "RETURNING" in executed_after_zone3_leg[-1][0]

    def test_zone5_entity_attribute_does_not_survive_zone5_erasure_leg(
        self, sprint2_wired_system: Sprint2WiredSystem
    ) -> None:
        """AC-025-2: `EntityMemoryRepository.erase_entity` removes the seeded attribute.

        Inverts this file's prior `test_zone5_entity_attribute_survives_
        subject_erasure_cascade` (module docstring, HISTORY): seeds one
        real attribute via `write_attribute`, confirms it is stored,
        confirms `SubjectErasureCascadeService.request_erasure` still
        never reaches Zone 5 (unchanged from before this story), then
        calls Zone 5's own new `erase_entity` leg directly and re-reads
        via `get_entity` -- AC-025-2's own literal "a re-read via
        get_entity no longer returns the erased attribute."
        """
        write_result = sprint2_wired_system.zone5_entity_repository.write_attribute(
            tenant_id=DEFAULT_TENANT_ID,
            entity_id=_SUBJECT_ID,
            attribute_name="role",
            value=PLACEHOLDER_PAYLOAD,
            provenance_id="prov-cross-zone-1",
        )
        assert isinstance(write_result, WriteAccepted)
        seeded = sprint2_wired_system.zone5_entity_repository.get_entity(
            DEFAULT_TENANT_ID, _SUBJECT_ID
        )
        assert seeded is not None

        sprint2_wired_system.zone8_subject_erasure_cascade.request_erasure(
            SubjectErasureRequest(tenant_id=DEFAULT_TENANT_ID, subject_id=_SUBJECT_ID)
        )
        still_present_after_zone8_only = sprint2_wired_system.zone5_entity_repository.get_entity(
            DEFAULT_TENANT_ID, _SUBJECT_ID
        )
        assert still_present_after_zone8_only is not None, (
            "SubjectErasureCascadeService reached Zone 5 -- it still must not: "
            "Zone 5's own erasure leg is a separate component (erase_entity)"
        )

        erased_item_ids = sprint2_wired_system.zone5_entity_repository.erase_entity(
            DEFAULT_TENANT_ID, _SUBJECT_ID
        )

        assert erased_item_ids == (f"{_SUBJECT_ID}:role",)
        after_erasure = sprint2_wired_system.zone5_entity_repository.get_entity(
            DEFAULT_TENANT_ID, _SUBJECT_ID
        )
        assert after_erasure is None, (
            "Zone 5's entity record for the erased subject_id survived erase_entity -- "
            "if this fires, AC-025-2's own inverted assertion is stale"
        )

    def test_erasure_cascade_completes_while_leaving_zones_3_and_5_untouched(
        self, sprint2_wired_system: Sprint2WiredSystem
    ) -> None:
        """The cascade itself succeeds end-to-end (Zone 8's real work) despite the scope gap.

        Confirms `request_erasure` completes successfully -- accepting the
        request, running Zone 8's real crypto-shredding cascade (empty
        here since no Zone 8 archive was seeded for `_SUBJECT_ID` in this
        test), and recording the job `COMPLETED` -- which is the behavior
        that makes the Zone 3/5 no-op in the two tests above easy to miss:
        the `202`/job-completion contract looks identical whether or not
        Zone 3/4/5 were actually reached.
        """
        accepted = sprint2_wired_system.zone8_subject_erasure_cascade.request_erasure(
            SubjectErasureRequest(tenant_id=DEFAULT_TENANT_ID, subject_id=_SUBJECT_ID)
        )

        job = sprint2_wired_system.zone8_subject_erasure_cascade.get_job(
            accepted.job_id
        )
        assert job is not None
        assert job.status is SubjectErasureJobStatus.COMPLETED
        assert job.item_ids == (), (
            "no Zone 8 archive was seeded for _SUBJECT_ID in this test, so the "
            "real Zone 8 crypto-shredding cascade must report zero affected items"
        )
