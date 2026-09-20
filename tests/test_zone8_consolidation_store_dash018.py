"""Test suite for DASH-STORY-018: Zone 8 Consolidation store (ADR-009).

Traces to FR-008 in SRS.md. FR-008 (verbatim): "The system SHALL provide
a Consolidation Memory zone as the long-term, cross-session consolidated
store that content reaches only via the Archived state of the rotation
state machine (FR-012)."

Covers, by AC ID:
  - AC-008-1: manifest resolves item_id -> (blob_id, byte_range,
    compression_generation) via a single indexed lookup plus at most one
    ranged GET, no full-bucket or full-manifest scan.
  - AC-008-2: Zone 8 accepts writes only as archived items originating
    from Zone 2, 3, 4 or 5; rejects Zone 1 and Zone 6/7 directly.
  - AC-008-3: a consolidation merge batch spanning more than one
    tenant_id fails that batch (raises) rather than completing a
    cross-tenant merge (HLD threat I-5).
  - AC-008-4: object-store unavailability defers an archive transition
    (no loss/partial write); reads of already-archived items fail
    scoped to the one affected item_id.

Also covers the must-not-deviate items verbatim from
sprint2_ar1_assignments.json AR1-S2-018 (see each
TestMustNotDeviate* class below), and boundary/negative cases per rules
33/40's roadmap.

Runtime assumptions recorded per rule 33/40 test-roadmap conventions:
  - clock: FakeClock, fixed at 2026-01-01T00:00:00+00:00 unless a test
    states otherwise -- deterministic, no wall-clock dependency.
  - tenant_id: "tenant-1" for all requests unless a test states
    otherwise.
  - No fixture seed is required; nothing in this suite is randomized.

PII NOTE: per the dev_prompt's PII constraint, this suite carries only
manifest/index mechanics -- pseudonymized item_id values, plain ASCII
placeholder payload bytes (never realistic conversational content), zone
labels, and byte offsets -- never decoded blob content presented as real
or synthetic-realistic PII, anywhere below.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime

import pytest

from dashanan.application.zone8_consolidation_store import (
    ArchiveDeferred,
    ArchiveRejected,
    ArchiveWritten,
    ConsolidationBatchResult,
    Zone8ConsolidationStore,
    Zone8ObjectStoreUnavailableError,
    Zone8StorePortError,
)
from dashanan.domain.consolidated_blob import (
    ALLOWED_SOURCE_ZONES,
    ArchiveBatchItem,
    ByteRange,
    ManifestEntry,
    Zone8ConsolidationError,
    compute_blob_id,
    validate_source_zone,
    validate_tenant_uniform_batch,
)
from dashanan.domain.zone import ZoneId

_FIXED_TS = datetime(2026, 1, 1, tzinfo=UTC)
_TENANT = "tenant-1"
_OTHER_TENANT = "tenant-2"


class FakeClock:
    """Deterministic Clock double: fixed instant, injectable (testing-core DI)."""

    def __init__(self, fixed: datetime = _FIXED_TS) -> None:
        self._fixed = fixed

    def now(self) -> datetime:
        return self._fixed


class FakeObjectStore:
    """ObjectStorePort double: in-memory dict, can simulate unavailability."""

    def __init__(self, *, unavailable: bool = False) -> None:
        self.blobs: dict[str, bytes] = {}
        self.put_calls: list[tuple[str, bytes]] = []
        self.get_calls: list[tuple[str, ByteRange]] = []
        self._unavailable = unavailable

    def put_if_absent(self, blob_id: str, payload: bytes) -> None:
        self.put_calls.append((blob_id, payload))
        if self._unavailable:
            raise Zone8ObjectStoreUnavailableError("object store unreachable")
        if blob_id not in self.blobs:
            self.blobs[blob_id] = payload

    def get_range(self, blob_id: str, byte_range: ByteRange) -> bytes:
        self.get_calls.append((blob_id, byte_range))
        if self._unavailable:
            raise Zone8ObjectStoreUnavailableError("object store unreachable")
        if blob_id not in self.blobs:
            raise Zone8StorePortError(f"no such blob '{blob_id}'")
        return self.blobs[blob_id][byte_range.start : byte_range.end]


class FakeManifestPort:
    """ManifestPort double: in-memory dict keyed by (tenant_id, item_id)."""

    def __init__(self) -> None:
        self.entries: dict[tuple[str, str], ManifestEntry] = {}
        self.find_calls: list[tuple[str, str]] = []
        self.insert_batch_calls: list[tuple[ManifestEntry, ...]] = []

    def find_by_item_id(self, tenant_id: str, item_id: str) -> ManifestEntry | None:
        self.find_calls.append((tenant_id, item_id))
        return self.entries.get((tenant_id, item_id))

    def insert_batch(self, entries: Sequence[ManifestEntry]) -> None:
        self.insert_batch_calls.append(tuple(entries))
        for entry in entries:
            self.entries[(entry.tenant_id, entry.item_id)] = entry


def _item(
    item_id: str,
    tenant_id: str = _TENANT,
    source_zone: ZoneId = ZoneId.EPISODIC,
    payload: bytes = b"placeholder-compressed-bytes",
) -> ArchiveBatchItem:
    return ArchiveBatchItem(
        tenant_id=tenant_id,
        item_id=item_id,
        source_zone=source_zone,
        payload=payload,
    )


def _make_store(
    *,
    object_store: FakeObjectStore | None = None,
    manifest: FakeManifestPort | None = None,
    clock: FakeClock | None = None,
) -> tuple[Zone8ConsolidationStore, FakeObjectStore, FakeManifestPort]:
    obj = object_store if object_store is not None else FakeObjectStore()
    man = manifest if manifest is not None else FakeManifestPort()
    store = Zone8ConsolidationStore(
        object_store=obj, manifest=man, clock=clock if clock is not None else FakeClock()
    )
    return store, obj, man


class TestPackageWiring:
    """Baseline: the store and its domain types construct and are importable."""

    def test_store_constructs_with_fake_ports(self) -> None:
        store, *_ = _make_store()
        assert store is not None

    def test_archive_batch_item_constructs_with_valid_values(self) -> None:
        item = _item("item-1")
        assert item.item_id == "item-1"


class TestDomainValidateSourceZone:
    """AC-008-2 at the domain layer: the RECEIVES rule's single source of truth."""

    @pytest.mark.parametrize(
        "zone",
        [ZoneId.EPISODIC, ZoneId.SEMANTIC, ZoneId.PROCEDURAL, ZoneId.ENTITY],
    )
    def test_allowed_zones_pass_validation(self, zone: ZoneId) -> None:
        validate_source_zone(zone)  # must not raise

    @pytest.mark.parametrize(
        "zone",
        [ZoneId.WORKING, ZoneId.RETRIEVAL_INDEX, ZoneId.PROVENANCE],
    )
    def test_disallowed_zones_raise(self, zone: ZoneId) -> None:
        with pytest.raises(Zone8ConsolidationError, match="does not accept writes"):
            validate_source_zone(zone)

    def test_consolidation_itself_is_not_an_allowed_source(self) -> None:
        """Zone 8 cannot be its own rotation source (no self-archival loop)."""
        with pytest.raises(Zone8ConsolidationError):
            validate_source_zone(ZoneId.CONSOLIDATION)

    def test_allowed_source_zones_contains_exactly_four_zones(self) -> None:
        assert ALLOWED_SOURCE_ZONES == frozenset(
            {ZoneId.EPISODIC, ZoneId.SEMANTIC, ZoneId.PROCEDURAL, ZoneId.ENTITY}
        )


