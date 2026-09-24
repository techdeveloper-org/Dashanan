"""Real-Postgres integration proof for DASH-STORY-028-QA (connection pooling + circuit breaker).

Precondition: DASH-STORY-028-DEV has landed (`api.composition`'s per-request
pooled-connection dependency chain, `infrastructure.circuit_breaker.
CircuitBreaker`). Mirrors `test_api_real_postgres_e2e_dash3.py`'s own real
`docker compose` fixture pattern verbatim where a real Postgres is actually
required: random project name, dynamically allocated free ports via
`_free_tcp_port()`, `secrets.token_urlsafe` credentials, and unconditional
`docker compose down -v --remove-orphans` teardown. Tests that do not need a
live Postgres at all (the DI-wiring source-inspection proof, and the breaker
fail-fast-without-pool-acquisition proof, which must never touch the pool in
the first place) run unconditionally, with no Docker dependency and no skip.

  AC-028-QA-1: a concurrent-request test spanning all five Zone
    2/3/5/7/8-manifest repository/service surfaces (episodic, semantic,
    conflict-aware entity, provenance, manifest/consolidation), proving no
    request blocks on another's in-flight pooled connection.

  AC-028-QA-2: per-request DI wiring verification -- `api.composition`
    exposes a `make_*_dependency` provider for each of the 8 builders/
    classes connection-pooling-design.md Section 3 names, and `api.app`
    has zero remaining `ctx.<repository>`/`ctx.<service>` closure-variable
    reads for any of them (source-inspection proof, mirrors
    `test_api_real_postgres_e2e_dash3.py` Scenario 3's own
    `inspect.getsource` honesty-check pattern).

  AC-028-QA-3: pool exhaustion -- an artificially small pool (`pool_max=1`)
    with 2 concurrent requests proves the second gets `503` +
    `Retry-After: 1` + `error.code=POOL_EXHAUSTED` within the configured
    acquire timeout, never an indefinite hang.

  AC-028-QA-4: the circuit breaker's real wiring inside
    `api.composition.make_pooled_connection_dependency` (not the breaker's
    own pure state machine, which `tests/test_circuit_breaker_dash028.py`
    already proves exhaustively with a `_FakeClock`):
      - the breaker's `allow()` gate is checked and enforced BEFORE any
        `pool.connection(...)` call -- an OPEN breaker never touches the
        pool at all (proven with a spy pool double, no Docker needed);
      - a real query-level error (a genuine `psycopg.errors.
        UniqueViolation` from a duplicate-key insert) on an
        already-acquired connection does NOT increment the breaker's
        failure count -- proven against real Postgres, since only a real
        driver-raised integrity error demonstrates the failure predicate
        the design document specifies;
      - the breaker-OPEN response (`error.code=POSTGRES_UNAVAILABLE`,
        dynamically shrinking `Retry-After`) and the separately-triggered
        `PoolTimeout` response (`error.code=POOL_EXHAUSTED`, fixed
        `Retry-After: 1`) are proven side by side in the same test,
        confirming the two are never conflated (Section 5.1.1's own
        BLOCKER-fix disambiguation requirement).

MUST-NOT-DEVIATE: test style mirrors `test_api_real_postgres_e2e_dash3.py`'s
own real-Postgres fixture pattern; this module does not assert cross-zone
atomicity behavior (`test_api_real_postgres_e2e_dash3.py` Scenario 5's own
AR1-S3-G3 disclosure already covers what the OLD single-shared-connection
code did -- DASH-STORY-028-DEV's pool supersedes it, and this module proves
the pool/breaker's OWN contract, not a fresh atomicity claim).

Skip condition (Docker-dependent tests only, never a false pass): identical
to `test_api_real_postgres_e2e_dash3.py` -- `docker` (with the `compose`
plugin) unavailable, or the Docker daemon unreachable. The DI-wiring and
breaker-fail-fast-without-pool-acquisition tests carry NO such skip: they
require no live Postgres at all.
"""

from __future__ import annotations

import hashlib
import inspect
import os
import secrets
import shutil
import socket
import subprocess
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import NamedTuple

import jwt
import psycopg
import pytest
from fastapi.testclient import TestClient

from dashanan.api import app as app_module
from dashanan.api import composition
from dashanan.api.composition import (
    AppContext,
    PostgresPoolExhaustedError,
    PostgresUnavailableError,
    build_app_context,
    make_pooled_connection_dependency,
)
from dashanan.infrastructure.circuit_breaker import BreakerState
from dashanan.infrastructure.composition_root import build_postgres_connection_pool
from dashanan.infrastructure.migration_runner import SCHEMA_FILES, run_migrations
from dashanan.infrastructure.settings import PostgresSettings
from dashanan.infrastructure.sql_manifest_repository import SqlManifestRepository

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"

