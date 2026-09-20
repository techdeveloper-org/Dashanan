"""Smoke assertions for DASH-STORY-018's two concrete infrastructure adapters.

Covers `LocalObjectStore` (`ObjectStorePort`, filesystem-backed) and
`SqlManifestRepository` (`ManifestPort`, SQL-backed) -- the formal
Facade-level suite covering every AC is
`tests/test_zone8_consolidation_store_dash018.py`; this file exercises
the two adapters those Facade tests inject fakes for, against real I/O
(`LocalObjectStore`) and fake DB-API doubles (`SqlManifestRepository`,
mirroring `test_smoke_provenance.py`'s identical `RecordingConnection`/
`RecordingCursor` pattern).

PII NOTE: per the dev_prompt's PII constraint, this suite carries only
plain ASCII placeholder bytes and pseudonymized identifiers -- never
decoded blob content presented as real or synthetic-realistic PII.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dashanan.application.zone8_consolidation_store import (
    Zone8ObjectStoreUnavailableError,
    Zone8StorePortError,
)
from dashanan.domain.consolidated_blob import ByteRange, ManifestEntry, compute_blob_id
from dashanan.domain.zone import ZoneId
from dashanan.infrastructure.local_object_store import LocalObjectStore
from dashanan.infrastructure.sql_manifest_repository import (
    _FIND_BY_ITEM_ID_SQL,
    _INSERT_SQL,
    SqlManifestRepository,
)

_FIXED_TS = datetime(2026, 1, 1, tzinfo=UTC)
_TENANT = "tenant-1"


class RecordingCursor:
    """DB-API cursor double: records every execute() call, returns canned rows."""

    def __init__(self, row: Sequence[object] | None = None) -> None:
        self.executed: list[tuple[str, tuple[object, ...]]] = []
        self._row = row
        self._raise: Exception | None = None

    def execute(self, sql: str, params: Sequence[object]) -> None:
        self.executed.append((sql, tuple(params)))
        if self._raise is not None:
            raise self._raise

    def fetchone(self) -> Sequence[object] | None:
        return self._row


class RecordingConnection:
    """DB-API connection double exposing one shared RecordingCursor."""

    def __init__(self, row: Sequence[object] | None = None) -> None:
        self.cursor_obj = RecordingCursor(row)
        self.committed = 0

    def cursor(self) -> RecordingCursor:
        return self.cursor_obj

    def commit(self) -> None:
        self.committed += 1


def _entry(item_id: str = "item-1", blob_id: str = "a" * 64) -> ManifestEntry:
    return ManifestEntry.for_write(
        tenant_id=_TENANT,
        item_id=item_id,
        blob_id=blob_id,
        byte_range=ByteRange(0, 5),
        compression_generation=0,
        source_zone=ZoneId.EPISODIC,
        written_at=_FIXED_TS,
    )


class TestLocalObjectStorePutIfAbsent:
    """`LocalObjectStore.put_if_absent`: real filesystem, exclusive-create semantics."""

    def test_writes_a_new_blob(self, tmp_path: Path) -> None:
        store = LocalObjectStore(tmp_path)
        blob_id = compute_blob_id(b"hello")

        store.put_if_absent(blob_id, b"hello")

        assert (tmp_path / blob_id).read_bytes() == b"hello"

    def test_second_write_of_identical_content_is_a_silent_no_op(
        self, tmp_path: Path
    ) -> None:
        store = LocalObjectStore(tmp_path)
        blob_id = compute_blob_id(b"hello")

        store.put_if_absent(blob_id, b"hello")
        store.put_if_absent(blob_id, b"hello")  # must not raise

        assert (tmp_path / blob_id).read_bytes() == b"hello"

    def test_creates_root_dir_if_missing(self, tmp_path: Path) -> None:
        nested = tmp_path / "does" / "not" / "exist"
        store = LocalObjectStore(nested)
        assert nested.is_dir()

    def test_unwritable_root_dir_raises_unavailable(self, tmp_path: Path) -> None:
        blocked = tmp_path / "blocked"
        blocked.write_text("this is a file, not a directory")

        with pytest.raises(Zone8ObjectStoreUnavailableError):
            LocalObjectStore(blocked / "child")


class TestLocalObjectStoreGetRange:
    """`LocalObjectStore.get_range`: one seek + one bounded read."""

    def test_returns_exact_byte_slice(self, tmp_path: Path) -> None:
        store = LocalObjectStore(tmp_path)
        blob_id = compute_blob_id(b"0123456789")
        store.put_if_absent(blob_id, b"0123456789")

        result = store.get_range(blob_id, ByteRange(start=3, end=6))

        assert result == b"345"

    def test_missing_blob_raises_store_port_error_not_unavailable(
        self, tmp_path: Path
    ) -> None:
        store = LocalObjectStore(tmp_path)

        with pytest.raises(Zone8StorePortError) as exc_info:
            store.get_range("a" * 64, ByteRange(0, 1))
        assert not isinstance(exc_info.value, Zone8ObjectStoreUnavailableError), (
            "a missing blob is a manifest/object-store CONSISTENCY fault, "
            "not a store-availability outage -- the two must stay distinct "
            "so AC-008-4's degraded-mode handling is not triggered for the "
            "wrong reason"
        )


class TestSqlManifestRepositoryFindByItemId:
    """AC-008-1: a single parameterized point-lookup SELECT."""

    def test_issues_exactly_one_select_with_expected_params(self) -> None:
        connection = RecordingConnection(row=None)
        repo = SqlManifestRepository(connection)

        result = repo.find_by_item_id(_TENANT, "item-1")

        assert result is None
        assert connection.cursor_obj.executed == [
            (_FIND_BY_ITEM_ID_SQL, (_TENANT, "item-1"))
        ]

    def test_maps_a_found_row_back_to_a_manifest_entry(self) -> None:
        row = (_TENANT, "item-1", "a" * 64, 0, 5, 2, "episodic", _FIXED_TS)
        connection = RecordingConnection(row=row)
        repo = SqlManifestRepository(connection)

        entry = repo.find_by_item_id(_TENANT, "item-1")

        assert entry is not None
        assert entry.blob_id == "a" * 64
        assert entry.byte_range == ByteRange(0, 5)
        assert entry.compression_generation == 2
        assert entry.source_zone == ZoneId.EPISODIC

    def test_rejects_blank_tenant_id_before_querying(self) -> None:
        connection = RecordingConnection()
        repo = SqlManifestRepository(connection)

        with pytest.raises(ValueError, match="tenant_id"):
            repo.find_by_item_id("", "item-1")
        assert connection.cursor_obj.executed == []

    def test_wraps_underlying_failure_as_zone8_store_port_error(self) -> None:
        connection = RecordingConnection()
        connection.cursor_obj._raise = RuntimeError("connection refused")
        repo = SqlManifestRepository(connection)

        with pytest.raises(Zone8StorePortError):
            repo.find_by_item_id(_TENANT, "item-1")


class TestSqlManifestRepositoryInsertBatch:
    """Must-not-deviate item 3: an atomic, single-commit batch insert."""

    def test_inserts_every_entry_then_commits_once(self) -> None:
        connection = RecordingConnection()
        repo = SqlManifestRepository(connection)
        entries = [_entry("a"), _entry("b")]

        repo.insert_batch(entries)

        assert len(connection.cursor_obj.executed) == 2
        assert connection.committed == 1
        for sql_used, _params in connection.cursor_obj.executed:
            assert sql_used == _INSERT_SQL

    def test_empty_batch_issues_no_statements(self) -> None:
        connection = RecordingConnection()
        repo = SqlManifestRepository(connection)

        repo.insert_batch([])

        assert connection.cursor_obj.executed == []
        assert connection.committed == 0

    def test_no_manifest_query_string_contains_update_or_delete(self) -> None:
        for sql in (_FIND_BY_ITEM_ID_SQL, _INSERT_SQL):
            assert re.search(r"\bUPDATE\b", sql, re.IGNORECASE) is None
            assert re.search(r"\bDELETE\b", sql, re.IGNORECASE) is None

    def test_failure_raises_zone8_store_port_error(self) -> None:
        connection = RecordingConnection()
        connection.cursor_obj._raise = RuntimeError("unique violation")
        repo = SqlManifestRepository(connection)

        with pytest.raises(Zone8StorePortError):
            repo.insert_batch([_entry("a")])