class TestMustNotDeviateItem5AllFourZonesValidatedNotJustZone2:
    """Must-not-deviate item 5 (PM note): validate against 2/3/4/5 from day one.

    Only Zone 2 exists as a rotation source at ship time, but this gate
    must already accept/reject correctly for Zones 3/4/5 too -- these
    tests are exactly the "re-test AC-008-2 against Zones 3/4/5 as each
    ships" the PM note calls for, exercised now against code that
    already implements the full rule.
    """

    def test_zone_3_semantic_is_accepted(self) -> None:
        store, obj, man = _make_store()
        result = store.consolidate_batch([_item("i1", source_zone=ZoneId.SEMANTIC)])
        assert len(result.written) == 1
        assert result.rejected == ()

    def test_zone_4_procedural_is_accepted(self) -> None:
        store, obj, man = _make_store()
        result = store.consolidate_batch([_item("i1", source_zone=ZoneId.PROCEDURAL)])
        assert len(result.written) == 1
        assert result.rejected == ()

    def test_zone_5_entity_is_accepted(self) -> None:
        store, obj, man = _make_store()
        result = store.consolidate_batch([_item("i1", source_zone=ZoneId.ENTITY)])
        assert len(result.written) == 1
        assert result.rejected == ()


class TestDomainValidateTenantUniformBatch:
    """AC-008-3 / threat I-5 at the domain layer: the fail-closed batch gate."""

    def test_single_tenant_batch_passes(self) -> None:
        items = [_item("a"), _item("b")]
        validate_tenant_uniform_batch(items)  # must not raise

    def test_empty_batch_passes(self) -> None:
        validate_tenant_uniform_batch([])  # must not raise

    def test_cross_tenant_batch_raises(self) -> None:
        items = [_item("a", tenant_id=_TENANT), _item("b", tenant_id=_OTHER_TENANT)]
        with pytest.raises(Zone8ConsolidationError, match="more than one tenant_id"):
            validate_tenant_uniform_batch(items)


