"""Formal pytest suite for `infrastructure.composition_root` (DSHN-60 attempt 2).

Proves the two HIGH "still open" findings from the attempt-1 re-audit are now closed
for real: HLD Threat S-1 tenant-credential verification and the FR-013 conflict-detection
sweep are both reachable through a real, importable composition root that wires them ON
by default, not merely present as unwired primitives.

Runtime assumptions recorded per rule 33/40 test-roadmap conventions:
  - clock: FakeClock, fixed at 2026-01-01T00:00:00+00:00 unless a test states otherwise.
  - tenant_id: "tenant-1" for all requests unless a test states otherwise.
  - No fixture seed is random; nothing in this suite generates a UUID a test needs to
    match against.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest

from dashanan.application.context_assembly_request import ContextAssemblyRequest
from dashanan.application.conflict_aware_zone_writes import (
    ConflictAwareEntityMemoryRepository,
    ConflictAwareSemanticRepository,
)
from dashanan.application.conflict_detection_sweep import (
    ConflictDetectingProvenanceRepository,
)
from dashanan.domain.exceptions import ZoneRepositoryError
from dashanan.domain.provenance_record import SourceType
from dashanan.domain.semantic_memory import SemanticEdge
from dashanan.domain.tenant_credential import (
    TenantAuthenticationError,
    sign_tenant_credential,
)
from dashanan.domain.zone import ZoneId
from dashanan.infrastructure.composition_root import (
    TENANT_CREDENTIAL_SIGNING_KEY_ENV_VAR,
    CompositionRootConfigurationError,
    PostgresConnectionError,
    build_conflict_aware_entity_memory_repository,
    build_conflict_aware_semantic_repository,
    build_episodic_repository,
    build_memory_orchestrator,
    build_postgres_connection,
    build_provenance_repository,
    load_tenant_credential_signing_key_from_env,
)
from dashanan.infrastructure.entity_memory_repository import EntityMemoryRepository
from dashanan.infrastructure.settings import PostgresSettings
from dashanan.infrastructure.sql_provenance_repository import SqlProvenanceRepository
from tests.test_smoke_episodic import RecordingConnection

_PLACEHOLDER_CONTEXT_HASH = hashlib.sha256(b"<PII_EXAMPLE_REDACTED>").hexdigest()

_VALID_SIGNING_KEY = b"\x01" * 32
_VALID_SIGNING_KEY_HEX = _VALID_SIGNING_KEY.hex()

# `[(is_superuser, is_createrole, is_table_owner)]` -- a connected role holding none of
# these is exactly what `verify_append_only_role_is_safe` treats as safe (composition
# root's `verify_privileges=True` default must not raise against this fixture row).
_SAFE_PRIVILEGE_ROW = [(False, False, False)]


class FakeClock:
    """Deterministic Clock double: fixed instant, injectable (testing-core DI)."""

    def __init__(self, fixed: datetime) -> None:
        self._fixed = fixed

    def now(self) -> datetime:
        return self._fixed


class RecordingEventBus:
    """EventBus double that records every publish call for assertion."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, object]]] = []

    def publish(self, event_type: str, payload: dict[str, object]) -> None:
        self.published.append((event_type, dict(payload)))


@pytest.fixture
def fixed_clock() -> FakeClock:
    return FakeClock(datetime(2026, 1, 1, tzinfo=UTC))


@pytest.fixture
def event_bus() -> RecordingEventBus:
    return RecordingEventBus()


def _request(**overrides: object) -> ContextAssemblyRequest:
    defaults: dict[str, object] = {
        "tenant_id": "tenant-1",
        "session_id": "session-1",
        "task": "recall the last order",
        "token_budget": 1000,
    }
    defaults.update(overrides)
    return ContextAssemblyRequest(**defaults)  # type: ignore[arg-type]


class TestLoadTenantCredentialSigningKeyFromEnv:
    """`load_tenant_credential_signing_key_from_env` -- fail-closed env-var loading."""

    def test_decodes_a_valid_hex_key(self):
        key = load_tenant_credential_signing_key_from_env(
            {TENANT_CREDENTIAL_SIGNING_KEY_ENV_VAR: _VALID_SIGNING_KEY_HEX}
        )
        assert key == _VALID_SIGNING_KEY

    def test_raises_when_env_var_is_missing(self):
        with pytest.raises(CompositionRootConfigurationError, match="not set"):
            load_tenant_credential_signing_key_from_env({})

    def test_raises_when_env_var_is_blank(self):
        with pytest.raises(CompositionRootConfigurationError, match="not set"):
            load_tenant_credential_signing_key_from_env(
                {TENANT_CREDENTIAL_SIGNING_KEY_ENV_VAR: "   "}
            )

    def test_raises_on_invalid_hex(self):
        with pytest.raises(CompositionRootConfigurationError, match="hex-encoded"):
            load_tenant_credential_signing_key_from_env(
                {TENANT_CREDENTIAL_SIGNING_KEY_ENV_VAR: "not-hex-at-all"}
            )

    def test_raises_when_decoded_key_is_too_short(self):
        short_key_hex = (b"\x02" * 16).hex()
        with pytest.raises(CompositionRootConfigurationError, match="minimum"):
            load_tenant_credential_signing_key_from_env(
                {TENANT_CREDENTIAL_SIGNING_KEY_ENV_VAR: short_key_hex}
            )


