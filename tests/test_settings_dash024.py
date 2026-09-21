"""Formal pytest suite for `infrastructure.settings` (DASH-STORY-024, FR-015).

Covers the env-config loaders' fail-closed behavior: every credential-bearing
field must be sourced from an environment variable with no hardcoded fallback,
mirroring `test_composition_root.py`'s coverage of
`load_tenant_credential_signing_key_from_env`. No test here touches Docker or
a real network connection -- see
`tests/test_regression_dash024_shape_b_deployment_infra.py` for the real,
docker-compose-backed integration coverage of AC-024-1/2/3.

Runtime assumptions recorded per rule 33/40 test-roadmap conventions:
  - No fixture seed is random; nothing in this suite generates a UUID a test
    needs to match against.
"""

from __future__ import annotations

import pytest

from dashanan.infrastructure.settings import (
    OpenSearchSettings,
    PostgresSettings,
    QdrantSettings,
    RedisSettings,
    S3Settings,
    SettingsConfigurationError,
    load_opensearch_settings_from_env,
    load_postgres_settings_from_env,
    load_qdrant_settings_from_env,
    load_redis_settings_from_env,
    load_s3_settings_from_env,
)

_VALID_POSTGRES_ENV = {
    "DASHANAN_POSTGRES_HOST": "pg.internal",
    "DASHANAN_POSTGRES_PORT": "5433",
    "DASHANAN_POSTGRES_DATABASE": "dashanan_test",
    "DASHANAN_POSTGRES_MIGRATION_USER": "migration_role",
    "DASHANAN_POSTGRES_MIGRATION_PASSWORD": "migration-secret-1",
    "DASHANAN_POSTGRES_APP_USER": "app_role",
    "DASHANAN_POSTGRES_APP_PASSWORD": "app-secret-1",
}


