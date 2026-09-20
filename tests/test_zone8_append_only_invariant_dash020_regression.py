"""Regression suite: `zone8_manifest`'s append-only invariant, DASH-STORY-020 (DSHN-69).

Traces to FR-008 in SRS.md and `zone8_consolidation_schema.sql`'s own
`REVOKE UPDATE, DELETE ON zone8_manifest FROM PUBLIC` (DASH-STORY-018,
must-not-deviate item 3): a `zone8_manifest` row, once written, is never
mutated.

DEFECT THIS SUITE GUARDS AGAINST: an earlier version of
`dashanan.infrastructure.sql_zone8_subject_index_repository.
SqlZone8SubjectIndexRepository.record_item` issued a live `UPDATE`
against `zone8_manifest` to backfill `subject_id` onto an
already-archived row -- a real privilege violation against a database
actually enforcing the `REVOKE` above, not merely a style issue. The
existing test double this defect shipped alongside
(`tests/test_dpdp_erasure_cascade_zone8_dash020.py`'s prior `FakeCursor`)
was PERMISSIVE: it silently applied the `UPDATE` instead of rejecting
it, so no test caught the violation.

THE FIX (applied): `dashanan.infrastructure.sql_manifest_repository.
SqlManifestRepository.insert_batch` (DASH-STORY-018's own file) now
writes `subject_id` in the SAME `INSERT` that writes every other column
(`dashanan.domain.consolidated_blob.ArchiveBatchItem.subject_id` ->
`ManifestEntry.subject_id`, threaded through by
`dashanan.application.zone8_crypto_shredding_store.
Zone8SubjectKeyedArchiver.archive_for_subject` before it ever calls
`Zone8ConsolidationStore.consolidate_batch`). `SqlZone8SubjectIndexRepository.
record_item` no longer issues any SQL.

THIS SUITE's OWN DOUBLE IS DELIBERATELY STRICT, NOT PERMISSIVE: every
connection double below raises a driver-shaped privilege error
(`ZoneManifestPrivilegeError`, mirroring psycopg2's
`errors.InsufficientPrivilege`) the instant an `UPDATE` or `DELETE`
naming `zone8_manifest` is executed -- so a regression of this exact
defect fails loudly here, the same way a real Postgres connection
enforcing the schema's `REVOKE` would.

PII NOTE: per the dev_prompt's PII constraint, this suite carries only
manifest/index mechanics, pseudonymized identifiers, and plain ASCII
placeholder payload bytes -- never decoded blob content or subject key
material presented as meaningful, anywhere below.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime

import pytest

from dashanan.application.zone8_consolidation_store import Zone8ConsolidationStore
from dashanan.application.zone8_crypto_shredding_store import Zone8SubjectKeyedArchiver
from dashanan.domain.consolidated_blob import ArchiveBatchItem, ByteRange
from dashanan.domain.zone import ZoneId
from dashanan.infrastructure.in_memory_subject_key_store import InMemorySubjectKeyStore
from dashanan.infrastructure.sql_manifest_repository import (
    _INSERT_SQL,
    SqlManifestRepository,
)
from dashanan.infrastructure.sql_zone8_subject_index_repository import (
    _FIND_ITEM_IDS_SQL,
    SqlZone8SubjectIndexRepository,
)

_FIXED_TS = datetime(2026, 1, 1, tzinfo=UTC)
_TENANT = "tenant-1"


class ZoneManifestPrivilegeError(Exception):
    """Mirrors a real driver's privilege-violation error (e.g. psycopg2's
    `errors.InsufficientPrivilege`) for an `UPDATE`/`DELETE` a database
    actually enforcing `zone8_consolidation_schema.sql`'s
    `REVOKE UPDATE, DELETE ON zone8_manifest FROM PUBLIC` would raise."""


class PrivilegeEnforcingCursor:
    """A DB-API cursor double that ACTUALLY enforces the real REVOKE UPDATE,
    DELETE privilege model against `zone8_manifest` -- not a permissive fake.

    Every statement this cursor executes is classified by its SQL verb.
    `SELECT`/`INSERT` against `zone8_manifest` are served from an
    in-memory table. Any `UPDATE`/`DELETE` naming `zone8_manifest`
    raises `ZoneManifestPrivilegeError` immediately, unconditionally --
    exactly the shape of failure a real Postgres connection would return
    for a role the schema's `REVOKE` applies to.
    """

    def __init__(self, store: "PrivilegeEnforcingStore") -> None:
        self._store = store
        self._last_result: list[tuple[object, ...]] = []

    def execute(self, sql: str, params: Sequence[object]) -> None:
        normalized = " ".join(sql.split())
        self._store.executed_sql.append(normalized)
        verb_match = re.match(r"^([A-Za-z]+)", normalized)
        verb = verb_match.group(1).upper() if verb_match else ""

        if "zone8_manifest" not in normalized:  # pragma: no cover -- defensive
            raise AssertionError(f"unexpected statement not touching zone8_manifest: {sql}")

        if verb in ("UPDATE", "DELETE"):
            raise ZoneManifestPrivilegeError(
                f"permission denied for table zone8_manifest: {verb} is revoked "
                "from PUBLIC (zone8_consolidation_schema.sql, must-not-deviate item 3)"
            )

        if verb == "INSERT":
            (
                tenant_id,
                item_id,
                blob_id,
                byte_start,
                byte_end,
                compression_generation,
                source_zone,
                written_at,
                subject_id,
            ) = params
            self._store.rows[(tenant_id, item_id)] = {
                "tenant_id": tenant_id,
                "item_id": item_id,
                "blob_id": blob_id,
                "byte_start": byte_start,
                "byte_end": byte_end,
                "compression_generation": compression_generation,
                "source_zone": source_zone,
                "written_at": written_at,
                "subject_id": subject_id,
            }
            return

        if verb == "SELECT" and "subject_id" in normalized:
            tenant_id, subject_id = params
            self._last_result = [
                (row["item_id"],)
                for (t, _iid), row in self._store.rows.items()
                if t == tenant_id and row.get("subject_id") == subject_id
            ]
            return

        if verb == "SELECT":
            tenant_id, item_id = params
            row = self._store.rows.get((tenant_id, item_id))
            self._last_result = (
                [
                    (
                        row["tenant_id"],
                        row["item_id"],
                        row["blob_id"],
                        row["byte_start"],
                        row["byte_end"],
                        row["compression_generation"],
                        row["source_zone"],
                        row["written_at"],
                    )
                ]
                if row is not None
                else []
            )
            return

        raise AssertionError(f"unexpected SQL verb '{verb}' in statement: {sql}")  # pragma: no cover

    def fetchall(self) -> list[tuple[object, ...]]:
        return self._last_result

    def fetchone(self) -> tuple[object, ...] | None:
        return self._last_result[0] if self._last_result else None


class PrivilegeEnforcingStore:
    """Backing state for `PrivilegeEnforcingCursor`/`PrivilegeEnforcingConnection`."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, object]] = {}
        self.executed_sql: list[str] = []
        self.commit_count = 0


