"""DASH-STORY-027-QA: real-Postgres integration proof for DASH-STORY-027-DEV's shipped erasure cascade.

Reuses, unedited in spirit, `test_api_real_postgres_e2e_dash3.py`'s own
`shape_b_stack`/`applied_migrations` docker-compose fixture pattern (random
project name, dynamically allocated free ports, `secrets.token_urlsafe`
credentials, unconditional `docker compose down -v --remove-orphans`
teardown) and its identical skip-when-Docker-unavailable posture.

SCOPE, TESTED AGAINST THE ACTUALLY SHIPPED CODE (not a mock, not a
pre-DASH-STORY-027-DEV state) -- this module drives the real
`UnifiedSubjectErasureOrchestrator` composed from the SAME real adapter
classes `dashanan.api.composition.make_unified_subject_erasure_orchestrator_
dependency` composes it from: `SqlSemanticRepository` (Zone 3),
`SqlEntityMemoryRepository` (Zone 5, Shape B), `Zone8SubjectKeyedArchiver` +
`DurableZone8ErasureCoordinator` (Zone 8, with the write-ahead-marker/
finalize durability boundary), and `SqlErasureMarkerStore`/
`SqlSubjectToItemIndexRepository` for the durability + index bookkeeping.

DISCLOSED, NOT-FABRICATED GAPS this module documents rather than "fixes"
(each traced to a real, grep-verifiable fact about the shipped code, not
an assumption):

  1. AC-027-QA-1 asks for cascade proof across "Zones 1/2/3/6/7/8 and both
     retrieval indices." The real, shipped orchestrator's Zone 1 leg is
     `NullZone1ErasureLeg` (`unified_subject_erasure_orchestrator.py`'s own
     docstring: `WorkingItem` carries no subject-linkable field at all --
     there is no real seam to erase through). The real, shipped
     `make_unified_subject_erasure_orchestrator_dependency` also
     constructs the orchestrator with `zone2_6_cascade=None` (that
     factory's own docstring: "this composition root wires no
     VectorIndexPort/LexicalIndexPort adapter anywhere ... so
     CrossZoneDpdpErasureCascade's own RetrievalIndexEviction collaborator
     has nothing real to compose against yet"), so the Zone 2/6 leg and
     "both retrieval indices" are NOT reachable through the real,
     currently-wired system today. `TestZone1AndZone26LegsAreHonestNoOps`
     below proves both of these are genuine, currently-shipped facts (not
     an oversight of this test) by inspecting the real source, rather than
     fabricating positive coverage for a cascade leg that does not exist.
     What IS genuinely, durably provable today is the Zone 3/5/8 leg of
     the cascade plus Zone 7's own audit trail -- that is what
     `TestScenario1FullCascadeAcrossZone3Zone5Zone8` and
     `TestScenario2Zone7AuditTrailNoPii` below prove.

  2. AC-027-QA-3's second clause asks for "the specific failed (zone_id,
     item_id) pairs visible via GET /jobs/{job_id}." The real, shipped
     `GET /jobs/{job_id}` handler (`api/app.py`'s `get_job_status`) reads
     `AdminJobStore`/`AdminJobEntry` (`api/state.py`), whose only fields
     are `job_id`/`status`/`progress`/`error_message` -- there is no
     `zone_id`/`item_id` field on that entry or on `JobStatus`
     (`api/schemas.py`), and `recover_pending_erasures`
     (`zone8_erasure_durability.py`) is a standalone recovery sweep that
     is never wired to write into `AdminJobStore` at all (grep-verifiable:
     no caller of `admin_job_store.record` exists in that module). A
     FAILED marker's outcome is therefore never observable through
     `GET /jobs/{job_id}` in the real, shipped system -- this is a real,
     pre-existing gap this module discloses rather than works around by
     inventing a wiring that does not exist. `TestScenario4CrashRecovery`
     below instead proves the FAILED-state/escalation half of AC-027-QA-3
     directly against the real `recover_pending_erasures`/
     `SqlErasureMarkerStore` (the actual durable state this AC's
     "terminal FAILED state" refers to), and
     `test_get_job_status_has_no_field_to_carry_failed_zone_item_pairs`
     proves the endpoint-level gap itself so it stays visible rather than
     silently assumed away.

  3. Zone 5's real Shape B storage (`SqlEntityMemoryRepository`,
     DASH-STORY-029) requires `entity_attributes`/`entity_aliases` writes
     and compliance-role deletes that `entity_schema.sql`'s own grants
     restrict to `dashanan_entity_role` (writes) and
     `dashanan_compliance_erasure_role` (deletes) -- neither role is a
     member of `_PER_ZONE_LOGIN_MEMBER_ROLES`
     (`migration_runner.py`), so the ordinary app-login connection cannot
     issue either statement against a genuinely fresh, migrated database
     (confirmed by direct exploration during this dispatch, the same class
     of gap `test_api_real_postgres_e2e_dash3.py`'s own Scenario 3
     docstring already discloses for `zone8_manifest`). This module
     therefore constructs `SqlSemanticRepository`/`SqlEntityMemoryRepository`
     against the MIGRATION-role connection (the one role already proven to
     hold every schema-owner grant) for the write/erase calls under test,
     exactly mirroring that Scenario 3 precedent's own "seed on the
     migration connection while still driving the real, unmodified
     erasure call" pattern -- this substitutes only which authenticated
     role issues the SQL, never what `SqlEntityMemoryRepository.
     erase_entity`/`SqlSemanticRepository.delete_edges_by_subject`
     themselves do.

PII NOTE: every identifier below is a synthetic, opaque test fixture
string (`subj-<hex>`, `item-<hex>`) -- never real personal data, matching
DPDP Act 2023 SS8(4) test-data-masking requirements this codebase's own
`integration-testing-core`/`contract-testing-core` skills mandate.

Skip condition (never a false pass): identical to
`test_api_real_postgres_e2e_dash3.py`'s own -- `docker` (with the
`compose` plugin) unavailable, or the Docker daemon unreachable.
"""