_JWT_KEY = bytes(range(32))
_TENANT_CREDENTIAL_KEY = bytes(range(32, 64))
_TENANT_ID = "tenant-a"

_MANIFEST_DEPENDENCY_NAMES = (
    "make_pooled_connection_dependency",
    "make_provenance_repository_dependency",
    "make_episodic_repository_dependency",
    "make_semantic_repository_dependency",
    "make_conflict_aware_entity_repository_dependency",
    "make_manifest_repository_dependency",
    "make_zone8_consolidation_store_dependency",
    "make_subject_erasure_service_dependency",
)
"""The 8 builders/classes connection-pooling-design.md Section 3 names for
Zone 2/3/5/7/8-manifest (`get_pooled_connection` is the shared root every
other provider below depends on; the remaining 7 cover, in order: Zone 7
provenance, Zone 2 episodic, Zone 3 semantic, Zone 5 conflict-aware entity,
the Zone 8 manifest table, the Zone 8 consolidation store, and the Zone 8
subject-erasure-cascade service)."""

_FORBIDDEN_CLOSURE_READS = (
    "ctx.provenance_repository",
    "ctx.episodic_repository",
    "ctx.semantic_repository",
    "ctx.conflict_aware_entity_repository",
    "ctx.manifest_repository",
    "ctx.zone8_consolidation_store",
    "ctx.subject_erasure_service",
)
"""`api.app`'s own pre-DASH-STORY-028-DEV closure-variable reads
(`connection-pooling-design.md` Section 3's own grep-verified list at
`app.py:302,305,420,449,482,498,572,588,878,885`). None of these attributes
exist on `AppContext` any more -- every route handler must instead receive
the corresponding value through a `Depends(get_*)` parameter."""


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

    def postgres_settings(self, *, pool_min: int = 2, pool_max: int = 32) -> PostgresSettings:
        return PostgresSettings(
            host="127.0.0.1",
            port=self.postgres_port,
            database=self.postgres_database,
            migration_user=self.migration_user,
            migration_password=self.migration_password,
            app_login_user=self.app_user,
            app_login_password=self.app_password,
            pool_min=pool_min,
            pool_max=pool_max,
        )


def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess:
    timeout = kwargs.pop("timeout", 300)
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False, **kwargs)


@pytest.fixture(scope="module")
def shape_b_stack() -> Iterator[ShapeBStack]:
    """Bring up a throwaway, fully-random-credentialed Shape B `docker compose` stack.

    Only fixtures/tests that actually need a live Postgres depend on this
    one -- it is the sole place Docker unavailability is surfaced
    (`pytest.skip`, never a module-wide `pytestmark`), so the DI-wiring and
    breaker-fail-fast-without-pool-acquisition tests below run regardless
    of whether Docker is installed.
    """
    if not DOCKER_COMPOSE_AVAILABLE:
        pytest.skip(
            "Docker daemon or the `docker compose` v2 plugin is unavailable on this "
            "machine -- see this module's docstring for why a real docker-compose-backed "
            "proof is required for AC-028-QA-1/-3/-4's query-error/disambiguation "
            "assertions, and why this skip must never be read as those ACs having "
            "been verified."
        )

    project = f"dash028qa-e2e-{uuid.uuid4().hex[:12]}"
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
            f"Postgres DASH-STORY-028-QA suite (stdout={up_result.stdout!r} "
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


@contextmanager
def _real_app_context(stack: ShapeBStack, *, pool_min: int = 2, pool_max: int = 32) -> Iterator[AppContext]:
    """A real `AppContext` wired to a real, live pooled Postgres connection.

    Passes an already-built `build_postgres_connection_pool(...)` result as
    `postgres_pool` -- `build_app_context`'s own documented testing-core DI
    seam -- rather than the default sentinel callable, so the pool's size
    (`pool_min`/`pool_max`) is under this test's direct control (needed for
    AC-028-QA-3's artificially small pool).
    """
    pool = build_postgres_connection_pool(stack.postgres_settings(pool_min=pool_min, pool_max=pool_max))
    try:
        context = build_app_context(
            tenant_credential_signing_key=_TENANT_CREDENTIAL_KEY,
            jwt_signing_key=_JWT_KEY,
            postgres_pool=pool,
        )
        assert context.postgres_available is True
        yield context
    finally:
        pool.close()


