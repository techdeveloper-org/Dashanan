"""Zone 8 consolidation invariants: cross-tenant batch rejection and blob idempotency.

Traces to FR-008 in SRS.md, AC-008-3 (HLD threat I-5: a consolidation merge
batch spanning more than one tenant_id fails the whole batch, never a
partial cross-tenant merge) and must-not-deviate item 3 of
sprint2_ar1_assignments.json AR1-S2-018 ("Blobs are immutable and
content-addressed -- never overwritten").

Uses the real, wired Sprint 2 composition (`Sprint2WiredSystem`) rather
than hand-built fakes, so both invariants are exercised against the real
`Zone8ConsolidationStore` Facade, the real `LocalObjectStore` filesystem
adapter, and the real `SqlManifestRepository` write path -- not a
re-implementation of their contracts.

`conftest_sprint2.py` is not named `conftest.py`, so pytest does not
auto-discover it; `pytest_plugins` below registers it as a plugin for
this module, mirroring `test_smoke_fixture_wiring_sprint2.py`'s own
convention.

PII NOTE: every payload below is a plain ASCII placeholder string this
module never presents as, or derives from, real or synthetic-realistic
conversational content (dev_prompt's PII constraint).
"""

from __future__ import annotations

import os

import pytest

from dashanan.domain.consolidated_blob import ArchiveBatchItem, Zone8ConsolidationError, compute_blob_id
from dashanan.domain.zone import ZoneId

from tests.integration.conftest import DEFAULT_TENANT_ID

from .conftest_sprint2 import Sprint2WiredSystem

pytest_plugins = ["tests.integration.conftest_sprint2"]

_OTHER_TENANT_ID = "tenant-cross-2"


class TestConsolidateBatchRejectsCrossTenantMixWithNoPartialWrite:
    """AC-008-3 / HLD threat I-5: a cross-tenant batch raises, and writes nothing."""

    def test_mixed_tenant_batch_raises_and_leaves_no_object_store_or_manifest_trace(
        self,
        sprint2_wired_system: Sprint2WiredSystem,
        zone8_object_store_dir,
    ) -> None:
        """A batch mixing two tenant_ids raises `Zone8ConsolidationError` before
        any port is touched -- `validate_tenant_uniform_batch` runs first,
        ahead of `object_store.put_if_absent`/`manifest.insert_batch`
        (`zone8_consolidation_store.Zone8ConsolidationStore.consolidate_batch`'s
        own docstring, step 1).
        """
        items = [
            ArchiveBatchItem(
                tenant_id=DEFAULT_TENANT_ID,
                item_id="cross-tenant-item-1",
                source_zone=ZoneId.EPISODIC,
                payload=b"already-compressed-opaque-bytes-tenant-a",
            ),
            ArchiveBatchItem(
                tenant_id=_OTHER_TENANT_ID,
                item_id="cross-tenant-item-2",
                source_zone=ZoneId.SEMANTIC,
                payload=b"already-compressed-opaque-bytes-tenant-b",
            ),
        ]

        with pytest.raises(Zone8ConsolidationError, match="more than one tenant_id"):
            sprint2_wired_system.zone8_consolidation_store.consolidate_batch(items)

        assert os.listdir(zone8_object_store_dir) == [], (
            "the object store must hold zero blobs after a rejected cross-tenant "
            "batch -- validate_tenant_uniform_batch raises before put_if_absent "
            "is ever called"
        )
        assert sprint2_wired_system.zone8_manifest_connection.cursor_obj.executed == [], (
            "the manifest connection must never be touched -- no INSERT was "
            "issued for either tenant's item"
        )
        assert sprint2_wired_system.zone8_manifest_connection.commit_calls == 0, (
            "no commit() call means no manifest write was even attempted, let "
            "alone completed"
        )

        for tenant_id, item_id in (
            (DEFAULT_TENANT_ID, "cross-tenant-item-1"),
            (_OTHER_TENANT_ID, "cross-tenant-item-2"),
        ):
            resolved = sprint2_wired_system.zone8_consolidation_store.resolve_manifest(
                tenant_id, item_id
            )
            assert resolved is None, (
                f"no manifest row for {tenant_id}/{item_id} may exist after the "
                "whole batch was rejected -- resolve_manifest must report it as "
                "never archived"
            )

    def test_single_tenant_batch_with_same_items_after_a_rejected_mixed_batch_still_succeeds(
        self,
        sprint2_wired_system: Sprint2WiredSystem,
        zone8_object_store_dir,
    ) -> None:
        """A follow-up, single-tenant batch succeeds cleanly -- the earlier
        rejected cross-tenant batch left no partial state behind that could
        interfere with it (e.g. a stray blob or manifest row under either
        tenant_id).
        """
        mixed_items = [
            ArchiveBatchItem(
                tenant_id=DEFAULT_TENANT_ID,
                item_id="retry-item-1",
                source_zone=ZoneId.EPISODIC,
                payload=b"already-compressed-opaque-bytes-retry",
            ),
            ArchiveBatchItem(
                tenant_id=_OTHER_TENANT_ID,
                item_id="retry-item-2",
                source_zone=ZoneId.SEMANTIC,
                payload=b"already-compressed-opaque-bytes-retry-other",
            ),
        ]
        with pytest.raises(Zone8ConsolidationError):
            sprint2_wired_system.zone8_consolidation_store.consolidate_batch(mixed_items)

        retry_batch = [
            ArchiveBatchItem(
                tenant_id=DEFAULT_TENANT_ID,
                item_id="retry-item-1",
                source_zone=ZoneId.EPISODIC,
                payload=b"already-compressed-opaque-bytes-retry",
            )
        ]
        result = sprint2_wired_system.zone8_consolidation_store.consolidate_batch(retry_batch)

        assert [w.item_id for w in result.written] == ["retry-item-1"]
        assert os.listdir(zone8_object_store_dir) == [result.written[0].manifest_entry.blob_id]


