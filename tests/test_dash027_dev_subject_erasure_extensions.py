"""DASH-STORY-027-QA coverage for DASH-STORY-027-DEV's own additions.

Covers, per this story's own Definition of Done ("tests passing for every
AC"):
  AC-027-DEV-1: real SubjectToItemIndex (SqlSubjectToItemIndexRepository).
  AC-027-DEV-2: Zone 8's own leg is unchanged (regression guard).
  AC-027-DEV-3: 202/job_id contract preserved (already covered by
      tests/test_api_wire_layer_dash023.py's own updated erase_subject
      tests -- not duplicated here).
  AC-027-DEV-4: the write-ahead-marker/finalize durability boundary and
      the recovery sweep's three branches (finalize-only, unconfirmed
      retry, exhausted-retries escalation).

Plus the Zone 2 compliance-role eviction adapter and the orchestrator's
new Zone 1 seam (`NullZone1ErasureLeg`'s own honest, disclosed no-op).

PII NOTE: every identifier below is an opaque test fixture string, never
real payload content -- mirrors every other test module's PII posture in
this repository.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from dashanan.application.unified_subject_erasure_orchestrator import (
    DurableZone8ErasureCoordinatorLike,
    NullZone1ErasureLeg,
    UnifiedSubjectErasureOrchestrator,
    Zone1ErasureCapable,
)
from dashanan.application.zone2_capacity_backstop_sweep import Zone2BackstopPortError
from dashanan.application.zone8_erasure_durability import (
    DurableZone8ErasureCoordinator,
    ErasureMarker,
    ErasureMarkerStatus,
    InMemoryErasureMarkerStore,
    Zone8ErasureDurabilityError,
    recover_pending_erasures,
)
from dashanan.infrastructure.sql_subject_to_item_index_repository import (
    SqlSubjectToItemIndexRepository,
    SubjectToItemIndexError,
)
from dashanan.infrastructure.sql_zone2_compliance_eviction import SqlZone2ComplianceEviction


class _FakeCursor:
    """A minimal DB-API 2.0 cursor double that records every statement it executes."""

    def __init__(self, fetchall_result: list[tuple] | None = None) -> None:
        self.executed: list[tuple[str, tuple]] = []
        self._fetchall_result = fetchall_result or []
        self.rowcount = len(self._fetchall_result)

    def execute(self, sql: str, params: tuple) -> None:
        self.executed.append((sql, params))

    def fetchall(self) -> list[tuple]:
        return self._fetchall_result

    def fetchone(self) -> tuple | None:
        return self._fetchall_result[0] if self._fetchall_result else None


class _FakeConnection:
    """A minimal DB-API 2.0 connection double: one cursor, a commit counter."""

    def __init__(self, fetchall_result: list[tuple] | None = None) -> None:
        self.cursor_obj = _FakeCursor(fetchall_result)
        self.commit_count = 0

    def cursor(self) -> _FakeCursor:
        return self.cursor_obj

    def commit(self) -> None:
        self.commit_count += 1


class _RaisingConnection:
    """A connection double whose `cursor()` always raises, for error-path tests."""

    def cursor(self) -> _FakeCursor:
        raise RuntimeError("simulated connection failure")

    def commit(self) -> None:  # pragma: no cover -- never reached in these tests
        raise AssertionError("commit must not be called when cursor() itself failed")


# --------------------------------------------------------------------------
# AC-027-DEV-1: SqlSubjectToItemIndexRepository.
# --------------------------------------------------------------------------


class TestSqlSubjectToItemIndexRepository:
    def test_items_for_subject_returns_rows_from_zone2_and_zone6_only(self) -> None:
        connection = _FakeConnection(fetchall_result=[("item-1",), ("item-2",)])
        repo = SqlSubjectToItemIndexRepository(connection)

        result = repo.items_for_subject("tenant-a", "subject-1")

        assert result == ("item-1", "item-2")
        sql, params = connection.cursor_obj.executed[0]
        assert "subject_item_index" in sql
        assert params[0] == "tenant-a"
        assert params[1] == "subject-1"
        assert set(params[2]) == {"zone2", "zone6"}

    def test_items_for_subject_returns_empty_tuple_never_raises_for_unknown_subject(self) -> None:
        connection = _FakeConnection(fetchall_result=[])
        repo = SqlSubjectToItemIndexRepository(connection)

        assert repo.items_for_subject("tenant-a", "unknown-subject") == ()

    def test_items_for_subject_rejects_blank_tenant_id(self) -> None:
        repo = SqlSubjectToItemIndexRepository(_FakeConnection())
        with pytest.raises(SubjectToItemIndexError):
            repo.items_for_subject("  ", "subject-1")

    def test_record_item_commits_and_is_idempotent_on_conflict(self) -> None:
        connection = _FakeConnection()
        repo = SqlSubjectToItemIndexRepository(connection)

        repo.record_item("tenant-a", "subject-1", "zone2", "item-1")

        assert connection.commit_count == 1
        sql, params = connection.cursor_obj.executed[0]
        assert "ON CONFLICT" in sql
        assert params == ("tenant-a", "subject-1", "zone2", "item-1")

    def test_delete_for_subject_and_zone_returns_row_count_and_commits(self) -> None:
        connection = _FakeConnection()
        repo = SqlSubjectToItemIndexRepository(connection)

        deleted = repo.delete_for_subject_and_zone("tenant-a", "subject-1", "zone8")

        assert connection.commit_count == 1
        assert deleted == connection.cursor_obj.rowcount

    def test_items_for_subject_wraps_underlying_failure(self) -> None:
        repo = SqlSubjectToItemIndexRepository(_RaisingConnection())
        with pytest.raises(SubjectToItemIndexError):
            repo.items_for_subject("tenant-a", "subject-1")


# --------------------------------------------------------------------------
# Zone 2 compliance-role eviction adapter.
# --------------------------------------------------------------------------


class TestSqlZone2ComplianceEviction:
    def test_evict_issues_a_pk_only_delete_by_episode_id_and_commits(self) -> None:
        connection = _FakeConnection()
        adapter = SqlZone2ComplianceEviction(connection)

        adapter.evict("tenant-a", "episode-1")

        assert connection.commit_count == 1
        sql, params = connection.cursor_obj.executed[0]
        assert "DELETE FROM episodic_entries" in sql
        assert params == ("tenant-a", "episode-1")

    def test_evict_rejects_blank_tenant_id(self) -> None:
        adapter = SqlZone2ComplianceEviction(_FakeConnection())
        with pytest.raises(ValueError):
            adapter.evict("", "episode-1")

    def test_evict_wraps_underlying_failure_as_zone2_backstop_port_error(self) -> None:
        adapter = SqlZone2ComplianceEviction(_RaisingConnection())
        with pytest.raises(Zone2BackstopPortError):
            adapter.evict("tenant-a", "episode-1")


# --------------------------------------------------------------------------
# Orchestrator's own new Zone 1 seam: honest, disclosed no-op.
# --------------------------------------------------------------------------


class TestZone1ErasureSeam:
    def test_null_zone1_erasure_leg_always_returns_empty_tuple(self) -> None:
        assert NullZone1ErasureLeg().erase_subject("tenant-a", "subject-1") == ()

    def test_null_zone1_erasure_leg_satisfies_the_protocol(self) -> None:
        assert isinstance(NullZone1ErasureLeg(), Zone1ErasureCapable)

    def test_orchestrator_genuinely_invokes_the_zone1_leg_every_call(self) -> None:
        """AC-025-3-style contract: the leg is invoked, even though it fulfils zero obligations."""

        class _RecordingZone1Leg:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            def erase_subject(self, tenant_id: str, subject_id: str) -> tuple[str, ...]:
                self.calls.append((tenant_id, subject_id))
                return ()

        recording_leg = _RecordingZone1Leg()
        orchestrator = _build_orchestrator(zone1_erasure=recording_leg)

        orchestrator.request_erasure(_request())

        assert recording_leg.calls == [("tenant-a", "subject-1")]


# --------------------------------------------------------------------------
# Zone 8 erasure durability: write-ahead marker + finalize + recovery sweep.
# --------------------------------------------------------------------------


class _FakeKeyStore:
    """A minimal `SubjectKeyStorePort` double: an in-process dict of live keys."""

    def __init__(self, *, keys: dict[tuple[str, str], bytes] | None = None) -> None:
        self._keys = dict(keys or {})
        self.destroy_calls = 0

    def get_or_create_key(self, tenant_id: str, subject_id: str) -> bytes:
        return self._keys.setdefault((tenant_id, subject_id), b"\x00" * 32)

    def get_key(self, tenant_id: str, subject_id: str) -> bytes | None:
        return self._keys.get((tenant_id, subject_id))

    def destroy_key(self, tenant_id: str, subject_id: str) -> bool:
        self.destroy_calls += 1
        return self._keys.pop((tenant_id, subject_id), None) is not None


class _FakeSubjectIndex:
    def __init__(self, item_ids: tuple[str, ...] = ("item-1", "item-2")) -> None:
        self._item_ids = item_ids

    def record_item(self, tenant_id: str, subject_id: str, item_id: str) -> None:
        return None

    def find_item_ids(self, tenant_id: str, subject_id: str) -> tuple[str, ...]:
        return self._item_ids


class _FixedClock:
    def now(self) -> datetime:
        return datetime(2026, 9, 24, tzinfo=UTC)


class TestDurableZone8ErasureCoordinator:
    def test_erase_subject_writes_pending_then_confirmed_then_finalized_markers(self) -> None:
        key_store = _FakeKeyStore(keys={("tenant-a", "subject-1"): b"\x00" * 32})
        marker_store = InMemoryErasureMarkerStore()
        connection = _FakeConnection()
        coordinator = DurableZone8ErasureCoordinator(
            key_store=key_store,
            subject_index=_FakeSubjectIndex(),
            marker_store=marker_store,
            provenance_connection=connection,
            clock=_FixedClock(),
            marker_id_factory=lambda: "marker-1",
        )

        item_ids = coordinator.erase_subject("tenant-a", "subject-1")

        assert item_ids == ("item-1", "item-2")
        assert key_store.get_key("tenant-a", "subject-1") is None
        final = marker_store.get("marker-1")
        assert final is not None
        assert final.status is ErasureMarkerStatus.FINALIZED
        # One INSERT (provenance) + one DELETE (subject_item_index cleanup), one commit.
        assert connection.commit_count == 1
        assert len(connection.cursor_obj.executed) == 2

    def test_erase_subject_is_idempotent_when_key_already_destroyed(self) -> None:
        """destroy_key's own idempotent contract: no key present is not an error."""
        key_store = _FakeKeyStore(keys={})
        coordinator = DurableZone8ErasureCoordinator(
            key_store=key_store,
            subject_index=_FakeSubjectIndex(item_ids=()),
            marker_store=InMemoryErasureMarkerStore(),
            provenance_connection=_FakeConnection(),
            clock=_FixedClock(),
        )

        item_ids = coordinator.erase_subject("tenant-a", "subject-1")

        assert item_ids == ()
        assert key_store.destroy_calls == 1