from __future__ import annotations

import inspect
import json
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

from dashanan.application import unified_subject_erasure_orchestrator as orchestrator_module
from dashanan.application.zone8_consolidation_store import Zone8ConsolidationStore
from dashanan.application.zone8_crypto_shredding_store import Zone8SubjectKeyedArchiver
from dashanan.application.zone8_erasure_durability import (
    DurableZone8ErasureCoordinator,
    ErasureMarker,
    ErasureMarkerStatus,
    SqlErasureMarkerStore,
    recover_pending_erasures,
)
from dashanan.application.subject_erasure_cascade import SubjectErasureCascadeService
from dashanan.application.unified_subject_erasure_orchestrator import (
    NullSubjectToItemIndex,
    NullZone1ErasureLeg,
    UnifiedSubjectErasureOrchestrator,
)
from dashanan.domain.consolidated_blob import ArchiveBatchItem
from dashanan.domain.semantic_memory import SemanticEdge
from dashanan.domain.subject_erasure import SubjectErasureRequest
from dashanan.domain.zone import ZoneId
from dashanan.domain.zone8_crypto_shredding import Zone8CryptoShreddingError, decrypt_payload
from dashanan.infrastructure.composition_root import build_postgres_connection
from dashanan.infrastructure.in_memory_subject_erasure_job_store import InMemorySubjectErasureJobStore
from dashanan.infrastructure.in_memory_subject_key_store import InMemorySubjectKeyStore
from dashanan.infrastructure.in_memory_zone8_subject_index import InMemoryZone8SubjectIndex
from dashanan.infrastructure.local_object_store import LocalObjectStore
from dashanan.infrastructure.migration_runner import SCHEMA_FILES, run_migrations
from dashanan.infrastructure.sql_entity_memory_repository import SqlEntityMemoryRepository
from dashanan.infrastructure.sql_manifest_repository import SqlManifestRepository
from dashanan.infrastructure.sql_semantic_repository import SqlSemanticRepository
from dashanan.infrastructure.settings import PostgresSettings
from dashanan.infrastructure.system_clock import SystemClock

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"

