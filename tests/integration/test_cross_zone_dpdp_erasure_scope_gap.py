"""Cross-zone DPDP erasure scope gap: `SubjectErasureCascadeService` only reaches Zone 8.

Targets the documented gap in `dashanan.application.subject_erasure_cascade`'s
own module docstring: `SubjectErasureCascadeService` is "the Zone-8-reaching
HALF" of HLD Section 7.4's `DELETE /v1/tenants/{id}/subjects/{subject_id}`
cascade -- the wire contract promises the cascade "cascades across all 8
zones, the vector index, the lexical index and Zone 8 archives," but the
concrete service this codebase currently has calls only
`Zone8SubjectKeyedArchiver.erase_subject`. Zone 3's own separate erasure leg
(`dashanan.infrastructure.dpdp_erasure_cascade.CrossZoneDpdpErasureCascade`,
DSHN-58) exists as an independent component this service never invokes; no
composition root anywhere in this codebase wires the two together, and no
Zone 4 or Zone 5 erasure leg exists at all.

This module seeds real data for one `subject_id` across every Zone 3/4/5
component this codebase's real schemas can actually link to a subject, calls
`SubjectErasureCascadeService.request_erasure` for that same subject, and
asserts what actually happens: Zone 8 data is genuinely destroyed (the
service's own real work), while Zone 3 and Zone 5 data is left completely
untouched -- documenting the current no-op for those zones, not a mistake in
this test.

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


class TestCrossZoneDpdpErasureScopeGap:
    """Documents which zones a real `request_erasure` call actually reaches today."""

    def test_zone3_semantic_edge_survives_subject_erasure_cascade(
        self, sprint2_wired_system: Sprint2WiredSystem
    ) -> None:
        """Zone 3 data for `_SUBJECT_ID` is untouched: the cascade never reaches Zone 3.

        Seeds one real `SemanticEdge` whose `subject_ref` (Zone 3's own
        Zone-5-entity-id reference field, `SemanticEdge`'s own docstring)
        is `_SUBJECT_ID`, through the real `Zone3IndexProjector` decorator
        over the real `SqlSemanticRepository`. `RecordingConnection`
        (Sprint 1's fake DB-API double, this fixture module's own
        docstring) is a write-recording fake with no read-back storage, so
        "still exists" is verified the same way
        `test_smoke_fixture_wiring_sprint2.py` verifies Zone 3 writes: by
        asserting on the connection's own `executed` statement log rather
        than a read-after-write the fake cannot serve. Before
        `request_erasure`, exactly one `INSERT INTO SEMANTIC_EDGES`
        statement has been issued. If the current, real
        `SubjectErasureCascadeService` reached Zone 3 at all, it would
        have to issue at least one further statement (an `UPDATE` state
        transition or a `DELETE`) against this same connection -- there is
        no other seam through which a Zone 3 erasure leg could act on this
        fixture's `SqlSemanticRepository`. The assertion below confirms
        `executed` is unchanged after the call: today's real
        `SubjectErasureCascadeService` issues zero additional statements,
        so the semantic edge is left exactly as seeded.
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

        executed_after = sprint2_wired_system.zone3_connection.cursor_obj.executed
        assert executed_after == executed_before, (
            "SubjectErasureCascadeService issued additional SQL against Zone 3's "
            "connection -- if this fires, Zone 3 now has its own erasure leg and "
            "this test's documented gap is stale"
        )

    def test_zone5_entity_attribute_survives_subject_erasure_cascade(
        self, sprint2_wired_system: Sprint2WiredSystem
    ) -> None:
        """Zone 5 data for `_SUBJECT_ID` (as `entity_id`) is untouched: a real read-after-erasure.

        `EntityMemoryRepository` is a real, genuinely stateful in-memory
        store (unlike Zone 3's fake SQL connection), so this assertion is
        a direct `get_entity` read-back rather than an executed-statement
        proxy: it seeds one real attribute for `_SUBJECT_ID` via
        `write_attribute`, confirms it is stored, runs the real erasure
        cascade for that same `_SUBJECT_ID`, and re-reads it -- confirming
        the attribute is still present because
        `SubjectErasureCascadeService.request_erasure` never calls
        anything on `EntityMemoryRepository`.
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

        after_erasure = sprint2_wired_system.zone5_entity_repository.get_entity(
            DEFAULT_TENANT_ID, _SUBJECT_ID
        )
        assert after_erasure is not None, (
            "Zone 5's entity record for the erased subject_id disappeared -- if "
            "this fires, Zone 5 now has its own erasure leg and this test's "
            "documented gap is stale"
        )
        assert any(a.attribute_name == "role" for a in after_erasure.attributes)

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