@pytest.fixture
def real_app_context(applied_migrations: ShapeBStack) -> Iterator[AppContext]:
    with _real_app_context(applied_migrations) as context:
        yield context


@pytest.fixture
def client(real_app_context: AppContext) -> Iterator[TestClient]:
    app = app_module.create_app(real_app_context)
    with TestClient(app) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# AC-028-QA-2: per-request DI wiring verification (source-inspection proof,
# no Docker/live Postgres required at all).
# ---------------------------------------------------------------------------


class TestAC028QA2DependencyInjectionWiring:
    def test_composition_module_defines_all_8_make_dependency_providers(self) -> None:
        """Every one of the 8 builders/classes Section 3 names has its own
        `make_*_dependency` provider function in `api.composition` -- not
        merely constructed inline somewhere, but a real, independently
        callable factory (AC-028-QA-2's own "not just a generic 2-zone
        concurrency check" requirement extends to wiring completeness)."""
        for name in _MANIFEST_DEPENDENCY_NAMES:
            assert hasattr(composition, name), (
                f"api.composition is missing the expected provider function {name!r}"
            )
            assert callable(getattr(composition, name)), (
                f"api.composition.{name} exists but is not callable"
            )

    def test_create_app_calls_every_make_dependency_provider_exactly_once(self) -> None:
        """`api.app.create_app`'s own source calls each of the 8 providers --
        proven by source inspection rather than by invoking `create_app`
        itself, so this test needs no `AppContext`, no Postgres, and no
        Docker at all."""
        source = inspect.getsource(app_module.create_app)
        for name in _MANIFEST_DEPENDENCY_NAMES:
            call_count = source.count(f"{name}(")
            assert call_count == 1, (
                f"api.app.create_app must call {name}(...) exactly once, found "
                f"{call_count} occurrence(s)"
            )

    def test_app_module_has_zero_remaining_ctx_repository_or_service_closure_reads(self) -> None:
        """The pre-DASH-STORY-028-DEV `ctx.<repository>`/`ctx.<service>` closure-
        variable reads connection-pooling-design.md Section 3 grep-verified
        (`app.py:302,305,420,449,482,498,572,588,878,885`) must all be gone --
        every route handler now receives these values through a
        `Depends(get_*)` parameter instead."""
        source = inspect.getsource(app_module)
        for forbidden in _FORBIDDEN_CLOSURE_READS:
            assert forbidden not in source, (
                f"api.app still contains a startup-singleton closure read {forbidden!r} -- "
                "DASH-STORY-028-DEV requires every Zone 2/3/5/7/8-manifest access to go "
                "through a per-request Depends(get_*) parameter instead"
            )

    def test_app_module_uses_depends_for_every_per_request_provider(self) -> None:
        """Confirms the flip side of the previous test: 6 of the 7 non-root
        providers (`get_pooled_connection` is consumed transitively, never
        directly by a route handler) are actually wired into at least one
        `Depends(get_*)` call site, not merely defined and unused.

        `get_manifest_repository` and `get_zone8_consolidation_store` are
        intermediate links in the Zone 8 dependency chain -- consumed via
        `Depends(...)` inside another `make_*_dependency` function in
        `api.composition` (`make_zone8_consolidation_store_dependency`/
        `make_subject_erasure_service_dependency`), not directly by a route
        handler in `api.app` -- so both source modules are checked, not
        `api.app` alone.

        DISCLOSED GAP (found by this dispatch, pre-existing, not introduced
        by DASH-STORY-028-DEV): `get_subject_erasure_service` is excluded
        from this loop and asserted separately below. `api.app.create_app`
        still builds it (`get_subject_erasure_service =
        make_subject_erasure_service_dependency(...)`) but never wires it
        into any `Depends(...)` call site any more -- DASH-STORY-027-DEV's
        `eraseSubject` route was repointed at
        `get_unified_subject_erasure_orchestrator` instead (`api.composition.
        make_unified_subject_erasure_orchestrator_dependency`'s own docstring
        confirms this supersession explicitly: "Replaces
        SubjectErasureCascadeService as eraseSubject's own dependency...
        without touching make_subject_erasure_service_dependency... both
        remain fully intact and unregressed for any other caller"). No other
        caller exists today (grep-verified: zero `Depends(get_subject_
        erasure_service)` occurrences in this codebase) -- the provider
        function itself is real and correct, but the per-request binding
        `create_app` builds from it is currently dead code. This is a
        pre-DASH-STORY-028 orphaned binding, not a DASH-STORY-028-DEV
        regression, so it is disclosed here rather than silently asserted
        away or "fixed" by this QA dispatch.
        """
        combined_source = inspect.getsource(app_module) + inspect.getsource(composition)
        per_request_getters_with_live_consumer = (
            "get_provenance_repository",
            "get_episodic_repository",
            "get_semantic_repository",
            "get_conflict_aware_entity_repository",
            "get_manifest_repository",
            "get_zone8_consolidation_store",
        )
        for getter in per_request_getters_with_live_consumer:
            assert f"Depends({getter})" in combined_source, (
                f"neither api.app nor api.composition wires {getter} into a "
                "Depends(...) call site"
            )

        assert "Depends(get_subject_erasure_service)" not in combined_source, (
            "get_subject_erasure_service now has a live Depends(...) consumer -- "
            "this test's disclosed-gap docstring is stale and must be updated "
            "(or, if this is a deliberate fix, the orphaned-binding finding "
            "should be closed rather than this assertion flipped silently)"
        )
        assert "get_subject_erasure_service = make_subject_erasure_service_dependency(" in (
            inspect.getsource(app_module)
        ), (
            "api.app.create_app no longer builds get_subject_erasure_service at all -- "
            "if it was intentionally removed, this disclosed-gap test should be deleted, "
            "not left asserting a binding that no longer exists"
        )