_TENANT_ID = "tenant-a"


def _docker_compose_available() -> bool:
    """Mirror `test_regression_dash024...`/`test_api_real_postgres_e2e_dash3.py`'s own identical check."""
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
        "scenarios having been verified."
    ),
)


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ShapeBStack(NamedTuple):
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
    """Identical fixture shape to `test_api_real_postgres_e2e_dash3.py`'s own `shape_b_stack`."""
    project = f"dash027-qa-{uuid.uuid4().hex[:12]}"
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
            "Could not bring up the Shape B docker-compose stack for DASH-STORY-027-QA "
            f"(stdout={up_result.stdout!r} stderr={up_result.stderr!r})"
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
    applied = run_migrations(shape_b_stack.postgres_settings())
    assert applied == list(SCHEMA_FILES)
    return shape_b_stack


@pytest.fixture
def migration_connection(applied_migrations: ShapeBStack) -> Iterator[psycopg.Connection]:
    """The MIGRATION-role connection -- see module docstring gap 3 for why this role is used.

    Mirrors `test_api_real_postgres_e2e_dash3.py`'s own Scenario 3
    workaround precedent exactly: this dispatch's own exploration found
    `entity_attributes`/`entity_aliases` writes and compliance-role
    deletes are not grantable to the ordinary app-login role today.
    """
    connection = psycopg.connect(applied_migrations.postgres_settings().migration_dsn())
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def independent_connection(applied_migrations: ShapeBStack) -> Iterator[psycopg.Connection]:
    """A SECOND, wholly independent connection for durability-proof reads (never the writer's own)."""
    connection = psycopg.connect(applied_migrations.postgres_settings().migration_dsn())
    try:
        yield connection
    finally:
        connection.close()


def _subject_id() -> str:
    return f"subj-{uuid.uuid4().hex[:10]}"


def _item_id(label: str) -> str:
    return f"item-{label}-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def zone8_consolidation_store(migration_connection: psycopg.Connection, tmp_path: Path) -> Zone8ConsolidationStore:
    """A REAL Zone 8 Facade: `LocalObjectStore` (real filesystem blobs) + `SqlManifestRepository`.

    `LocalObjectStore` is a real, unmocked adapter (one file per blob, see
    its own module docstring) -- using it instead of bringing up a real S3
    endpoint keeps this module's Zone 8 proof scoped to what this AC set
    actually needs (real Postgres manifest rows + real, undecryptable
    ciphertext) without adding an unrelated MinIO dependency this story's
    own AC set never asked for.
    """
    return Zone8ConsolidationStore(
        object_store=LocalObjectStore(tmp_path / "zone8-blobs"),
        manifest=SqlManifestRepository(migration_connection),
        clock=SystemClock(),
    )


@pytest.fixture
def zone8_key_store() -> InMemorySubjectKeyStore:
    return InMemorySubjectKeyStore()


@pytest.fixture
def zone8_subject_index() -> InMemoryZone8SubjectIndex:
    return InMemoryZone8SubjectIndex()


@pytest.fixture
def zone8_archiver(
    zone8_consolidation_store: Zone8ConsolidationStore,
    zone8_key_store: InMemorySubjectKeyStore,
    zone8_subject_index: InMemoryZone8SubjectIndex,
) -> Zone8SubjectKeyedArchiver:
    return Zone8SubjectKeyedArchiver(
        zone8_store=zone8_consolidation_store,
        key_store=zone8_key_store,
        subject_index=zone8_subject_index,
    )


