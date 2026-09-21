"""QA subtask for DASH-STORY-024 (FR-015) -- scope guard and golden regression anchors.

FR-015 (verbatim, `docs/phase-6-sprint-planning/backlog_draft.json`
`future_sprints_backlog.remaining_frs` entry, `note` field): "real Shape B
deployment per HLD Section 2's container/service table (Postgres, Redis,
Qdrant, OpenSearch, S3), a real DB driver dependency (pyproject.toml
currently declares zero), docker-compose, env config, and a migration
runner -- none of which exist anywhere in this repo today (confirmed by
direct exploration 2026-09-20: no docker-compose*, no .env*, no
settings/config module, no alembic). This is also where the production
DB-role-wiring gap closes for real: dashanan_app_role/dashanan_schema_owner
(defined in provenance_schema.sql/episodic_schema.sql since DSHN-55) get
bound to an actual login credential via this story's connection-string/
composition-root wiring, not before."

This suite closes the parts of the QA dispatch's own TEST REQUIREMENTS that
`tests/test_migration_runner_dash024.py`, `tests/test_settings_dash024.py`
and `tests/test_regression_dash024_shape_b_deployment_infra.py` (the DEV
subtask's own accompanying tests) do not already cover:

  - "Assert no mutation outside this story's ownership" (sprint3_ar1_
    assignments.json `must_not_deviate`) -- mirrors every other
    `test_qa_*_dash0NN.py` module's own `MustNotDeviate`-class convention
    (see e.g. `test_qa_semantic_memory_dash012.py`).
  - A statically-verifiable stand-in for "a service reachable on the wrong
    interface/port" (AC-024-1's adverse case) that runs without a Docker
    daemon, by asserting every `docker-compose.yml` port binding is
    loopback-only (`127.0.0.1:`), never `0.0.0.0` or a bare port (which
    Docker publishes on every interface).
  - Negative coverage for every REQUIRED credential-bearing env var across
    all five services (the existing `test_settings_dash024.py` covers
    Postgres, OpenSearch, and S3 fully; this file adds the two services'
    fields that are typed as `str | None` -- Redis/Qdrant -- proving that
    design choice is intentional per `settings.py`'s own docstrings, not an
    untested gap).
  - One golden regression-guard test per AC (AC-024-1/2/3), each citing its
    AC ID and FR-015 verbatim, as this repo's own `rules/33-test-case-
    roadmap.md` / QA-prompt convention requires.

Runtime assumptions recorded per rules 33/40 (test-roadmap conventions):
  - No fixture seed is random; nothing in this suite generates a value a
    test needs to match against.
  - No clock or tenant_id is used -- every assertion here is pure static
    analysis of `docker-compose.yml`, `pyproject.toml`, and `settings.py`,
    with no wall-clock or tenant-scoped state.
  - This suite requires no Docker daemon and no real database; it is safe
    to run in any CI environment, unlike
    `test_regression_dash024_shape_b_deployment_infra.py`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from dashanan.infrastructure.settings import (
    QdrantSettings,
    RedisSettings,
    load_qdrant_settings_from_env,
    load_redis_settings_from_env,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
PYPROJECT_FILE = REPO_ROOT / "pyproject.toml"

_HLD_SECTION_2_SERVICE_NAMES = frozenset(
    {"redis", "postgres", "qdrant", "opensearch", "minio"}
)
"""The five backing services AC-024-1 names, by their `docker-compose.yml`
service key: Redis (event bus/hot store), PostgreSQL, Qdrant, OpenSearch,
and MinIO (the S3-compatible object store). `minio` is the correct service
key even though AC-024-1's own text says "S3-compatible object store" --
MinIO is this repo's chosen S3-compatible implementation, per ADR-009."""


def _load_compose() -> dict:
    return yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))


class TestMustNotDeviateStoryScope:
    """sprint3_ar1_assignments.json DASH-STORY-024 `must_not_deviate`:

    - "Does not reopen HLD Section 2's locked technology selections for any
      of the five services"
    - "Does not include the FR-014 host application's own request-handling
      code -- this story's scope stops at bringing the five backing
      services up, wiring env config, and running migrations"
    """

    def test_dockerCompose_should_defineExactlyTheFiveHldSection2Services_nothingMore(
        self,
    ) -> None:
        compose = _load_compose()
        service_names = set(compose.get("services", {}).keys())

        assert service_names == _HLD_SECTION_2_SERVICE_NAMES, (
            "docker-compose.yml must define exactly the five HLD Section 2 "
            f"services and no others (scope creep beyond AC-024-1); got "
            f"{sorted(service_names)}"
        )

    def test_dockerCompose_should_notReopenTheLockedTechnologySelections(self) -> None:
        compose = _load_compose()
        images = {
            name: svc.get("image", "")
            for name, svc in compose.get("services", {}).items()
        }

        assert "postgres" in images["postgres"].lower()
        assert "redis" in images["redis"].lower()
        assert "qdrant" in images["qdrant"].lower()
        assert "opensearch" in images["opensearch"].lower()
        assert "minio" in images["minio"].lower()

    def test_infrastructurePackage_should_notContainFr014HostApplicationRequestHandlingCode(
        self,
    ) -> None:
        """FR-014 (DASH-STORY-023) is the host application's own
        request-handling wiring; this story's scope stops at infrastructure
        (docker-compose, env config, the migration runner). No FastAPI/Flask
        router, `@app.route`/`@router.` decorator, or ASGI/WSGI app object
        should appear in the infrastructure files this story adds.
        """
        infra_dir = REPO_ROOT / "src" / "dashanan" / "infrastructure"
        story_files = ["settings.py", "migration_runner.py"]

        for filename in story_files:
            text = (infra_dir / filename).read_text(encoding="utf-8")
            assert "@app.route" not in text
            assert "@router." not in text
            assert "FastAPI(" not in text
            assert "Flask(" not in text