class TestLocalObjectStorePutIfAbsentIsATrueNoOpOnRepeatIdenticalWrite:
    """Must-not-deviate item 3: byte-identical content written twice is one blob, one write."""

    def test_second_put_of_byte_identical_content_is_a_true_no_op(
        self,
        sprint2_wired_system: Sprint2WiredSystem,
        zone8_object_store_dir,
    ) -> None:
        """Writing the exact same bytes twice via `put_if_absent` does not
        raise on the second call, does not grow the object-store directory,
        and does not touch the already-written file's bytes on disk a
        second time (verified via the file's `mtime`, which a real second
        write -- even one that rewrites identical bytes -- would advance).
        """
        payload = b"byte-identical-content-for-idempotency-check"
        blob_id = compute_blob_id(payload)
        object_store = sprint2_wired_system.zone8_object_store

        object_store.put_if_absent(blob_id, payload)

        blob_path = zone8_object_store_dir / blob_id
        assert blob_path.exists()
        first_write_mtime_ns = blob_path.stat().st_mtime_ns
        assert os.listdir(zone8_object_store_dir) == [blob_id]

        try:
            object_store.put_if_absent(blob_id, payload)
        except Exception as exc:  # noqa: BLE001 -- the assertion itself is the test
            pytest.fail(
                f"put_if_absent must be a silent no-op on byte-identical "
                f"repeat content, raised instead: {exc!r}"
            )

        assert os.listdir(zone8_object_store_dir) == [blob_id], (
            "the object store directory must not grow -- the second "
            "put_if_absent call must not create a second file/object"
        )
        second_write_mtime_ns = blob_path.stat().st_mtime_ns
        assert second_write_mtime_ns == first_write_mtime_ns, (
            "the blob's mtime must be unchanged after the second call -- "
            "the underlying file was written exactly once, the second call "
            "never opened it for writing (LocalObjectStore.put_if_absent "
            "catches FileExistsError from the O_CREAT | O_EXCL open before "
            "any write() happens)"
        )
        assert blob_path.read_bytes() == payload