@pytest.fixture
def durable_orchestrator(
    migration_connection: psycopg.Connection,
    zone8_archiver: Zone8SubjectKeyedArchiver,
    zone8_key_store: InMemorySubjectKeyStore,
    zone8_subject_index: InMemoryZone8SubjectIndex,
) -> UnifiedSubjectErasureOrchestrator:
    """The real orchestrator, composed exactly as `make_unified_subject_erasure_orchestrator_dependency`
    composes it in production -- same collaborator classes, same durability wrapper, same `None`/no-op
    legs for the two disclosed, currently-shipped gaps (module docstring item 1). The only substitution
    versus production wiring is which connection role issues the Zone 3/5/durability SQL (module
    docstring item 3) -- `SqlSemanticRepository`/`SqlEntityMemoryRepository`/`SqlErasureMarkerStore`
    themselves are the exact, unmodified production classes.
    """
    clock = SystemClock()
    zone8_service = SubjectErasureCascadeService(
        archiver=zone8_archiver, job_store=InMemorySubjectErasureJobStore(), clock=clock
    )
    durable_coordinator = DurableZone8ErasureCoordinator(
        key_store=zone8_key_store,
        subject_index=zone8_subject_index,
        marker_store=SqlErasureMarkerStore(migration_connection),
        provenance_connection=migration_connection,
        clock=clock,
    )
    return UnifiedSubjectErasureOrchestrator(
        zone8_service=zone8_service,
        zone3_repository=SqlSemanticRepository(connection=migration_connection),
        zone5_repository=SqlEntityMemoryRepository(migration_connection, clock=clock),
        job_store=InMemorySubjectErasureJobStore(),
        clock=clock,
        zone2_6_cascade=None,
        subject_to_item_index=NullSubjectToItemIndex(),
        zone1_erasure=NullZone1ErasureLeg(),
        zone8_durable_coordinator=durable_coordinator,
    )


# ---------------------------------------------------------------------------
# Scenario 1 (AC-027-QA-1): full real cascade across the zones the shipped
# orchestrator ACTUALLY reaches -- Zone 3 (semantic edges), Zone 5 (entity
# attributes), Zone 8 (crypto-shredded archive, blob rendered unreadable).
# ---------------------------------------------------------------------------