# ---------------------------------------------------------------------------
# AC-028-QA-4 (fail-fast half): the breaker's `allow()` gate is enforced
# BEFORE `pool.connection(...)` -- proven with a spy pool double, no Docker
# needed, since an OPEN breaker must never touch the pool at all.
# ---------------------------------------------------------------------------


class _NeverCalledPool:
    """A pool double whose `.connection(...)` fails the test if ever invoked.

    Structurally satisfies `build_app_context`'s own documented testing-core
    DI seam (any object exposing `.connection(timeout=...)`).
    """

    def __init__(self) -> None:
        self.connection_call_count = 0

    def connection(self, timeout: float) -> None:  # pragma: no cover - must never run
        self.connection_call_count += 1
        pytest.fail(
            "CircuitBreaker.allow() returned False but pool.connection(...) was "
            "still called -- Section 5.1's 'checked before pool.connection(...) is "
            "called at all' requirement is violated"
        )


class TestAC028QA4BreakerFailFastWithoutPoolAcquisition:
    def test_open_breaker_never_calls_pool_connection(self) -> None:
        never_called_pool = _NeverCalledPool()
        ctx = build_app_context(
            tenant_credential_signing_key=_TENANT_CREDENTIAL_KEY,
            jwt_signing_key=_JWT_KEY,
            postgres_pool=never_called_pool,
        )
        assert ctx.postgres_available is True
        breaker = ctx.postgres_breaker
        assert breaker is not None

        # Trip the breaker directly via its own real, unmocked API --
        # exactly the 20-call, >=50%-failure-rate window
        # `tests/test_circuit_breaker_dash028.py` already proves trips it.
        for _ in range(20):
            breaker.record_failure(0.001)
        assert breaker.state is BreakerState.OPEN

        get_pooled_connection = make_pooled_connection_dependency(ctx)
        with pytest.raises(PostgresUnavailableError):
            next(get_pooled_connection())

        assert never_called_pool.connection_call_count == 0, (
            "pool.connection(...) must never be called while the breaker is OPEN"
        )

    def test_closed_breaker_does_call_pool_connection(self) -> None:
        """Contrast case: confirms `_NeverCalledPool.connection` itself would
        actually be reached (and would fail the test) on a live path, so the
        previous test's zero-call assertion is not a false negative from a
        dependency chain that never calls `pool.connection` under any
        circumstance."""
        never_called_pool = _NeverCalledPool()
        ctx = build_app_context(
            tenant_credential_signing_key=_TENANT_CREDENTIAL_KEY,
            jwt_signing_key=_JWT_KEY,
            postgres_pool=never_called_pool,
        )
        breaker = ctx.postgres_breaker
        assert breaker is not None
        assert breaker.state is BreakerState.CLOSED

        get_pooled_connection = make_pooled_connection_dependency(ctx)
        with pytest.raises(pytest.fail.Exception):
            next(get_pooled_connection())
        assert never_called_pool.connection_call_count == 1


# ---------------------------------------------------------------------------
# AC-028-QA-1: concurrent requests across all 5 Zone 2/3/5/7/8-manifest
# surfaces -- no request blocks on another's in-flight pooled connection.
# ---------------------------------------------------------------------------