class TestAC024_1_StaticInterfaceAndPortShape:
    """AC-024-1 adverse case (this story's own QA TEST REQUIREMENTS: "a
    service reachable on the wrong interface/port"), verified statically so
    it runs without a Docker daemon: every published port must be bound
    loopback-only, never to every interface.
    """

    def test_everyService_should_bindPublishedPorts_toLoopbackOnly_neverAllInterfaces(
        self,
    ) -> None:
        compose = _load_compose()

        for name, service in compose["services"].items():
            for port_mapping in service.get("ports", []):
                assert port_mapping.startswith("127.0.0.1:"), (
                    f"Service '{name}' publishes port mapping {port_mapping!r} "
                    "without a loopback-only bind address -- HLD Section 2's "
                    "documented port/protocol shape is a local Shape B "
                    "deployment, not a service reachable on every network "
                    "interface (AC-024-1 adverse case)"
                )

    def test_everyService_should_declareAHealthcheck(self) -> None:
        compose = _load_compose()

        for name, service in compose["services"].items():
            assert "healthcheck" in service, (
                f"Service '{name}' has no healthcheck -- AC-024-1 requires "
                "docker-compose to prove each service 'started successfully', "
                "which `docker compose up --wait` can only verify via a "
                "declared healthcheck (this story's own regression suite, "
                "`test_regression_dash024_shape_b_deployment_infra.py`, "
                "depends on `--wait` blocking until every healthcheck passes)"
            )
            assert service["healthcheck"].get("test"), (
                f"Service '{name}' declares an empty healthcheck 'test'"
            )

    def test_everyService_should_declareARestartPolicy(self) -> None:
        """A service failing its healthcheck and never restarting would
        silently leave AC-024-1's "started successfully" guarantee unmet on
        any transient failure -- this is the closest statically-verifiable
        proxy for the adverse "a docker-compose service failing its
        healthcheck" scenario the QA prompt names, since actually forcing a
        healthcheck failure requires a live daemon (covered instead, where
        possible, by the real stack in the regression suite).
        """
        compose = _load_compose()

        for name, service in compose["services"].items():
            assert service.get("restart"), (
                f"Service '{name}' has no restart policy declared"
            )

    def test_dockerCompose_should_declareEveryServicesPortEnvVar_withADocumentedDefault(
        self,
    ) -> None:
        """Every service's published host port is driven by an
        `${DASHANAN_*_PORT:-default}` interpolation, never a bare literal --
        the same env-config-sourcing contract AC-024-2 requires for
        credentials extends here to port numbers, so a deployer can safely
        run more than one Shape B stack on the same host (as
        `test_regression_dash024_shape_b_deployment_infra.py`'s own
        randomized-port fixture already depends on for CI parallelism).
        """
        compose = _load_compose()
        port_var_pattern = re.compile(r"\$\{DASHANAN_[A-Z0-9_]+_PORT:-\d+\}")

        for name, service in compose["services"].items():
            for port_mapping in service.get("ports", []):
                assert port_var_pattern.search(port_mapping), (
                    f"Service '{name}' port mapping {port_mapping!r} is not "
                    "driven by a `${DASHANAN_*_PORT:-default}` env override"
                )


