"""Real-Postgres integration proof for DASH-STORY-029-QA (Zone 5 Shape B storage).

Proves, against a real, live PostgreSQL instance (never mocks -- mirrors
`test_api_real_postgres_e2e_dash3.py`'s own real-`docker compose` fixture
pattern verbatim: random project name, dynamically allocated free ports
via `_free_tcp_port()`, `secrets.token_urlsafe` credentials, and
unconditional `docker compose down -v --remove-orphans` teardown):

  AC-029-QA-1: `SqlEntityMemoryRepository.erase_entity` (Shape B) returns
    byte-for-byte identical evidence to `EntityMemoryRepository.erase_entity`
    (Shape A) for an identical seed fixture -- the design doc Section 6
    interface-compatibility table's central claim, proven for real rather
    than merely asserted in a docstring.

  AC-029-QA-2: the `dashanan_entity_role` / `dashanan_compliance_erasure_role`
    privilege boundary that `entity_schema.sql` actually grants. See this
    module's own `TestAC029QA2PrivilegeBoundary` docstring for TWO disclosed,
    ESCALATED findings confirmed by direct execution against a real
    PostgreSQL 16 instance during this dispatch (never guessed, never
    inferred from the DDL alone):

      1. `dashanan_compliance_erasure_role`'s own `erase_entity` DELETE
         statement (`SqlEntityMemoryRepository.erase_entity`'s real,
         unmodified SQL) FAILS with a real `psycopg.errors.
         InsufficientPrivilege: permission denied for table
         entity_attributes` even after correctly assuming the role via
         `SET ROLE` -- `entity_schema.sql` grants this role `DELETE` only,
         but PostgreSQL's real privilege model additionally requires
         `SELECT` on every column referenced in a `DELETE`'s `WHERE`
         clause or `RETURNING` list (here: `tenant_id`, `entity_id`,
         `attribute_name`), which this role was never granted. This is a
         genuine, functional defect in the schema/adapter pairing, not a
         test-authoring gap or a benign asymmetry -- confirmed by real
         execution, reproduced identically both inside this suite's own
         pytest run and in an isolated, pytest-free standalone script
         against a fresh throwaway stack.

      2. `dashanan_entity_role` holds a strict superset of
         `dashanan_compliance_erasure_role`'s own grants, so there is no
         operation on which `dashanan_entity_role` is rejected -- the
         assignment's "each rejected on the other's exclusive grant"
         premise does not hold symmetrically for this schema as written.

    Per this story's own escalation policy ("a FAIL here is NOT retried; it
    escalates immediately to the user"), both findings are disclosed here
    and in this dispatch's own final report rather than silently narrowed
    or "fixed" by editing `entity_schema.sql` or
    `sql_entity_memory_repository.py` (a schema/scope decision for a
    maintainer, exactly as `test_api_real_postgres_e2e_dash3.py`'s own
    Scenario 3 precedent treats an equivalent discovered grant gap). Because
    finding 1 means `erase_entity` cannot actually run to completion through
    `dashanan_compliance_erasure_role` today, `TestAC029QA1EraseEntityInterfaceParity`
    below is left asserting the INTENDED contract (real Postgres, the
    documented least-privilege role) rather than routed around the gap --
    it fails for real, and that failure is the honest, correct signal until
    a maintainer grants the missing `SELECT` (or narrower, column-level
    equivalent) privilege.

  AC-029-QA-3: `SqlEntityMemoryRepository.resolve_alias_prefix`/
    `resolve_exact_term` (Shape B) return identical `frozenset[str]` results
    to `EntityMemoryRepository`'s own `AliasTrie`-backed equivalents (Shape
    A) for the exact alias/prefix input set
    `tests/test_entity_memory_repository.py::TestAliasResolutionAC005_3AndAC005_4`
    and `::TestAliasTrieUnit` already use.

MUST-NOT-DEVIATE (this dispatch's own instructions): test style mirrors
`test_api_real_postgres_e2e_dash3.py` -- real Postgres, never mocks; this
module does not assert resolution of any of
`docs/phase-1.5-design/zone5-shape-b-storage-design.md` v4 Section 8's
disclosed open items (that document remains DRAFT).

Skip condition (never a false pass): identical to
`test_api_real_postgres_e2e_dash3.py` -- `docker` (with the `compose`
plugin) unavailable, or the Docker daemon unreachable.
"""

from __future__ import annotations

import os
import secrets
import shutil
import socket
import subprocess
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

import psycopg
import pytest
from psycopg import sql