class TestScenario1FullCascadeAcrossZone3Zone5Zone8:
    def test_erasure_completes_and_zone3_zone5_zone8_return_nothing_recoverable(
        self,
        durable_orchestrator: UnifiedSubjectErasureOrchestrator,
        migration_connection: psycopg.Connection,
        independent_connection: psycopg.Connection,
        zone8_key_store: InMemorySubjectKeyStore,
    ) -> None:
        """AC-027-QA-1: erase a subject with real data in Zone 3, Zone 5, and Zone 8;
        confirm a fresh, independent read of each zone returns nothing recoverable,
        and Zone 8's blob is confirmed unreadable (crypto-shredded, not merely deleted)."""
        subject_id = _subject_id()
        edge_id = _item_id("edge")
        # `UnifiedSubjectErasureOrchestrator._run_all_legs` calls
        # `zone5_repository.erase_entity(tenant_id, request.subject_id)` --
        # Zone 5's own erasure key IS the subject_id (the Zone 5 `entity_id`
        # column), never a separate column value -- so the seeded attribute
        # row's `entity_id` must equal `subject_id` for real erasure to
        # reach it (the same convention `SqlSemanticRepository.
        # delete_edges_by_subject`'s own `subject_ref` column follows).
        entity_id = subject_id
        object_ref = f"entity-{uuid.uuid4().hex[:8]}"
        zone8_item_id = _item_id("zone8")
        plaintext_payload = b"real-payload-bytes-for-dash027-qa-cascade-proof"

        # Seed Zone 3: one real semantic edge naming `subject_id` as its subject_ref.
        semantic_repo = SqlSemanticRepository(connection=migration_connection)
        semantic_repo.insert_edge(
            SemanticEdge(
                tenant_id=_TENANT_ID,
                edge_id=edge_id,
                subject_ref=subject_id,
                predicate="located_in",
                object_ref=object_ref,
            )
        )

        # Seed Zone 5: one real entity attribute keyed by `entity_id == subject_id`.
        entity_repo = SqlEntityMemoryRepository(migration_connection, clock=SystemClock())
        write_result = entity_repo.write_attribute(
            tenant_id=_TENANT_ID,
            entity_id=entity_id,
            attribute_name="status",
            value="active",
            provenance_id=f"prov-{uuid.uuid4().hex[:8]}",
            subject_id=subject_id,
        )
        assert write_result.write_id, "Zone 5 seed write must be accepted before erasure is proven"

        # Seed Zone 8: archive one real, plaintext-sealed item for `subject_id`,
        # then confirm the sealed ciphertext is durably readable BEFORE erasure.
        archiver = durable_orchestrator._zone8_service._archiver  # type: ignore[attr-defined]
        archive_result = archiver.archive_for_subject(
            ArchiveBatchItem(
                tenant_id=_TENANT_ID,
                item_id=zone8_item_id,
                source_zone=ZoneId.EPISODIC,
                payload=plaintext_payload,
                subject_id=None,
            ),
            subject_id,
        )
        assert any(w.item_id == zone8_item_id for w in archive_result.written)
        sealed_before = archiver._zone8_store.read_blob_range(_TENANT_ID, zone8_item_id)
        key_before = zone8_key_store.get_key(_TENANT_ID, subject_id)
        assert key_before is not None
        from dashanan.domain.zone8_crypto_shredding import SealedPayload

        recovered_before = decrypt_payload(
            key_before, SealedPayload(sealed_bytes=sealed_before),
            tenant_id=_TENANT_ID, item_id=zone8_item_id,
        )
        assert recovered_before == plaintext_payload, (
            "sanity precondition: the seeded Zone 8 payload must be genuinely "
            "readable (decryptable) before erasure runs"
        )

        # Real erasure, through the real, unmodified orchestrator.
        durable_orchestrator.request_erasure(
            SubjectErasureRequest(tenant_id=_TENANT_ID, subject_id=subject_id)
        )

        # Independent-connection reads: never the writer's own connection.
        with independent_connection.cursor() as cursor:
            cursor.execute(
                "SELECT edge_id FROM semantic_edges WHERE tenant_id = %s AND subject_ref = %s",
                (_TENANT_ID, subject_id),
            )
            remaining_edges = cursor.fetchall()
        assert remaining_edges == [], "Zone 3 must return nothing recoverable for the erased subject"

        with independent_connection.cursor() as cursor:
            cursor.execute(
                "SELECT attribute_name FROM entity_attributes "
                "WHERE tenant_id = %s AND entity_id = %s",
                (_TENANT_ID, entity_id),
            )
            remaining_attributes = cursor.fetchall()
        assert remaining_attributes == [], "Zone 5 must return nothing recoverable for the erased subject"

        # Zone 8: the blob's ciphertext bytes are still present in object
        # storage (crypto-shredding never deletes the archive itself, only
        # the key) -- confirm THAT, then confirm decryption is impossible.
        sealed_after = archiver._zone8_store.read_blob_range(_TENANT_ID, zone8_item_id)
        assert sealed_after == sealed_before, (
            "crypto-shredding must not rewrite/delete the archived ciphertext -- "
            "only the key is destroyed"
        )
        assert zone8_key_store.get_key(_TENANT_ID, subject_id) is None, (
            "the subject's Zone 8 key must be destroyed after erasure"
        )
        with pytest.raises(Zone8CryptoShreddingError):
            decrypt_payload(
                b"\x00" * 32,  # any key other than the real (now-destroyed) one --
                # the real key is gone (asserted above), so no caller can ever
                # again supply the one key that would decrypt this ciphertext
                SealedPayload(sealed_bytes=sealed_after),
                tenant_id=_TENANT_ID, item_id=zone8_item_id,
            )


# ---------------------------------------------------------------------------
# Scenario 2 (AC-027-QA-2): Zone 7's own audit trail records that an erasure
# occurred, without retaining the erased PII itself.
# ---------------------------------------------------------------------------


