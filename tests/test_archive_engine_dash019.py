"""Test suite for DASH-STORY-019: ArchiveEngine (Compressed -> Archived, HLD Section 12).

Traces to FR-008 in SRS.md. FR-008 (verbatim): "The system SHALL provide
a Consolidation Memory zone as the long-term, cross-session consolidated
store that content reaches only via the Archived state of the rotation
state machine (FR-012)."

Covers, by AC ID:
  - AC-008-ROT-1: a Compressed item crossing ArchiveThreshold (0.25)
    transitions to Archived and is persisted into Zone 8 as a
    ConsolidatedBlob with a resolvable manifest entry.
  - AC-008-ROT-2: an item with payload_tokens < 64 is fast-tracked
    through Compressed and Archived within the same sweep, per OAQ-12.
  - AC-008-ROT-3: all eligible Compressed items are consolidated in a
    single weekly batch window, no individual per-item archive call
    outside the batch (other than the OAQ-12 fast-track).
  - AC-008-ROT-4: the Archived-transition decision uses the
    post-mutation, non-stale rotation deadline, not a cached
    pre-mutation value.

Also covers every must-not-deviate item verbatim from
sprint2_ar1_assignments.json AR1-S2-019 (see each `TestMustNotDeviate*`
class below), plus boundary and adverse cases per rules 33/40's roadmap
(a stale re-scored item, an already-archived item, per-item port-failure
isolation, an unavailable Zone 8 object store, malformed `tenant_id`).

Runtime assumptions (rule 33/40 test-roadmap conventions):
  - clock: `FakeClock`, fixed at `2026-01-01T00:00:00+00:00` unless a
    test states otherwise -- deterministic, no wall-clock dependency.
  - tenant_id: `"tenant-1"` for all requests unless a test states
    otherwise.
  - No fixture seed is required; nothing in this suite is randomized.

PII NOTE: per the dev_prompt's PII constraint, this suite carries only
scheduling and state-machine control metadata -- pseudonymized `item_id`
values, `RotationState` enum members, `MemoryScore` floats, token counts,
plain ASCII placeholder payload bytes (never realistic conversational
content) -- never zone payload/fact content presented as real or
synthetic-realistic PII, anywhere below.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from dashanan.application.archive_engine import (
    ArchiveCandidateSnapshot,
    ArchiveEngine,
    ArchiveEnginePortError,
    ArchiveFailure,
    ArchiveMarkResult,
    ArchiveSkip,
    ArchiveSweepResult,
    ArchiveTransitionOutcome,
)
from dashanan.application.zone8_consolidation_store import (
    Zone8ConsolidationStore,
    Zone8ObjectStoreUnavailableError,
    Zone8StorePortError,
)
from dashanan.domain.consolidated_blob import ByteRange, ManifestEntry
from dashanan.domain.rotation_state import RotationState
from dashanan.domain.zone import ZoneId

_FIXED_TS = datetime(2026, 1, 1, tzinfo=UTC)
_TENANT = "tenant-1"


class FakeClock:
    """Deterministic Clock double: fixed instant (testing-core DI)."""

    def __init__(self, fixed: datetime = _FIXED_TS) -> None:
        self._fixed = fixed

    def now(self) -> datetime:
        return self._fixed


class RecordingEventBus:
    """EventBus double: records every publish() call, never raises."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, object]]] = []

    def publish(self, event_type: str, payload: dict[str, object]) -> None:
        self.published.append((event_type, dict(payload)))


class FakeArchiveCandidatePort:
    """ArchiveCandidatePort double: returns configured snapshots by `item_id`."""

    def __init__(
        self, snapshots: dict[str, ArchiveCandidateSnapshot] | None = None
    ) -> None:
        self._snapshots = dict(snapshots or {})
        self.calls: list[tuple[str, ZoneId, str]] = []

    def current_state(
        self, tenant_id: str, zone_id: ZoneId, item_id: str
    ) -> ArchiveCandidateSnapshot:
        self.calls.append((tenant_id, zone_id, item_id))
        return self._snapshots[item_id]


class RaisingArchiveCandidatePort:
    """ArchiveCandidatePort double that always raises `ArchiveEnginePortError`."""

    def current_state(
        self, tenant_id: str, zone_id: ZoneId, item_id: str
    ) -> ArchiveCandidateSnapshot:
        raise ArchiveEnginePortError("episodic repository unreachable")