class PrivilegeEnforcingConnection:
    """A DB-API connection double wired to one shared `PrivilegeEnforcingStore`."""

    def __init__(self, store: PrivilegeEnforcingStore) -> None:
        self._store = store

    def cursor(self) -> PrivilegeEnforcingCursor:
        return PrivilegeEnforcingCursor(self._store)

    def commit(self) -> None:
        self._store.commit_count += 1


class FakeObjectStore:
    """A minimal, real (non-mocked) `ObjectStorePort`: an in-memory dict."""

    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    def put_if_absent(self, blob_id: str, payload: bytes) -> None:
        if blob_id not in self.blobs:
            self.blobs[blob_id] = payload

    def get_range(self, blob_id: str, byte_range: ByteRange) -> bytes:
        return self.blobs[blob_id][byte_range.start : byte_range.end]


class FakeClock:
    """Deterministic Clock double: fixed instant, injectable (testing-core DI)."""

    def now(self) -> datetime:
        return _FIXED_TS


def _archiver_over_sql_adapters() -> tuple[
    Zone8SubjectKeyedArchiver, PrivilegeEnforcingStore
]:
    """Wire `Zone8SubjectKeyedArchiver` over the REAL SQL adapters (Shape B),
    against a connection double that actually enforces the append-only
    privilege model -- never a mock, never a permissive fake."""
    store = PrivilegeEnforcingStore()
    manifest_repo = SqlManifestRepository(PrivilegeEnforcingConnection(store))
    subject_index_repo = SqlZone8SubjectIndexRepository(PrivilegeEnforcingConnection(store))
    zone8_store = Zone8ConsolidationStore(
        object_store=FakeObjectStore(), manifest=manifest_repo, clock=FakeClock()
    )
    archiver = Zone8SubjectKeyedArchiver(
        zone8_store, InMemorySubjectKeyStore(), subject_index_repo
    )
    return archiver, store


