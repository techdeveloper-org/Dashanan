"""Cross-zone fix verification: ArchiveEngine's weekly sweep now reaches subject-keyed erasure.

DSHN-69 FIX: `ArchiveEngine.run_weekly_archive_sweep` (`dashanan.application.
archive_engine`) previously wrote every eligible `Compressed` item straight
into `Zone8ConsolidationStore.consolidate_batch` with no `subject_id` on the
`ArchiveBatchItem` it built, and never called `Zone8SubjectIndexPort.
record_item` -- so a subject-scoped `erase_subject()` call for an item
archived through the ordinary weekly sweep found nothing, even though the
item was durably archived into Zone 8. This violated AC-008-DPDP-1
(backlog_draft.json DASH-STORY-020): "A DPDP subject-erasure request's
cascade reaches Zone 8 archives via the subject_id secondary index" -- a
blanket promise about "Zone 8 archives," not just ones written via the
separate subject-keyed crypto-shredding path
(`Zone8SubjectKeyedArchiver.archive_for_subject`).

THE FIX (this module verifies it, does not re-derive it -- see
`archive_engine.py`'s own docstring updates for the full rationale):

  - `ArchiveCandidateSnapshot` gained an optional `subject_id` field: the
    source zone's own `ArchiveCandidatePort` adapter now supplies a
    derivable `subject_id` at re-score time when the underlying item
    carries one. It remains `None` for a source item with no
    subject-linkable field at all (e.g. a Zone 4 Procedure, per DSHN-70) --
    the fix does NOT assume every archived item has a subject.
  - `ArchiveEngine` now accepts an optional `subject_index:
    Zone8SubjectIndexPort` constructor argument. When supplied, and when a
    candidate's `subject_id` is non-`None`, both `run_weekly_archive_sweep`
    and `fast_track_archive` pass `subject_id` through to the
    `ArchiveBatchItem` they build (already-supported field,
    `dashanan.domain.consolidated_blob.ArchiveBatchItem.subject_id`) and
    call `Zone8SubjectIndexPort.record_item` immediately after Zone 8
    durably writes the item -- mirroring `Zone8SubjectKeyedArchiver.
    archive_for_subject`'s own "write, then record" sequencing, without
    performing that class's envelope-encryption (full crypto-shredding of
    un-keyed sweep payloads is explicitly out of this fix's scope; only the
    subject_id index entry -- the part `erase_subject`'s own resolution
    depends on -- is added).
  - `tests/integration/conftest_sprint2.py`'s `zone8_archive_engine`
    fixture now composes `ArchiveEngine` with the SAME `zone8_subject_index`
    instance `zone8_subject_keyed_archiver` uses, so both write paths feed
    one shared index, matching how a real composition root would wire one
    Zone 8 subject index per tenant.

Uses `Sprint2WiredSystem` (`tests/integration/conftest_sprint2.py`),
registered as a plugin here exactly as `test_smoke_fixture_wiring_sprint2.py`
and `test_zone8_subject_index_adapter_parity.py` already register it -- this
module makes no change to `conftest_sprint2.py`'s file-disjointness beyond
what the fix itself required (the `zone8_archive_engine` fixture now wires
`subject_index`).

PII NOTE: payload bytes below are an opaque placeholder standing in for an
already-Compressed item's content (`ArchiveTransitionPort.mark_archived`'s
own PII posture, `archive_engine.py`'s module docstring); no example
conversational content appears anywhere in this file.
"""

from __future__ import annotations

from dashanan.application.archive_engine import (
    ArchiveCandidateSnapshot,
    ArchiveTransitionOutcome,
)
from dashanan.domain.rotation_state import RotationState
from dashanan.domain.zone import ZoneId

from tests.integration.conftest import DEFAULT_TENANT_ID

from .conftest_sprint2 import Sprint2WiredSystem

pytest_plugins = ["tests.integration.conftest_sprint2"]

_SUBJECT_ID = "subject-archive-sweep-gap-1"
_ITEM_ID = "sweep-archived-item-1"
_ZONE = ZoneId.EPISODIC
_PLAINTEXT_PAYLOAD = b"placeholder-compressed-payload-for-sweep-archived-item"