class FakeArchiveTransitionPort:
    """ArchiveTransitionPort double: records mark_archived() calls, can raise for specific items."""

    def __init__(
        self,
        payloads: dict[str, bytes] | None = None,
        raise_for: frozenset[str] = frozenset(),
    ) -> None:
        self._payloads = dict(payloads or {})
        self._raise_for = raise_for
        self.marked: list[tuple[str, ZoneId, str]] = []
        self._generation_by_item: dict[str, int] = {}

    def mark_archived(
        self, tenant_id: str, zone_id: ZoneId, item_id: str
    ) -> ArchiveMarkResult:
        if item_id in self._raise_for:
            raise ArchiveEnginePortError(f"store unreachable for {item_id}")
        self.marked.append((tenant_id, zone_id, item_id))
        generation = self._generation_by_item.get(item_id, 0) + 1
        self._generation_by_item[item_id] = generation
        payload = self._payloads.get(item_id, item_id.encode("ascii"))
        return ArchiveMarkResult(payload=payload, generation=generation)


class FakeObjectStore:
    """ObjectStorePort double: in-memory dict, records put calls, can simulate unavailability."""

    def __init__(self, *, unavailable: bool = False) -> None:
        self.blobs: dict[str, bytes] = {}
        self.put_calls: list[tuple[str, bytes]] = []
        self._unavailable = unavailable

    def put_if_absent(self, blob_id: str, payload: bytes) -> None:
        self.put_calls.append((blob_id, payload))
        if self._unavailable:
            raise Zone8ObjectStoreUnavailableError("object store unreachable")
        if blob_id not in self.blobs:
            self.blobs[blob_id] = payload

    def get_range(self, blob_id: str, byte_range: ByteRange) -> bytes:
        if self._unavailable:
            raise Zone8ObjectStoreUnavailableError("object store unreachable")
        if blob_id not in self.blobs:
            raise Zone8StorePortError(f"no such blob '{blob_id}'")
        return self.blobs[blob_id][byte_range.start : byte_range.end]


class FakeManifestPort:
    """ManifestPort double: in-memory dict keyed by (tenant_id, item_id), records batch calls."""

    def __init__(self) -> None:
        self.entries: dict[tuple[str, str], ManifestEntry] = {}
        self.insert_batch_calls: list[tuple[ManifestEntry, ...]] = []

    def find_by_item_id(self, tenant_id: str, item_id: str) -> ManifestEntry | None:
        return self.entries.get((tenant_id, item_id))

    def insert_batch(self, entries: list[ManifestEntry]) -> None:
        self.insert_batch_calls.append(tuple(entries))
        for entry in entries:
            self.entries[(entry.tenant_id, entry.item_id)] = entry


def _make_engine(
    candidate_port: object,
    transition_port: object,
    *,
    object_store: FakeObjectStore | None = None,
    manifest_port: FakeManifestPort | None = None,
    event_bus: RecordingEventBus | None = None,
    clock: FakeClock | None = None,
) -> tuple[ArchiveEngine, RecordingEventBus, FakeClock, FakeObjectStore, FakeManifestPort]:
    fake_clock = clock if clock is not None else FakeClock()
    fake_object_store = object_store if object_store is not None else FakeObjectStore()
    fake_manifest = manifest_port if manifest_port is not None else FakeManifestPort()
    bus = event_bus if event_bus is not None else RecordingEventBus()
    zone8_store = Zone8ConsolidationStore(
        object_store=fake_object_store, manifest=fake_manifest, clock=fake_clock
    )
    engine = ArchiveEngine(
        transition_port=transition_port,
        candidate_port=candidate_port,
        zone8_store=zone8_store,
        event_bus=bus,
        clock=fake_clock,
    )
    return engine, bus, fake_clock, fake_object_store, fake_manifest