from dashanan.infrastructure.entity_memory_repository import EntityMemoryRepository
from dashanan.infrastructure.migration_runner import SCHEMA_FILES, run_migrations
from dashanan.infrastructure.noop_event_bus import NoOpEventBus
from dashanan.infrastructure.settings import PostgresSettings
from dashanan.infrastructure.sql_entity_memory_repository import (
    SqlEntityMemoryRepository,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"

_TENANT_ID = "tenant-qa-029"


def _docker_compose_available() -> bool:
    """Mirror `test_api_real_postgres_e2e_dash3.py`'s own identical availability check."""
    if shutil.which("docker") is None:
        return False
    try:
        info = subprocess.run(["docker", "info"], capture_output=True, timeout=15, check=False)
        if info.returncode != 0:
            return False
        compose_check = subprocess.run(
            ["docker", "compose", "version"], capture_output=True, timeout=15, check=False
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return compose_check.returncode == 0


DOCKER_COMPOSE_AVAILABLE = _docker_compose_available()

pytestmark = pytest.mark.skipif(
    not DOCKER_COMPOSE_AVAILABLE,
    reason=(
        "Docker daemon or the `docker compose` v2 plugin is unavailable on this "
        "machine -- see this module's docstring for why a real docker-compose-backed "
        "proof is required, and why this skip must never be read as DASH-STORY-029-QA's "
        "three ACs having been verified."
    ),
)


def _free_tcp_port() -> int:
    """Return a currently-unused TCP port on localhost (best-effort, TOCTOU-tolerant)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ShapeBStack(NamedTuple):
    """Connection details for one throwaway Shape B `docker compose` stack."""

    project: str
    postgres_port: int
    postgres_database: str
    migration_user: str
    migration_password: str
    app_user: str
    app_password: str

    def postgres_settings(self) -> PostgresSettings:
        return PostgresSettings(
            host="127.0.0.1",
            port=self.postgres_port,
            database=self.postgres_database,
            migration_user=self.migration_user,
            migration_password=self.migration_password,
            app_login_user=self.app_user,
            app_login_password=self.app_password,
        )


def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess:
    timeout = kwargs.pop("timeout", 300)
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False, **kwargs)


@pytest.fixture(scope="module")
def shape_b_stack() -> Iterator[ShapeBStack]:
    """Bring up a throwaway, fully-random-credentialed Shape B `docker compose` stack.

    Mirrors `test_api_real_postgres_e2e_dash3.py`'s own `shape_b_stack`
    fixture verbatim.
    """
    project = f"dash029qa-e2e-{uuid.uuid4().hex[:12]}"
    stack = ShapeBStack(
        project=project,
        postgres_port=_free_tcp_port(),
        postgres_database="dashanan_e2e_test",
        migration_user=f"migration_{uuid.uuid4().hex[:8]}",
        migration_password=secrets.token_urlsafe(24),
        app_user=f"app_{uuid.uuid4().hex[:8]}",
        app_password=secrets.token_urlsafe(24),
    )
    env = {
        "DASHANAN_POSTGRES_PORT": str(stack.postgres_port),
        "DASHANAN_POSTGRES_DATABASE": stack.postgres_database,
        "DASHANAN_POSTGRES_MIGRATION_USER": stack.migration_user,
        "DASHANAN_POSTGRES_MIGRATION_PASSWORD": stack.migration_password,
        "DASHANAN_REDIS_PORT": str(_free_tcp_port()),
        "DASHANAN_REDIS_PASSWORD": secrets.token_urlsafe(24),
        "DASHANAN_QDRANT_PORT": str(_free_tcp_port()),
        "DASHANAN_QDRANT_GRPC_PORT": str(_free_tcp_port()),
        "DASHANAN_QDRANT_API_KEY": secrets.token_urlsafe(24),
        "DASHANAN_OPENSEARCH_PORT": str(_free_tcp_port()),
        "DASHANAN_OPENSEARCH_PASSWORD": f"Aa1!{secrets.token_urlsafe(20)}",
        "DASHANAN_S3_PORT": str(_free_tcp_port()),
        "DASHANAN_S3_CONSOLE_PORT": str(_free_tcp_port()),
        "DASHANAN_S3_ACCESS_KEY": f"s3access{uuid.uuid4().hex[:8]}",
        "DASHANAN_S3_SECRET_KEY": secrets.token_urlsafe(24),
    }
    full_env = {**os.environ, **env}

    up_result = _run(
        ["docker", "compose", "-p", project, "-f", str(COMPOSE_FILE), "up", "-d", "--wait",
         "--wait-timeout", "180"],
        env=full_env,
        timeout=240,
    )
    if up_result.returncode != 0:
        _run(
            ["docker", "compose", "-p", project, "-f", str(COMPOSE_FILE), "down", "-v",
             "--remove-orphans"],
            env=full_env,
        )
        pytest.skip(
            "Could not bring up the Shape B docker-compose stack for the real-"
            f"Postgres DASH-STORY-029-QA suite (stdout={up_result.stdout!r} "
            f"stderr={up_result.stderr!r})"
        )
    try:
        yield stack
    finally:
        _run(
            ["docker", "compose", "-p", project, "-f", str(COMPOSE_FILE), "down", "-v",
             "--remove-orphans"],
            env=full_env,
            timeout=120,
        )


@pytest.fixture(scope="module")
def applied_migrations(shape_b_stack: ShapeBStack) -> ShapeBStack:
    """Apply every zone schema file exactly once per module (schema files are not re-runnable)."""
    applied = run_migrations(shape_b_stack.postgres_settings())
    assert applied == list(SCHEMA_FILES)
    return shape_b_stack


def _random_role_name(prefix: str) -> str:
    """A fixed, this-codebase-controlled identifier prefix + a random suffix.

    Never derived from external input (mirrors `migration_runner.
    bind_app_login_role`'s own comment on identifier safety): the random
    suffix only avoids collisions across parallel test runs against the
    same throwaway database, it is not attacker-controlled content.
    """
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _create_login_role_with_membership(
    migration_connection: psycopg.Connection, *, member_of: str
) -> tuple[str, str]:
    """Create a throwaway real LOGIN role granted membership in `member_of`.

    `member_of` is one of this schema's own two NOLOGIN roles
    (`dashanan_entity_role` / `dashanan_compliance_erasure_role`) --
    neither is wired into `migration_runner._PER_ZONE_LOGIN_MEMBER_ROLES`
    yet (that module's own docstring discloses this as `dashanan_entity_role`'s
    "own follow-on, out of this story's scope"), so this test grants the
    membership itself, directly, exactly the way a real deployment's own
    follow-on wiring eventually would -- never by relaxing or editing the
    schema file's own grants.

    Returns:
        `(role_name, role_password)` -- a real, randomly generated
        credential this test connects with directly, mirroring
        `migration_runner.bind_app_login_role`'s own `sql.Literal`-quoted
        `CREATE ROLE ... LOGIN PASSWORD` statement shape.
    """
    role_name = _random_role_name("qa029_login")
    role_password = secrets.token_urlsafe(24)
    with migration_connection.cursor() as cursor:
        cursor.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(role_name), sql.Literal(role_password)
            )
        )
        cursor.execute(
            sql.SQL("GRANT {} TO {}").format(
                sql.Identifier(member_of), sql.Identifier(role_name)
            )
        )
    migration_connection.commit()
    return role_name, role_password


def _connect_as(
    stack: ShapeBStack, *, user: str, password: str, activate_role: str
) -> psycopg.Connection:
    """Open a real Postgres connection as a login role, then `SET ROLE` its granted membership.

    `GRANT {nologin_role} TO {login_role}` alone does not reliably make the
    login role's *table-level* grants effective within this session on
    every PostgreSQL build/version combination this suite may run
    against (confirmed by direct exploration during this dispatch: a bare
    membership grant, with no `SET ROLE`, left a real
    `dashanan_compliance_erasure_role` member unable to `DELETE` despite
    that table-level grant existing) -- `SET ROLE` is the deterministic,
    version-independent way to make a session's privileges exactly the
    activated role's own grants, and is the mechanism a real compliance
    erasure job assuming this role would use too.
    """
    from psycopg.conninfo import make_conninfo

    connection = psycopg.connect(
        make_conninfo(
            host="127.0.0.1",
            port=stack.postgres_port,
            dbname=stack.postgres_database,
            user=user,
            password=password,
            connect_timeout=10,
        )
    )
    with connection.cursor() as cursor:
        cursor.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(activate_role)))
    connection.commit()
    return connection


@pytest.fixture(scope="module")
def migration_connection(applied_migrations: ShapeBStack) -> Iterator[psycopg.Connection]:
    """A real connection as the migration role -- used only to provision throwaway login roles."""
    connection = psycopg.connect(applied_migrations.postgres_settings().migration_dsn())
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture(scope="module")
def entity_role_credential(
    migration_connection: psycopg.Connection,
) -> tuple[str, str]:
    """A real LOGIN role granted membership in `dashanan_entity_role`."""
    return _create_login_role_with_membership(migration_connection, member_of="dashanan_entity_role")


@pytest.fixture(scope="module")
def erasure_role_credential(
    migration_connection: psycopg.Connection,
) -> tuple[str, str]:
    """A real LOGIN role granted membership in `dashanan_compliance_erasure_role`."""
    return _create_login_role_with_membership(
        migration_connection, member_of="dashanan_compliance_erasure_role"
    )


@pytest.fixture
def entity_role_connection(
    applied_migrations: ShapeBStack, entity_role_credential: tuple[str, str]
) -> Iterator[psycopg.Connection]:
    """A fresh real connection as the `dashanan_entity_role`-member login, per test."""
    user, password = entity_role_credential
    connection = _connect_as(
        applied_migrations, user=user, password=password, activate_role="dashanan_entity_role"
    )
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def erasure_role_connection(
    applied_migrations: ShapeBStack, erasure_role_credential: tuple[str, str]
) -> Iterator[psycopg.Connection]:
    """A fresh real connection as the `dashanan_compliance_erasure_role`-member login, per test."""
    user, password = erasure_role_credential
    connection = _connect_as(
        applied_migrations,
        user=user,
        password=password,
        activate_role="dashanan_compliance_erasure_role",
    )
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def shape_a_repository() -> EntityMemoryRepository:
    """The real, in-process Shape A adapter -- the parity fixture's baseline."""
    return EntityMemoryRepository(clock=_FixedClock(), event_bus=NoOpEventBus())


@pytest.fixture
def shape_b_repository(
    entity_role_connection: psycopg.Connection,
) -> SqlEntityMemoryRepository:
    """The real Shape B adapter, wired to a real `dashanan_entity_role`-member connection.

    Ordinary (non-erasure) operations -- `write_attribute`, `get_entity`,
    `register_alias`, `resolve_alias_prefix`, `resolve_exact_term` -- run
    through `dashanan_entity_role`'s own grant, exactly as a real
    deployment's application connection would (design doc Section 4).
    """
    return SqlEntityMemoryRepository(
        connection=entity_role_connection, clock=_FixedClock(), event_bus=NoOpEventBus()
    )


@pytest.fixture
def shape_b_erasure_repository(
    erasure_role_connection: psycopg.Connection,
) -> SqlEntityMemoryRepository:
    """A SEPARATE Shape B adapter instance, wired to the `dashanan_compliance_erasure_role`
    connection -- used only for `erase_entity` calls, mirroring design doc Section 5's own
    "connects through the existing... compliance_erasure_role" requirement."""
    return SqlEntityMemoryRepository(
        connection=erasure_role_connection, clock=_FixedClock(), event_bus=NoOpEventBus()
    )


class _FixedClock:
    """A trivial, deterministic `Clock` double: both repositories share the same instant.

    Byte-for-byte parity (AC-029-QA-1) requires `updated_at` to match
    between the two adapters' own writes too -- a `SystemClock` on each
    side would make every write's `updated_at` diverge by however many
    microseconds elapsed between the two calls.
    """

    def __init__(self) -> None:
        self._now = datetime(2026, 9, 24, 12, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now


# ---------------------------------------------------------------------------
# AC-029-QA-1: erase_entity byte-for-byte return-value parity, Shape A vs
# Shape B, against an identical seed fixture.
# ---------------------------------------------------------------------------


class TestAC029QA1EraseEntityInterfaceParity:
    def test_erase_entity_returns_identical_item_ids_shape_a_vs_shape_b(
        self,
        shape_a_repository: EntityMemoryRepository,
        shape_b_repository: SqlEntityMemoryRepository,
        shape_b_erasure_repository: SqlEntityMemoryRepository,
    ) -> None:
        """Seeds the identical fixture into both adapters, erases both, compares real output.

        Real Postgres, real in-process dict -- never a mock of either
        side. `write_attribute`'s own `WriteGateResult` is asserted
        `WriteAccepted` on both sides first, so a silent 422 rejection on
        either adapter cannot masquerade as "erase returned nothing to
        compare".
        """
        entity_id = f"entity-{uuid.uuid4().hex[:10]}"
        attributes = {
            "display_name": "attr-value-alpha",
            "role_title": "attr-value-beta",
            "team": "attr-value-gamma",
        }

        for attribute_name, value in attributes.items():
            provenance_id = f"prov-{attribute_name}"
            a_result = shape_a_repository.write_attribute(
                _TENANT_ID, entity_id, attribute_name, value, provenance_id
            )
            b_result = shape_b_repository.write_attribute(
                _TENANT_ID, entity_id, attribute_name, value, provenance_id
            )
            assert type(a_result).__name__ == "WriteAccepted", a_result
            assert type(b_result).__name__ == "WriteAccepted", b_result

        a_record = shape_a_repository.get_entity(_TENANT_ID, entity_id)
        b_record = shape_b_repository.get_entity(_TENANT_ID, entity_id)
        assert a_record is not None
        assert b_record is not None
        assert {attr.attribute_name: attr.value for attr in a_record.attributes} == attributes
        assert {attr.attribute_name: attr.value for attr in b_record.attributes} == attributes

        a_erased = shape_a_repository.erase_entity(_TENANT_ID, entity_id)
        b_erased = shape_b_erasure_repository.erase_entity(_TENANT_ID, entity_id)

        expected_item_ids = {f"{entity_id}:{name}" for name in attributes}
        assert set(a_erased) == expected_item_ids, (
            "Shape A erase_entity must return exactly the seeded item_ids"
        )
        assert set(b_erased) == expected_item_ids, (
            "Shape B erase_entity must return exactly the seeded item_ids"
        )
        assert set(a_erased) == set(b_erased), (
            "AC-029-QA-1: Shape A and Shape B erase_entity must return "
            f"byte-for-byte identical item_id sets; got Shape A={sorted(a_erased)!r} "
            f"vs Shape B={sorted(b_erased)!r}"
        )
        for item_id in a_erased:
            assert item_id in b_erased, (
                f"item_id {item_id!r} present in Shape A's return but not Shape B's "
                "-- not byte-for-byte identical"
            )

        # Post-erasure parity: both adapters report the entity gone.
        assert shape_a_repository.get_entity(_TENANT_ID, entity_id) is None
        assert shape_b_repository.get_entity(_TENANT_ID, entity_id) is None

    def test_erase_entity_empty_no_op_parity_shape_a_vs_shape_b(
        self,
        shape_a_repository: EntityMemoryRepository,
        shape_b_erasure_repository: SqlEntityMemoryRepository,
    ) -> None:
        """An entity with no attributes ever written erases as a clean, empty no-op on both sides."""
        entity_id = f"entity-never-written-{uuid.uuid4().hex[:10]}"

        a_erased = shape_a_repository.erase_entity(_TENANT_ID, entity_id)
        b_erased = shape_b_erasure_repository.erase_entity(_TENANT_ID, entity_id)

        assert a_erased == ()
        assert b_erased == ()


# ---------------------------------------------------------------------------
# AC-029-QA-2: dashanan_entity_role / dashanan_compliance_erasure_role
# privilege boundary.
# ---------------------------------------------------------------------------


class TestAC029QA2PrivilegeBoundary:
    """Real-role privilege-boundary proof for `entity_schema.sql`'s two grants.

    UPDATED 2026-09-24 (fix verification, not the original authoring pass):
    finding 1 below (a real, reproducible functional defect) was confirmed
    by this class's own tests, then fixed in `entity_schema.sql` by adding
    the `SELECT` grant PostgreSQL requires alongside `DELETE` for any
    `WHERE`/`RETURNING`-clause statement -- see that file's own "FIX (found
    2026-09-24...)" comment for the full writeup. The two tests below that
    asserted the pre-fix broken behavior have been flipped to assert the
    fixed behavior, per their own original docstrings' instruction to do so
    "the moment a maintainer grants the missing SELECT." Finding 2 remains
    an accurate, still-true disclosed judgment call, not a defect -- see
    below.

    DISCLOSED FINDINGS (both confirmed by real execution against a real
    PostgreSQL 16 instance):

      1. FIXED 2026-09-24: `dashanan_compliance_erasure_role` could not run
         the exact, real `DELETE ... WHERE tenant_id = %s AND entity_id = %s`
         statement `SqlEntityMemoryRepository.erase_entity` issues, even
         after correctly `SET ROLE`-ing into it (confirmed: `current_user`
         becomes `dashanan_compliance_erasure_role`, `pg_auth_members`
         shows `inherit_option=true, set_option=true` for its membership)
         -- PostgreSQL requires `SELECT` on `tenant_id`/`entity_id` (the
         `WHERE` columns) in addition to `DELETE`. `entity_schema.sql` now
         grants this role `SELECT` as well as `DELETE` on both tables --
         confirmed fixed below.

      2. STILL TRUE, DISCLOSED, NOT A DEFECT: `dashanan_entity_role` has
         `SELECT, INSERT, UPDATE, DELETE` on `entity_attributes` and
         `SELECT, INSERT, DELETE` on `entity_aliases` -- still a strict
         SUPERSET of `dashanan_compliance_erasure_role`'s own `SELECT,
         DELETE` grant even after the fix above (entity_role additionally
         holds INSERT/UPDATE). There is therefore still NO operation
         `dashanan_compliance_erasure_role` can perform that
         `dashanan_entity_role` cannot; the "rejected on the other's
         exclusive grant" half of this AC, as applied to
         `dashanan_entity_role`, still cannot be satisfied by the schema as
         written -- this was always the design doc's own disclosed
         defense-in-depth judgment call (Section 5), not something the
         fix above was meant to change.

    Both real, distinct LOGIN roles used below are throwaway, randomly
    credentialed, granted membership in the one NOLOGIN role each test
    needs (`_create_login_role_with_membership`), and `SET ROLE`-activated
    (`_connect_as`) -- never a superuser/migration-role shortcut.
    """

    def test_compliance_erasure_role_can_now_delete_entity_attributes(
        self, erasure_role_connection: psycopg.Connection
    ) -> None:
        """FIXED 2026-09-24 (finding 1, class docstring): the role's own sole
        purpose -- deleting via `erase_entity`'s real `WHERE`-clause DELETE --
        now succeeds, since `entity_schema.sql` grants `SELECT` on the
        `WHERE` columns alongside `DELETE`. This test asserts the REAL,
        current (fixed) behavior -- flipped from the original pre-fix
        assertion per that test's own instruction to do so once the grant
        landed."""
        with erasure_role_connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM entity_attributes WHERE tenant_id = %s AND entity_id = %s",
                (_TENANT_ID, "nonexistent-for-privilege-probe"),
            )
        erasure_role_connection.commit()

    def test_compliance_erasure_role_can_delete_with_no_where_clause_columns_read(
        self, erasure_role_connection: psycopg.Connection
    ) -> None:
        """Isolates finding 1 to the `WHERE`-column `SELECT` requirement specifically:
        an unconditional `DELETE` (no column read at all) DOES succeed under this
        role's real `DELETE`-only grant, confirming the grant itself is not
        entirely inert -- only `erase_entity`'s own `WHERE`-clause shape is
        blocked by the missing `SELECT`."""
        with erasure_role_connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM entity_attributes WHERE false"
            )
        erasure_role_connection.commit()

    def test_compliance_erasure_role_can_now_select_entity_attributes(
        self, erasure_role_connection: psycopg.Connection
    ) -> None:
        """UPDATED 2026-09-24: SELECT is no longer `dashanan_entity_role`'s own
        exclusive grant -- `entity_schema.sql`'s fix for finding 1 (above)
        also grants `dashanan_compliance_erasure_role` `SELECT`, since
        PostgreSQL requires it for the role's own `WHERE`-clause `DELETE` to
        function. This test asserts the current, correct behavior -- flipped
        from the original pre-fix assertion. `dashanan_entity_role` remains
        the only role with INSERT/UPDATE (see finding 2, class docstring),
        so the privilege boundary is narrower, not eliminated."""
        with erasure_role_connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM entity_attributes WHERE tenant_id = %s LIMIT 1",
                (_TENANT_ID,),
            )
        erasure_role_connection.commit()

    def test_compliance_erasure_role_rejected_on_insert_entity_attributes(
        self, erasure_role_connection: psycopg.Connection
    ) -> None:
        """INSERT is `dashanan_entity_role`'s own exclusive grant -- the erasure role lacks it."""
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with erasure_role_connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO entity_attributes "
                    "(tenant_id, entity_id, attribute_name, value, provenance_id, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s, now())",
                    (_TENANT_ID, "privilege-probe", "attr", "value", "prov"),
                )
        erasure_role_connection.rollback()

    def test_compliance_erasure_role_rejected_on_update_entity_attributes(
        self, erasure_role_connection: psycopg.Connection
    ) -> None:
        """UPDATE is `dashanan_entity_role`'s own exclusive grant -- the erasure role lacks it."""
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with erasure_role_connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE entity_attributes SET value = %s WHERE tenant_id = %s",
                    ("new-value", _TENANT_ID),
                )
        erasure_role_connection.rollback()

    def test_entity_role_can_perform_full_ordinary_crud(
        self, entity_role_connection: psycopg.Connection
    ) -> None:
        """Contrast case: `dashanan_entity_role` succeeds at every ordinary CRUD verb."""
        entity_id = f"privilege-probe-{uuid.uuid4().hex[:8]}"
        with entity_role_connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO entity_attributes "
                "(tenant_id, entity_id, attribute_name, value, provenance_id, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, now())",
                (_TENANT_ID, entity_id, "attr", "value", "prov"),
            )
            cursor.execute(
                "SELECT value FROM entity_attributes WHERE tenant_id = %s AND entity_id = %s",
                (_TENANT_ID, entity_id),
            )
            assert cursor.fetchone() == ("value",)
            cursor.execute(
                "UPDATE entity_attributes SET value = %s WHERE tenant_id = %s AND entity_id = %s",
                ("updated-value", _TENANT_ID, entity_id),
            )
            cursor.execute(
                "DELETE FROM entity_attributes WHERE tenant_id = %s AND entity_id = %s",
                (_TENANT_ID, entity_id),
            )
        entity_role_connection.commit()

    def test_entity_role_is_not_rejected_by_any_compliance_role_exclusive_grant(
        self, entity_role_connection: psycopg.Connection
    ) -> None:
        """Documents the disclosed asymmetry (class docstring) as a real, passing assertion.

        `dashanan_compliance_erasure_role`'s entire grant is `DELETE` on
        both Zone 5 tables -- `dashanan_entity_role` already holds `DELETE`
        on both too (`entity_schema.sql`'s own `GRANT SELECT, INSERT,
        UPDATE, DELETE ON entity_attributes` / `GRANT SELECT, INSERT,
        DELETE ON entity_aliases`), so there is no compliance-role-exclusive
        privilege to probe a rejection against. This test asserts exactly
        that: `dashanan_entity_role` performs the compliance role's own
        sole capability (`DELETE`) without any `InsufficientPrivilege`,
        confirming the AC's "each rejected on the other's exclusive grant"
        premise does not hold symmetrically for this schema as written
        today (see class docstring for the escalation).
        """
        with entity_role_connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM entity_aliases WHERE tenant_id = %s AND entity_id = %s",
                (_TENANT_ID, "nonexistent-for-privilege-probe"),
            )
        entity_role_connection.commit()


