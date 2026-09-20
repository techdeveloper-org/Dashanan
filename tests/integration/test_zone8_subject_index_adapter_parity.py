"""Zone 8 subject-index adapter parity: does the Shape A/B `record_item` divergence
break end-to-end `find_item_ids` resolution as the system is actually composed today?

`dashanan.infrastructure.sql_zone8_subject_index_repository.
SqlZone8SubjectIndexRepository.record_item` is a documented no-op (DASH-STORY-020,
DSHN-69): `subject_id` reaches `zone8_manifest` through `SqlManifestRepository.
insert_batch`'s own `INSERT`, so the Shape B adapter's `record_item` validates its
arguments and returns without issuing SQL. `dashanan.infrastructure.
in_memory_zone8_subject_index.InMemoryZone8SubjectIndex.record_item`, Shape A's
adapter, is a real write into an in-process `dict` of lists -- the two adapters
that both implement `Zone8SubjectIndexPort.record_item` behave completely
differently for the exact same call.

`tests/integration/conftest_sprint2.py`'s own composition-root docstring records
the real pairing this system runs today: `Zone8SubjectKeyedArchiver` is wired with
`zone8_consolidation_store` (SQL manifest, via `SqlManifestRepository`) AND
`zone8_subject_index` (`InMemoryZone8SubjectIndex`, Shape A) -- never
`SqlZone8SubjectIndexRepository`. `archive_for_subject`'s single call site for
`record_item` (`zone8_crypto_shredding_store.py`) therefore always lands on the
real-write adapter in this composition, and `find_item_ids` is always resolved
against that same in-memory index, not against `zone8_manifest`'s `subject_id`
column. This module archives several items for one subject through that real,
wired crypto-shredding path and confirms `find_item_ids` resolves every one of
them -- the divergence documented above exists at the adapter level, but it does
not silently break subject-key resolution end-to-end because the system's actual
composition never calls the no-op adapter for this port.

Uses `Sprint2WiredSystem` (`tests/integration/conftest_sprint2.py`), registered
as a plugin here exactly as `test_smoke_fixture_wiring_sprint2.py` already
registers it -- this module makes no change to `conftest_sprint2.py` or to
`src/`.

PII NOTE: payload bytes below are opaque placeholder content standing in for an
already-sealed Zone 8 blob; no example conversational content appears anywhere
in this file.
"""

from __future__ import annotations

import inspect

from dashanan.domain.consolidated_blob import ArchiveBatchItem
from dashanan.domain.zone import ZoneId
from dashanan.infrastructure.in_memory_zone8_subject_index import InMemoryZone8SubjectIndex
from dashanan.infrastructure.sql_zone8_subject_index_repository import (
    SqlZone8SubjectIndexRepository,
)

from tests.integration.conftest import DEFAULT_TENANT_ID

from .conftest_sprint2 import Sprint2WiredSystem

pytest_plugins = ["tests.integration.conftest_sprint2"]

_SUBJECT_ID = "subject-parity-1"
_ITEM_IDS = ("parity-item-1", "parity-item-2", "parity-item-3")


class TestZone8SubjectIndexAdapterDivergence:
    """Documents the `record_item` divergence itself, independent of any fixture wiring."""

    def test_sql_adapter_record_item_is_a_documented_no_op(self) -> None:
        """`SqlZone8SubjectIndexRepository.record_item` issues no SQL against its connection."""

        class _ExplodingCursor:
            def execute(self, sql: str, params: object) -> None:
                raise AssertionError(
                    "SqlZone8SubjectIndexRepository.record_item must not "
                    "execute any SQL -- it is a documented no-op"
                )

        class _ExplodingConnection:
            def cursor(self) -> _ExplodingCursor:
                return _ExplodingCursor()

        repository = SqlZone8SubjectIndexRepository(connection=_ExplodingConnection())

        repository.record_item(DEFAULT_TENANT_ID, _SUBJECT_ID, "any-item-id")

        assert "no-op" in inspect.getdoc(SqlZone8SubjectIndexRepository.record_item).lower()

    def test_in_memory_adapter_record_item_is_a_real_write(self) -> None:
        """`InMemoryZone8SubjectIndex.record_item` makes the item resolvable by `find_item_ids`."""
        index = InMemoryZone8SubjectIndex()

        assert index.find_item_ids(DEFAULT_TENANT_ID, _SUBJECT_ID) == ()

        index.record_item(DEFAULT_TENANT_ID, _SUBJECT_ID, "any-item-id")

        assert index.find_item_ids(DEFAULT_TENANT_ID, _SUBJECT_ID) == ("any-item-id",)


class TestZone8SubjectIndexEndToEndResolutionUnderRealWiring:
    """Exercises the real, currently-wired pairing end-to-end for one subject."""

    def test_find_item_ids_resolves_every_item_archived_for_the_subject(
        self, sprint2_wired_system: Sprint2WiredSystem
    ) -> None:
        """Archives several items for one subject through the crypto-shredded path.

        `sprint2_wired_system.zone8_subject_keyed_archiver` is composed exactly as
        `conftest_sprint2.py` composes it in production code paths: its
        `_zone8_store` writes through `SqlManifestRepository` (SQL manifest) and
        its `_subject_index` is `InMemoryZone8SubjectIndex` (Shape A). Each
        `archive_for_subject` call therefore both durably records the item's
        `subject_id` on the SQL side (via `consolidate_batch` -> `insert_batch`)
        AND records it on the in-memory subject index's real-write `record_item`
        -- the same pairing this fixture module's own docstring documents as the
        real composition.
        """
        archiver = sprint2_wired_system.zone8_subject_keyed_archiver

        for item_id in _ITEM_IDS:
            item = ArchiveBatchItem(
                tenant_id=DEFAULT_TENANT_ID,
                item_id=item_id,
                source_zone=ZoneId.EPISODIC,
                payload=f"plaintext-payload-for-{item_id}".encode(),
            )
            result = archiver.archive_for_subject(item, subject_id=_SUBJECT_ID)
            assert [w.item_id for w in result.written] == [item_id]

        resolved = sprint2_wired_system.zone8_subject_index.find_item_ids(
            DEFAULT_TENANT_ID, _SUBJECT_ID
        )

        assert set(resolved) == set(_ITEM_IDS)
        assert len(resolved) == len(_ITEM_IDS)

    def test_manifest_also_carries_subject_id_for_every_archived_item(
        self, sprint2_wired_system: Sprint2WiredSystem
    ) -> None:
        """The SQL side of the pairing durably records `subject_id` too (DASH-STORY-018).

        This is the column `SqlZone8SubjectIndexRepository.find_item_ids` would
        query if it were ever composed as the subject index instead of
        `InMemoryZone8SubjectIndex` -- confirming the SQL manifest row itself
        carries correct data even though, in today's real wiring, nothing reads
        `subject_id` back off it for subject resolution.
        """
        archiver = sprint2_wired_system.zone8_subject_keyed_archiver

        item = ArchiveBatchItem(
            tenant_id=DEFAULT_TENANT_ID,
            item_id="parity-manifest-item-1",
            source_zone=ZoneId.EPISODIC,
            payload=b"plaintext-payload-for-manifest-check",
        )
        archiver.archive_for_subject(item, subject_id=_SUBJECT_ID)

        executed = sprint2_wired_system.zone8_manifest_connection.cursor_obj.executed
        insert_sql, insert_params = executed[-1]
        assert insert_sql.strip().upper().startswith("INSERT INTO ZONE8_MANIFEST")
        assert "parity-manifest-item-1" in insert_params
        assert _SUBJECT_ID in insert_params