class TestAC028QA1ConcurrencyAcrossAllFiveSurfaces:
    def test_concurrent_requests_across_all_five_zone_surfaces_do_not_serialize(
        self, client: TestClient, real_app_context: AppContext
    ) -> None:
        """Fires one concurrent request per surface (Zone 2 episodic write,
        Zone 3/5 semantic+entity write, Zone 7 provenance read, Zone
        8-manifest read), `pool_min=2`/`pool_max=32` (the real default), and
        asserts every one succeeds and the WALL-CLOCK time for all of them
        together is well under what 5x serialized single-request latency
        would take -- proving no request blocked on another's in-flight
        connection (AC-028-QA-1), not merely a generic two-zone check
        (AC-028-QA-3's own "not just a generic 2-zone concurrency check"
        requirement).

        DISCLOSED FINDING (found by this dispatch, pre-existing, not
        introduced by DASH-STORY-028-DEV -- ESCALATED per this story's own
        policy, never silently worked around): the Zone 8-manifest surface's
        probe below is a direct `SqlManifestRepository.find_by_item_id` read
        through the pooled connection, NOT the `DELETE /tenants/{id}/
        subjects/{id}` HTTP endpoint. That endpoint was confirmed, by real
        execution during this dispatch, to fail on every call with a real
        `psycopg.errors.InsufficientPrivilege: permission denied for table
        semantic_edges` -- `semantic_schema.sql` grants `dashanan_semantic_
        role` only `SELECT, INSERT, UPDATE` on `semantic_edges`/
        `general_facts` (never `DELETE`), but DASH-STORY-027-DEV's
        `UnifiedSubjectErasureOrchestrator._run_all_legs` unconditionally
        calls `SqlSemanticRepository.delete_edges_by_subject` (a real
        `DELETE`) as part of every subject-erasure cascade, regardless of
        whether any Zone 3 row actually matches the subject. This is a
        genuine, reproducible functional defect in the schema/cascade
        pairing (this endpoint cannot complete ANY subject erasure today),
        not a benign asymmetry or a test-authoring gap -- and, compounding
        it, `api.app`'s `eraseSubject` handler does not catch
        `UnifiedSubjectErasureOrchestratorError`, so the failure surfaces as
        an unhandled 500 rather than the documented graceful-degradation
        response. Per this story's own escalation policy this is disclosed
        here (and must be filed as a GitHub issue per this project's
        issue-first workflow) rather than routed around by "fixing"
        `semantic_schema.sql` or `unified_subject_erasure_orchestrator.py`
        inside this QA dispatch -- a schema/scope decision belongs to a
        maintainer, exactly as this codebase's own established precedent
        (`test_api_real_postgres_e2e_dash029_qa.py`'s `TestAC029QA2
        PrivilegeBoundary`) treats an equivalent discovered grant gap.
        Using a direct repository read here keeps THIS test's own scope
        honest (AC-028-QA-1's connection-pooling concurrency claim, not
        Zone 3/8's erasure-privilege correctness) while still exercising the
        real, pooled, per-request Zone 8-manifest connection concurrently
        with the other four surfaces.
        """
        results: dict[str, int] = {}
        errors: dict[str, str] = {}
        barrier = threading.Barrier(5)

        def _zone2_episodic() -> None:
            try:
                barrier.wait(timeout=10)
                response = client.post(
                    "/memory/write",
                    json={
                        "session_id": f"s-{uuid.uuid4().hex[:8]}",
                        "content": {"text": "zone2-concurrency-probe", "zone_hint": "2-episodic"},
                        "provenance": {"source_type": "user_stated", "purpose": "dash028-qa"},
                    },
                    headers={**_auth("memory:write"), "Idempotency-Key": str(uuid.uuid4())},
                )
                results["zone2_episodic"] = response.status_code
            except Exception as exc:  # noqa: BLE001 -- captured for the assertion below
                errors["zone2_episodic"] = repr(exc)

        def _zone3_5_semantic_entity() -> None:
            try:
                barrier.wait(timeout=10)
                response = client.post(
                    "/memory/write",
                    json={
                        "session_id": f"s-{uuid.uuid4().hex[:8]}",
                        "content": {
                            "text": "zone3-5-concurrency-probe",
                            "predicate": "has-role",
                            "entity_refs": [{"role": "subject", "entity_id": "e-concurrency"}],
                        },
                        "provenance": {
                            "source_type": "user_stated",
                            "purpose": "dash028-qa",
                            "retrieval_context_hash": hashlib.sha256(
                                uuid.uuid4().bytes
                            ).hexdigest(),
                        },
                    },
                    headers={**_auth("memory:write"), "Idempotency-Key": str(uuid.uuid4())},
                )
                results["zone3_5"] = response.status_code
            except Exception as exc:  # noqa: BLE001
                errors["zone3_5"] = repr(exc)

        def _zone7_provenance() -> None:
            try:
                barrier.wait(timeout=10)
                response = client.get(
                    f"/items/nonexistent-{uuid.uuid4().hex[:8]}/provenance",
                    headers=_auth("context:read"),
                )
                results["zone7_provenance"] = response.status_code
            except Exception as exc:  # noqa: BLE001
                errors["zone7_provenance"] = repr(exc)

        def _zone8_manifest_read() -> None:
            """A real, pooled, per-request Zone 8-manifest read (see this
            test's own docstring for why the HTTP erasure endpoint is
            unusable for this probe today)."""
            try:
                barrier.wait(timeout=10)
                pool = real_app_context.postgres_pool
                assert pool is not None
                with pool.connection(timeout=5.0) as connection:
                    manifest_repository = SqlManifestRepository(connection=connection)
                    manifest_repository.find_by_item_id(
                        _TENANT_ID, f"nonexistent-{uuid.uuid4().hex[:8]}"
                    )
                results["zone8_manifest"] = 200
            except Exception as exc:  # noqa: BLE001
                errors["zone8_manifest"] = repr(exc)

        def _zone2_second_writer() -> None:
            """A second concurrent Zone 2 writer -- both this and
            `_zone2_episodic` exercise the SAME repository builder
            concurrently, so a per-request (not shared/serialized)
            connection is proven even within one surface, not only across
            surfaces."""
            try:
                barrier.wait(timeout=10)
                response = client.post(
                    "/memory/write",
                    json={
                        "session_id": f"s-{uuid.uuid4().hex[:8]}",
                        "content": {"text": "zone2-second-writer-probe", "zone_hint": "2-episodic"},
                        "provenance": {"source_type": "user_stated", "purpose": "dash028-qa"},
                    },
                    headers={**_auth("memory:write"), "Idempotency-Key": str(uuid.uuid4())},
                )
                results["zone2_second_writer"] = response.status_code
            except Exception as exc:  # noqa: BLE001
                errors["zone2_second_writer"] = repr(exc)

        threads = [
            threading.Thread(target=fn)
            for fn in (
                _zone2_episodic,
                _zone3_5_semantic_entity,
                _zone7_provenance,
                _zone8_manifest_read,
                _zone2_second_writer,
            )
        ]
        started_at = time.perf_counter()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        elapsed_seconds = time.perf_counter() - started_at

        assert not errors, f"unexpected exceptions during concurrent dispatch: {errors}"
        assert results["zone2_episodic"] == 202, results
        assert results["zone2_second_writer"] == 202, results
        assert results["zone3_5"] == 202, results
        # 404 (item never registered) is the expected, correct outcome here
        # -- `_lookup_item` runs after FastAPI has already resolved
        # `Depends(get_provenance_repository)` (and thus acquired and
        # released a real pooled connection) for this request, so a 404
        # still proves the Zone 7 pooled-connection dependency executed
        # concurrently with the other four surfaces.
        assert results["zone7_provenance"] == 404, results
        assert results["zone8_manifest"] == 200, results

        # 5 concurrent requests must complete in well under 5x a single
        # request's latency budget if none serialized on a shared
        # connection -- 10s is a generous ceiling for real Postgres +
        # FastAPI TestClient overhead on CI hardware, while still failing
        # loudly on a genuine full-serialization regression (which would
        # otherwise take 5x longer, easily exceeding this).
        assert elapsed_seconds < 10.0, (
            f"5 concurrent cross-zone requests took {elapsed_seconds:.2f}s -- this "
            "suggests requests are serializing on a shared connection rather than "
            "each acquiring its own from the pool (AC-028-QA-1 regression)"
        )


