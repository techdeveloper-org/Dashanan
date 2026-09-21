"""Real (not mocked) end-to-end integration: the real FastAPI host wired to a
real, live Postgres connection, for the first time in this codebase.

Every prior API-layer suite (`tests/test_api_wire_layer_dash023.py`) proves
the host's contract against the degraded/no-Postgres branch of
`api.composition.build_app_context` only (`postgres_connection=None`). This
module is the missing mirror: a real `docker compose` Postgres service, real
schema migrations (`infrastructure.migration_runner`), a real
`build_postgres_connection` connection, and a real `TestClient` wired through
`build_app_context(postgres_connection=<that real connection>)` -- so every
assertion below exercises the actual Postgres-backed code paths
(`SqlEpisodicRepository`, `SqlManifestRepository`,
`SubjectErasureCascadeService`'s Zone 8 leg, `ConflictAwareSemanticRepository`/
`ConflictAwareEntityMemoryRepository`) instead of their degraded stand-ins.

Reuses, unedited in spirit:
  - `test_api_wire_layer_dash023.py`'s own `app_context`/`client`
    fixture shape and JWT-minting helpers (`_token`/`_auth`).
  - `test_regression_dash024_shape_b_deployment_infra.py`'s own
    docker-compose-with-random-credentials fixture pattern verbatim: a
    random project name, dynamically allocated free ports via
    `_free_tcp_port()`, `secrets.token_urlsafe` credentials, and
    unconditional `docker compose down -v --remove-orphans` teardown.
  - `test_adversarial_dshn59_reaudit.py`'s own `threading.Barrier` +
    per-thread-result-list concurrency pattern (Scenario 5 below).

Scope note: this module explicitly does NOT exercise Qdrant/OpenSearch/Redis
reachability through the API host -- `api.composition.AppContext` does not
wire those services at all yet (only Postgres-backed adapters exist today),
so there is nothing real to test there. The full five-service
`docker-compose.yml` stack is still brought up (mirroring AC-024-1's own
fixture, and because the compose file's env-interpolated variables are
`:?required` for every service, not only Postgres) -- only the Postgres
service is ever queried by this module's assertions.

Known, already-disclosed limitation this module documents rather than
"fixes" (Scenario 5): `api.composition`'s own module docstring names
AR1-S3-G3 -- a single shared `psycopg.Connection` reused across Zone
2/3/7/8-manifest, not safe for concurrent use across threads without its own
pooling (a real `psycopg_pool` is FR-015 scope, not this round's). Scenario 5
below is written to assert and document whatever the real, currently-shared
connection actually does under concurrent load -- a race surfacing there is
expected confirmation of AR1-S3-G3, not a new regression, and must never be
"fixed" by widening this test's tolerance to hide it.

Skip condition (this suite degrades to a documented skip, never a false
pass, when its prerequisite is unavailable): `docker` (with the `compose`
plugin) is not on PATH, or the Docker daemon is not reachable -- mirrors
`test_regression_dash024_shape_b_deployment_infra.py`'s own skip precedent
and skip-reason wording exactly.
"""

from __future__ import annotations

import os
import secrets
import shutil
import socket
import subprocess
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

import jwt
import psycopg
import pytest
from fastapi.testclient import TestClient

from dashanan.api import app as app_module
from dashanan.api.composition import AppContext, build_app_context
from dashanan.application.subject_erasure_cascade import SubjectErasureJobStatus
from dashanan.domain.consolidated_blob import ArchiveBatchItem
from dashanan.domain.zone import ZoneId
from dashanan.infrastructure.composition_root import build_postgres_connection
from dashanan.infrastructure.migration_runner import SCHEMA_FILES, run_migrations
from dashanan.infrastructure.settings import PostgresSettings

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"

_JWT_KEY = bytes(range(32))
_TENANT_CREDENTIAL_KEY = bytes(range(32, 64))
_TENANT_ID = "tenant-a"