class TestAC024_2_RedisAndQdrantOptionalCredentialFieldsAreIntentional:
    """AC-024-2 negative coverage extended to Redis/Qdrant: `settings.py`
    types their credential fields as `str | None` rather than raising on a
    missing value (unlike Postgres/OpenSearch/S3's `_require_str` fields).
    This suite proves that is a documented, intentional design choice
    (RESP/Qdrant both support unauthenticated connections at the protocol
    level, with the fail-closed behavior enforced instead by
    `docker-compose.yml`'s own `--requirepass`/API-key configuration -- see
    `settings.py`'s own docstrings), not an untested gap in the missing-env-
    var coverage `test_settings_dash024.py` already gives Postgres/
    OpenSearch/S3.
    """

    def test_redisSettings_should_returnNonePassword_never_raise_when_unset(
        self,
    ) -> None:
        settings = load_redis_settings_from_env({})

        assert settings == RedisSettings(host="localhost", port=6379, password=None)

    def test_qdrantSettings_should_returnNoneApiKey_never_raise_when_unset(
        self,
    ) -> None:
        settings = load_qdrant_settings_from_env({})

        assert settings == QdrantSettings(host="localhost", port=6333, api_key=None)

    def test_dockerCompose_redisService_should_stillEnforceAPassword_atTheContainerLevel(
        self,
    ) -> None:
        """The fail-closed guarantee for Redis is enforced by
        `docker-compose.yml`'s own `--requirepass ${DASHANAN_REDIS_PASSWORD}`
        command flag, not by `settings.py` raising -- this test proves that
        flag is actually present, closing the loop this class's docstring
        describes.
        """
        compose = _load_compose()
        redis_command = compose["services"]["redis"].get("command", "")
        redis_command_text = (
            " ".join(redis_command) if isinstance(redis_command, list) else redis_command
        )

        assert "--requirepass" in redis_command_text
        assert "${DASHANAN_REDIS_PASSWORD" in redis_command_text

    def test_dockerCompose_qdrantService_should_stillEnforceAnApiKey_atTheContainerLevel(
        self,
    ) -> None:
        compose = _load_compose()
        qdrant_env = compose["services"]["qdrant"].get("environment", {})
        qdrant_env_text = str(qdrant_env)

        assert "QDRANT__SERVICE__API_KEY" in qdrant_env_text
        assert "${DASHANAN_QDRANT_API_KEY" in qdrant_env_text


class TestGoldenRegressionGuardPerAcceptanceCriterion:
    """One golden regression-guard test per AC, each citing its exact AC ID
    and FR-015 verbatim, as a permanent anchor for any later story that
    touches Shape B deployment infrastructure -- per this story's own QA
    TEST REQUIREMENTS ("Maintain one golden test case per AC as a
    regression guard").
    """

    def test_golden_AC024_1_dockerCompose_definesAllFiveDocumentedServices(
        self,
    ) -> None:
        """AC-024-1: "...all five services start successfully and are
        reachable on their documented ports/protocols (RESP for Redis,
        PostgreSQL wire, HTTP for Qdrant/OpenSearch, S3 API for the object
        store)." This golden guard proves the five-service shape statically;
        the "reachable" half is proved dynamically by
        `test_regression_dash024_shape_b_deployment_infra.py::
        TestAC024_1_AllFiveServicesReachable`, whose docstring cites this
        exact AC-024-1 text.
        """
        compose = _load_compose()

        assert set(compose["services"].keys()) == _HLD_SECTION_2_SERVICE_NAMES

    def test_golden_AC024_2_migrationRunner_bindsAppLoginRole_toEnvSourcedCredential(
        self,
    ) -> None:
        """AC-024-2: "...all three roles are bound to real, non-default
        login credentials sourced from env config (never hardcoded in a
        committed file)...". This golden guard proves the source-level
        contract statically (the password literal never appears in
        `migration_runner.py`, only `settings.PostgresSettings.
        app_login_password`, itself env-sourced per `test_settings_dash024.
        py`); the "bound...to a real...credential" half is proved
        dynamically by `test_regression_dash024_shape_b_deployment_infra.
        py::TestAC024_2_RolesBoundToRealEnvSourcedCredentials`.
        """
        migration_runner_text = (
            REPO_ROOT / "src" / "dashanan" / "infrastructure" / "migration_runner.py"
        ).read_text(encoding="utf-8")

        assert "settings.app_login_password" in migration_runner_text
        assert not re.search(
            r'app_login_password\s*=\s*["\']', migration_runner_text
        ), "app_login_password must never be a hardcoded literal in migration_runner.py"

    def test_golden_AC024_3_pyprojectToml_declaresAPinnedRealPostgresDriver(
        self,
    ) -> None:
        """AC-024-3: "...a real, pinned PostgreSQL driver dependency (e.g.
        psycopg or asyncpg) is added and used by the migration runner and
        the composition root's real (non-embedded) adapters." This golden
        guard proves the pinned-dependency half statically; the "used by"
        half is proved dynamically by
        `test_regression_dash024_shape_b_deployment_infra.py::
        TestAC024_3_RealPinnedDriverUsedByMigrationRunnerAndCompositionRoot`
        and unit-tested (no live database) by
        `test_migration_runner_dash024.py::TestBindAppLoginRole::
        test_bind_should_useSqlLiteral_forThePassword_neverRawStringInterpolation`.
        """
        pyproject_text = PYPROJECT_FILE.read_text(encoding="utf-8")

        pin_pattern = re.compile(r'"psycopg\[binary\]>=3\.\d+,<4\.0"')
        assert pin_pattern.search(pyproject_text), (
            "pyproject.toml must declare a pinned psycopg[binary] range "
            "dependency (AC-024-3)"
        )
