"""Formal pytest suite for `infrastructure.migration_runner` (DASH-STORY-024, FR-015).

Unit-level coverage using DB-API doubles (no Docker, no real Postgres) for:
  - file-application order and content (`apply_schema_files`)
  - the app-login-role creation/update and per-zone GRANT logic
    (`bind_app_login_role`)

The real, docker-compose-backed end-to-end proof that this logic actually
closes AC-024-1/2/3 against a live PostgreSQL 16 instance lives in
`tests/test_regression_dash024_shape_b_deployment_infra.py`, mirroring
`test_qa_dash055_ownership_reassignment_bypass.py`'s own split between
unit-level and Docker-backed regression coverage.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Self

import psycopg
import pytest

from dashanan.infrastructure.migration_runner import (
    SCHEMA_FILES,
    MigrationConnectionError,
    apply_schema_files,
    bind_app_login_role,
    run_migrations,
)
from dashanan.infrastructure.settings import PostgresSettings

_SETTINGS = PostgresSettings(
    host="localhost",
    port=5432,
    database="dashanan",
    migration_user="migration_role",
    migration_password="migration-secret-1",
    app_login_user="dashanan_app_login",
    app_login_password="app-secret-1",
)


class FakeCursor:
    """DB-API cursor double supporting the context-manager protocol.

    Distinct from `tests.test_smoke_episodic.RecordingCursor`: this module's
    real code (`with connection.cursor() as cursor:`) requires
    `__enter__`/`__exit__`, which that shared double does not implement.
    `fetch_results` lets a test script canned `fetchone()`/`fetchall()`
    return values per call, in order.
    """

    def __init__(self, fetchone_results: Sequence[object | None] | None = None) -> None:
        self.executed: list[tuple[str, tuple[object, ...] | None]] = []
        self.executed_raw: list[object] = []
        self._fetchone_results = list(fetchone_results or [])

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def execute(self, sql: object, params: Sequence[object] | None = None) -> None:
        self.executed.append((str(sql), tuple(params) if params is not None else None))
        self.executed_raw.append(sql)

    def fetchone(self) -> object | None:
        if not self._fetchone_results:
            return None
        return self._fetchone_results.pop(0)


class FakeConnection:
    """DB-API connection double: one shared `FakeCursor`, records `commit()` calls."""

    def __init__(self, fetchone_results: Sequence[object | None] | None = None) -> None:
        self.cursor_obj = FakeCursor(fetchone_results)
        self.commit_count = 0

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def cursor(self) -> FakeCursor:
        return self.cursor_obj

    def commit(self) -> None:
        self.commit_count += 1


class TestSchemaFilesOrdering:
    """`SCHEMA_FILES` dependency order."""

    def test_schemaFiles_should_applyEpisodicAndProvenance_before_semantic(self) -> None:
        assert SCHEMA_FILES.index("episodic_schema.sql") < SCHEMA_FILES.index(
            "semantic_schema.sql"
        )
        assert SCHEMA_FILES.index("provenance_schema.sql") < SCHEMA_FILES.index(
            "semantic_schema.sql"
        )

    def test_schemaFiles_should_applyZone8Consolidation_before_itsOwnMigration(
        self,
    ) -> None:
        assert SCHEMA_FILES.index(
            "zone8_consolidation_schema.sql"
        ) < SCHEMA_FILES.index("zone8_manifest_subject_index_migration.sql")

    def test_schemaFiles_should_listEveryZoneSchemaFileOnDisk(self) -> None:
        schema_dir = (
            Path(__file__).resolve().parent.parent
            / "src"
            / "dashanan"
            / "infrastructure"
        )
        on_disk = {p.name for p in schema_dir.glob("*.sql")}

        assert set(SCHEMA_FILES) == on_disk


class TestApplySchemaFiles:
    """`apply_schema_files`."""

    def test_apply_should_executeEveryFile_inOrder_and_commitAfterEach(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "a.sql").write_text("-- statement a;", encoding="utf-8")
        (tmp_path / "b.sql").write_text("-- statement b;", encoding="utf-8")
        connection = FakeConnection()

        applied = apply_schema_files(
            connection,  # type: ignore[arg-type]
            schema_dir=tmp_path,
            schema_files=("a.sql", "b.sql"),
        )

        assert applied == ["a.sql", "b.sql"]
        executed_sql = [call[0] for call in connection.cursor_obj.executed]
        assert executed_sql == ["-- statement a;", "-- statement b;"]
        assert connection.commit_count == 2

    def test_apply_should_raise_when_aSchemaFileIsMissing(self, tmp_path: Path) -> None:
        connection = FakeConnection()

        with pytest.raises(FileNotFoundError):
            apply_schema_files(
                connection,  # type: ignore[arg-type]
                schema_dir=tmp_path,
                schema_files=("missing.sql",),
            )


class TestBindAppLoginRole:
    """`bind_app_login_role`."""

    def test_bind_should_createRole_when_roleDoesNotYetExist(self) -> None:
        connection = FakeConnection(fetchone_results=[None])

        bind_app_login_role(connection, _SETTINGS)  # type: ignore[arg-type]

        executed = connection.cursor_obj.executed
        create_calls = [
            call for call in executed if "CREATE ROLE" in call[0]
        ]
        assert len(create_calls) == 1
        # No separate bind-parameter tuple: CREATE ROLE ... PASSWORD does not
        # accept extended-protocol bind parameters (see bind_app_login_role's
        # own docstring) -- the password is rendered via sql.Literal instead.
        assert create_calls[0][1] is None
        alter_calls = [call for call in executed if "ALTER ROLE" in call[0]]
        assert alter_calls == []
        assert connection.commit_count == 1

    def test_bind_should_updatePassword_when_roleAlreadyExists(self) -> None:
        connection = FakeConnection(fetchone_results=[(1,)])

        bind_app_login_role(connection, _SETTINGS)  # type: ignore[arg-type]

        executed = connection.cursor_obj.executed
        alter_calls = [call for call in executed if "ALTER ROLE" in call[0]]
        assert len(alter_calls) == 1
        assert alter_calls[0][1] is None
        create_calls = [call for call in executed if "CREATE ROLE" in call[0]]
        assert create_calls == []

    def test_bind_should_grantAllPerZoneRoles_toTheAppLoginRole(self) -> None:
        """GitHub #22 / #24: dashanan_zone8_role and dashanan_semantic_role
        both pre-existed in their own schema files but were never added to
        _PER_ZONE_LOGIN_MEMBER_ROLES, so the real app login role had no
        grant path to either -- this asserts all four are now granted."""
        connection = FakeConnection(fetchone_results=[None])

        bind_app_login_role(connection, _SETTINGS)  # type: ignore[arg-type]

        grant_calls = [
            call[0] for call in connection.cursor_obj.executed if "GRANT" in call[0]
        ]
        assert len(grant_calls) == 4
        assert any("dashanan_provenance_role" in call for call in grant_calls)
        assert any("dashanan_episodic_role" in call for call in grant_calls)
        assert any("dashanan_semantic_role" in call for call in grant_calls)
        assert any("dashanan_zone8_role" in call for call in grant_calls)

    def test_bind_should_neverGrant_dashananSchemaOwner_toAPersistentLoginRole(
        self,
    ) -> None:
        """AC-024-2 / provenance_schema.sql's own design: dashanan_schema_owner
        is never-granted-out. Binding it to a second persistent LOGIN role here
        would contradict that -- this suite proves `bind_app_login_role` never
        does so.
        """
        connection = FakeConnection(fetchone_results=[None])

        bind_app_login_role(connection, _SETTINGS)  # type: ignore[arg-type]

        grant_calls = [
            call[0] for call in connection.cursor_obj.executed if "GRANT" in call[0]
        ]
        assert not any("dashanan_schema_owner" in call for call in grant_calls)

    def test_bind_should_useSqlLiteral_forThePassword_neverRawStringInterpolation(
        self,
    ) -> None:
        """application-security-core: never string-concatenate untrusted input
        into SQL. `CREATE ROLE ... PASSWORD` does not accept an extended-
        protocol bind parameter (see `bind_app_login_role`'s own docstring),
        so the safe-quoting mechanism here is `psycopg.sql.Literal` -- this
        test proves that mechanism is actually used (a `psycopg.sql.Composed`
        object containing a `sql.Literal` wrapping the password), not an
        f-string/`%`-interpolated plain Python string.
        """
        from psycopg import sql as psycopg_sql

        connection = FakeConnection(fetchone_results=[None])

        bind_app_login_role(connection, _SETTINGS)  # type: ignore[arg-type]

        create_role_query = next(
            raw
            for raw in connection.cursor_obj.executed_raw
            if isinstance(raw, psycopg_sql.Composed)
            and "CREATE ROLE" in str(next(iter(raw)))
        )
        literals = [
            part for part in create_role_query if isinstance(part, psycopg_sql.Literal)
        ]
        assert len(literals) == 1
        assert literals[0]._obj == _SETTINGS.app_login_password
        assert not isinstance(create_role_query, str)


class TestRunMigrationsConnectionErrorHandling:
    """`run_migrations` wraps a connection-phase `psycopg.Error` (DSHN-76)."""

    def test_runMigrations_should_raiseMigrationConnectionError_when_connectFails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        underlying = psycopg.OperationalError(
            'connection to server failed: FATAL:  password authentication '
            f'failed for user "{_SETTINGS.migration_user}"'
        )

        def _raise_connect(dsn: str) -> None:
            raise underlying

        monkeypatch.setattr(psycopg, "connect", _raise_connect)

        with pytest.raises(MigrationConnectionError) as excinfo:
            run_migrations(_SETTINGS)

        assert _SETTINGS.migration_password not in str(excinfo.value)
        assert "FATAL" not in str(excinfo.value)
        assert excinfo.value.__cause__ is underlying

    def test_runMigrations_should_propagateConnection_when_connectSucceeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        connection = FakeConnection(fetchone_results=[None])

        def _fake_apply(conn: object, **kwargs: object) -> list[str]:
            assert conn is connection
            return ["a.sql"]

        def _fake_bind(conn: object, settings: object) -> None:
            assert conn is connection

        monkeypatch.setattr(psycopg, "connect", lambda dsn: connection)
        monkeypatch.setattr(
            "dashanan.infrastructure.migration_runner.apply_schema_files", _fake_apply
        )
        monkeypatch.setattr(
            "dashanan.infrastructure.migration_runner.bind_app_login_role", _fake_bind
        )

        applied = run_migrations(_SETTINGS)

        assert applied == ["a.sql"]