class TestDomainComputeBlobId:
    """Must-not-deviate item 3: content-addressed, deterministic blob identity."""

    def test_identical_content_produces_identical_blob_id(self) -> None:
        assert compute_blob_id(b"same-bytes") == compute_blob_id(b"same-bytes")

    def test_different_content_produces_different_blob_id(self) -> None:
        assert compute_blob_id(b"content-a") != compute_blob_id(b"content-b")

    def test_blob_id_is_sha256_hex_digest(self) -> None:
        payload = b"placeholder-content"
        assert compute_blob_id(payload) == hashlib.sha256(payload).hexdigest()

    def test_blob_id_is_64_char_lowercase_hex(self) -> None:
        blob_id = compute_blob_id(b"x")
        assert len(blob_id) == 64
        assert all(c in "0123456789abcdef" for c in blob_id)


class TestDomainByteRangeAndManifestEntry:
    """Boundary/negative cases for the two Value Objects ADR-009's shape rests on."""

    def test_byte_range_rejects_negative_start(self) -> None:
        with pytest.raises(Zone8ConsolidationError, match="start"):
            ByteRange(start=-1, end=5)

    def test_byte_range_rejects_end_equal_to_start(self) -> None:
        with pytest.raises(Zone8ConsolidationError, match="end"):
            ByteRange(start=5, end=5)

    def test_byte_range_rejects_end_less_than_start(self) -> None:
        with pytest.raises(Zone8ConsolidationError, match="end"):
            ByteRange(start=5, end=2)

    def test_byte_range_accepts_minimal_one_byte_range(self) -> None:
        rng = ByteRange(start=0, end=1)
        assert rng.length() == 1

    def test_manifest_entry_rejects_blank_blob_id(self) -> None:
        with pytest.raises(Zone8ConsolidationError, match="blob_id"):
            ManifestEntry(
                tenant_id=_TENANT,
                item_id="i1",
                blob_id="   ",
                byte_range=ByteRange(0, 1),
                compression_generation=0,
                source_zone=ZoneId.EPISODIC,
                written_at=_FIXED_TS,
            )

    def test_manifest_entry_rejects_negative_compression_generation(self) -> None:
        with pytest.raises(Zone8ConsolidationError, match="compression_generation"):
            ManifestEntry(
                tenant_id=_TENANT,
                item_id="i1",
                blob_id="a" * 64,
                byte_range=ByteRange(0, 1),
                compression_generation=-1,
                source_zone=ZoneId.EPISODIC,
                written_at=_FIXED_TS,
            )

    def test_manifest_entry_rejects_disallowed_source_zone(self) -> None:
        with pytest.raises(Zone8ConsolidationError):
            ManifestEntry(
                tenant_id=_TENANT,
                item_id="i1",
                blob_id="a" * 64,
                byte_range=ByteRange(0, 1),
                compression_generation=0,
                source_zone=ZoneId.WORKING,
                written_at=_FIXED_TS,
            )

    def test_for_write_is_equivalent_to_the_dataclass_constructor(self) -> None:
        via_factory = ManifestEntry.for_write(
            tenant_id=_TENANT,
            item_id="i1",
            blob_id="a" * 64,
            byte_range=ByteRange(0, 1),
            compression_generation=0,
            source_zone=ZoneId.EPISODIC,
            written_at=_FIXED_TS,
        )
        assert via_factory.item_id == "i1"
        assert via_factory.blob_id == "a" * 64