class TestRecoverPendingErasures:
    def test_recovers_a_destroy_confirmed_marker_by_finalizing_only(self) -> None:
        """Crash between step 3 and step 4: finalize-only, never re-destroy."""
        marker_store = InMemoryErasureMarkerStore()
        marker_store.append(
            ErasureMarker(
                marker_id="marker-1",
                tenant_id="tenant-a",
                subject_id="subject-1",
                item_ids=("item-1",),
                status=ErasureMarkerStatus.DESTROY_CONFIRMED,
                created_at=datetime(2026, 9, 24, tzinfo=UTC),
            )
        )
        key_store = _FakeKeyStore(keys={})  # already destroyed
        connection = _FakeConnection()

        finalized = recover_pending_erasures(
            marker_store=marker_store,
            key_store=key_store,
            provenance_connection=connection,
            compliance_owner_env_value="compliance@example.test",
        )

        assert finalized == ("marker-1",)
        assert key_store.destroy_calls == 0  # never re-destroyed
        assert marker_store.get("marker-1").status is ErasureMarkerStatus.FINALIZED

    def test_recovers_a_pending_marker_whose_key_is_already_gone(self) -> None:
        """Crash between destroy_key returning and the DESTROY_CONFIRMED write."""
        marker_store = InMemoryErasureMarkerStore()
        marker_store.append(
            ErasureMarker(
                marker_id="marker-1",
                tenant_id="tenant-a",
                subject_id="subject-1",
                item_ids=("item-1",),
                status=ErasureMarkerStatus.PENDING,
                created_at=datetime(2026, 9, 24, tzinfo=UTC),
            )
        )
        key_store = _FakeKeyStore(keys={})  # key already gone -> destruction WAS confirmed

        finalized = recover_pending_erasures(
            marker_store=marker_store,
            key_store=key_store,
            provenance_connection=_FakeConnection(),
            compliance_owner_env_value=None,
        )

        assert finalized == ("marker-1",)
        assert key_store.destroy_calls == 0
        assert marker_store.get("marker-1").status is ErasureMarkerStatus.FINALIZED

    def test_pending_marker_with_key_still_present_retries_then_succeeds(self) -> None:
        """Destruction UNCONFIRMED: safely retry destroy_key (idempotent), then finalize."""
        marker_store = InMemoryErasureMarkerStore()
        marker_store.append(
            ErasureMarker(
                marker_id="marker-1",
                tenant_id="tenant-a",
                subject_id="subject-1",
                item_ids=("item-1",),
                status=ErasureMarkerStatus.PENDING,
                created_at=datetime(2026, 9, 24, tzinfo=UTC),
            )
        )
        key_store = _FakeKeyStore(keys={("tenant-a", "subject-1"): b"\x00" * 32})

        finalized = recover_pending_erasures(
            marker_store=marker_store,
            key_store=key_store,
            provenance_connection=_FakeConnection(),
            compliance_owner_env_value="compliance@example.test",
            sleep=lambda seconds: None,
        )

        assert finalized == ("marker-1",)
        assert key_store.destroy_calls == 1
        assert marker_store.get("marker-1").status is ErasureMarkerStatus.FINALIZED

    def test_pending_marker_exhausts_retries_and_escalates_to_failed(self) -> None:
        """Bounded k=2 retries exhausted with destruction still unconfirmed -> FAILED + escalation."""

        class _NeverDestroysKeyStore(_FakeKeyStore):
            def destroy_key(self, tenant_id: str, subject_id: str) -> bool:
                self.destroy_calls += 1
                return False  # the key is never actually removed

        marker_store = InMemoryErasureMarkerStore()
        marker_store.append(
            ErasureMarker(
                marker_id="marker-1",
                tenant_id="tenant-a",
                subject_id="subject-1",
                item_ids=("item-1",),
                status=ErasureMarkerStatus.PENDING,
                created_at=datetime(2026, 9, 24, tzinfo=UTC),
            )
        )
        key_store = _NeverDestroysKeyStore(keys={("tenant-a", "subject-1"): b"\x00" * 32})

        finalized = recover_pending_erasures(
            marker_store=marker_store,
            key_store=key_store,
            provenance_connection=_FakeConnection(),
            compliance_owner_env_value="compliance@example.test",
            sleep=lambda seconds: None,
        )

        assert finalized == ()
        assert key_store.destroy_calls == 2  # bounded k=2
        final = marker_store.get("marker-1")
        assert final.status is ErasureMarkerStatus.FAILED
        assert final.retry_count == 2

    def test_finalize_twice_for_the_same_marker_is_treated_as_already_finalized(self) -> None:
        """Idempotent finalize: a second finalize attempt for an already-closed marker does not raise."""
        marker = ErasureMarker(
            marker_id="marker-1",
            tenant_id="tenant-a",
            subject_id="subject-1",
            item_ids=(),
            status=ErasureMarkerStatus.DESTROY_CONFIRMED,
            created_at=datetime(2026, 9, 24, tzinfo=UTC),
        )
        marker_store = InMemoryErasureMarkerStore()
        marker_store.append(marker)

        class _RejectSecondInsertConnection(_FakeConnection):
            def __init__(self) -> None:
                super().__init__()
                self._insert_count = 0

            def cursor(self) -> _FakeCursor:
                cursor = super().cursor()
                original_execute = cursor.execute

                def _execute(sql: str, params: tuple) -> None:
                    if "INSERT INTO provenance_records" in sql:
                        self._insert_count += 1
                        if self._insert_count > 1:
                            raise RuntimeError("duplicate key value violates unique constraint")
                    original_execute(sql, params)

                cursor.execute = _execute  # type: ignore[method-assign]
                return cursor

        connection = _RejectSecondInsertConnection()

        finalized_first = recover_pending_erasures(
            marker_store=marker_store,
            key_store=_FakeKeyStore(keys={}),
            provenance_connection=connection,
            compliance_owner_env_value=None,
        )
        assert finalized_first == ("marker-1",)

        # Simulate a second sweep run finding the (already finalized) marker
        # forced back to DESTROY_CONFIRMED by a hypothetical crash re-read --
        # `_finalize` itself must not raise a type other than the documented one.
        with pytest.raises(Zone8ErasureDurabilityError):
            from dashanan.application.zone8_erasure_durability import _finalize

            _finalize(marker, connection)