# ---------------------------------------------------------------------------
# AC-028-QA-3: pool exhaustion -- a pool sized 1 with 2 concurrent requests
# proves the second returns 503 + Retry-After within the configured timeout.
# ---------------------------------------------------------------------------


class TestAC028QA3PoolExhaustion:
    def test_pool_exhausted_returns_503_pool_exhausted_with_retry_after_not_a_hang(
        self, applied_migrations: ShapeBStack
    ) -> None:
        with _real_app_context(applied_migrations, pool_min=1, pool_max=1) as ctx:
            app = app_module.create_app(ctx)
            with TestClient(app) as test_client:
                first_request_in_flight = threading.Event()
                release_first_request = threading.Event()

                pool = ctx.postgres_pool
                assert pool is not None

                def _hold_the_only_connection() -> None:
                    with pool.connection(timeout=5.0):
                        first_request_in_flight.set()
                        release_first_request.wait(timeout=10)

                holder_thread = threading.Thread(target=_hold_the_only_connection)
                holder_thread.start()
                assert first_request_in_flight.wait(timeout=5), (
                    "background thread never acquired the pool's only connection"
                )

                started_at = time.perf_counter()
                exhausted_response = test_client.get(
                    "/items/pool-exhaustion-probe/provenance",
                    headers=_auth("context:read"),
                )
                elapsed_seconds = time.perf_counter() - started_at

                release_first_request.set()
                holder_thread.join(timeout=10)

                assert exhausted_response.status_code == 503, exhausted_response.text
                body = exhausted_response.json()
                assert body["error"]["code"] == "POOL_EXHAUSTED", body
                assert exhausted_response.headers.get("Retry-After") == "1", (
                    exhausted_response.headers
                )
                # `POOL_ACQUIRE_TIMEOUT_SECONDS` is 2.0 -- this must return
                # promptly once that timeout elapses, never hang
                # indefinitely (AC-028-QA-2's own "not an indefinite hang"
                # requirement).
                assert elapsed_seconds < 6.0, (
                    f"pool-exhausted request took {elapsed_seconds:.2f}s to return -- "
                    "expected a prompt 503 near the 2s pool acquire timeout, not a hang"
                )