class TestAppendOnlyInvariantHeldAgainstEnforcingConnection:
    """The regression class: exercises the real fix against a connection that
    actually enforces `REVOKE UPDATE, DELETE` -- not a permissive double."""

    def test_archive_for_subject_never_issues_update_or_delete(self) -> None:
        archiver, store = _archiver_over_sql_adapters()
        item = ArchiveBatchItem(
            tenant_id=_TENANT,
            item_id="item-a",
            source_zone=ZoneId.EPISODIC,
            payload=b"plaintext-a",
        )

        archiver.archive_for_subject(item, "subject-1")  # must not raise

        assert store.rows[(_TENANT, "item-a")]["subject_id"] == "subject-1", (
            "subject_id must land in the manifest row via the INSERT itself"
        )
        for sql in store.executed_sql:
            assert not sql.upper().startswith("UPDATE"), f"an UPDATE was issued: {sql}"
            assert not sql.upper().startswith("DELETE"), f"a DELETE was issued: {sql}"

    def test_full_archive_then_erase_cycle_never_issues_update_or_delete(self) -> None:
        archiver, store = _archiver_over_sql_adapters()
        item_a = ArchiveBatchItem(
            tenant_id=_TENANT, item_id="item-a", source_zone=ZoneId.EPISODIC, payload=b"a"
        )
        item_b = ArchiveBatchItem(
            tenant_id=_TENANT, item_id="item-b", source_zone=ZoneId.EPISODIC, payload=b"b"
        )

        archiver.archive_for_subject(item_a, "subject-1")
        archiver.archive_for_subject(item_b, "subject-1")
        item_ids = archiver.erase_subject(_TENANT, "subject-1")

        assert set(item_ids) == {"item-a", "item-b"}
        assert store.executed_sql, "the double must have actually been exercised"
        for sql in store.executed_sql:
            assert not sql.upper().startswith("UPDATE"), f"an UPDATE was issued: {sql}"
            assert not sql.upper().startswith("DELETE"), f"a DELETE was issued: {sql}"

    def test_multiple_items_for_the_same_subject_each_insert_with_subject_id(self) -> None:
        archiver, store = _archiver_over_sql_adapters()
        item_a = ArchiveBatchItem(
            tenant_id=_TENANT, item_id="item-a", source_zone=ZoneId.EPISODIC, payload=b"a"
        )
        item_b = ArchiveBatchItem(
            tenant_id=_TENANT, item_id="item-b", source_zone=ZoneId.EPISODIC, payload=b"b"
        )

        archiver.archive_for_subject(item_a, "subject-1")
        archiver.archive_for_subject(item_b, "subject-1")

        assert store.rows[(_TENANT, "item-a")]["subject_id"] == "subject-1"
        assert store.rows[(_TENANT, "item-b")]["subject_id"] == "subject-1"
        insert_count = sum(
            1 for sql in store.executed_sql if sql.upper().startswith("INSERT")
        )
        assert insert_count == 2, (
            "each archived item must reach the row through exactly one INSERT, "
            "never a second write to set subject_id"
        )


class TestEnforcingDoubleItselfActuallyRejectsUpdateAndDelete:
    """Meta-test: proves the double above is a REAL enforcer, not another
    permissive fake -- if this class fails, the regression coverage above
    is worthless."""

    def test_direct_update_against_zone8_manifest_is_rejected(self) -> None:
        store = PrivilegeEnforcingStore()
        cursor = PrivilegeEnforcingConnection(store).cursor()

        with pytest.raises(ZoneManifestPrivilegeError):
            cursor.execute(
                "UPDATE zone8_manifest SET subject_id = %s WHERE tenant_id = %s AND item_id = %s",
                ("subject-1", _TENANT, "item-a"),
            )

    def test_direct_delete_against_zone8_manifest_is_rejected(self) -> None:
        store = PrivilegeEnforcingStore()
        cursor = PrivilegeEnforcingConnection(store).cursor()

        with pytest.raises(ZoneManifestPrivilegeError):
            cursor.execute(
                "DELETE FROM zone8_manifest WHERE tenant_id = %s AND item_id = %s",
                (_TENANT, "item-a"),
            )

    def test_insert_and_select_against_zone8_manifest_are_allowed(self) -> None:
        store = PrivilegeEnforcingStore()
        connection = PrivilegeEnforcingConnection(store)
        repo = SqlManifestRepository(connection)
        from dashanan.domain.consolidated_blob import ManifestEntry

        entry = ManifestEntry.for_write(
            tenant_id=_TENANT,
            item_id="item-a",
            blob_id="a" * 64,
            byte_range=ByteRange(0, 1),
            compression_generation=0,
            source_zone=ZoneId.EPISODIC,
            written_at=_FIXED_TS,
            subject_id="subject-1",
        )

        repo.insert_batch([entry])  # must not raise
        resolved = repo.find_by_item_id(_TENANT, "item-a")

        assert resolved is not None
        assert resolved.blob_id == "a" * 64


class TestNoManifestWriteQueryStringContainsUpdateOrDelete:
    """Static guard on the SQL text itself, mirroring
    `test_smoke_zone8_infrastructure_dash018.py`'s identical convention --
    catches a regression even before any connection double is involved."""

    def test_insert_sql_has_no_update_or_delete_keyword(self) -> None:
        assert re.search(r"\bUPDATE\b", _INSERT_SQL, re.IGNORECASE) is None
        assert re.search(r"\bDELETE\b", _INSERT_SQL, re.IGNORECASE) is None

    def test_insert_sql_writes_the_subject_id_column(self) -> None:
        assert "subject_id" in _INSERT_SQL

    def test_find_item_ids_sql_has_no_update_or_delete_keyword(self) -> None:
        assert re.search(r"\bUPDATE\b", _FIND_ITEM_IDS_SQL, re.IGNORECASE) is None
        assert re.search(r"\bDELETE\b", _FIND_ITEM_IDS_SQL, re.IGNORECASE) is None

    def test_sql_zone8_subject_index_repository_module_defines_no_update_sql_constant(
        self,
    ) -> None:
        """The old `_RECORD_ITEM_SQL` UPDATE constant must be gone entirely,
        not merely unused -- its continued presence would invite reintroducing
        the call site that made it a live violation."""
        import dashanan.infrastructure.sql_zone8_subject_index_repository as module

        assert not hasattr(module, "_RECORD_ITEM_SQL")