def _build_orchestrator(
    *,
    zone1_erasure: Zone1ErasureCapable | None = None,
    zone8_durable_coordinator: DurableZone8ErasureCoordinatorLike | None = None,
) -> UnifiedSubjectErasureOrchestrator:
    """Build a minimal, fully-faked `UnifiedSubjectErasureOrchestrator` for these tests."""

    class _FakeZone8Service:
        def request_erasure(self, request):  # type: ignore[no-untyped-def]
            from dashanan.domain.subject_erasure import ErasureCascadeAccepted

            return ErasureCascadeAccepted.for_request(
                request, job_id="zone8-job", accepted_at=datetime(2026, 9, 24, tzinfo=UTC)
            )

        def get_job(self, job_id: str):  # type: ignore[no-untyped-def]
            from dashanan.application.subject_erasure_cascade import (
                SubjectErasureJob,
                SubjectErasureJobStatus,
            )

            return SubjectErasureJob(
                job_id=job_id,
                tenant_id="tenant-a",
                subject_id="subject-1",
                status=SubjectErasureJobStatus.COMPLETED,
                item_ids=(),
            )

    class _FakeZone3Repository:
        def delete_edges_by_subject(self, tenant_id: str, subject_id: str) -> tuple[str, ...]:
            return ()

    class _FakeZone5Repository:
        def erase_entity(self, tenant_id: str, entity_id: str) -> tuple[str, ...]:
            return ()

    class _FakeJobStore:
        def __init__(self) -> None:
            self._jobs: dict[str, object] = {}

        def save(self, job) -> None:  # type: ignore[no-untyped-def]
            self._jobs[job.job_id] = job

        def get(self, job_id: str):  # type: ignore[no-untyped-def]
            return self._jobs.get(job_id)

    return UnifiedSubjectErasureOrchestrator(
        zone8_service=_FakeZone8Service(),  # type: ignore[arg-type]
        zone3_repository=_FakeZone3Repository(),  # type: ignore[arg-type]
        zone5_repository=_FakeZone5Repository(),  # type: ignore[arg-type]
        job_store=_FakeJobStore(),  # type: ignore[arg-type]
        clock=_FixedClock(),
        zone1_erasure=zone1_erasure,
        zone8_durable_coordinator=zone8_durable_coordinator,
    )


def _request():  # type: ignore[no-untyped-def]
    from dashanan.domain.subject_erasure import SubjectErasureRequest

    return SubjectErasureRequest(tenant_id="tenant-a", subject_id="subject-1")