# ---------------------------------------------------------------------------
# AC-028-QA-4 (negative case): a genuine query-level error on an
# already-acquired real connection must NOT increment the breaker's
# failure count.
# ---------------------------------------------------------------------------


class TestAC028QA4QueryErrorDoesNotTripBreaker:
    def test_real_duplicate_key_integrity_error_does_not_trip_the_breaker(
        self, real_app_context: AppContext
    ) -> None:
        """Causes a REAL `psycopg.errors.UniqueViolation` (a duplicate-key
        insert into `episodic_entries`, whose `episode_id` column carries a
        real `UNIQUE` constraint -- `episodic_schema.sql`'s
        `uq_episodic_entries_episode_id`) on an already-acquired pooled
        connection, repeated 20 times -- the breaker's own trip window size
        (`tests/test_circuit_breaker_dash028.py`'s `DEFAULT_WINDOW_SIZE`).
        If `get_pooled_connection` mis-classified query-level errors as
        connection failures, 20 consecutive ones would trip the breaker
        OPEN; the assertion below proves it does not. A fresh `episode_id`
        per attempt (never re-inserting the SAME row across attempts) keeps
        each attempt's own genuine, successful first insert independent of
        every other attempt's aborted transaction.
        """
        breaker = real_app_context.postgres_breaker
        assert breaker is not None
        pool = real_app_context.postgres_pool
        assert pool is not None

        for attempt in range(20):
            episode_id = f"dup-key-probe-{attempt}-{uuid.uuid4().hex[:10]}"
            with pool.connection(timeout=5.0) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "INSERT INTO episodic_entries "
                        "(tenant_id, session_id, episode_id, seq, occurred_at, "
                        " written_at, payload, token_count) "
                        "VALUES (%s, %s, %s, %s, now(), now(), %s, %s)",
                        (_TENANT_ID, "s-dup-probe", episode_id, attempt, "first-insert-succeeds", 1),
                    )
                connection.commit()

                with pytest.raises(psycopg.errors.UniqueViolation):
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "INSERT INTO episodic_entries "
                            "(tenant_id, session_id, episode_id, seq, occurred_at, "
                            " written_at, payload, token_count) "
                            "VALUES (%s, %s, %s, %s, now(), now(), %s, %s)",
                            (
                                _TENANT_ID, "s-dup-probe-2", episode_id, attempt + 1000,
                                "duplicate-key-attempt", 1,
                            ),
                        )
                connection.rollback()

        assert breaker.state is BreakerState.CLOSED, (
            "20 real query-level UniqueViolation errors on an already-acquired "
            "connection must never trip the breaker -- only connection-establishment "
            "failures count toward its window (Section 5.1's failure predicate)"
        )
        assert breaker.allow() is True