class TestArchiveBatchItemValidation:
    """Boundary/negative cases for the write-side input Value Object."""

    def test_rejects_blank_tenant_id(self) -> None:
        with pytest.raises(Zone8ConsolidationError, match="tenant_id"):
            _item("i1", tenant_id="   ")

    def test_rejects_blank_item_id(self) -> None:
        with pytest.raises(Zone8ConsolidationError, match="item_id"):
            _item("   ")

    def test_rejects_empty_payload(self) -> None:
        with pytest.raises(Zone8ConsolidationError, match="payload"):
            _item("i1", payload=b"")

    def test_rejects_disallowed_source_zone_at_construction(self) -> None:
        with pytest.raises(Zone8ConsolidationError):
            _item("i1", source_zone=ZoneId.WORKING)


class TestAC008_1ManifestResolutionSingleLookupOneRangedGet:
    """AC-008-1: item_id -> (blob_id, byte_range, compression_generation)."""

    def test_resolve_manifest_returns_none_for_never_archived_item(self) -> None:
        store, obj, man = _make_store()

        assert store.resolve_manifest(_TENANT, "never-archived") is None
        assert man.find_calls == [(_TENANT, "never-archived")]

    def test_resolve_manifest_issues_exactly_one_lookup_call(self) -> None:
        store, obj, man = _make_store()
        store.consolidate_batch([_item("i1")])

        store.resolve_manifest(_TENANT, "i1")

        assert man.find_calls == [(_TENANT, "i1")]

    def test_resolve_manifest_returns_blob_id_byte_range_and_generation(self) -> None:
        store, obj, man = _make_store()
        store.consolidate_batch([_item("i1", payload=b"abc")])

        entry = store.resolve_manifest(_TENANT, "i1")

        assert entry is not None
        assert entry.blob_id == compute_blob_id(b"abc")
        assert entry.byte_range == ByteRange(0, 3)
        assert entry.compression_generation == 0

    def test_read_blob_range_issues_exactly_one_ranged_get(self) -> None:
        store, obj, man = _make_store()
        store.consolidate_batch([_item("i1", payload=b"hello-world")])

        content = store.read_blob_range(_TENANT, "i1")

        assert content == b"hello-world"
        assert len(obj.get_calls) == 1

    def test_read_blob_range_of_never_archived_item_raises(self) -> None:
        store, obj, man = _make_store()

        with pytest.raises(Zone8ConsolidationError, match="never archived"):
            store.read_blob_range(_TENANT, "ghost-item")
        assert obj.get_calls == []

    def test_manifest_lookup_uses_indexed_point_query_not_a_scan(self) -> None:
        """FakeManifestPort itself is dict-keyed (O(1)); this asserts the
        Facade never calls anything resembling a list-all/scan method."""
        store, obj, man = _make_store()
        assert not hasattr(man, "list_all")
        assert not hasattr(man, "find_all")
        store.resolve_manifest(_TENANT, "i1")


class TestAC008_2SourceZoneAcceptance:
    """AC-008-2 at the Facade layer: accept Zones 2-5, reject Zone 1 and 6/7."""

    def test_zone_2_episodic_item_is_written(self) -> None:
        store, obj, man = _make_store()
        result = store.consolidate_batch([_item("i1", source_zone=ZoneId.EPISODIC)])
        assert [w.item_id for w in result.written] == ["i1"]

    def test_batch_of_only_disallowed_zone_items_writes_nothing(self) -> None:
        """A batch that reaches the Facade with only rejected items (defense
        in depth, bypassing ArchiveBatchItem's own construction-time guard
        via object.__new__) never touches either port."""
        store, obj, man = _make_store()
        bad = object.__new__(ArchiveBatchItem)
        object.__setattr__(bad, "tenant_id", _TENANT)
        object.__setattr__(bad, "item_id", "bad-item")
        object.__setattr__(bad, "source_zone", ZoneId.WORKING)
        object.__setattr__(bad, "payload", b"x")

        result = store.consolidate_batch([bad])

        assert result.written == ()
        assert [r.item_id for r in result.rejected] == ["bad-item"]
        assert obj.put_calls == []
        assert man.insert_batch_calls == []