_NO_SUBJECT_ITEM_ID = "sweep-archived-item-no-subject-1"
"""A second item, archived through the same weekly sweep, with NO derivable
`subject_id` (e.g. a source zone item with no subject-linkable field at
all, per DSHN-70) -- proves the fix does not fabricate a subject_id for an
item that genuinely has none."""


class TestArchiveSweepReachesSubjectKeyedErasure:
    """Verifies the fix: sweep-archived items with a derivable subject_id are now reachable."""

    def test_weekly_sweep_archives_the_item_via_the_plain_zone8_store(
        self, sprint2_wired_system: Sprint2WiredSystem
    ) -> None:
        """`ArchiveEngine.run_weekly_archive_sweep` durably archives the item into Zone 8.

        Confirms the item genuinely reaches Zone 8 (the precondition for
        the fix below to matter at all) -- if the sweep failed to archive
        it, `erase_subject` finding something would be an unrelated,
        uninteresting result rather than proof of the DPDP fix this module
        verifies.
        """
        system = sprint2_wired_system
        system.zone8_archive_candidate_store.seed_item(
            DEFAULT_TENANT_ID,
            _ZONE,
            _ITEM_ID,
            snapshot=ArchiveCandidateSnapshot(
                lifecycle_state=RotationState.COMPRESSED,
                memory_score=0.1,
                payload_tokens=200,
                subject_id=_SUBJECT_ID,
            ),
        )
        system.zone8_archive_transition_store.seed_payload(
            DEFAULT_TENANT_ID,
            _ZONE,
            _ITEM_ID,
            payload=_PLAINTEXT_PAYLOAD,
            generation=0,
        )

        result = system.zone8_archive_engine.run_weekly_archive_sweep(
            DEFAULT_TENANT_ID, _ZONE, [_ITEM_ID]
        )

        assert [outcome.item_id for outcome in result.archived] == [_ITEM_ID]
        archived_outcome = result.archived[0]
        assert isinstance(archived_outcome, ArchiveTransitionOutcome)
        assert archived_outcome.manifest_blob_id

    def test_erase_subject_finds_and_erases_a_sweep_archived_item(
        self, sprint2_wired_system: Sprint2WiredSystem
    ) -> None:
        """The fixed DPDP path: `erase_subject` now reaches a sweep-archived item.

        `_ITEM_ID` is archived through `ArchiveEngine.run_weekly_archive_sweep`
        (the real, production-shaped write path), carrying `_SUBJECT_ID` on
        its `ArchiveCandidateSnapshot` -- exactly the situation a DPDP
        erasure request for `_SUBJECT_ID` must cover.
        `Zone8SubjectKeyedArchiver.erase_subject` is then called for that
        exact `(tenant_id, subject_id)` pair.

        Given the fixed code, this asserts the CORRECTED (compliant)
        outcome, and explicitly proves the pre-fix assertions (pinned by
        the module this file replaces) would now fail:
          - `find_item_ids` for `_SUBJECT_ID` DOES include `_ITEM_ID`,
            because `ArchiveEngine.run_weekly_archive_sweep` now calls
            `Zone8SubjectIndexPort.record_item` for it.
          - `erase_subject`'s own return value -- "every item_id this
            subject had archived into Zone 8, now unreadable" per its own
            docstring -- consequently includes `_ITEM_ID`.
        """
        system = sprint2_wired_system
        system.zone8_archive_candidate_store.seed_item(
            DEFAULT_TENANT_ID,
            _ZONE,
            _ITEM_ID,
            snapshot=ArchiveCandidateSnapshot(
                lifecycle_state=RotationState.COMPRESSED,
                memory_score=0.1,
                payload_tokens=200,
                subject_id=_SUBJECT_ID,
            ),
        )
        system.zone8_archive_transition_store.seed_payload(
            DEFAULT_TENANT_ID,
            _ZONE,
            _ITEM_ID,
            payload=_PLAINTEXT_PAYLOAD,
            generation=0,
        )

        sweep_result = system.zone8_archive_engine.run_weekly_archive_sweep(
            DEFAULT_TENANT_ID, _ZONE, [_ITEM_ID]
        )
        assert [outcome.item_id for outcome in sweep_result.archived] == [_ITEM_ID], (
            "precondition: the item must actually be archived into Zone 8 by the "
            "weekly sweep for the erasure fix below to be meaningful"
        )

        resolved_item_ids = system.zone8_subject_index.find_item_ids(
            DEFAULT_TENANT_ID, _SUBJECT_ID
        )
        assert _ITEM_ID in resolved_item_ids, (
            "FIX: the subject index now records the sweep-archived item, "
            "because ArchiveEngine.run_weekly_archive_sweep calls "
            "Zone8SubjectIndexPort.record_item for it when a subject_id is "
            "derivable"
        )
        # Proof the OLD (pre-fix) assertion would now fail: the pre-fix
        # module asserted `resolved_item_ids == ()`. Against the fixed
        # code, that assertion is false -- the index is non-empty.
        assert resolved_item_ids != (), (
            "regression guard: the pre-fix behavior asserted an EMPTY "
            "index for this subject; the fixed code must never regress "
            "back to that empty result once a subject_id is derivable"
        )

        erased_item_ids = system.zone8_subject_keyed_archiver.erase_subject(
            DEFAULT_TENANT_ID, _SUBJECT_ID
        )

        assert _ITEM_ID in erased_item_ids, (
            "FIX (DPDP-compliance): erase_subject now reports _ITEM_ID as "
            "erased for _SUBJECT_ID -- a subject-scoped DPDP erasure "
            "request issued against this real, currently-wired system no "
            "longer silently skips an item archived through the ordinary "
            "weekly sweep"
        )
        # Proof the OLD (pre-fix) assertion would now fail: the pre-fix
        # module asserted `erased_item_ids == ()`. Against the fixed code,
        # that assertion is false -- _ITEM_ID is present.
        assert erased_item_ids != (), (
            "regression guard: the pre-fix behavior asserted zero items "
            "erased; the fixed code must never regress back to that "
            "empty result once a subject_id is derivable"
        )

    def test_weekly_sweep_item_with_no_derivable_subject_is_not_fabricated(
        self, sprint2_wired_system: Sprint2WiredSystem
    ) -> None:
        """The fix does not assume every archived item has a subject (DSHN-70).

        An item whose `ArchiveCandidateSnapshot.subject_id` is `None` (the
        default -- mirrors a source zone item with no subject-linkable
        field at all, e.g. a Zone 4 Procedure) is archived normally, but
        `ArchiveEngine` must not register it under any subject, and no
        subject's `find_item_ids` may resolve it.
        """
        system = sprint2_wired_system
        system.zone8_archive_candidate_store.seed_item(
            DEFAULT_TENANT_ID,
            _ZONE,
            _NO_SUBJECT_ITEM_ID,
            snapshot=ArchiveCandidateSnapshot(
                lifecycle_state=RotationState.COMPRESSED,
                memory_score=0.1,
                payload_tokens=200,
            ),
        )
        system.zone8_archive_transition_store.seed_payload(
            DEFAULT_TENANT_ID,
            _ZONE,
            _NO_SUBJECT_ITEM_ID,
            payload=_PLAINTEXT_PAYLOAD,
            generation=0,
        )

        sweep_result = system.zone8_archive_engine.run_weekly_archive_sweep(
            DEFAULT_TENANT_ID, _ZONE, [_NO_SUBJECT_ITEM_ID]
        )
        assert [outcome.item_id for outcome in sweep_result.archived] == [
            _NO_SUBJECT_ITEM_ID
        ]

        resolved_item_ids = system.zone8_subject_index.find_item_ids(
            DEFAULT_TENANT_ID, _SUBJECT_ID
        )
        assert _NO_SUBJECT_ITEM_ID not in resolved_item_ids, (
            "an item with no derivable subject_id must never be registered "
            "under an unrelated subject_id"
        )

        never_minted_key = system.zone8_subject_key_store.get_key(
            DEFAULT_TENANT_ID, _SUBJECT_ID
        )
        assert never_minted_key is None