class TestPostgresSettings:
    """`load_postgres_settings_from_env` / `PostgresSettings`."""

    def test_load_should_returnPopulatedSettings_when_everyVariableIsSet(self) -> None:
        settings = load_postgres_settings_from_env(_VALID_POSTGRES_ENV)

        assert settings == PostgresSettings(
            host="pg.internal",
            port=5433,
            database="dashanan_test",
            migration_user="migration_role",
            migration_password="migration-secret-1",
            app_login_user="app_role",
            app_login_password="app-secret-1",
        )

    def test_load_should_applyLocalDevDefaults_when_hostPortDatabaseAreUnset(
        self,
    ) -> None:
        env = {
            k: v
            for k, v in _VALID_POSTGRES_ENV.items()
            if k not in ("DASHANAN_POSTGRES_HOST", "DASHANAN_POSTGRES_PORT",
                         "DASHANAN_POSTGRES_DATABASE")
        }

        settings = load_postgres_settings_from_env(env)

        assert settings.host == "localhost"
        assert settings.port == 5432
        assert settings.database == "dashanan"

    @pytest.mark.parametrize(
        "missing_var",
        [
            "DASHANAN_POSTGRES_MIGRATION_USER",
            "DASHANAN_POSTGRES_MIGRATION_PASSWORD",
            "DASHANAN_POSTGRES_APP_USER",
            "DASHANAN_POSTGRES_APP_PASSWORD",
        ],
    )
    def test_load_should_raise_when_credentialVariableIsMissing(
        self, missing_var: str
    ) -> None:
        env = {k: v for k, v in _VALID_POSTGRES_ENV.items() if k != missing_var}

        with pytest.raises(SettingsConfigurationError, match=missing_var):
            load_postgres_settings_from_env(env)

    @pytest.mark.parametrize(
        "missing_var",
        [
            "DASHANAN_POSTGRES_MIGRATION_USER",
            "DASHANAN_POSTGRES_APP_PASSWORD",
        ],
    )
    def test_load_should_raise_when_credentialVariableIsBlank(
        self, missing_var: str
    ) -> None:
        env = dict(_VALID_POSTGRES_ENV)
        env[missing_var] = "   "

        with pytest.raises(SettingsConfigurationError, match=missing_var):
            load_postgres_settings_from_env(env)

    def test_load_should_raise_when_portIsNotAnInteger(self) -> None:
        env = dict(_VALID_POSTGRES_ENV)
        env["DASHANAN_POSTGRES_PORT"] = "not-a-port"

        with pytest.raises(SettingsConfigurationError, match="DASHANAN_POSTGRES_PORT"):
            load_postgres_settings_from_env(env)

    def test_migrationDsn_should_containMigrationCredential_never_appCredential(
        self,
    ) -> None:
        settings = load_postgres_settings_from_env(_VALID_POSTGRES_ENV)

        dsn = settings.migration_dsn()

        assert "migration_role" in dsn
        assert "migration-secret-1" in dsn
        assert "app_role" not in dsn
        assert "app-secret-1" not in dsn

    def test_appDsn_should_containAppCredential_never_migrationCredential(self) -> None:
        settings = load_postgres_settings_from_env(_VALID_POSTGRES_ENV)

        dsn = settings.app_dsn()

        assert "app_role" in dsn
        assert "app-secret-1" in dsn
        assert "migration_role" not in dsn
        assert "migration-secret-1" not in dsn

    def test_load_should_useRealOsEnviron_when_noEnvMappingSupplied(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for key, value in _VALID_POSTGRES_ENV.items():
            monkeypatch.setenv(key, value)

        settings = load_postgres_settings_from_env()

        assert settings.app_login_user == "app_role"

    @pytest.mark.parametrize(
        "credential_var",
        [
            "DASHANAN_POSTGRES_MIGRATION_USER",
            "DASHANAN_POSTGRES_MIGRATION_PASSWORD",
            "DASHANAN_POSTGRES_APP_USER",
            "DASHANAN_POSTGRES_APP_PASSWORD",
        ],
    )
    def test_load_should_raise_when_credentialIsAnUnreplacedEnvExamplePlaceholder(
        self, credential_var: str
    ) -> None:
        """A literal `.env.example` placeholder value (e.g. the exact string
        that file ships for `DASHANAN_POSTGRES_MIGRATION_PASSWORD`) must never
        be accepted as a real credential -- a developer who copies
        `.env.example` to `.env` without replacing every placeholder must
        fail closed at startup, not silently run against a well-known,
        publicly documented "secret".
        """
        env = dict(_VALID_POSTGRES_ENV)
        env[credential_var] = "REPLACE_WITH_A_REAL_RANDOM_PASSWORD"

        with pytest.raises(SettingsConfigurationError, match=credential_var):
            load_postgres_settings_from_env(env)

    def test_migrationDsn_should_treatSpaceAndKeyValueFragmentInPassword_asLiteralContent(
        self,
    ) -> None:
        """`migration_dsn`/`app_dsn` build the conninfo string via
        `psycopg.conninfo.make_conninfo` (not manual f-string interpolation)
        specifically so a password containing a space followed by a
        `key=value`-shaped fragment cannot be parsed as an extra, attacker-
        controlled libpq keyword -- this proves that safety property end to
        end by round-tripping the built DSN back through
        `psycopg.conninfo.conninfo_to_dict` and asserting the injected
        fragment landed entirely inside the `password` field, not as a
        separate `sslmode` keyword.
        """
        from psycopg.conninfo import conninfo_to_dict

        env = dict(_VALID_POSTGRES_ENV)
        env["DASHANAN_POSTGRES_MIGRATION_PASSWORD"] = "real-secret sslmode=disable"
        settings = load_postgres_settings_from_env(env)

        dsn = settings.migration_dsn()
        parsed = conninfo_to_dict(dsn)

        assert parsed["password"] == "real-secret sslmode=disable"
        assert parsed.get("sslmode") != "disable"


class TestRedisSettings:
    """`load_redis_settings_from_env` / `RedisSettings`."""

    def test_load_should_returnPopulatedSettings_when_everyVariableIsSet(self) -> None:
        env = {
            "DASHANAN_REDIS_HOST": "redis.internal",
            "DASHANAN_REDIS_PORT": "6380",
            "DASHANAN_REDIS_PASSWORD": "redis-secret-1",
        }

        settings = load_redis_settings_from_env(env)

        assert settings == RedisSettings(
            host="redis.internal", port=6380, password="redis-secret-1"
        )

    def test_load_should_applyDefaultHostAndPort_when_unset(self) -> None:
        settings = load_redis_settings_from_env({})

        assert settings.host == "localhost"
        assert settings.port == 6379

    def test_load_should_returnNonePassword_when_passwordVariableIsUnset(self) -> None:
        settings = load_redis_settings_from_env({})

        assert settings.password is None


class TestQdrantSettings:
    """`load_qdrant_settings_from_env` / `QdrantSettings`."""

    def test_load_should_returnPopulatedSettings_when_everyVariableIsSet(self) -> None:
        env = {
            "DASHANAN_QDRANT_HOST": "qdrant.internal",
            "DASHANAN_QDRANT_PORT": "6335",
            "DASHANAN_QDRANT_API_KEY": "qdrant-secret-1",
        }

        settings = load_qdrant_settings_from_env(env)

        assert settings == QdrantSettings(
            host="qdrant.internal", port=6335, api_key="qdrant-secret-1"
        )

    def test_load_should_returnNoneApiKey_when_apiKeyVariableIsUnset(self) -> None:
        settings = load_qdrant_settings_from_env({})

        assert settings.api_key is None


class TestOpenSearchSettings:
    """`load_opensearch_settings_from_env` / `OpenSearchSettings`."""

    def test_load_should_returnPopulatedSettings_when_everyVariableIsSet(self) -> None:
        env = {
            "DASHANAN_OPENSEARCH_HOST": "os.internal",
            "DASHANAN_OPENSEARCH_PORT": "9201",
            "DASHANAN_OPENSEARCH_USERNAME": "os_admin",
            "DASHANAN_OPENSEARCH_PASSWORD": "os-secret-1",
        }

        settings = load_opensearch_settings_from_env(env)

        assert settings == OpenSearchSettings(
            host="os.internal", port=9201, username="os_admin", password="os-secret-1"
        )

    def test_load_should_raise_when_passwordVariableIsMissing(self) -> None:
        with pytest.raises(
            SettingsConfigurationError, match="DASHANAN_OPENSEARCH_PASSWORD"
        ):
            load_opensearch_settings_from_env({})

    def test_load_should_defaultUsernameToAdmin_when_unset(self) -> None:
        env = {"DASHANAN_OPENSEARCH_PASSWORD": "os-secret-1"}

        settings = load_opensearch_settings_from_env(env)

        assert settings.username == "admin"


class TestS3Settings:
    """`load_s3_settings_from_env` / `S3Settings`."""

    def test_load_should_returnPopulatedSettings_when_everyVariableIsSet(self) -> None:
        env = {
            "DASHANAN_S3_ENDPOINT_URL": "http://minio.internal:9000",
            "DASHANAN_S3_ACCESS_KEY": "s3-access-1",
            "DASHANAN_S3_SECRET_KEY": "s3-secret-1",
            "DASHANAN_S3_BUCKET": "custom-bucket",
        }

        settings = load_s3_settings_from_env(env)

        assert settings == S3Settings(
            endpoint_url="http://minio.internal:9000",
            access_key="s3-access-1",
            secret_key="s3-secret-1",
            bucket="custom-bucket",
        )

    @pytest.mark.parametrize(
        "missing_var", ["DASHANAN_S3_ACCESS_KEY", "DASHANAN_S3_SECRET_KEY"]
    )
    def test_load_should_raise_when_credentialVariableIsMissing(
        self, missing_var: str
    ) -> None:
        env = {
            "DASHANAN_S3_ACCESS_KEY": "s3-access-1",
            "DASHANAN_S3_SECRET_KEY": "s3-secret-1",
        }
        del env[missing_var]

        with pytest.raises(SettingsConfigurationError, match=missing_var):
            load_s3_settings_from_env(env)

    def test_load_should_applyDefaultEndpointAndBucket_when_unset(self) -> None:
        env = {
            "DASHANAN_S3_ACCESS_KEY": "s3-access-1",
            "DASHANAN_S3_SECRET_KEY": "s3-secret-1",
        }

        settings = load_s3_settings_from_env(env)

        assert settings.endpoint_url == "http://localhost:9000"
        assert settings.bucket == "dashanan-zone8"