class TestAc008Rot1CompressedToArchivedAndZone8Write:
    """AC-008-ROT-1: a Compressed item crossing ArchiveThreshold transitions to
    Archived and is persisted into Zone 8 with a resolvable manifest entry.
    """

    def test_eligible_compressed_item_is_archived_and_resolvable_in_zone8(self) -> None:
        candidate_port = FakeArchiveCandidatePort(
            {
                "item-1": ArchiveCandidateSnapshot(
                    lifecycle_state=RotationState.COMPRESSED,
                    memory_score=0.10,
                    payload_tokens=500,
                )
            }
        )
        transition_port = FakeArchiveTransitionPort({"item-1": b"compressed content"})
        engine, bus, _, _, manifest = _make_engine(candidate_port, transition_port)

        result = engine.run_weekly_archive_sweep(_TENANT, ZoneId.EPISODIC, ["item-1"])

        assert len(result.archived) == 1
        assert result.archived[0].item_id == "item-1"
        assert transition_port.marked == [(_TENANT, ZoneId.EPISODIC, "item-1")]
        resolved = manifest.find_by_item_id(_TENANT, "item-1")
        assert resolved is not None
        assert resolved.blob_id == result.archived[0].manifest_blob_id
        event_types = {event_type for event_type, _ in bus.published}
        assert event_types == {"memory.archived"}

    def test_item_completing_dash009_ac_012_rot_1_deferred_transition(self) -> None:
        """"completing DASH-STORY-009's AC-012-ROT-1-deferred transition" --
        an item that would have crossed ArchiveThreshold in DASH-STORY-009
        (where it had no destination) now reaches Archived here.
        """
        candidate_port = FakeArchiveCandidatePort(
            {
                "item-1": ArchiveCandidateSnapshot(
                    lifecycle_state=RotationState.COMPRESSED,
                    memory_score=0.05,
                    payload_tokens=500,
                )
            }
        )
        transition_port = FakeArchiveTransitionPort()
        engine, _, _, object_store, _ = _make_engine(candidate_port, transition_port)

        result = engine.run_weekly_archive_sweep(_TENANT, ZoneId.EPISODIC, ["item-1"])

        assert len(result.archived) == 1
        assert len(object_store.put_calls) == 1


class TestAc008Rot2Oaq12FastTrack:
    """AC-008-ROT-2 / OAQ-12: fast-tracked items reach Archived within the same call."""

    def test_fast_track_archive_transitions_and_writes_immediately(self) -> None:
        candidate_port = FakeArchiveCandidatePort(
            {
                "small-item": ArchiveCandidateSnapshot(
                    lifecycle_state=RotationState.COMPRESSED,
                    memory_score=0.20,
                    payload_tokens=10,
                )
            }
        )
        transition_port = FakeArchiveTransitionPort({"small-item": b"tiny"})
        engine, bus, _, object_store, manifest = _make_engine(
            candidate_port, transition_port
        )

        outcome = engine.fast_track_archive(_TENANT, ZoneId.EPISODIC, "small-item")

        assert isinstance(outcome, ArchiveTransitionOutcome)
        assert outcome.item_id == "small-item"
        assert len(object_store.put_calls) == 1
        assert manifest.find_by_item_id(_TENANT, "small-item") is not None
        assert [e for e, _ in bus.published] == ["memory.archived"]


class TestAc008Rot3SingleBatchWindowExceptFastTrack:
    """AC-008-ROT-3: all eligible items consolidate in ONE batch call; only
    the OAQ-12 fast-track calls Zone 8 outside that batch.
    """

    def test_weekly_sweep_calls_object_store_exactly_once_for_multiple_items(
        self,
    ) -> None:
        candidate_port = FakeArchiveCandidatePort(
            {
                "item-1": ArchiveCandidateSnapshot(
                    lifecycle_state=RotationState.COMPRESSED,
                    memory_score=0.10,
                    payload_tokens=500,
                ),
                "item-2": ArchiveCandidateSnapshot(
                    lifecycle_state=RotationState.COMPRESSED,
                    memory_score=0.15,
                    payload_tokens=500,
                ),
                "item-3": ArchiveCandidateSnapshot(
                    lifecycle_state=RotationState.COMPRESSED,
                    memory_score=0.20,
                    payload_tokens=500,
                ),
            }
        )
        transition_port = FakeArchiveTransitionPort(
            {"item-1": b"a", "item-2": b"b", "item-3": b"c"}
        )
        engine, _, _, object_store, manifest = _make_engine(
            candidate_port, transition_port
        )

        result = engine.run_weekly_archive_sweep(
            _TENANT, ZoneId.EPISODIC, ["item-1", "item-2", "item-3"]
        )

        assert len(result.archived) == 3
        assert len(object_store.put_calls) == 1, (
            "the whole eligible batch must reach the object store in exactly "
            "one put_if_absent call"
        )
        assert len(manifest.insert_batch_calls) == 1
        assert len(manifest.insert_batch_calls[0]) == 3

    def test_fast_track_is_a_separate_call_from_the_weekly_batch(self) -> None:
        """The OAQ-12 fast-track is the named exception -- it writes to Zone 8
        independently of, and never waits for, the weekly batch window.
        """
        candidate_port = FakeArchiveCandidatePort(
            {
                "fast-item": ArchiveCandidateSnapshot(
                    lifecycle_state=RotationState.COMPRESSED,
                    memory_score=0.10,
                    payload_tokens=5,
                )
            }
        )
        transition_port = FakeArchiveTransitionPort({"fast-item": b"tiny"})
        engine, _, _, object_store, _ = _make_engine(candidate_port, transition_port)

        engine.fast_track_archive(_TENANT, ZoneId.EPISODIC, "fast-item")

        empty_sweep = engine.run_weekly_archive_sweep(_TENANT, ZoneId.EPISODIC, [])

        assert len(object_store.put_calls) == 1
        assert empty_sweep.archived == ()