class TestScenario2Zone7AuditTrailNoPii:
    def test_zone7_record_confirms_erasure_without_retaining_pii(
        self,
        durable_orchestrator: UnifiedSubjectErasureOrchestrator,
        migration_connection: psycopg.Connection,
        independent_connection: psycopg.Connection,
    ) -> None:
        subject_id = _subject_id()
        zone8_item_id = _item_id("zone8")
        pii_payload = b"synthetic-subject-pii-marker-should-never-appear-in-provenance"

        archiver = durable_orchestrator._zone8_service._archiver  # type: ignore[attr-defined]
        archiver.archive_for_subject(
            ArchiveBatchItem(
                tenant_id=_TENANT_ID,
                item_id=zone8_item_id,
                source_zone=ZoneId.EPISODIC,
                payload=pii_payload,
                subject_id=None,
            ),
            subject_id,
        )

        durable_orchestrator.request_erasure(
            SubjectErasureRequest(tenant_id=_TENANT_ID, subject_id=subject_id)
        )

        with independent_connection.cursor() as cursor:
            cursor.execute(
                "SELECT update_history, source_type, item_id FROM provenance_records "
                "WHERE tenant_id = %s AND item_id = %s",
                (_TENANT_ID, f"subject:{subject_id}"),
            )
            rows = cursor.fetchall()
        assert len(rows) == 1, "exactly one Zone 7 record must confirm this subject's erasure"
        raw_update_history, source_type, item_id = rows[0]
        update_history = (
            raw_update_history if isinstance(raw_update_history, list)
            else json.loads(str(raw_update_history))
        )
        change = update_history[0]["change"]
        assert "erasure" in change.lower() or "shred" in change.lower(), (
            "the Zone 7 record must state that an erasure occurred"
        )
        record_text = json.dumps(update_history)
        assert pii_payload not in record_text.encode("utf-8"), (
            "the Zone 7 record must never contain the erased PII payload itself"
        )
        assert subject_id in change or subject_id in item_id, (
            "the record must still name the (opaque) subject_id it applies to -- "
            "an opaque identifier is not the PII content itself"
        )


# ---------------------------------------------------------------------------
# Scenario 3 (must-not-deviate item 4): Zone 4 is NEVER silently claimed as
# erased -- confirmed directly against the real source, per
# `UnifiedSubjectErasureOrchestrator`'s own disclosed, structural gap
# (`Procedure` carries no subject-linkable field at all, so there is no
# real erasure call to make in the first place). Zone 5 is deliberately
# EXCLUDED from this negative test: DASH-STORY-029-DEV's real, shipped
# `zone5_repository.erase_entity(...)` call in `_run_all_legs` DOES reach
# Zone 5 today (proven positively by Scenario 1 above) -- asserting the
# opposite here would be a fabricated negative, not an honest one.
# ---------------------------------------------------------------------------


class TestScenario3Zone4NeverClaimedErased:
    def test_orchestrator_source_has_no_zone4_procedural_erasure_call(self) -> None:
        source = inspect.getsource(orchestrator_module)
        assert "procedural" not in source.lower(), (
            "the real orchestrator must not claim (or silently attempt) a Zone 4 "
            "erasure call -- Procedure has no subject-linkable field to erase through"
        )

    def test_run_all_legs_only_names_the_five_real_legs(self) -> None:
        source = inspect.getsource(orchestrator_module.UnifiedSubjectErasureOrchestrator._run_all_legs)
        for real_leg in (
            "_zone1_erasure", "_run_zone2_6_leg", "_zone3_repository",
            "_zone5_repository", "_run_zone8_leg",
        ):
            assert real_leg in source
        assert "zone4" not in source.lower()


class TestZone1AndZone26LegsAreHonestNoOps:
    """Module docstring gap 1: proves, against the real source, that Zone 1
    and Zone 2/6 are genuinely NOT reachable through the shipped system
    today -- so AC-027-QA-1's "Zones 1 ... and 6 ... both retrieval
    indices" clause is flagged as an untestable gap rather than faked."""

    def test_default_zone1_leg_is_the_honest_null_object(self) -> None:
        assert NullZone1ErasureLeg().erase_subject(_TENANT_ID, _subject_id()) == ()

    def test_production_wiring_passes_zone2_6_cascade_as_none(self) -> None:
        import dashanan.api.composition as composition_module

        source = inspect.getsource(
            composition_module.make_unified_subject_erasure_orchestrator_dependency
        )
        assert "zone2_6_cascade=None" in source, (
            "AC-027-QA-1's 'both retrieval indices' clause is not testable against "
            "the real, currently-wired production system: composition.py's own "
            "factory disclosed docstring confirms zone2_6_cascade is wired as None"
        )


