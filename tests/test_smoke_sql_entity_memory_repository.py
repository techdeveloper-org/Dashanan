"""Smoke assertions for DASH-STORY-029-DEV (Zone 5 Shape B storage), inline
per the dev subtask scope -- mirroring `tests/test_smoke_semantic.py`'s own
scope split between a dev-pass smoke suite and a later formal QA suite.

Runtime assumptions recorded per rule 33/40 test-roadmap conventions:
  - clock: `FixedClock` below, so every test uses a deterministic instant.
  - tenant_id: "tenant-1" for all requests unless a test states otherwise.

PII NOTE: every illustration below uses pseudonymized `entity-a`/`alias-a`
style placeholders, never a real or HLD-sourced example value.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

import pytest

from dashanan.domain.exceptions import ZoneRepositoryError
from dashanan.domain.ports import ZoneQuery
from dashanan.domain.write_gate import WriteAccepted, WriteRejected
from dashanan.infrastructure.entity_memory_repository import (
    ERROR_MISSING_ATTRIBUTE_PROVENANCE_ID,
)
from dashanan.infrastructure.sql_entity_memory_repository import (
    _DELETE_ALIASES_BY_ENTITY_SQL,
    _DELETE_ATTRIBUTES_BY_ENTITY_SQL,
    _GET_ENTITY_SQL,
    _INSERT_ALIAS_SQL,
    _RESOLVE_ALIAS_PREFIX_SQL,
    _RESOLVE_EXACT_TERM_SQL,
    _UPSERT_ATTRIBUTE_SQL,
    SqlEntityMemoryRepository,
)

_SCHEMA_SQL_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "dashanan"
    / "infrastructure"
    / "entity_schema.sql"
)
_FIXED_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class FixedClock:
    """Deterministic `Clock` port double."""

    def now(self) -> datetime:
        return _FIXED_NOW


class RecordingCursor:
    """DB-API cursor double: records every execute() call, returns canned rows."""

    def __init__(self, rows: list[tuple[object, ...]] | None = None) -> None:
        self.executed: list[tuple[str, tuple[object, ...]]] = []
        self._rows = rows or []
        self._raise: Exception | None = None

    def execute(self, sql: str, params: Sequence[object]) -> None:
        self.executed.append((sql, tuple(params)))
        if self._raise is not None:
            raise self._raise

    def fetchall(self) -> list[tuple[object, ...]]:
        return list(self._rows)


class RecordingConnection:
    """DB-API connection double exposing one shared RecordingCursor, no commit()."""

    def __init__(self, rows: list[tuple[object, ...]] | None = None) -> None:
        self.cursor_obj = RecordingCursor(rows)

    def cursor(self) -> RecordingCursor:
        return self.cursor_obj


class RecordingConnectionWithCommit(RecordingConnection):
    """As above, but exposes `commit()` -- mirrors a real driver connection."""

    def __init__(self, rows: list[tuple[object, ...]] | None = None) -> None:
        super().__init__(rows)
        self.commit_count = 0

    def commit(self) -> None:
        self.commit_count += 1


class TestPackageWiring:
    """Baseline: the adapter constructs and is importable, before any AC-level suite."""

    def test_repository_constructs_with_fake_connection(self) -> None:
        repo = SqlEntityMemoryRepository(RecordingConnection())
        assert repo is not None

    def test_repository_defaults_clock_and_event_bus_when_omitted(self) -> None:
        repo = SqlEntityMemoryRepository(RecordingConnection())
        assert repo._clock is not None
        assert repo._event_bus is not None


class TestWriteAttributeAC005_2AndAC005_5:
    """`write_attribute`: single-row upsert, AC-005-5 rejection, AC-005-6 event."""

    def test_write_attribute_issues_a_single_upsert_statement(self) -> None:
        connection = RecordingConnectionWithCommit()
        repo = SqlEntityMemoryRepository(connection, clock=FixedClock())

        result = repo.write_attribute(
            tenant_id="tenant-1",
            entity_id="entity-a",
            attribute_name="display_name",
            value="v1",
            provenance_id="prov-1",
        )

        assert isinstance(result, WriteAccepted)
        assert result.write_id == "prov-1"
        assert result.accepted_at == _FIXED_NOW
        assert len(connection.cursor_obj.executed) == 1
        sql_used, params_used = connection.cursor_obj.executed[0]
        assert sql_used == _UPSERT_ATTRIBUTE_SQL
        assert params_used == (
            "tenant-1",
            "entity-a",
            "display_name",
            "v1",
            "prov-1",
            None,
            _FIXED_NOW,
        )
        assert connection.commit_count == 1

    def test_write_attribute_forwards_the_new_subject_id_parameter(self) -> None:
        connection = RecordingConnectionWithCommit()
        repo = SqlEntityMemoryRepository(connection, clock=FixedClock())

        repo.write_attribute(
            tenant_id="tenant-1",
            entity_id="entity-a",
            attribute_name="display_name",
            value="v1",
            provenance_id="prov-1",
            subject_id="subject-1",
        )

        _, params_used = connection.cursor_obj.executed[0]
        assert params_used[5] == "subject-1"

    def test_write_attribute_rejects_blank_provenance_id_before_any_statement(
        self,
    ) -> None:
        connection = RecordingConnectionWithCommit()
        repo = SqlEntityMemoryRepository(connection, clock=FixedClock())

        result = repo.write_attribute(
            tenant_id="tenant-1",
            entity_id="entity-a",
            attribute_name="display_name",
            value="v1",
            provenance_id="   ",
        )

        assert isinstance(result, WriteRejected)
        assert result.http_status == 422
        assert result.error_code == ERROR_MISSING_ATTRIBUTE_PROVENANCE_ID
        assert connection.cursor_obj.executed == []
        assert connection.commit_count == 0

    def test_write_attribute_publishes_the_ac005_6_projection_event(self) -> None:
        published: list[tuple[str, dict[str, object]]] = []

        class RecordingEventBus:
            def publish(self, event_type: str, payload: dict[str, object]) -> None:
                published.append((event_type, payload))

        connection = RecordingConnectionWithCommit()
        repo = SqlEntityMemoryRepository(
            connection, clock=FixedClock(), event_bus=RecordingEventBus()
        )

        repo.write_attribute(
            tenant_id="tenant-1",
            entity_id="entity-a",
            attribute_name="display_name",
            value="v1",
            provenance_id="prov-1",
        )

        assert len(published) == 1
        event_type, payload = published[0]
        assert event_type == "zone5.entity_attribute_projected"
        assert payload["item_id"] == "entity-a:display_name"
        assert "value" not in payload

    def test_write_attribute_rejects_blank_tenant_id_before_any_statement(self) -> None:
        connection = RecordingConnectionWithCommit()
        repo = SqlEntityMemoryRepository(connection)

        with pytest.raises(ValueError, match="tenant_id"):
            repo.write_attribute(
                tenant_id="",
                entity_id="entity-a",
                attribute_name="display_name",
                value="v1",
                provenance_id="prov-1",
            )
        assert connection.cursor_obj.executed == []

    def test_write_attribute_wraps_underlying_failure_as_zone_repository_error(
        self,
    ) -> None:
        connection = RecordingConnectionWithCommit()
        connection.cursor_obj._raise = RuntimeError("simulated DB failure")
        repo = SqlEntityMemoryRepository(connection, clock=FixedClock())

        with pytest.raises(ZoneRepositoryError):
            repo.write_attribute(
                tenant_id="tenant-1",
                entity_id="entity-a",
                attribute_name="display_name",
                value="v1",
                provenance_id="prov-1",
            )


class TestGetEntityAC005_1:
    """`get_entity`: point lookup, assembled into `EntityRecord`."""

    def test_get_entity_returns_none_when_no_row_matches(self) -> None:
        connection = RecordingConnection(rows=[])
        repo = SqlEntityMemoryRepository(connection)

        assert repo.get_entity(tenant_id="tenant-1", entity_id="entity-a") is None

    def test_get_entity_assembles_every_attribute_row(self) -> None:
        rows = [
            ("tenant-1", "entity-a", "display_name", "v1", "prov-1", _FIXED_NOW),
            ("tenant-1", "entity-a", "role", "v2", "prov-2", _FIXED_NOW),
        ]
        connection = RecordingConnection(rows=rows)
        repo = SqlEntityMemoryRepository(connection)

        record = repo.get_entity(tenant_id="tenant-1", entity_id="entity-a")

        assert record is not None
        assert len(record.attributes) == 2
        assert record.get_attribute("display_name").value == "v1"
        sql_used, params_used = connection.cursor_obj.executed[0]
        assert sql_used == _GET_ENTITY_SQL
        assert params_used == ("tenant-1", "entity-a")

    def test_get_entity_rejects_blank_entity_id(self) -> None:
        connection = RecordingConnection()
        repo = SqlEntityMemoryRepository(connection)

        with pytest.raises(ValueError, match="entity_id"):
            repo.get_entity(tenant_id="tenant-1", entity_id="")
        assert connection.cursor_obj.executed == []


class TestEraseEntityDash025Dsh70:
    """`erase_entity` (AC-029-DEV-1): DASH-STORY-025's Zone 5 erasure leg, Shape B."""

    def test_erase_entity_deletes_attributes_then_aliases_and_returns_item_ids(
        self,
    ) -> None:
        connection = RecordingConnectionWithCommit(
            rows=[("display_name",), ("role",)]
        )
        repo = SqlEntityMemoryRepository(connection)

        erased = repo.erase_entity(tenant_id="tenant-1", entity_id="entity-a")

        assert erased == ("entity-a:display_name", "entity-a:role")
        executed = connection.cursor_obj.executed
        assert len(executed) == 2
        assert executed[0][0] == _DELETE_ATTRIBUTES_BY_ENTITY_SQL
        assert executed[0][1] == ("tenant-1", "entity-a")
        assert executed[1][0] == _DELETE_ALIASES_BY_ENTITY_SQL
        assert executed[1][1] == ("tenant-1", "entity-a")
        assert connection.commit_count == 2

    def test_erase_entity_is_a_clean_noop_when_nothing_existed(self) -> None:
        connection = RecordingConnectionWithCommit(rows=[])
        repo = SqlEntityMemoryRepository(connection)

        erased = repo.erase_entity(tenant_id="tenant-1", entity_id="entity-a")

        assert erased == ()

    def test_erase_entity_rejects_blank_tenant_id_before_any_statement(self) -> None:
        connection = RecordingConnectionWithCommit()
        repo = SqlEntityMemoryRepository(connection)

        with pytest.raises(ValueError, match="tenant_id"):
            repo.erase_entity(tenant_id="", entity_id="entity-a")
        assert connection.cursor_obj.executed == []

    def test_erase_entity_wraps_underlying_failure_as_zone_repository_error(
        self,
    ) -> None:
        connection = RecordingConnectionWithCommit()
        connection.cursor_obj._raise = RuntimeError("simulated DB failure")
        repo = SqlEntityMemoryRepository(connection)

        with pytest.raises(ZoneRepositoryError):
            repo.erase_entity(tenant_id="tenant-1", entity_id="entity-a")