class TestAc008Rot4NonStaleRescore:
    """AC-008-ROT-4: the archive decision uses the current, re-scored
    MemoryScore -- never a value cached from Compress time.
    """

    def test_candidate_rescored_above_threshold_is_skipped_not_archived(self) -> None:
        candidate_port = FakeArchiveCandidatePort(
            {
                "item-1": ArchiveCandidateSnapshot(
                    lifecycle_state=RotationState.COMPRESSED,
                    memory_score=0.90,
                    payload_tokens=500,
                )
            }
        )
        transition_port = FakeArchiveTransitionPort()
        engine, bus, _, object_store, _ = _make_engine(candidate_port, transition_port)

        result = engine.run_weekly_archive_sweep(_TENANT, ZoneId.EPISODIC, ["item-1"])

        assert result.archived == ()
        assert len(result.skipped) == 1
        assert transition_port.marked == []
        assert object_store.put_calls == []
        assert bus.published == []

    def test_every_candidate_is_re_scored_via_the_port_not_a_cached_value(self) -> None:
        candidate_port = FakeArchiveCandidatePort(
            {
                "item-1": ArchiveCandidateSnapshot(
                    lifecycle_state=RotationState.COMPRESSED,
                    memory_score=0.10,
                    payload_tokens=500,
                )
            }
        )
        transition_port = FakeArchiveTransitionPort()
        engine, _, _, _, _ = _make_engine(candidate_port, transition_port)

        engine.run_weekly_archive_sweep(_TENANT, ZoneId.EPISODIC, ["item-1"])

        assert candidate_port.calls == [(_TENANT, ZoneId.EPISODIC, "item-1")]


class TestMustNotDeviateCannotReachArchivedWithoutCompressed:
    """Must-not-deviate item 2: "An item SHALL NOT reach Archived without
    having passed through Compressed".
    """

    def test_active_candidate_is_skipped_never_archived(self) -> None:
        candidate_port = FakeArchiveCandidatePort(
            {
                "item-1": ArchiveCandidateSnapshot(
                    lifecycle_state=RotationState.ACTIVE,
                    memory_score=0.10,
                    payload_tokens=500,
                )
            }
        )
        transition_port = FakeArchiveTransitionPort()
        engine, bus, _, object_store, _ = _make_engine(candidate_port, transition_port)

        result = engine.run_weekly_archive_sweep(_TENANT, ZoneId.EPISODIC, ["item-1"])

        assert result.archived == ()
        assert len(result.skipped) == 1
        assert "active" in result.skipped[0].reason
        assert transition_port.marked == []
        assert object_store.put_calls == []
        assert bus.published == []

    def test_already_archived_candidate_is_never_re_archived(self) -> None:
        candidate_port = FakeArchiveCandidatePort(
            {
                "item-1": ArchiveCandidateSnapshot(
                    lifecycle_state=RotationState.ARCHIVED,
                    memory_score=0.10,
                    payload_tokens=500,
                )
            }
        )
        transition_port = FakeArchiveTransitionPort()
        engine, _, _, _, _ = _make_engine(candidate_port, transition_port)

        result = engine.run_weekly_archive_sweep(_TENANT, ZoneId.EPISODIC, ["item-1"])

        assert result.archived == ()
        assert len(result.skipped) == 1
        assert transition_port.marked == []