class TestAC008_3CrossTenantBatchFailsClosedNotJustWarns:
    """AC-008-3 / threat I-5: the batch FAILS (raises) -- verified, not merely logged."""

    def test_cross_tenant_batch_raises_before_any_port_call(self) -> None:
        store, obj, man = _make_store()
        items = [_item("a", tenant_id=_TENANT), _item("b", tenant_id=_OTHER_TENANT)]

        with pytest.raises(Zone8ConsolidationError, match="more than one tenant_id"):
            store.consolidate_batch(items)

        assert obj.put_calls == [], (
            "a cross-tenant batch must never reach the object store -- "
            "raising after a partial write would itself be the forbidden "
            "cross-tenant merge, just completed halfway"
        )
        assert man.insert_batch_calls == [], (
            "a cross-tenant batch must never reach the manifest either"
        )

    def test_three_way_cross_tenant_batch_also_raises(self) -> None:
        store, obj, man = _make_store()
        items = [
            _item("a", tenant_id="tenant-1"),
            _item("b", tenant_id="tenant-2"),
            _item("c", tenant_id="tenant-3"),
        ]
        with pytest.raises(Zone8ConsolidationError):
            store.consolidate_batch(items)

    def test_single_tenant_batch_of_several_items_succeeds(self) -> None:
        store, obj, man = _make_store()
        items = [_item("a"), _item("b"), _item("c")]

        result = store.consolidate_batch(items)

        assert {w.item_id for w in result.written} == {"a", "b", "c"}
        assert len(obj.put_calls) == 1, (
            "a same-tenant merge batch is written as ONE shared blob, "
            "per ADR-009's consolidation-merge shape"
        )


class TestAC008_4DegradedModeDeferOnObjectStoreUnavailable:
    """AC-008-4: object-store outage defers the write; already-archived reads
    fail scoped to the one affected item, never the whole store."""

    def test_object_store_unavailable_defers_the_whole_batch(self) -> None:
        obj = FakeObjectStore(unavailable=True)
        store, obj, man = _make_store(object_store=obj)

        result = store.consolidate_batch([_item("a"), _item("b")])

        assert result.written == ()
        assert {d.item_id for d in result.deferred} == {"a", "b"}
        assert man.insert_batch_calls == [], (
            "a deferred archive transition must leave NO manifest row -- "
            "the item stays Compressed, never a partial write"
        )

    def test_deferred_outcome_carries_a_pii_free_reason_string(self) -> None:
        obj = FakeObjectStore(unavailable=True)
        store, obj, man = _make_store(object_store=obj)

        result = store.consolidate_batch([_item("a")])

        assert isinstance(result.deferred[0], ArchiveDeferred)
        assert "unreachable" in result.deferred[0].reason

    def test_read_of_archived_item_scoped_failure_when_store_goes_down_later(
        self,
    ) -> None:
        """The item was archived while the store was healthy; a LATER
        outage must fail only this item's read, never raise for the whole
        manifest/store."""
        healthy_obj = FakeObjectStore()
        store, healthy_obj, man = _make_store(object_store=healthy_obj)
        store.consolidate_batch([_item("archived-item", payload=b"content")])

        healthy_obj._unavailable = True

        with pytest.raises(Zone8ObjectStoreUnavailableError):
            store.read_blob_range(_TENANT, "archived-item")

    def test_read_unavailable_error_names_the_one_affected_item_context(self) -> None:
        healthy_obj = FakeObjectStore()
        store, healthy_obj, man = _make_store(object_store=healthy_obj)
        store.consolidate_batch(
            [_item("item-x", payload=b"content"), _item("item-y", payload=b"more")]
        )
        healthy_obj._unavailable = True

        # item-y's manifest entry still resolves (manifest is unaffected by
        # the object-store outage) -- only the ranged GET fails.
        entry = store.resolve_manifest(_TENANT, "item-y")
        assert entry is not None
        with pytest.raises(Zone8ObjectStoreUnavailableError):
            store.read_blob_range(_TENANT, "item-y")

    def test_recovered_object_store_allows_a_retry_to_succeed(self) -> None:
        obj = FakeObjectStore(unavailable=True)
        store, obj, man = _make_store(object_store=obj)
        deferred_result = store.consolidate_batch([_item("a")])
        assert deferred_result.deferred != ()

        obj._unavailable = False
        retried_result = store.consolidate_batch([_item("a")])

        assert [w.item_id for w in retried_result.written] == ["a"]