# ---------------------------------------------------------------------------
# Scenario 4 (AC-027-QA-3): the Zone 8 step-3/step-4 durability boundary,
# against a REAL Postgres-backed `SqlErasureMarkerStore` -- two SEPARATE
# tests, retry-succeeds and retries-exhausted, never merged into one.
# ---------------------------------------------------------------------------


class TestScenario4CrashRecovery:
    def test_crash_before_destroy_confirmed_write_recovery_retries_and_succeeds(
        self,
        migration_connection: psycopg.Connection,
        independent_connection: psycopg.Connection,
        zone8_key_store: InMemorySubjectKeyStore,
    ) -> None:
        """AC-027-QA-3, case 1: a write-ahead marker is durably written (step 3's
        own precondition) but destruction is left UNCONFIRMED (simulated crash
        before/during step 3's own destroy call) -- the recovery sweep's retry
        succeeds, producing a finalized Zone 7 record and a cleaned-up
        `subject_item_index` row."""
        subject_id = _subject_id()
        item_ids = (_item_id("z8a"), _item_id("z8b"))
        marker_id = f"marker-{uuid.uuid4().hex[:8]}"
        zone8_key_store.get_or_create_key(_TENANT_ID, subject_id)

        # Seed the subject_item_index zone8 bookkeeping row the finalize step cleans up.
        with migration_connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO subject_item_index (tenant_id, subject_id, zone, item_id) "
                "VALUES (%s, %s, 'zone8', %s)",
                (_TENANT_ID, subject_id, item_ids[0]),
            )
        migration_connection.commit()

        marker_store = SqlErasureMarkerStore(migration_connection)
        marker_store.append(
            ErasureMarker(
                marker_id=marker_id,
                tenant_id=_TENANT_ID,
                subject_id=subject_id,
                item_ids=item_ids,
                status=ErasureMarkerStatus.PENDING,  # write-ahead marker: destroy not yet confirmed
                created_at=datetime.now(UTC),
            )
        )

        finalized = recover_pending_erasures(
            marker_store=marker_store,
            key_store=zone8_key_store,
            provenance_connection=migration_connection,
            compliance_owner_env_value="compliance-owner@example.test",
            sleep=lambda seconds: None,
        )

        assert finalized == (marker_id,)
        recovered = marker_store.get(marker_id)
        assert recovered is not None
        assert recovered.status is ErasureMarkerStatus.FINALIZED
        assert zone8_key_store.get_key(_TENANT_ID, subject_id) is None

        with independent_connection.cursor() as cursor:
            cursor.execute(
                "SELECT provenance_id FROM provenance_records "
                "WHERE tenant_id = %s AND item_id = %s",
                (_TENANT_ID, f"subject:{subject_id}"),
            )
            provenance_rows = cursor.fetchall()
        assert len(provenance_rows) == 1, (
            "recovery must produce exactly one finalized Zone 7 record"
        )

        with independent_connection.cursor() as cursor:
            cursor.execute(
                "SELECT item_id FROM subject_item_index "
                "WHERE tenant_id = %s AND subject_id = %s AND zone = 'zone8'",
                (_TENANT_ID, subject_id),
            )
            remaining_index_rows = cursor.fetchall()
        assert remaining_index_rows == [], (
            "finalize must clean up the subject_item_index zone8 bookkeeping row"
        )

    def test_exhausted_retries_reaches_failed_and_escalates_to_compliance_owner(
        self,
        migration_connection: psycopg.Connection,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """AC-027-QA-3, case 2: bounded k=2 retries exhausted with destruction
        still unconfirmed -> terminal FAILED state + CRITICAL escalation naming
        the compliance owner. A SEPARATE test from case 1 above, per this
        dispatch's own must-not-deviate instruction.

        Endpoint-level gap (module docstring item 2, not re-tested here per
        AC-027-QA-3's own wording): the real `GET /jobs/{job_id}` handler
        cannot surface this FAILED marker or its `(zone_id, item_id)` pairs --
        see `test_get_job_status_has_no_field_to_carry_failed_zone_item_pairs`
        below for that disclosed, structural fact.
        """

        class _NeverDestroysKeyStore(InMemorySubjectKeyStore):
            def destroy_key(self, tenant_id: str, subject_id: str) -> bool:
                self.get_or_create_key(tenant_id, subject_id)  # ensure "still present"
                return False

        subject_id = _subject_id()
        item_ids = (_item_id("z8"),)
        marker_id = f"marker-{uuid.uuid4().hex[:8]}"
        key_store = _NeverDestroysKeyStore()
        key_store.get_or_create_key(_TENANT_ID, subject_id)

        marker_store = SqlErasureMarkerStore(migration_connection)
        marker_store.append(
            ErasureMarker(
                marker_id=marker_id,
                tenant_id=_TENANT_ID,
                subject_id=subject_id,
                item_ids=item_ids,
                status=ErasureMarkerStatus.PENDING,
                created_at=datetime.now(UTC),
            )
        )

        with caplog.at_level("CRITICAL"):
            finalized = recover_pending_erasures(
                marker_store=marker_store,
                key_store=key_store,
                provenance_connection=migration_connection,
                compliance_owner_env_value="compliance-owner@example.test",
                sleep=lambda seconds: None,
            )

        assert finalized == ()
        failed_marker = marker_store.get(marker_id)
        assert failed_marker is not None
        assert failed_marker.status is ErasureMarkerStatus.FAILED
        assert failed_marker.retry_count == 2, "bounded k=2 retry policy"

        escalation_records = [
            r for r in caplog.records
            if r.levelname == "CRITICAL" and "escalating" in r.message.lower()
        ]
        assert escalation_records, "an escalation must be logged when retries are exhausted"
        escalated = escalation_records[0]
        assert getattr(escalated, "compliance_owner", None) == "compliance-owner@example.test" or (
            "compliance-owner@example.test" in escalated.message
        )
        assert getattr(escalated, "marker_id", None) == marker_id or marker_id in escalated.message


class TestGetJobStatusGapForFailedZoneItemPairs:
    """Module docstring gap 2: proves, against the real source, that
    `GET /jobs/{job_id}` has no field through which AC-027-QA-3's "specific
    failed (zone_id, item_id) pairs" could ever be surfaced, and that
    `recover_pending_erasures` is never wired to write into `AdminJobStore`
    -- so this half of AC-027-QA-3 is flagged as an untestable gap against
    the real, currently-shipped system rather than fabricated."""

    def test_admin_job_entry_has_no_zone_id_or_item_id_field(self) -> None:
        from dashanan.api.state import AdminJobEntry

        field_names = set(AdminJobEntry.__dataclass_fields__)
        assert "zone_id" not in field_names
        assert "item_id" not in field_names
        assert "item_ids" not in field_names

    def test_job_status_response_schema_has_no_zone_id_or_item_id_field(self) -> None:
        from dashanan.api.schemas import JobStatus

        field_names = set(JobStatus.model_fields)
        assert "zone_id" not in field_names
        assert "item_id" not in field_names
        assert "item_ids" not in field_names

    def test_recovery_sweep_module_never_calls_admin_job_store(self) -> None:
        from dashanan.application import zone8_erasure_durability as durability_module

        source = inspect.getsource(durability_module)
        assert "admin_job_store" not in source, (
            "recover_pending_erasures's own FAILED-state outcome is never wired "
            "to AdminJobStore -- GET /jobs/{job_id} cannot observe it today"
        )