class TestZone8DegradedModeDefersRatherThanLoses:
    """AC-008-4 (relayed): object-store unavailability defers the batch --
    items stay Compressed, no partial write, expected to retry later.
    """

    def test_object_store_unavailable_defers_the_whole_batch(self) -> None:
        candidate_port = FakeArchiveCandidatePort(
            {
                "item-1": ArchiveCandidateSnapshot(
                    lifecycle_state=RotationState.COMPRESSED,
                    memory_score=0.10,
                    payload_tokens=500,
                )
            }
        )
        transition_port = FakeArchiveTransitionPort()
        unavailable_store = FakeObjectStore(unavailable=True)
        engine, bus, _, _, manifest = _make_engine(
            candidate_port, transition_port, object_store=unavailable_store
        )

        result = engine.run_weekly_archive_sweep(_TENANT, ZoneId.EPISODIC, ["item-1"])

        assert result.archived == ()
        assert len(result.deferred) == 1
        assert result.deferred[0].item_id == "item-1"
        assert manifest.entries == {}
        assert bus.published == []

    def test_fast_track_defers_a_single_item_on_object_store_outage(self) -> None:
        candidate_port = FakeArchiveCandidatePort(
            {
                "small-item": ArchiveCandidateSnapshot(
                    lifecycle_state=RotationState.COMPRESSED,
                    memory_score=0.10,
                    payload_tokens=5,
                )
            }
        )
        transition_port = FakeArchiveTransitionPort()
        unavailable_store = FakeObjectStore(unavailable=True)
        engine, bus, _, _, _ = _make_engine(
            candidate_port, transition_port, object_store=unavailable_store
        )

        outcome = engine.fast_track_archive(_TENANT, ZoneId.EPISODIC, "small-item")

        assert isinstance(outcome, ArchiveSkip)
        assert bus.published == []


class TestPerItemFailureIsolation:
    """A single port failure isolates to one item -- the rest of the batch still archives."""

    def test_candidate_port_failure_is_recorded_not_raised(self) -> None:
        transition_port = FakeArchiveTransitionPort()
        engine, _, _, _, _ = _make_engine(
            RaisingArchiveCandidatePort(), transition_port
        )

        result = engine.run_weekly_archive_sweep(_TENANT, ZoneId.EPISODIC, ["item-1"])

        assert result.archived == ()
        assert len(result.failed) == 1
        assert result.failed[0].item_id == "item-1"

    def test_one_failing_transition_port_does_not_block_a_sibling_items_archive(
        self,
    ) -> None:
        candidate_port = FakeArchiveCandidatePort(
            {
                "good": ArchiveCandidateSnapshot(
                    lifecycle_state=RotationState.COMPRESSED,
                    memory_score=0.10,
                    payload_tokens=500,
                ),
                "bad": ArchiveCandidateSnapshot(
                    lifecycle_state=RotationState.COMPRESSED,
                    memory_score=0.10,
                    payload_tokens=500,
                ),
            }
        )
        transition_port = FakeArchiveTransitionPort(raise_for=frozenset({"bad"}))
        engine, _, _, _, _ = _make_engine(candidate_port, transition_port)

        result = engine.run_weekly_archive_sweep(
            _TENANT, ZoneId.EPISODIC, ["good", "bad"]
        )

        assert [a.item_id for a in result.archived] == ["good"]
        assert [f.item_id for f in result.failed] == ["bad"]


class TestRunWeeklyArchiveSweepInputValidation:
    def test_blank_tenant_id_raises_value_error(self) -> None:
        engine, _, _, _, _ = _make_engine(
            FakeArchiveCandidatePort({}), FakeArchiveTransitionPort()
        )
        with pytest.raises(ValueError, match="tenant_id"):
            engine.run_weekly_archive_sweep("   ", ZoneId.EPISODIC, [])

    def test_empty_candidate_list_archives_nothing_and_calls_no_port(self) -> None:
        candidate_port = FakeArchiveCandidatePort({})
        transition_port = FakeArchiveTransitionPort()
        engine, bus, _, object_store, _ = _make_engine(candidate_port, transition_port)

        result = engine.run_weekly_archive_sweep(_TENANT, ZoneId.EPISODIC, [])

        assert result == ArchiveSweepResult(
            tenant_id=_TENANT,
            zone_id=ZoneId.EPISODIC,
            archived=(),
            deferred=(),
            skipped=(),
            failed=(),
        )
        assert candidate_port.calls == []
        assert object_store.put_calls == []
        assert bus.published == []


class TestFastTrackArchiveInputValidation:
    def test_blank_tenant_id_raises_value_error(self) -> None:
        engine, _, _, _, _ = _make_engine(
            FakeArchiveCandidatePort({}), FakeArchiveTransitionPort()
        )
        with pytest.raises(ValueError, match="tenant_id"):
            engine.fast_track_archive("   ", ZoneId.EPISODIC, "item-1")