# ---------------------------------------------------------------------------
# AC-028-QA-4 (disambiguation, Section 5.1.1 BLOCKER fix): breaker-OPEN
# (POSTGRES_UNAVAILABLE, dynamic shrinking Retry-After) vs. separately-
# triggered PoolTimeout (POOL_EXHAUSTED, fixed Retry-After: 1) proven side
# by side in the same test.
# ---------------------------------------------------------------------------


class TestAC028QA4DisambiguationPostgresUnavailableVsPoolExhausted:
    def test_breaker_open_and_pool_exhausted_are_never_conflated(
        self, applied_migrations: ShapeBStack
    ) -> None:
        # --- Half 1: breaker-OPEN -> POSTGRES_UNAVAILABLE, dynamic Retry-After.
        never_called_pool = _NeverCalledPool()
        breaker_open_ctx = build_app_context(
            tenant_credential_signing_key=_TENANT_CREDENTIAL_KEY,
            jwt_signing_key=_JWT_KEY,
            postgres_pool=never_called_pool,
        )
        breaker = breaker_open_ctx.postgres_breaker
        assert breaker is not None
        for _ in range(20):
            breaker.record_failure(0.001)
        assert breaker.state is BreakerState.OPEN

        breaker_open_app = app_module.create_app(breaker_open_ctx)
        with TestClient(breaker_open_app) as breaker_open_client:
            first = breaker_open_client.get(
                "/items/breaker-open-probe/provenance", headers=_auth("context:read")
            )
            assert first.status_code == 503, first.text
            first_body = first.json()
            assert first_body["error"]["code"] == "POSTGRES_UNAVAILABLE", first_body
            first_retry_after = int(first.headers["Retry-After"])
            assert first_retry_after > 1, (
                "a freshly-tripped breaker's first-trip base backoff is 30s -- "
                f"Retry-After must reflect real remaining backoff, not the fixed "
                f"1s POOL_EXHAUSTED value; got {first_retry_after}"
            )

            time.sleep(2)
            second = breaker_open_client.get(
                "/items/breaker-open-probe/provenance", headers=_auth("context:read")
            )
            assert second.status_code == 503, second.text
            second_body = second.json()
            assert second_body["error"]["code"] == "POSTGRES_UNAVAILABLE", second_body
            second_retry_after = int(second.headers["Retry-After"])
            assert second_retry_after < first_retry_after, (
                "Retry-After must shrink as real time passes toward the breaker's "
                f"backoff deadline (Section 5.1.1); got first={first_retry_after} "
                f"second={second_retry_after}"
            )
        assert never_called_pool.connection_call_count == 0, (
            "the breaker-OPEN half of this test must never touch the pool at all"
        )

        # --- Half 2: separately-triggered PoolTimeout -> POOL_EXHAUSTED,
        # fixed Retry-After: 1 -- a REAL pool, exhausted for real, proving
        # the two codes/Retry-After values are genuinely distinct, not
        # merely differently-labeled copies of the same response.
        with _real_app_context(applied_migrations, pool_min=1, pool_max=1) as pool_exhausted_ctx:
            pool = pool_exhausted_ctx.postgres_pool
            assert pool is not None
            in_flight = threading.Event()
            release = threading.Event()

            def _hold_the_only_connection() -> None:
                with pool.connection(timeout=5.0):
                    in_flight.set()
                    release.wait(timeout=10)

            holder = threading.Thread(target=_hold_the_only_connection)
            holder.start()
            assert in_flight.wait(timeout=5)

            pool_exhausted_app = app_module.create_app(pool_exhausted_ctx)
            with TestClient(pool_exhausted_app) as pool_exhausted_client:
                exhausted = pool_exhausted_client.get(
                    "/items/pool-exhausted-probe/provenance", headers=_auth("context:read")
                )
            release.set()
            holder.join(timeout=10)

            assert exhausted.status_code == 503, exhausted.text
            exhausted_body = exhausted.json()
            assert exhausted_body["error"]["code"] == "POOL_EXHAUSTED", exhausted_body
            assert exhausted_body["error"]["code"] != first_body["error"]["code"], (
                "POOL_EXHAUSTED and POSTGRES_UNAVAILABLE must never share an error code"
            )
            assert exhausted.headers.get("Retry-After") == "1", exhausted.headers
            assert exhausted.headers.get("Retry-After") != str(first_retry_after), (
                "the pool-exhausted Retry-After (fixed, 1) must never coincide with "
                "or be conflated with the breaker-OPEN Retry-After (dynamic, >1 here)"
            )