class TestAliasResolutionAC005_3AndAC005_4:
    """`register_alias`/`resolve_alias_prefix`/`resolve_exact_term`."""

    def test_register_alias_issues_an_idempotent_insert(self) -> None:
        connection = RecordingConnectionWithCommit()
        repo = SqlEntityMemoryRepository(connection, clock=FixedClock())

        repo.register_alias(tenant_id="tenant-1", entity_id="entity-a", alias="alias-a")

        sql_used, params_used = connection.cursor_obj.executed[0]
        assert sql_used == _INSERT_ALIAS_SQL
        assert params_used == ("tenant-1", "entity-a", "alias-a", _FIXED_NOW)
        assert "ON CONFLICT" in _INSERT_ALIAS_SQL

    def test_register_alias_rejects_empty_alias(self) -> None:
        connection = RecordingConnectionWithCommit()
        repo = SqlEntityMemoryRepository(connection)

        with pytest.raises(ValueError, match="alias"):
            repo.register_alias(tenant_id="tenant-1", entity_id="entity-a", alias="")
        assert connection.cursor_obj.executed == []

    def test_resolve_alias_prefix_returns_matching_entity_ids(self) -> None:
        connection = RecordingConnection(rows=[("entity-a",), ("entity-b",)])
        repo = SqlEntityMemoryRepository(connection)

        found = repo.resolve_alias_prefix(tenant_id="tenant-1", prefix="ali")

        assert found == frozenset({"entity-a", "entity-b"})
        sql_used, params_used = connection.cursor_obj.executed[0]
        assert sql_used == _RESOLVE_ALIAS_PREFIX_SQL
        assert params_used == ("tenant-1", "ali%")

    def test_resolve_alias_prefix_escapes_like_metacharacters_in_the_prefix(
        self,
    ) -> None:
        connection = RecordingConnection(rows=[])
        repo = SqlEntityMemoryRepository(connection)

        repo.resolve_alias_prefix(tenant_id="tenant-1", prefix="50%_off")

        _, params_used = connection.cursor_obj.executed[0]
        assert params_used[1] == r"50\%\_off%"

    def test_resolve_alias_prefix_returns_empty_frozenset_when_no_match(self) -> None:
        connection = RecordingConnection(rows=[])
        repo = SqlEntityMemoryRepository(connection)

        found = repo.resolve_alias_prefix(tenant_id="tenant-1", prefix="nope")

        assert found == frozenset()

    def test_resolve_exact_term_returns_matching_entity_ids(self) -> None:
        connection = RecordingConnection(rows=[("entity-a",)])
        repo = SqlEntityMemoryRepository(connection)

        found = repo.resolve_exact_term(tenant_id="tenant-1", term="alias-a")

        assert found == frozenset({"entity-a"})
        sql_used, params_used = connection.cursor_obj.executed[0]
        assert sql_used == _RESOLVE_EXACT_TERM_SQL
        assert params_used == ("tenant-1", "alias-a")