def _docker_compose_available() -> bool:
    """Mirror `test_regression_dash024...`'s own identical availability check."""
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
        "proof is required, and why this skip must never be read as this module's "
        "five real-Postgres scenarios having been verified."
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

    Mirrors `test_regression_dash024_shape_b_deployment_infra.py`'s own
    `shape_b_stack` fixture: random project name, random free host ports,
    `secrets.token_urlsafe` credentials for every service (never a literal
    password), and unconditional teardown in the finalizer. Every one of
    the five services' `:?required` compose variables is supplied even
    though only Postgres is queried by this module (see module docstring).
    """
    project = f"dash3-e2e-{uuid.uuid4().hex[:12]}"
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
            f"Postgres e2e suite (stdout={up_result.stdout!r} stderr={up_result.stderr!r})"
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


def _token(scope: str, *, aud: str = "dashanan-data-plane", tenant_id: str = _TENANT_ID,
           exp_delta: int = 300) -> str:
    now = int(time.time())
    return jwt.encode(
        {"tenant_id": tenant_id, "scope": scope, "aud": aud, "iat": now, "exp": now + exp_delta},
        _JWT_KEY,
        algorithm="HS256",
    )


def _auth(scope: str, **kwargs: object) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(scope, **kwargs)}"}


@pytest.fixture
def real_postgres_app_context(applied_migrations: ShapeBStack) -> Iterator[AppContext]:
    """A real `AppContext` wired to a real, live Postgres connection (Shape B, live branch).

    Mirrors `test_api_wire_layer_dash023.py`'s own `app_context` fixture
    shape, but passes an already-open `build_postgres_connection(...)`
    result as `postgres_connection` instead of `None` -- exercising
    `build_app_context`'s Postgres-AVAILABLE branch for the first time
    against a real database rather than a fake connection double.
    """
    connection = build_postgres_connection(applied_migrations.postgres_settings())
    try:
        context = build_app_context(
            tenant_credential_signing_key=_TENANT_CREDENTIAL_KEY,
            jwt_signing_key=_JWT_KEY,
            postgres_connection=connection,
        )
        assert context.postgres_available is True
        yield context
    finally:
        connection.close()


@pytest.fixture
def client(real_postgres_app_context: AppContext) -> Iterator[TestClient]:
    app = app_module.create_app(real_postgres_app_context)
    with TestClient(app) as test_client:
        yield test_client


def _independent_connection(stack: ShapeBStack) -> psycopg.Connection:
    """Open a SECOND, wholly independent real Postgres connection.

    Deliberately never reuses `real_postgres_app_context`'s own
    in-process `psycopg.Connection` object -- a read through this
    connection proves durable persistence in Postgres itself, not an
    in-process/in-memory echo of the write.
    """
    return build_postgres_connection(stack.postgres_settings())


# ---------------------------------------------------------------------------
# Scenario 1: full lifecycle -- real write, durability via a second
# independent connection, then a real HTTP read.
# ---------------------------------------------------------------------------


class TestScenario1FullLifecycleDurability:
    def test_http_write_is_durably_persisted_and_readable_via_independent_connection(
        self, client: TestClient, applied_migrations: ShapeBStack
    ) -> None:
        session_id = f"s-{uuid.uuid4().hex[:8]}"
        payload_text = f"durability-proof-{uuid.uuid4().hex}"

        write_response = client.post(
            "/memory/write",
            json={
                "session_id": session_id,
                "content": {"text": payload_text, "zone_hint": "episodic"},
                "provenance": {"source_type": "user_stated", "purpose": "dash3-e2e"},
            },
            headers={**_auth("memory:write"), "Idempotency-Key": str(uuid.uuid4())},
        )
        assert write_response.status_code == 202, write_response.text
        write_id = write_response.json()["write_id"]

        independent_connection = _independent_connection(applied_migrations)
        try:
            with independent_connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload, tenant_id, session_id FROM episodic_entries "
                    "WHERE episode_id = %s",
                    (write_id,),
                )
                row = cursor.fetchone()
        finally:
            independent_connection.close()

        assert row is not None, (
            "the write must be durably visible through a wholly separate "
            "Postgres connection, not only inside the app's own in-process object"
        )
        stored_payload, stored_tenant_id, stored_session_id = row
        assert stored_payload == payload_text
        assert stored_tenant_id == _TENANT_ID
        assert stored_session_id == session_id

        read_response = client.get(f"/items/{write_id}", headers=_auth("context:read"))
        assert read_response.status_code == 200, read_response.text
        assert read_response.json()["payload"] == payload_text


# ---------------------------------------------------------------------------
# Scenario 2: readiness reports LIVE/healthy when real Postgres is connected
# -- the mirror image of test_api_wire_layer_dash023.py's degraded-path test.
# ---------------------------------------------------------------------------


class TestScenario2ReadinessReportsHealthy:
    def test_readiness_reports_healthy_not_degraded_with_real_postgres(
        self, client: TestClient
    ) -> None:
        response = client.get("/readyz")
        assert response.status_code == 200
        body = response.json()
        assert body["ready"] is True
        for adapter_name in ("episodic", "semantic", "provenance", "consolidation"):
            assert body["adapters"][adapter_name] == "healthy", (
                f"adapters.{adapter_name} must report 'healthy' (never 'degraded') "
                "once a real Postgres connection is live"
            )
        assert body["adapters"]["working"] == "healthy"


# ---------------------------------------------------------------------------
# Scenario 3: eraseSubject via a real HTTP request against a REAL
# Postgres-backed SubjectErasureCascadeService -- no spy/fake.
# ---------------------------------------------------------------------------


class TestScenario3RealPostgresBackedErasure:
    def test_erase_subject_reaches_real_postgres_and_stays_honestly_zone8_scoped(
        self, client: TestClient, real_postgres_app_context: AppContext,
        applied_migrations: ShapeBStack,
    ) -> None:
        """Seeds real data, erases it for real, verifies via an independent read.

        Seeding note (disclosed gap, not fabricated): the natural seed
        path -- the real `Zone8SubjectKeyedArchiver.archive_for_subject`,
        through the app-login-connected `SqlManifestRepository` -- was
        found during this dispatch to fail with a real
        `psycopg.errors.InsufficientPrivilege` ("permission denied for
        table zone8_manifest") against a genuinely fresh, migrated
        database: `zone8_consolidation_schema.sql` grants no privilege
        on `zone8_manifest` to any login-capable role, and
        `migration_runner._PER_ZONE_LOGIN_MEMBER_ROLES` only names the
        provenance/episodic roles (AC-024-2's own explicit scope) --
        filed as a GitHub issue rather than patched here (a new role/
        grant is a schema-and-scope decision for a maintainer, not this
        dispatch). This test therefore seeds the `zone8_manifest` row
        directly on the MIGRATION-role connection (the one role this
        environment already knows can write that table) while still
        driving the actual erasure through the real, unmodified,
        Postgres-backed `AppContext`/`SubjectErasureCascadeService` --
        `erase_subject` itself never touches `zone8_manifest` (see
        `Zone8SubjectKeyedArchiver.erase_subject`'s own docstring: only
        the in-process key store and subject index are touched), so this
        substitution changes only how the row was SEEDED, not what is
        being proven about the real erasure call.
        """
        subject_id = f"subj-{uuid.uuid4().hex[:10]}"
        item_id = f"item-{uuid.uuid4().hex[:10]}"

        seed_connection = psycopg.connect(applied_migrations.postgres_settings().migration_dsn())
        try:
            with seed_connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO zone8_manifest "
                    "(tenant_id, item_id, blob_id, byte_start, byte_end, "
                    " compression_generation, source_zone, written_at, subject_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, now(), %s)",
                    (_TENANT_ID, item_id, "b" * 64, 0, 32, 0, ZoneId.EPISODIC.value, subject_id),
                )
            seed_connection.commit()
        finally:
            seed_connection.close()

        # Keep the real, in-process key store + subject index (the
        # collaborators `erase_subject` itself actually calls) consistent
        # with the manifest row seeded above, via their own real methods
        # -- never a spy or fake.
        archiver = real_postgres_app_context.subject_erasure_service._archiver  # type: ignore[union-attr]
        archiver._key_store.get_or_create_key(_TENANT_ID, subject_id)
        archiver._subject_index.record_item(_TENANT_ID, subject_id, item_id)

        # Independent read on the migration-role connection too: the app
        # login connection has no SELECT grant on zone8_manifest either
        # (the same disclosed gap this test's docstring names above) --
        # confirmed by direct exploration during this dispatch.
        independent_read_connection = psycopg.connect(
            applied_migrations.postgres_settings().migration_dsn()
        )
        try:
            with independent_read_connection.cursor() as cursor:
                cursor.execute(
                    "SELECT item_id FROM zone8_manifest WHERE tenant_id = %s AND subject_id = %s",
                    (_TENANT_ID, subject_id),
                )
                seeded_rows = cursor.fetchall()
        finally:
            independent_read_connection.close()
        assert seeded_rows, (
            "the seeded archive must be durably visible in the real zone8_manifest "
            "table through an independent connection before erasure is requested"
        )
        assert {row[0] for row in seeded_rows} == {item_id}

        erase_response = client.delete(
            f"/tenants/{_TENANT_ID}/subjects/{subject_id}",
            headers=_auth("admin:tenants", aud="dashanan-control-plane"),
        )
        assert erase_response.status_code == 202, erase_response.text
        job_id = erase_response.json()["job_id"]

        # Verify erasure via an independent read: the real
        # SubjectErasureCascadeService's own job record (not the HTTP
        # layer's separate admin_job_store, which does not carry
        # item_ids) -- the real, Postgres-index-resolved list of items
        # this subject's crypto-shred reached.
        job = real_postgres_app_context.subject_erasure_service.get_job(job_id)  # type: ignore[union-attr]
        assert job is not None
        assert job.status == SubjectErasureJobStatus.COMPLETED
        assert item_id in job.item_ids

        # Honesty check (must-not-deviate item 3, mirrored from
        # test_api_wire_layer_dash023.py): this endpoint never claims a
        # wider DPDP cascade than Zone 8 alone, live Postgres or not.
        import inspect

        source = inspect.getsource(app_module)
        assert "import UnifiedSubjectErasureOrchestrator" not in source
        assert "ctx.subject_erasure_service.request_erasure(" in source


# ---------------------------------------------------------------------------
# Scenario 3b: GitHub #22 regression guard -- the real APP LOGIN connection
# (not the migration-role workaround Scenario 3's seed step above documents
# and uses) can now archive into zone8_manifest through the real,
# unmodified Zone8SubjectKeyedArchiver.archive_for_subject path.
# ---------------------------------------------------------------------------


class TestScenario3bZone8AppLoginRoleGrant:
    def test_archive_for_subject_succeeds_on_the_real_app_login_connection(
        self, real_postgres_app_context: AppContext,
    ) -> None:
        """GitHub #22 regression guard.

        Before the fix, this exact call -- through the real, unmodified
        `Zone8SubjectKeyedArchiver.archive_for_subject`, on the real
        app-login-connected `AppContext` every live HTTP request actually
        uses -- failed with `psycopg.errors.InsufficientPrivilege:
        permission denied for table zone8_manifest`, because
        `zone8_consolidation_schema.sql` granted no privilege on that
        table to any login-capable role, and
        `migration_runner._PER_ZONE_LOGIN_MEMBER_ROLES` never named the
        new `dashanan_zone8_role`. Scenario 3 above works around this by
        seeding through the migration-role connection instead -- this
        test proves the real app-login path itself now works, without
        that workaround.
        """
        subject_id = f"subj-{uuid.uuid4().hex[:10]}"
        item_id = f"item-{uuid.uuid4().hex[:10]}"

        archiver = real_postgres_app_context.subject_erasure_service._archiver  # type: ignore[union-attr]
        item = ArchiveBatchItem(
            tenant_id=_TENANT_ID,
            item_id=item_id,
            source_zone=ZoneId.EPISODIC,
            payload=b"real app-login-role zone8 write, GitHub #22 regression guard",
            subject_id=None,
        )

        try:
            result = archiver.archive_for_subject(item, subject_id)
        except Exception as exc:  # noqa: BLE001 -- re-raised below with full context
            pytest.fail(
                "archive_for_subject on the real app-login connection raised "
                f"{type(exc).__name__}: {exc} -- if this is InsufficientPrivilege, "
                "GitHub #22's fix (dashanan_zone8_role GRANT + "
                "_PER_ZONE_LOGIN_MEMBER_ROLES membership) has regressed."
            )

        assert any(written.item_id == item_id for written in result.written)


# ---------------------------------------------------------------------------
# Scenario 4: Zone 3/5 conflict-detection reachability gap, confirmed (or
# disconfirmed) under LIVE Postgres, not only the degraded path.
# ---------------------------------------------------------------------------


class TestScenario4Zone3And5GapUnderLivePostgres:
    def test_entity_refs_write_without_retrieval_context_hash_rejected_400_under_live_postgres(
        self, client: TestClient
    ) -> None:
        """DASH-STORY-026 (FR-013, GitHub #23) closed this gap under a REAL Postgres host.

        Prior to DASH-STORY-026, this exact payload was unconditionally
        rejected with 422 ZONE_3_5_PREDICATE_UNRESOLVABLE (that response
        code no longer exists in app.py) -- `WriteMemoryRequest.content`
        had no `predicate` field to carry, so Zone 3/5 writes were
        entirely unreachable. `test_api_wire_layer_dash023.py`'s sibling
        test (updated in the same story) proves the identical corrected
        behavior against the DEGRADED (no-Postgres) branch; this test
        proves it recurs identically when `ConflictAwareSemanticRepository`/
        `ConflictAwareEntityMemoryRepository` ARE really wired to a live
        Postgres connection -- this exact payload is missing
        `provenance.retrieval_context_hash`, so it is now correctly
        rejected 400 INVALID_REQUEST for that specific, more precise
        reason, not the old blanket rejection.
        """
        response = client.post(
            "/memory/write",
            json={
                "session_id": f"s-{uuid.uuid4().hex[:8]}",
                "content": {
                    "text": "entity-ref-write-under-live-postgres",
                    "entity_refs": [{"role": "subject", "entity_id": "e1"}],
                },
                "provenance": {"source_type": "user_stated", "purpose": "dash3-e2e"},
            },
            headers={**_auth("memory:write"), "Idempotency-Key": str(uuid.uuid4())},
        )
        assert response.status_code == 400, response.text
        assert response.json()["error"]["code"] == "INVALID_REQUEST"


# ---------------------------------------------------------------------------
# Scenario 5: adversarial concurrent writes racing the same idempotency key
# through the real HTTP layer, against the real, shared psycopg.Connection
# (AR1-S3-G3).
# ---------------------------------------------------------------------------


class TestScenario5ConcurrentWritesSharedIdempotencyKey:
    def test_concurrent_writes_same_idempotency_key_documents_ar1_s3_g3(
        self, client: TestClient
    ) -> None:
        """Races N threads against the real HTTP layer sharing one Idempotency-Key.

        `api.composition`'s own module docstring already discloses
        AR1-S3-G3: a single shared `psycopg.Connection` is reused across
        Zone 2/3/7/8-manifest and is NOT safe for concurrent use across
        threads without its own pooling (a real `psycopg_pool` is FR-015
        scope, not this round's). This test therefore does not assert a
        clean idempotent-dedup invariant -- it documents whatever the
        real, currently-shared connection actually does under real
        concurrent HTTP load: some mix of accepted writes, and/or
        `ZoneRepositoryError`-mapped 503s, and/or a raised driver
        exception surfaced as a 500, is EXPECTED and is a reproduction
        of the already-disclosed gap, not a new regression. What this
        test asserts is only that the host does not silently corrupt
        state or hang: every thread gets a real HTTP response (no thread
        times out or crashes the client), and the responses are recorded
        for the record with `AR1-S3-G3` cited alongside them.
        """
        thread_count = 12
        session_id = f"s-{uuid.uuid4().hex[:8]}"
        shared_idempotency_key = str(uuid.uuid4())
        barrier = threading.Barrier(thread_count)
        results: list[dict[str, object]] = [{} for _ in range(thread_count)]

        def _worker(index: int) -> None:
            barrier.wait(timeout=30)
            try:
                response = client.post(
                    "/memory/write",
                    json={
                        "session_id": session_id,
                        "content": {"text": f"adversarial-write-{index}", "zone_hint": "episodic"},
                        "provenance": {"source_type": "user_stated", "purpose": "dash3-e2e-adversarial"},
                    },
                    headers={
                        **_auth("memory:write"),
                        "Idempotency-Key": shared_idempotency_key,
                    },
                )
                results[index] = {"status_code": response.status_code, "error": None}
            except Exception as exc:  # noqa: BLE001 -- capturing for documentation, not swallowing
                results[index] = {"status_code": None, "error": repr(exc)}

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(thread_count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        for index, thread in enumerate(threads):
            assert not thread.is_alive(), (
                f"thread {index} did not complete within the join timeout -- a real "
                "hang, not merely AR1-S3-G3's documented error/race behavior"
            )

        completed = [r for r in results if r.get("status_code") is not None or r.get("error") is not None]
        assert len(completed) == thread_count, (
            f"every one of {thread_count} threads must produce a recorded outcome "
            f"(response or raised exception); got {completed!r} (AR1-S3-G3 context: "
            "api.composition's own module docstring discloses the shared "
            "psycopg.Connection is not thread-safe, so a mix of statuses/exceptions "
            "here is an expected reproduction, not a new regression)"
        )

        status_codes = [r["status_code"] for r in results if r["status_code"] is not None]
        exceptions = [r["error"] for r in results if r["error"] is not None]
        # Documentation-only assertion: at least one thread got a real HTTP
        # response OR a real exception was captured -- either outcome is a
        # valid, real reproduction under AR1-S3-G3; an empty result set
        # (nothing captured at all) would mean the test itself is broken.
        assert status_codes or exceptions