class TestBuildMemoryOrchestratorFailsClosedWithNoKeySource:
    """No explicit key AND no env var -- must never silently build an unverified Orchestrator."""

    def test_raises_when_neither_explicit_key_nor_env_var_is_provided(
        self, event_bus: RecordingEventBus, fixed_clock: FakeClock
    ):
        with pytest.raises(CompositionRootConfigurationError):
            build_memory_orchestrator(
                zone_repositories={},
                event_bus=event_bus,
                clock=fixed_clock,
                env={},
            )


class TestBuildMemoryOrchestratorEnablesTenantVerificationByDefault:
    """The core DSHN-60 HIGH fix: verification is ON without the caller opting in."""

    def test_explicit_key_enables_verification_and_rejects_a_missing_credential(
        self, event_bus: RecordingEventBus, fixed_clock: FakeClock
    ):
        orchestrator = build_memory_orchestrator(
            zone_repositories={},
            event_bus=event_bus,
            clock=fixed_clock,
            tenant_credential_signing_key=_VALID_SIGNING_KEY,
        )

        with pytest.raises(TenantAuthenticationError):
            orchestrator.assemble_context(_request(tenant_credential=None))

    def test_env_sourced_key_enables_verification_and_rejects_a_forged_tenant(
        self, event_bus: RecordingEventBus, fixed_clock: FakeClock
    ):
        orchestrator = build_memory_orchestrator(
            zone_repositories={},
            event_bus=event_bus,
            clock=fixed_clock,
            env={TENANT_CREDENTIAL_SIGNING_KEY_ENV_VAR: _VALID_SIGNING_KEY_HEX},
        )
        genuine_for_tenant_a = sign_tenant_credential(
            secret=_VALID_SIGNING_KEY,
            tenant_id="tenant-a",
            issued_at=fixed_clock.now(),
        )

        with pytest.raises(TenantAuthenticationError):
            orchestrator.assemble_context(
                _request(
                    tenant_id="tenant-a-impersonated-as-tenant-b",
                    tenant_credential=genuine_for_tenant_a,
                )
            )

    def test_genuine_credential_for_the_claimed_tenant_is_accepted(
        self, event_bus: RecordingEventBus, fixed_clock: FakeClock
    ):
        orchestrator = build_memory_orchestrator(
            zone_repositories={},
            event_bus=event_bus,
            clock=fixed_clock,
            tenant_credential_signing_key=_VALID_SIGNING_KEY,
        )
        credential = sign_tenant_credential(
            secret=_VALID_SIGNING_KEY, tenant_id="tenant-1", issued_at=fixed_clock.now()
        )

        result = orchestrator.assemble_context(_request(tenant_credential=credential))

        assert result.zones_unavailable == list(ZoneId)


class TestBuildProvenanceRepository:
    """Defaults: `verify_privileges=True`, wrapped in the FR-013 conflict-detection sweep."""

    def test_defaults_wrap_in_conflict_detecting_repository(
        self, fixed_clock: FakeClock
    ):
        connection = RecordingConnection(rows=_SAFE_PRIVILEGE_ROW)

        repository = build_provenance_repository(connection, fixed_clock)

        assert isinstance(repository, ConflictDetectingProvenanceRepository)

    def test_defaults_run_the_append_only_privilege_check(self, fixed_clock: FakeClock):
        connection = RecordingConnection(rows=_SAFE_PRIVILEGE_ROW)

        build_provenance_repository(connection, fixed_clock)

        assert connection.cursor_obj.executed, (
            "verify_privileges=True must issue the pg_roles/pg_class privilege "
            "query at construction time -- the composition root's whole point is "
            "that this check is no longer skippable by omission."
        )

    def test_verify_privileges_false_opts_out_explicitly(self, fixed_clock: FakeClock):
        connection = RecordingConnection()

        build_provenance_repository(connection, fixed_clock, verify_privileges=False)

        assert connection.cursor_obj.executed == [], (
            "verify_privileges=False must be an explicit, visible opt-out, not "
            "silently reachable through the composition root's default"
        )

    def test_detect_conflicts_false_returns_the_bare_adapter(self, fixed_clock: FakeClock):
        connection = RecordingConnection(rows=_SAFE_PRIVILEGE_ROW)

        repository = build_provenance_repository(
            connection, fixed_clock, detect_conflicts=False
        )

        assert isinstance(repository, SqlProvenanceRepository)
        assert not isinstance(repository, ConflictDetectingProvenanceRepository)

    def test_unsafe_role_raises_even_through_the_composition_root(
        self, fixed_clock: FakeClock
    ):
        unsafe_row = [(True, False, False)]  # is_superuser=True
        connection = RecordingConnection(rows=unsafe_row)

        with pytest.raises(ZoneRepositoryError):
            build_provenance_repository(connection, fixed_clock)