class TestFetchDelegation:
    """`fetch` delegates to `resolve_alias_prefix` + `get_entity` (design doc Section 6)."""

    def test_fetch_returns_empty_for_blank_task(self) -> None:
        connection = RecordingConnection()
        repo = SqlEntityMemoryRepository(connection)
        query = ZoneQuery(
            tenant_id="tenant-1",
            task=None,
            query_embedding=None,
            max_items=10,
            min_provenance_conf=0.0,
        )

        assert repo.fetch(query) == []
        assert connection.cursor_obj.executed == []

    def test_fetch_assembles_items_from_matching_entities(self) -> None:
        class SequencedConnection(RecordingConnection):
            def __init__(self) -> None:
                super().__init__()
                self._call = 0

            def cursor(self) -> RecordingCursor:
                self._call += 1
                if self._call == 1:
                    self.cursor_obj = RecordingCursor(rows=[("entity-a",)])
                else:
                    self.cursor_obj = RecordingCursor(
                        rows=[
                            (
                                "tenant-1",
                                "entity-a",
                                "display_name",
                                "v1",
                                "prov-1",
                                _FIXED_NOW,
                            )
                        ]
                    )
                return self.cursor_obj

        repo = SqlEntityMemoryRepository(SequencedConnection())
        query = ZoneQuery(
            tenant_id="tenant-1",
            task="ent",
            query_embedding=None,
            max_items=10,
            min_provenance_conf=0.0,
        )

        items = repo.fetch(query)

        assert len(items) == 1
        assert items[0].item_id == "entity-a"