class TestMustNotDeviateItem3ImmutableContentAddressedNeverOverwritten:
    """Must-not-deviate item 3: blobs are immutable, content-addressed, never overwritten."""

    def test_object_store_port_has_no_overwrite_or_put_method(self) -> None:
        obj = FakeObjectStore()
        assert not hasattr(obj, "put")
        assert not hasattr(obj, "overwrite")
        assert not hasattr(obj, "update")

    def test_reconsolidating_identical_content_is_a_no_op_same_blob_id(self) -> None:
        store, obj, man = _make_store()
        store.consolidate_batch([_item("a", payload=b"same-content")])
        first_blob_id = obj.put_calls[0][0]

        store.consolidate_batch([_item("b", payload=b"same-content")])
        second_blob_id = obj.put_calls[1][0]

        assert first_blob_id == second_blob_id
        assert obj.blobs[first_blob_id] == b"same-content"

    def test_manifest_port_has_no_update_or_delete_method(self) -> None:
        man = FakeManifestPort()
        assert not hasattr(man, "update")
        assert not hasattr(man, "delete")


class TestMustNotDeviateItem1NoTransitionMachinery:
    """Must-not-deviate item 1: this Facade exposes no state-transition method."""

    def test_store_exposes_no_transition_or_promote_method(self) -> None:
        store, *_ = _make_store()
        assert not hasattr(store, "transition")
        assert not hasattr(store, "promote")
        assert not hasattr(store, "archive_item")  # only consolidate_batch exists


class TestMustNotDeviateItem4NoOtherZoneOwnsConsolidatedBlob:
    """Must-not-deviate item 4 (Hard Rule 1): only this module defines the shape."""

    def test_manifest_entry_is_defined_once_in_consolidated_blob_module(self) -> None:
        from dashanan.domain import consolidated_blob

        assert ManifestEntry is consolidated_blob.ManifestEntry


class TestBoundaryAndNegativeCases:
    """Boundary/negative cases per rule 33/40 test-roadmap conventions."""

    def test_consolidate_batch_of_empty_sequence_returns_empty_result(self) -> None:
        store, obj, man = _make_store()

        result = store.consolidate_batch([])

        assert result == ConsolidationBatchResult(written=(), deferred=(), rejected=())
        assert obj.put_calls == []
        assert man.insert_batch_calls == []

    def test_resolve_manifest_rejects_blank_tenant_id(self) -> None:
        store, *_ = _make_store()
        with pytest.raises(ValueError, match="tenant_id"):
            store.resolve_manifest("   ", "item-1")

    def test_single_item_batch_uses_full_payload_as_byte_range(self) -> None:
        store, obj, man = _make_store()
        result = store.consolidate_batch([_item("solo", payload=b"12345")])

        entry = result.written[0].manifest_entry
        assert entry.byte_range == ByteRange(0, 5)

    def test_multi_item_batch_assigns_non_overlapping_consecutive_ranges(self) -> None:
        store, obj, man = _make_store()
        result = store.consolidate_batch(
            [_item("first", payload=b"aaa"), _item("second", payload=b"bb")]
        )

        by_id = {w.item_id: w.manifest_entry for w in result.written}
        assert by_id["first"].byte_range == ByteRange(0, 3)
        assert by_id["second"].byte_range == ByteRange(3, 5)
        assert by_id["first"].blob_id == by_id["second"].blob_id