class TestBuildEpisodicRepository:
    """Default: `verify_privileges=True` (opposite of `SqlEpisodicRepository`'s own default)."""

    def test_defaults_run_the_append_only_privilege_check(self, fixed_clock: FakeClock):
        connection = RecordingConnection(rows=_SAFE_PRIVILEGE_ROW)

        build_episodic_repository(connection, fixed_clock)

        assert connection.cursor_obj.executed, (
            "verify_privileges=True must issue the privilege query by default"
        )

    def test_verify_privileges_false_opts_out_explicitly(self, fixed_clock: FakeClock):
        connection = RecordingConnection()

        build_episodic_repository(connection, fixed_clock, verify_privileges=False)

        assert connection.cursor_obj.executed == []


class TestBuildConflictAwareSemanticRepository:
    """DASH-STORY-022: Zone 3's write path composed through `build_provenance_repository`."""

    def test_wires_the_real_fr013_sweep_via_build_provenance_repository(
        self, fixed_clock: FakeClock
    ):
        semantic_connection = RecordingConnection()
        provenance_connection = RecordingConnection()

        writer = build_conflict_aware_semantic_repository(
            semantic_connection,
            provenance_connection,
            fixed_clock,
            verify_privileges=False,
        )

        assert isinstance(writer, ConflictAwareSemanticRepository)
        edge = SemanticEdge(
            tenant_id="tenant-1",
            edge_id="edge-1",
            subject_ref="entity-a",
            predicate="related_to",
            object_ref="entity-b",
        )
        writer.insert_edge(
            edge,
            provenance_id="prov-1",
            source_type=SourceType.USER_STATED,
            retrieval_context_hash=_PLACEHOLDER_CONTEXT_HASH,
        )

        assert provenance_connection.cursor_obj.executed, (
            "must-not-deviate item 3: this builder must route through "
            "build_provenance_repository so the FR-013 sweep's own SELECT/"
            "INSERT actually runs against provenance_connection"
        )

    def test_detect_conflicts_false_returns_a_writer_over_the_bare_adapter(
        self, fixed_clock: FakeClock
    ):
        semantic_connection = RecordingConnection()
        provenance_connection = RecordingConnection()

        writer = build_conflict_aware_semantic_repository(
            semantic_connection,
            provenance_connection,
            fixed_clock,
            verify_privileges=False,
            detect_conflicts=False,
        )

        assert not isinstance(writer._provenance_repository, ConflictDetectingProvenanceRepository)


class TestBuildConflictAwareEntityMemoryRepository:
    """DASH-STORY-022: Zone 5's write path composed through `build_provenance_repository`."""

    def test_wires_the_real_fr013_sweep_via_build_provenance_repository(
        self, fixed_clock: FakeClock, event_bus: RecordingEventBus
    ):
        entity_memory_repository = EntityMemoryRepository(
            clock=fixed_clock, event_bus=event_bus
        )
        provenance_connection = RecordingConnection()

        writer = build_conflict_aware_entity_memory_repository(
            entity_memory_repository,
            provenance_connection,
            fixed_clock,
            verify_privileges=False,
        )

        assert isinstance(writer, ConflictAwareEntityMemoryRepository)
        writer.write_attribute(
            "tenant-1",
            "entity-1",
            "display_name",
            "<PII_EXAMPLE_REDACTED>",
            "prov-1",
            source_type=SourceType.USER_STATED,
            retrieval_context_hash=_PLACEHOLDER_CONTEXT_HASH,
        )

        assert provenance_connection.cursor_obj.executed, (
            "must-not-deviate item 3: this builder must route through "
            "build_provenance_repository so the FR-013 sweep's own SELECT/"
            "INSERT actually runs against provenance_connection"
        )


class TestBuildPostgresConnectionErrorHandling:
    """`build_postgres_connection` wraps a connection-phase `psycopg.Error` (DSHN-76)."""

    _SETTINGS = PostgresSettings(
        host="localhost",
        port=5432,
        database="dashanan",
        migration_user="migration_role",
        migration_password="migration-secret-1",
        app_login_user="app_role",
        app_login_password="app-secret-1",
    )

    def test_build_should_raisePostgresConnectionError_when_connectFails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import psycopg

        from dashanan.infrastructure import composition_root as composition_root_module

        underlying = psycopg.OperationalError(
            'connection to server failed: FATAL:  password authentication '
            f'failed for user "{self._SETTINGS.app_login_user}"'
        )

        def _raise_connect(dsn: str) -> None:
            raise underlying

        monkeypatch.setattr(composition_root_module.psycopg, "connect", _raise_connect)

        with pytest.raises(PostgresConnectionError) as excinfo:
            build_postgres_connection(self._SETTINGS)

        assert self._SETTINGS.app_login_password not in str(excinfo.value)
        assert "FATAL" not in str(excinfo.value)
        assert excinfo.value.__cause__ is underlying

    def test_build_should_returnConnection_when_connectSucceeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from dashanan.infrastructure import composition_root as composition_root_module

        sentinel_connection = object()
        monkeypatch.setattr(
            composition_root_module.psycopg, "connect", lambda dsn: sentinel_connection
        )

        connection = build_postgres_connection(self._SETTINGS)

        assert connection is sentinel_connection