class TestSchemaFile:
    """The DDL itself: composite PKs, indexes, roles, no append-only trigger."""

    def test_schema_creates_exactly_the_two_zone5_tables(self) -> None:
        sql_text = _SCHEMA_SQL_PATH.read_text(encoding="utf-8")
        created_tables = {
            line.split()[2]
            for line in sql_text.splitlines()
            if line.strip().upper().startswith("CREATE TABLE")
        }
        assert created_tables == {"entity_attributes", "entity_aliases"}

    def test_schema_has_no_append_only_trigger(self) -> None:
        sql_text = _SCHEMA_SQL_PATH.read_text(encoding="utf-8")
        assert "CREATE TRIGGER" not in sql_text

    def test_schema_grants_delete_on_both_tables_to_the_existing_compliance_role(
        self,
    ) -> None:
        sql_text = _SCHEMA_SQL_PATH.read_text(encoding="utf-8")
        assert (
            "GRANT DELETE ON entity_attributes TO dashanan_compliance_erasure_role"
            in sql_text
        )
        assert (
            "GRANT DELETE ON entity_aliases TO dashanan_compliance_erasure_role"
            in sql_text
        )
        # MUST-NOT-DEVIATE: extend the existing role, never create a
        # second, Zone-5-specific compliance role.
        assert sql_text.count("CREATE ROLE dashanan_compliance_erasure_role") <= 1

    def test_no_other_zone_schema_file_creates_these_table_names(self) -> None:
        other_schema_files = [
            p
            for p in _SCHEMA_SQL_PATH.parent.glob("*_schema.sql")
            if p.name != "entity_schema.sql"
        ]
        assert other_schema_files, "expected sibling zone schema files to exist"
        for other in other_schema_files:
            text = other.read_text(encoding="utf-8")
            assert "CREATE TABLE entity_attributes" not in text
            assert "CREATE TABLE entity_aliases" not in text