# ---------------------------------------------------------------------------
# AC-029-QA-3: alias prefix/exact-resolution parity, Shape A vs Shape B,
# against the same input set `tests/test_entity_memory_repository.py`'s own
# `TestAliasResolutionAC005_3AndAC005_4` / `TestAliasTrieUnit` suites use.
# ---------------------------------------------------------------------------


class TestAC029QA3AliasResolutionParity:
    def test_prefix_search_returns_matching_entity_ids_parity(
        self,
        shape_a_repository: EntityMemoryRepository,
        shape_b_repository: SqlEntityMemoryRepository,
    ) -> None:
        """Mirrors `test_entity_memory_repository.
        TestAliasResolutionAC005_3AndAC005_4.test_prefix_search_returns_matching_entity_ids`'s
        exact input set (`alpha-one`/`alpha-two`/`beta-one` against three entities)."""
        tenant_id = f"{_TENANT_ID}-prefix"
        for entity_id, alias in (
            ("entity-1", "alpha-one"),
            ("entity-2", "alpha-two"),
            ("entity-3", "beta-one"),
        ):
            shape_a_repository.register_alias(tenant_id, entity_id, alias)
            shape_b_repository.register_alias(tenant_id, entity_id, alias)

        a_matches = shape_a_repository.resolve_alias_prefix(tenant_id, "alpha")
        b_matches = shape_b_repository.resolve_alias_prefix(tenant_id, "alpha")

        assert a_matches == frozenset({"entity-1", "entity-2"})
        assert b_matches == frozenset({"entity-1", "entity-2"})
        assert a_matches == b_matches

    def test_prefix_search_with_no_match_returns_empty_set_parity(
        self,
        shape_a_repository: EntityMemoryRepository,
        shape_b_repository: SqlEntityMemoryRepository,
    ) -> None:
        """Mirrors `test_entity_memory_repository...
        test_prefix_search_with_no_match_returns_empty_set_not_error`."""
        tenant_id = f"{_TENANT_ID}-noprefix"
        shape_a_repository.register_alias(tenant_id, "entity-1", "alpha-one")
        shape_b_repository.register_alias(tenant_id, "entity-1", "alpha-one")

        assert shape_a_repository.resolve_alias_prefix(tenant_id, "zzz-nonexistent") == frozenset()
        assert shape_b_repository.resolve_alias_prefix(tenant_id, "zzz-nonexistent") == frozenset()

    def test_prefix_search_on_tenant_with_no_aliases_returns_empty_set_parity(
        self,
        shape_a_repository: EntityMemoryRepository,
        shape_b_repository: SqlEntityMemoryRepository,
    ) -> None:
        """Mirrors `test_entity_memory_repository...
        test_prefix_search_on_tenant_with_no_aliases_returns_empty_set`."""
        unknown_tenant = f"{_TENANT_ID}-unknown-{uuid.uuid4().hex[:6]}"
        assert shape_a_repository.resolve_alias_prefix(unknown_tenant, "alpha") == frozenset()
        assert shape_b_repository.resolve_alias_prefix(unknown_tenant, "alpha") == frozenset()

    def test_alias_resolution_is_isolated_per_tenant_parity(
        self,
        shape_a_repository: EntityMemoryRepository,
        shape_b_repository: SqlEntityMemoryRepository,
    ) -> None:
        """Mirrors `test_entity_memory_repository...
        test_alias_resolution_is_isolated_per_tenant`'s exact two-tenant input set."""
        tenant_1 = f"{_TENANT_ID}-iso-1"
        tenant_2 = f"{_TENANT_ID}-iso-2"
        for repo in (shape_a_repository, shape_b_repository):
            repo.register_alias(tenant_1, "entity-1", "alpha-one")
            repo.register_alias(tenant_2, "entity-9", "alpha-one")

        assert shape_a_repository.resolve_alias_prefix(tenant_1, "alpha") == frozenset({"entity-1"})
        assert shape_a_repository.resolve_alias_prefix(tenant_2, "alpha") == frozenset({"entity-9"})
        assert shape_b_repository.resolve_alias_prefix(tenant_1, "alpha") == frozenset({"entity-1"})
        assert shape_b_repository.resolve_alias_prefix(tenant_2, "alpha") == frozenset({"entity-9"})

    def test_exact_term_does_not_match_a_strict_prefix_of_a_longer_alias_parity(
        self,
        shape_a_repository: EntityMemoryRepository,
        shape_b_repository: SqlEntityMemoryRepository,
    ) -> None:
        """Mirrors `test_entity_memory_repository...
        test_exact_term_does_not_match_a_strict_prefix_of_a_longer_alias`."""
        tenant_id = f"{_TENANT_ID}-exactstrict"
        for repo in (shape_a_repository, shape_b_repository):
            repo.register_alias(tenant_id, "entity-1", "alpha-one")

        assert shape_a_repository.resolve_exact_term(tenant_id, "alpha") == frozenset()
        assert shape_b_repository.resolve_exact_term(tenant_id, "alpha") == frozenset()
        assert shape_a_repository.resolve_exact_term(tenant_id, "alpha-one") == frozenset({"entity-1"})
        assert shape_b_repository.resolve_exact_term(tenant_id, "alpha-one") == frozenset({"entity-1"})

    def test_insert_and_exact_match_parity(
        self,
        shape_a_repository: EntityMemoryRepository,
        shape_b_repository: SqlEntityMemoryRepository,
    ) -> None:
        """Mirrors `test_entity_memory_repository.TestAliasTrieUnit.test_insert_and_exact_match`."""
        tenant_id = f"{_TENANT_ID}-trie-exact"
        for repo in (shape_a_repository, shape_b_repository):
            repo.register_alias(tenant_id, "entity-1", "alpha")

        assert shape_a_repository.resolve_exact_term(tenant_id, "alpha") == frozenset({"entity-1"})
        assert shape_b_repository.resolve_exact_term(tenant_id, "alpha") == frozenset({"entity-1"})

    def test_prefix_search_collects_multiple_entities_parity(
        self,
        shape_a_repository: EntityMemoryRepository,
        shape_b_repository: SqlEntityMemoryRepository,
    ) -> None:
        """Mirrors `test_entity_memory_repository.
        TestAliasTrieUnit.test_prefix_search_collects_multiple_entities`."""
        tenant_id = f"{_TENANT_ID}-trie-collect"
        for repo in (shape_a_repository, shape_b_repository):
            repo.register_alias(tenant_id, "entity-1", "alpha-one")
            repo.register_alias(tenant_id, "entity-2", "alpha-two")

        assert shape_a_repository.resolve_alias_prefix(tenant_id, "alpha") == frozenset(
            {"entity-1", "entity-2"}
        )
        assert shape_b_repository.resolve_alias_prefix(tenant_id, "alpha") == frozenset(
            {"entity-1", "entity-2"}
        )
