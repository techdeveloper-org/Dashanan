"""DASH-STORY-026-QA: real HTTP-level integration matrix for FR-013's
Zone 3 (Semantic) / Zone 5 (Entity) routing wired into POST /memory/write
(GitHub #23), against a REAL Postgres-backed host -- not mocks.

Traces to FR-013 (SRS.md, FR-013 row, cited verbatim): "On every write
targeting Zone 3 (Semantic) or Zone 5 (Entity), the system SHALL check
the candidate against existing records for the same (entity, predicate)
or (subject, predicate, object). On contradiction, neither record SHALL
be overwritten; both SHALL be marked conflict_status=disputed and both
confidences reduced, until resolved by re-confirmation, explicit user
correction, or majority-source consensus." This is the exact SRS.md
FR-013 row text, not a paraphrase (DASH-STORY-026-REVIEW attempt 1
found this module's citation had drifted from the source; this is the
corrected, verbatim quotation).

This module implements all 14 cases of
`docs/phase-1.5-api/fr013-predicate-schema-design.md` v7, Section 5,
against `DASH-STORY-026-DEV`'s shipped `_do_write`/`_do_write_zone3_5`
routing logic (`src/dashanan/api/app.py`) and the widened
`WriteMemoryContent`/`ProvenanceWriteBlock`/`WriteReceipt` schemas
(`src/dashanan/api/schemas.py`). Every assertion below runs against the
live, shipped code -- not a mock and not the pre-change codebase state
(sprint4_ar3_context_windows.json's own dependency_note for this
sub-task).

Style precedent: mirrors `tests/integration/test_api_real_postgres_e2e_
dash3.py`'s own fixture shape exactly -- real `docker compose` Postgres
service (Shape B, random project name and credentials via
`secrets.token_urlsafe`), real schema migrations
(`infrastructure.migration_runner`), a real `build_postgres_connection`
connection, and a real `TestClient` wired through
`build_app_context(postgres_connection=<that real connection>)`. This
module brings up its own throwaway stack (module-scoped) rather than
sharing `test_api_real_postgres_e2e_dash3.py`'s, mirroring that file's
own precedent of one stack per test module (also
`test_regression_dash024_shape_b_deployment_infra.py`'s pattern).

PII note (dev_prompt's own constraint, carried through unchanged): every
fixture below uses synthetic, abstract placeholder entity_refs/predicate/
subject_scope values only (e.g. "entity-a", "likes", "general-scope") --
never real or realistic PII-shaped content, matching the design doc's own
Section 5 abstract-placeholder discipline (|subjects|/|objects| counts and
boolean flags, never example entity/attribute values).

Cases 10/11 (partial Zone 3 failure after a successful Zone 5 write; a
Zone 5 failure before any Zone 3 attempt) cannot be produced by a genuine
Postgres fault injected from outside the process without corrupting the
schema for every other test in this module -- so, consistent with this
sub-task's own must-not-deviate item ("does not assert crypto-shredding or
cross-zone-atomicity behavior -- both are explicitly out of scope"; the
fail-fast-plus-honest-partial-reporting CONTRACT is what these two cases
test, not a specific storage-layer fault), these two cases monkeypatch one
write method on the real, live `ConflictAwareSemanticRepository`/
`ConflictAwareEntityMemoryRepository` instances the real `AppContext`
already built against the real Postgres connection, to raise the exact
`ZoneRepositoryError` `_do_write_zone3_5` (`app.py`) is documented to
catch (Section 4.2 step 6) -- every other collaborator in the request path
(auth, routing gate, CandidateFact construction, classify(), the
JSONResponse/WriteReceipt contract) remains the real, unmodified,
Postgres-backed code. This is the same class of "inject one failure at the
real collaborator's own boundary" technique used throughout this
codebase's other real-Postgres suites (see e.g.
`test_api_real_postgres_e2e_dash3.py`'s Scenario 5 docstring on
AR1-S3-G3), not a return to fully-mocked testing.

Skip condition: identical to `test_api_real_postgres_e2e_dash3.py`'s own
-- `docker` (with the `compose` plugin) unavailable degrades this suite to
a documented skip, never a false pass.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import shutil
import socket
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple
from unittest.mock import patch

import jwt
import pytest
from fastapi.testclient import TestClient

from dashanan.api import app as app_module
from dashanan.api.composition import AppContext, build_app_context
from dashanan.domain.exceptions import ZoneRepositoryError
from dashanan.infrastructure.composition_root import build_postgres_connection
from dashanan.infrastructure.migration_runner import SCHEMA_FILES, run_migrations
from dashanan.infrastructure.settings import PostgresSettings

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"

_JWT_KEY = bytes(range(32))
_TENANT_CREDENTIAL_KEY = bytes(range(32, 64))
_TENANT_ID = "tenant-a"


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
        "proof is required, and why this skip must never be read as this module's "
        "14-case FR-013 Zone 3/5 routing matrix having been verified."
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
    fixture verbatim in shape: random project name, random free host
    ports, `secrets.token_urlsafe` credentials for every service, and
    unconditional teardown in the finalizer.
    """
    project = f"dash026-e2e-{uuid.uuid4().hex[:12]}"
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
            "Could not bring up the Shape B docker-compose stack for the DASH-STORY-026 "
            f"Zone 3/5 routing matrix (stdout={up_result.stdout!r} stderr={up_result.stderr!r})"
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

    Mirrors `test_api_real_postgres_e2e_dash3.py`'s own
    `real_postgres_app_context` fixture exactly.
    """
    connection = build_postgres_connection(applied_migrations.postgres_settings())
    try:
        context = build_app_context(
            tenant_credential_signing_key=_TENANT_CREDENTIAL_KEY,
            jwt_signing_key=_JWT_KEY,
            postgres_connection=connection,
        )
        assert context.postgres_available is True
        assert context.semantic_repository is not None
        assert context.conflict_aware_entity_repository is not None
        yield context
    finally:
        connection.close()


@pytest.fixture
def client(real_postgres_app_context: AppContext) -> Iterator[TestClient]:
    app = app_module.create_app(real_postgres_app_context)
    with TestClient(app) as test_client:
        yield test_client


def _retrieval_context_hash(seed: str) -> str:
    """A well-formed `^[0-9a-f]{64}$` value: SHA-256 hex of an abstract, non-PII seed string."""
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _entity_refs(*, subjects: tuple[str, ...], objects: tuple[str, ...]) -> list[dict[str, str]]:
    refs = [{"role": "subject", "entity_id": s} for s in subjects]
    refs += [{"role": "object", "entity_id": o} for o in objects]
    return refs


def _write_body(
    *,
    session_id: str | None = None,
    text: str = "abstract-fact-placeholder",
    zone_hint: str | None = None,
    entity_refs: list[dict[str, str]] | None = None,
    index_reverse: bool = False,
    predicate: str | None = "predicate-placeholder",
    subject_scope: str | None = None,
    retrieval_context_hash: str | None = "seed-default",
) -> dict[str, object]:
    """Build one `POST /memory/write` JSON body.

    `retrieval_context_hash="seed-default"` is a sentinel meaning "derive
    a well-formed hash from this test's own case name" -- callers that
    want a missing/malformed value pass `None`/a literal malformed string
    explicitly instead.
    """
    content: dict[str, object] = {"text": text, "entity_refs": entity_refs or [], "index_reverse": index_reverse}
    if zone_hint is not None:
        content["zone_hint"] = zone_hint
    if predicate is not None:
        content["predicate"] = predicate
    if subject_scope is not None:
        content["subject_scope"] = subject_scope

    provenance: dict[str, object] = {"source_type": "user_stated", "purpose": "dash026-qa-matrix"}
    if retrieval_context_hash == "seed-default":
        provenance["retrieval_context_hash"] = _retrieval_context_hash(session_id or str(uuid.uuid4()))
    elif retrieval_context_hash is not None:
        provenance["retrieval_context_hash"] = retrieval_context_hash

    return {
        "session_id": session_id or f"s-{uuid.uuid4().hex[:8]}",
        "content": content,
        "provenance": provenance,
    }


def _post_write(client: TestClient, body: dict[str, object]) -> object:
    return client.post(
        "/memory/write",
        json=body,
        headers={**_auth("memory:write"), "Idempotency-Key": str(uuid.uuid4())},
    )


class TestCase0RoutingGateRegressionGuard:
    """Section 5 case 0: the single most important guard this change introduces.

    AC-026-QA-1: an ordinary Zone 1/2/4 write (`entity_refs` empty,
    `zone_hint` in {working, episodic, procedural} or absent) must NOT
    enter `CandidateFact`/`classify()` at all, and must NOT 422/400 on a
    missing `subject_scope`/`predicate`/`retrieval_context_hash` it was
    never asked to supply.
    """

    def test_case0_ordinary_write_no_entity_refs_absent_zone_hint_unaffected(
        self, client: TestClient
    ) -> None:
        session_id = f"s-{uuid.uuid4().hex[:8]}"
        response = _post_write(
            client,
            {
                "session_id": session_id,
                "content": {"text": "ordinary-zone1-write"},
                "provenance": {"source_type": "user_stated", "purpose": "dash026-qa-matrix"},
            },
        )
        assert response.status_code == 202, response.text
        body = response.json()
        assert body["status"] == "accepted"
        assert body["zone5_written"] is False
        assert body["zone3_edges_written"] == []
        assert body["zone3_edges_failed"] == []

    def test_case0_ordinary_write_zone_hint_episodic_unaffected(self, client: TestClient) -> None:
        response = _post_write(
            client,
            {
                "session_id": f"s-{uuid.uuid4().hex[:8]}",
                "content": {"text": "ordinary-zone2-write", "zone_hint": "2-episodic"},
                "provenance": {"source_type": "user_stated", "purpose": "dash026-qa-matrix"},
            },
        )
        assert response.status_code == 202, response.text
        assert response.json()["zone5_written"] is False


class TestCase1SubjectsZeroGeneralFact:
    """Section 5 case 1: `|subjects|==0`, `subject_scope` populated -> `GeneralFact`."""

    def test_case1_subjects_zero_writes_general_fact_no_zone5(self, client: TestClient) -> None:
        # entity_refs is empty for |subjects|==0/|objects|==0, so the
        # routing gate (Section 4.2 step 0) only fires via the
        # zone_hint-normalizes-to-Zone-3 path -- an empty entity_refs
        # AND an unset zone_hint would instead fall through to the
        # ordinary Zone 1/2/4 branch untouched by this story.
        response = _post_write(
            client,
            _write_body(
                entity_refs=_entity_refs(subjects=(), objects=()),
                zone_hint="3-semantic",
                predicate=None,
                subject_scope="general-scope-placeholder",
            ),
        )
        assert response.status_code == 202, response.text
        body = response.json()
        assert body["status"] == "accepted"
        assert body["zone5_written"] is False
        assert body["zone3_edges_written"] == []
        assert body["zone3_edges_failed"] == []


class TestCase2SubjectsOneObjectsZero:
    """Section 5 case 2: `|subjects|==1`, `|objects|==0` -> Zone 5 only."""

    def test_case2_single_subject_no_objects_writes_zone5_only(self, client: TestClient) -> None:
        response = _post_write(
            client,
            _write_body(entity_refs=_entity_refs(subjects=("entity-a",), objects=())),
        )
        assert response.status_code == 202, response.text
        body = response.json()
        assert body["status"] == "accepted"
        assert body["zone5_written"] is True
        assert body["zone3_edges_written"] == []
        assert body["zone3_edges_failed"] == []


class TestCase3SingleSubjectIndexReverseFalse:
    """Section 5 case 3: `|subjects|==1`, `|objects|>=1`, `index_reverse=False` -> Zone 5 only."""

    def test_case3_single_subject_with_objects_index_reverse_false_zone5_only(
        self, client: TestClient
    ) -> None:
        response = _post_write(
            client,
            _write_body(
                entity_refs=_entity_refs(subjects=("entity-a",), objects=("entity-b",)),
                index_reverse=False,
            ),
        )
        assert response.status_code == 202, response.text
        body = response.json()
        assert body["zone5_written"] is True
        assert body["zone3_edges_written"] == [], (
            "case 3 is the exact case v1 of the design doc got wrong: a non-empty "
            "objects list with index_reverse=False must NOT produce a Zone 3 edge"
        )


class TestCase4SingleSubjectIndexReverseTrue:
    """Section 5 case 4: same as case 3 but `index_reverse=True` -> Zone 5 AND Zone 3."""

    def test_case4_single_subject_with_objects_index_reverse_true_zone5_and_zone3(
        self, client: TestClient
    ) -> None:
        response = _post_write(
            client,
            _write_body(
                entity_refs=_entity_refs(subjects=("entity-a",), objects=("entity-b",)),
                index_reverse=True,
            ),
        )
        assert response.status_code == 202, response.text
        body = response.json()
        assert body["status"] == "accepted"
        assert body["zone5_written"] is True
        assert len(body["zone3_edges_written"]) == 1
        assert body["zone3_edges_failed"] == []


class TestCase5MultiSubjectsWithObjects:
    """Section 5 case 5: `|subjects|>1`, `|objects|>=1` -> cross-product edges, no Zone 5."""

    def test_case5_multi_subject_cross_product_edges_no_zone5(self, client: TestClient) -> None:
        response = _post_write(
            client,
            _write_body(
                entity_refs=_entity_refs(subjects=("entity-a", "entity-b"), objects=("entity-c",)),
            ),
        )
        assert response.status_code == 202, response.text
        body = response.json()
        assert body["zone5_written"] is False, (
            "multiple subjects means the truth table's |subjects|==1 Zone 5 "
            "condition never applies, regardless of index_reverse"
        )
        assert len(body["zone3_edges_written"]) == 2, "2 subjects x 1 object == 2 edges"
        assert body["zone3_edges_failed"] == []


class TestCase6MultiSubjectsNoObjects:
    """Section 5 case 6: `|subjects|>1`, `|objects|==0` -> documented empty cross-product."""

    def test_case6_multi_subject_no_objects_empty_cross_product_no_error(
        self, client: TestClient
    ) -> None:
        response = _post_write(
            client,
            _write_body(entity_refs=_entity_refs(subjects=("entity-a", "entity-b"), objects=())),
        )
        assert response.status_code == 202, response.text
        body = response.json()
        assert body["status"] == "accepted", (
            "classify()'s own documented judgment call: an empty cross-product is "
            "not an error (SemanticEdgeRouting(edges=())), never a 4xx/5xx"
        )
        assert body["zone5_written"] is False
        assert body["zone3_edges_written"] == []
        assert body["zone3_edges_failed"] == []


class TestCase7MissingPredicate:
    """Section 5 case 7: missing/blank `predicate` when `|subjects|>=1` -> 400, no partial write.

    Uses `|subjects|>1` (not `|subjects|==1`) deliberately: with exactly
    one subject, `_do_write_zone3_5` (`app.py`) attempts the Zone 5
    `write_attribute` call BEFORE `classify()` ever runs (Section 4.2
    step 3 precedes step 4), so a `|subjects|==1` case exercises the Zone
    5 write path first and cannot isolate this SemanticEdge-construction
    validation in isolation. With `|subjects|>1`, Zone 5 is never
    attempted (the truth table's Zone 5 condition is `|subjects|==1`
    only), so `classify()`'s `SemanticEdge.__post_init__` blank-predicate
    `ValueError` is the only thing that can fire here.
    """

    def test_case7_missing_predicate_with_multi_subject_returns_400_invalid_request(
        self, client: TestClient
    ) -> None:
        response = _post_write(
            client,
            _write_body(
                entity_refs=_entity_refs(subjects=("entity-a", "entity-b"), objects=("entity-c",)),
                predicate=None,
            ),
        )
        assert response.status_code == 400, response.text
        assert response.json()["error"]["code"] == "INVALID_REQUEST"

    def test_case7_blank_predicate_with_multi_subject_returns_400_invalid_request(
        self, client: TestClient
    ) -> None:
        response = _post_write(
            client,
            _write_body(
                entity_refs=_entity_refs(subjects=("entity-a", "entity-b"), objects=("entity-c",)),
                predicate="",
            ),
        )
        assert response.status_code == 400, response.text
        assert response.json()["error"]["code"] == "INVALID_REQUEST"


class TestCase8MissingSubjectScope:
    """Section 5 case 8: missing/blank `subject_scope` when `|subjects|==0` -> 400."""

    def test_case8_missing_subject_scope_for_general_fact_returns_400(
        self, client: TestClient
    ) -> None:
        # zone_hint="3-semantic" is required to trigger the routing gate
        # here -- entity_refs is empty for |subjects|==0, so without an
        # explicit zone_hint the write would fall through to the
        # ordinary Zone 1/2/4 branch untouched by this story (see case 1).
        response = _post_write(
            client,
            _write_body(
                entity_refs=_entity_refs(subjects=(), objects=()),
                zone_hint="3-semantic",
                predicate=None,
                subject_scope=None,
            ),
        )
        assert response.status_code == 400, response.text
        assert response.json()["error"]["code"] == "INVALID_REQUEST"

    def test_case8_blank_subject_scope_for_general_fact_returns_400(
        self, client: TestClient
    ) -> None:
        response = _post_write(
            client,
            _write_body(
                entity_refs=_entity_refs(subjects=(), objects=()),
                zone_hint="3-semantic",
                predicate=None,
                subject_scope="",
            ),
        )
        assert response.status_code == 400, response.text
        assert response.json()["error"]["code"] == "INVALID_REQUEST"


class TestCase9ObjectOnlyEntityRefs:
    """Section 5 case 9: object-only `entity_refs` collapses to the `|subjects|==0` branch."""

    def test_case9_object_only_entity_refs_collapses_to_general_fact_branch(
        self, client: TestClient
    ) -> None:
        response = _post_write(
            client,
            _write_body(
                entity_refs=_entity_refs(subjects=(), objects=("entity-b",)),
                predicate=None,
                subject_scope="general-scope-placeholder",
            ),
        )
        assert response.status_code == 202, response.text
        body = response.json()
        assert body["zone5_written"] is False, (
            "objects are only meaningful paired with a subject -- an object-only "
            "entity_refs list must not silently produce a Zone 5 write"
        )
        assert body["zone3_edges_written"] == [], (
            "with |subjects|==0, classify() takes the GeneralFact branch, which "
            "produces no SemanticEdge -- confirms this is not silently dropped"
        )


class TestCase10PartialZone3FailureAfterZone5Success:
    """Section 5 case 10: Zone 5 succeeds, a Zone 3 edge write then fails -> 207 partial."""

    def test_case10_zone3_edge_failure_after_zone5_success_returns_207_partial(
        self, client: TestClient, real_postgres_app_context: AppContext
    ) -> None:
        # This case's own precondition is "Zone 5 already succeeded" --
        # under the currently-disclosed, pre-existing
        # sql_provenance_repository.py bug (every real Zone 3/5 write's
        # provenance append raises, independent of this story's own
        # code -- see this module's docstring), the real Zone 5 write
        # never reaches that precondition on its own. `write_attribute`
        # is therefore ALSO patched here, to a plain success (no
        # side_effect), so this case can isolate and prove its own
        # actual subject -- the 207-partial contract for a Zone 3 edge
        # failure AFTER a successful Zone 5 write -- independent of that
        # unrelated, already-flagged infrastructure defect.
        with patch.object(
            real_postgres_app_context.conflict_aware_entity_repository,
            "write_attribute",
        ), patch.object(
            real_postgres_app_context.semantic_repository,
            "insert_edge",
            side_effect=ZoneRepositoryError(
                zone="3-semantic",
                reason="simulated Zone 3 edge write failure (DASH-STORY-026-QA case 10)",
            ),
        ):
            response = _post_write(
                client,
                _write_body(
                    entity_refs=_entity_refs(subjects=("entity-a",), objects=("entity-b",)),
                    index_reverse=True,
                ),
            )
        assert response.status_code == 207, response.text
        body = response.json()
        assert body["status"] == "partial"
        assert body["zone5_written"] is True, (
            "Zone 5 write happens before the Zone 3 loop (step 3 before step 4) "
            "and must have already committed by the time the edge write fails"
        )
        assert body["zone3_edges_written"] == []
        assert len(body["zone3_edges_failed"]) == 1, (
            "the single attempted edge must be reported as failed, not silently "
            "dropped or reported as full success"
        )


class TestCase11Zone5FailureBeforeAnyZone3Attempt:
    """Section 5 case 11: Zone 5 write itself fails -> 503, no Zone 3 attempt."""

    def test_case11_zone5_failure_returns_503_and_no_zone3_attempted(
        self, client: TestClient, real_postgres_app_context: AppContext
    ) -> None:
        with patch.object(
            real_postgres_app_context.conflict_aware_entity_repository,
            "write_attribute",
            side_effect=ZoneRepositoryError(
                zone="5-entity",
                reason="simulated Zone 5 write failure (DASH-STORY-026-QA case 11)",
            ),
        ):
            response = _post_write(
                client,
                _write_body(
                    entity_refs=_entity_refs(subjects=("entity-a",), objects=("entity-b",)),
                    index_reverse=True,
                ),
            )
        assert response.status_code == 503, response.text
        body = response.json()
        assert body["error"]["code"] == "WRITE_REJECTED_NOT_DURABLE"
        # Section 5 case 11: this response is the EXISTING `except
        # ZoneRepositoryError` error shape, never a WriteReceipt --
        # confirms no Zone 3 attempt was made (no zone3_edges_* fields to
        # even inspect on this error envelope).
        assert "zone3_edges_written" not in body
        assert "zone3_edges_failed" not in body


class TestCase12MissingRetrievalContextHash:
    """Section 5 case 12: missing/blank `retrieval_context_hash` for a Zone 3/5 write -> 400."""

    def test_case12_missing_retrieval_context_hash_returns_400(self, client: TestClient) -> None:
        response = _post_write(
            client,
            _write_body(
                entity_refs=_entity_refs(subjects=("entity-a",), objects=()),
                retrieval_context_hash=None,
            ),
        )
        assert response.status_code == 400, response.text
        assert response.json()["error"]["code"] == "INVALID_REQUEST"

    def test_case12_blank_retrieval_context_hash_is_rejected_by_the_wire_pattern_422(
        self, client: TestClient
    ) -> None:
        """A present-but-blank string still goes through Pydantic's own
        `Field(pattern=...)` check (an empty string does not match
        `^[0-9a-f]{64}$`), so it is rejected at 422 before `_do_write`
        even runs -- distinct from the wholly-absent-field 400 case
        above, which is domain-layer validation. Both are legitimate
        "missing/blank" outcomes per Section 4.2 step 5b/step 5's own
        text; this test documents which status code each actually
        produces so a caller is not surprised."""
        response = _post_write(
            client,
            {
                "session_id": f"s-{uuid.uuid4().hex[:8]}",
                "content": {
                    "text": "blank-hash-case",
                    "entity_refs": _entity_refs(subjects=("entity-a",), objects=()),
                    "predicate": "predicate-placeholder",
                },
                "provenance": {
                    "source_type": "user_stated",
                    "purpose": "dash026-qa-matrix",
                    "retrieval_context_hash": "",
                },
            },
        )
        assert response.status_code == 422, response.text


class TestCase13MalformedRetrievalContextHash:
    """Section 5 case 13: malformed `retrieval_context_hash` -> 422 from Pydantic's own validation."""

    @pytest.mark.parametrize(
        "malformed_hash",
        [
            "too-short",
            "g" * 64,  # non-hex character
            ("a" * 64).upper(),  # uppercase hex, pattern requires lowercase
            "a" * 63,  # one char short
            "a" * 65,  # one char over
        ],
        ids=["too-short", "non-hex-char", "uppercase-hex", "63-chars", "65-chars"],
    )
    def test_case13_malformed_retrieval_context_hash_returns_422(
        self, client: TestClient, malformed_hash: str
    ) -> None:
        response = _post_write(
            client,
            _write_body(
                entity_refs=_entity_refs(subjects=("entity-a",), objects=()),
                retrieval_context_hash=malformed_hash,
            ),
        )
        assert response.status_code == 422, response.text
        # This is FastAPI/Pydantic's own Field(pattern=...) request-schema
        # validation -- distinct from the domain-constructor 400s above --
        # so it never reaches _do_write at all.

    def test_case13_well_formed_hash_is_accepted_by_the_wire_boundary(
        self, client: TestClient
    ) -> None:
        """Confirms the positive control: a syntactically well-formed 64-char
        lowercase hex value is never itself rejected by the pattern check --
        isolating case 13's malformed variants as the actual cause of the 422s
        above, not an overly strict pattern rejecting valid input too."""
        response = _post_write(
            client,
            _write_body(entity_refs=_entity_refs(subjects=("entity-a",), objects=())),
        )
        assert response.status_code != 422, response.text