class TestMigrationRunnerIntegration:
    """`migration_runner.SCHEMA_FILES` includes `entity_schema.sql` (AC-029-DEV-3)."""

    def test_entity_schema_is_registered_in_schema_files(self) -> None:
        from dashanan.infrastructure.migration_runner import SCHEMA_FILES

        assert "entity_schema.sql" in SCHEMA_FILES


class TestOrchestratorConstructorWidening:
    """AC-029-DEV-2: the orchestrator accepts `SqlEntityMemoryRepository` unmodified."""

    def test_sql_entity_memory_repository_satisfies_zone5_erasure_capable(self) -> None:
        from dashanan.application.unified_subject_erasure_orchestrator import (
            Zone5ErasureCapable,
        )

        repo = SqlEntityMemoryRepository(RecordingConnection())
        assert isinstance(repo, Zone5ErasureCapable)

    def test_entity_memory_repository_still_satisfies_zone5_erasure_capable(
        self,
    ) -> None:
        """MUST-NOT-DEVIATE: Shape A (`EntityMemoryRepository`) is unmodified
        and must remain structurally compatible with the widened Protocol."""
        from dashanan.application.unified_subject_erasure_orchestrator import (
            Zone5ErasureCapable,
        )
        from dashanan.infrastructure.entity_memory_repository import (
            EntityMemoryRepository,
        )
        from dashanan.infrastructure.noop_event_bus import NoOpEventBus
        from dashanan.infrastructure.system_clock import SystemClock

        repo = EntityMemoryRepository(clock=SystemClock(), event_bus=NoOpEventBus())
        assert isinstance(repo, Zone5ErasureCapable)
