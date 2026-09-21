"""QA subtask for DASH-STORY-023: real FastAPI wire/API layer (FR-014, closing DSHN-60).

FR-014 (verbatim, SRS.md Section 3.1): "The system SHALL expose
docs/phase-1.5-api/openapi.yaml's 32 already-designed operationIds
through a real, running host application -- gRPC primary, FastAPI REST
gateway secondary (HLD Section 2 Communication Matrix, ADR-004) -- so
that a host AI can actually call Dashanan over the network, closing the
DSHN-60 gap `composition_root.py`'s own module docstring documents."

ACCEPTANCE CRITERIA UNDER TEST:
  AC-023-1: every one of the 32 operationIds resolves to a callable
      handler with no unimplemented (501/placeholder) route.
  AC-023-2: the host constructs every MemoryOrchestrator/
      SqlProvenanceRepository/SqlEpisodicRepository instance through
      `composition_root.py`, never a bare constructor call.
  AC-023-3: DELETE /v1/tenants/{id}/subjects/{subject_id} invokes the
      real, Zone-8-only SubjectErasureCascadeService, never a stub and
      never the wider UnifiedSubjectErasureOrchestrator.

TEST REQUIREMENTS traceability: this module enumerates every AC as a
test scenario (happy path, boundary, adverse) before any assertion code,
per the dev_prompt's own "Let's think step by step" instruction --
sections below are ordered exactly that way.
"""

from __future__ import annotations

import inspect
import time
import uuid
from collections.abc import Iterator

import jwt
import pytest
from fastapi.testclient import TestClient

from dashanan.api import app as app_module
from dashanan.api.composition import AppContext, build_app_context
from dashanan.domain.subject_erasure import ErasureCascadeAccepted, SubjectErasureRequest

_JWT_KEY = bytes(range(32))
_TENANT_CREDENTIAL_KEY = bytes(range(32, 64))
_TENANT_ID = "tenant-a"


def _token(
    scope: str,
    *,
    aud: str = "dashanan-data-plane",
    tenant_id: str = _TENANT_ID,
    exp_delta: int = 300,
) -> str:
    now = int(time.time())
    return jwt.encode(
        {"tenant_id": tenant_id, "scope": scope, "aud": aud, "iat": now, "exp": now + exp_delta},
        _JWT_KEY,
        algorithm="HS256",
    )


def _auth(scope: str, **kwargs: object) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(scope, **kwargs)}"}


@pytest.fixture
def app_context() -> AppContext:
    """A real `AppContext` with no live Postgres (Shape A / degraded-Shape-B branch)."""
    return build_app_context(
        tenant_credential_signing_key=_TENANT_CREDENTIAL_KEY,
        jwt_signing_key=_JWT_KEY,
        postgres_connection=None,
    )


@pytest.fixture
def client(app_context: AppContext) -> Iterator[TestClient]:
    app = app_module.create_app(app_context)
    with TestClient(app) as test_client:
        yield test_client


def _write_one_item(client: TestClient) -> str:
    response = client.post(
        "/memory/write",
        json={
            "session_id": "s1",
            "content": {"text": "hello world"},
            "provenance": {"source_type": "user_stated", "purpose": "qa-fixture"},
        },
        headers={**_auth("memory:write"), "Idempotency-Key": str(uuid.uuid4())},
    )
    assert response.status_code == 202, response.text
    return response.json()["write_id"]


# --------------------------------------------------------------------------
# AC-023-1: every operationId is a real, callable, non-501 handler.
# --------------------------------------------------------------------------


def test_all_32_operation_ids_are_registered_on_the_app(client: TestClient) -> None:
    """AC-023-1 happy path: every openapi.yaml operationId has a matching real route."""
    expected_operation_ids = {
        "assembleContext", "assembleContextSimple", "searchMemory", "getItem",
        "updateItem", "deleteItem", "getItemProvenance", "explainItemScore",
        "writeMemory", "writeMemoryBatch", "batchGetItems", "getWriteStatus",
        "listZones", "getZoneDetail", "getZoneConfig", "putZoneConfig",
        "forceZoneSweep", "listZoneItems", "reindexZone", "getScoringConfig",
        "putScoringConfig", "getJobStatus", "createTenant", "listTenants",
        "getTenant", "updateTenant", "deleteTenant", "eraseSubject",
        "exportSubjectData", "getLiveness", "getReadiness", "getMetrics",
    }
    assert len(expected_operation_ids) == 32
    registered = {
        route.operation_id
        for route in client.app.routes
        if getattr(route, "operation_id", None)
    }
    missing = expected_operation_ids - registered
    assert not missing, f"operationIds missing a real route: {missing}"


def test_every_operation_id_handler_returns_a_response_never_501(client: TestClient) -> None:
    """AC-023-1 boundary: exercise every operationId; none may return 501 (unimplemented)."""
    write_id = _write_one_item(client)
    tenant_scope = _auth("admin:tenants", aud="dashanan-control-plane")
    zones_scope = _auth("admin:zones", aud="dashanan-control-plane")
    read_scope = _auth("context:read")
    write_scope = {**_auth("memory:write"), "Idempotency-Key": str(uuid.uuid4())}

    calls = [
        ("POST", "/context/assemble", read_scope,
         {"json": {"session_id": "s1", "task": "hi", "token_budget": 1000}}),
        ("GET", "/context/assemble", read_scope,
         {"params": {"session_id": "s1", "q": "hi", "token_budget": 1000}}),
        ("POST", "/memory/search", read_scope, {"json": {"query": "hi"}}),
        ("GET", f"/items/{write_id}", read_scope, {}),
        ("PATCH", f"/items/{write_id}", write_scope,
         {"json": {"content": {"text": "x"}, "provenance": {"source_type": "user_stated", "purpose": "p"}}}),
        ("GET", f"/items/{write_id}/provenance", read_scope, {}),
        ("POST", f"/items/{write_id}/score:explain", read_scope, {}),
        ("POST", "/items:batchGet", read_scope, {"json": {"item_ids": [write_id]}}),
        ("POST", "/memory/write:batch",
         {**_auth("memory:write"), "Idempotency-Key": str(uuid.uuid4())},
         {"json": {"items": [{
             "session_id": "s1", "content": {"text": "y"},
             "provenance": {"source_type": "user_stated", "purpose": "p"},
         }]}}),
        ("GET", f"/writes/{write_id}", _auth("memory:write"), {}),
        ("GET", "/zones", zones_scope, {}),
        ("GET", "/zones/1-working", zones_scope, {}),
        ("GET", "/zones/1-working/config", zones_scope, {}),
        ("PUT", "/zones/1-working/config", zones_scope, {"json": {
            "zone": "1-working", "promote_threshold": 0.7, "compress_threshold": 0.4,
            "archive_threshold": 0.25, "capacity_cap": 200, "adapter_binding": "x",
        }}),
        ("POST", "/zones/1-working/sweep", zones_scope, {}),
        ("GET", "/zones/1-working/items", zones_scope, {}),
        ("POST", "/zones/6-retrieval-index/reindex", zones_scope, {}),
        ("GET", "/config/scoring", zones_scope, {}),
        ("PUT", "/config/scoring", zones_scope, {"json": {
            "w1": 0.3, "w2": 0.2, "w3": 0.2, "w4": 0.15, "w5": 0.1, "w6": 0.05,
            "compress_threshold": 0.45,
        }}),
        ("GET", "/healthz", {}, {}),
        ("GET", "/readyz", {}, {}),
        ("GET", "/metrics", zones_scope, {}),
    ]

    for method, path, headers, kwargs in calls:
        response = client.request(method, path, headers=headers, **kwargs)
        assert response.status_code != 501, f"{method} {path} returned 501 (placeholder route)"
        assert response.status_code < 500 or response.status_code == 503, (
            f"{method} {path} unexpectedly errored: {response.status_code} {response.text}"
        )

    # Tenant/job/erasure family (needs a created tenant + job first).
    create_resp = client.post(
        "/tenants", json={"display_name": "Acme"}, headers=tenant_scope
    )
    assert create_resp.status_code == 201
    created_tenant_id = create_resp.json()["tenant_id"]
    assert client.get("/tenants", headers=tenant_scope).status_code != 501
    assert client.get(f"/tenants/{created_tenant_id}", headers=tenant_scope).status_code != 501
    assert client.patch(
        f"/tenants/{created_tenant_id}", json={"display_name": "Acme2"}, headers=tenant_scope
    ).status_code != 501
    job_resp = client.post("/zones/1-working/sweep", headers=zones_scope)
    job_id = job_resp.json()["job_id"]
    assert client.get(f"/jobs/{job_id}", headers=zones_scope).status_code != 501
    assert client.get(
        f"/tenants/{created_tenant_id}/subjects/subj-1/export", headers=tenant_scope
    ).status_code != 501
    erase_resp = client.delete(
        f"/tenants/{created_tenant_id}/subjects/subj-1", headers=tenant_scope
    )
    assert erase_resp.status_code != 501
    delete_resp = client.delete(f"/tenants/{created_tenant_id}", headers=tenant_scope)
    assert delete_resp.status_code != 501


def test_unauthenticated_request_never_reaches_a_placeholder_it_is_rejected_401(
    client: TestClient,
) -> None:
    """AC-023-1 adverse: no bearer token -> 401, not 501/404 (the route IS real and wired)."""
    response = client.post("/context/assemble", json={"session_id": "s", "task": "t", "token_budget": 10})
    assert response.status_code == 401


# --------------------------------------------------------------------------
# AC-023-2: MemoryOrchestrator/SqlProvenanceRepository/SqlEpisodicRepository
# are constructed only via composition_root.py's own builders.
# --------------------------------------------------------------------------


def test_composition_module_never_bare_constructs_the_three_controlled_types() -> None:
    """AC-023-2 happy path: grep-verifiable -- no bare `ClassName(` constructor call.

    Every one of `MemoryOrchestrator(`/`SqlProvenanceRepository(`/
    `SqlEpisodicRepository(` in `api/composition.py`'s source must be a
    `composition_root.build_*(` call, never a direct constructor call.
    """
    from dashanan.api import composition as composition_module

    source = inspect.getsource(composition_module)
    for forbidden in ("MemoryOrchestrator(", "SqlProvenanceRepository(", "SqlEpisodicRepository("):
        assert forbidden not in source, (
            f"api/composition.py bare-constructs {forbidden!r} instead of routing "
            "through composition_root.py (AC-023-2 violation)"
        )
    assert "composition_root.build_memory_orchestrator(" in source
    assert "composition_root.build_provenance_repository(" in source
    assert "composition_root.build_episodic_repository(" in source


def test_app_module_never_imports_the_three_controlled_types_directly() -> None:
    """AC-023-2 boundary: `api/app.py` must not import the controlled classes at all."""
    source = inspect.getsource(app_module)
    for forbidden in (
        "from dashanan.application.memory_orchestrator import MemoryOrchestrator",
        "from dashanan.infrastructure.sql_provenance_repository import SqlProvenanceRepository",
        "from dashanan.infrastructure.sql_episodic_repository import SqlEpisodicRepository",
    ):
        assert forbidden not in source


def test_build_app_context_forwards_verify_privileges_true_to_all_four_real_builders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-023-2 adverse (attempt-2 fix): the DSHN-55 check is inherited by construction.

    Spies on `composition_root.build_provenance_repository`/
    `build_episodic_repository`/`build_conflict_aware_semantic_repository`/
    `build_conflict_aware_entity_memory_repository` and asserts each is
    called with `verify_privileges=True` (composition_root's own secure
    default) when `build_app_context` composes the real production
    Postgres-backed branch -- never `verify_privileges=False`, which
    would reopen DSHN-55 at this story's one new call site. A fake
    connection double stands in for the real `psycopg.Connection` so
    this stays a unit test (must-not-deviate item 1: no real Postgres).
    """
    from unittest.mock import MagicMock

    from dashanan.infrastructure import composition_root as composition_root_module

    captured_kwargs: dict[str, dict[str, object]] = {}

    def _stub(name: str):
        def _wrapped(*args: object, **kwargs: object) -> object:
            captured_kwargs[name] = kwargs
            return MagicMock(name=f"stub-{name}")

        return _wrapped

    for builder_name in (
        "build_provenance_repository",
        "build_episodic_repository",
        "build_conflict_aware_semantic_repository",
        "build_conflict_aware_entity_memory_repository",
    ):
        monkeypatch.setattr(composition_root_module, builder_name, _stub(builder_name))

    fake_connection = object()
    build_app_context(
        tenant_credential_signing_key=_TENANT_CREDENTIAL_KEY,
        jwt_signing_key=_JWT_KEY,
        postgres_connection=fake_connection,
    )

    for builder_name in (
        "build_provenance_repository",
        "build_episodic_repository",
        "build_conflict_aware_semantic_repository",
        "build_conflict_aware_entity_memory_repository",
    ):
        kwargs = captured_kwargs.get(builder_name)
        assert kwargs is not None, f"{builder_name} was never called"
        assert kwargs.get("verify_privileges", True) is True, (
            f"{builder_name} was called with verify_privileges="
            f"{kwargs.get('verify_privileges')!r} -- the real production Postgres "
            "path must inherit composition_root's secure-by-default posture, "
            "never opt out at this call site (AC-023-2, DSHN-55)."
        )


def test_app_context_orchestrator_is_the_real_composition_root_instance(
    app_context: AppContext,
) -> None:
    """AC-023-2 adverse: the wired orchestrator genuinely enforces HLD Threat S-1.

    `composition_root.build_memory_orchestrator` always supplies a signing
    key (module docstring); calling `assemble_context` with a forged
    `tenant_credential` must be rejected, proving this is the real,
    security-enforcing object, not an unverified bare `MemoryOrchestrator()`.
    """
    from dashanan.application.context_assembly_request import ContextAssemblyRequest
    from dashanan.domain.tenant_credential import TenantAuthenticationError

    forged_request = ContextAssemblyRequest(
        tenant_id=_TENANT_ID, session_id="s", task="t", token_budget=100, tenant_credential=None
    )
    with pytest.raises(TenantAuthenticationError):
        app_context.memory_orchestrator.assemble_context(forged_request)


# --------------------------------------------------------------------------
# AC-023-3: eraseSubject invokes the real SubjectErasureCascadeService.
# --------------------------------------------------------------------------


class _SpySubjectErasureCascadeService:
    """A real Protocol-shaped collaborator (not the domain class itself) that records calls.

    Used to prove the ENDPOINT CODE genuinely calls
    `AppContext.subject_erasure_service.request_erasure` -- a dependency-
    injection unit test, not a test of the fake itself.
    """

    def __init__(self) -> None:
        self.calls: list[SubjectErasureRequest] = []

    def request_erasure(self, request: SubjectErasureRequest) -> ErasureCascadeAccepted:
        self.calls.append(request)
        from datetime import UTC, datetime

        return ErasureCascadeAccepted.for_request(
            request, job_id="job-123", accepted_at=datetime.now(UTC)
        )


def test_erase_subject_invokes_the_real_subject_erasure_cascade_service(
    app_context: AppContext,
) -> None:
    """AC-023-3 happy path: DELETE .../subjects/{id} calls request_erasure exactly once."""
    spy = _SpySubjectErasureCascadeService()
    app_context.subject_erasure_service = spy  # type: ignore[assignment]
    app_context.postgres_available = True
    app = app_module.create_app(app_context)
    with TestClient(app) as client:
        response = client.delete(
            f"/tenants/{_TENANT_ID}/subjects/subject-99",
            headers=_auth("admin:tenants", aud="dashanan-control-plane"),
        )
    assert response.status_code == 202
    assert response.json()["job_id"] == "job-123"
    assert len(spy.calls) == 1
    assert spy.calls[0].tenant_id == _TENANT_ID
    assert spy.calls[0].subject_id == "subject-99"


def test_erase_subject_never_uses_the_wider_unified_orchestrator(app_context: AppContext) -> None:
    """AC-023-3 boundary (must-not-deviate item 3): the endpoint's own source stays scoped.

    `app.py`'s `erase_subject` handler must reference only
    `subject_erasure_service`, never `UnifiedSubjectErasureOrchestrator`
    -- this story does not present a wider cascade than Zone 8 alone.
    """
    source = inspect.getsource(app_module)
    assert "import UnifiedSubjectErasureOrchestrator" not in source
    assert "ctx.subject_erasure_service.request_erasure(" in source


def test_erase_subject_degrades_when_postgres_unavailable_never_fakes_success(
    client: TestClient,
) -> None:
    """AC-023-3 adverse: with no live Postgres, this degrades to 503 -- never a fake 202."""
    response = client.delete(
        f"/tenants/{_TENANT_ID}/subjects/subject-1",
        headers=_auth("admin:tenants", aud="dashanan-control-plane"),
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SERVICE_DEGRADED"


# --------------------------------------------------------------------------
# Cross-cutting: auth, scope enforcement, FR-010 boundary, honest gaps.
# --------------------------------------------------------------------------


def test_insufficient_scope_is_rejected_403_not_silently_upgraded(client: TestClient) -> None:
    response = client.post(
        "/context/assemble",
        json={"session_id": "s", "task": "t", "token_budget": 10},
        headers=_auth("memory:write"),
    )
    assert response.status_code == 403


def test_write_without_resolvable_source_type_rejected_422_fr010(client: TestClient) -> None:
    response = client.post(
        "/memory/write",
        json={"session_id": "s1", "content": {"text": "x"}, "provenance": {"purpose": "p"}},
        headers={**_auth("memory:write"), "Idempotency-Key": str(uuid.uuid4())},
    )
    assert response.status_code == 422


def test_zone3_5_entity_refs_write_without_retrieval_context_hash_rejected_400(
    client: TestClient,
) -> None:
    """DASH-STORY-026 (FR-013, GitHub #23): Zone 3/5 writes are reachable now, gated by
    a required provenance.retrieval_context_hash rather than unconditionally rejected.

    Prior to DASH-STORY-026, any entity_refs-bearing write was unconditionally rejected
    with 422 ZONE_3_5_PREDICATE_UNRESOLVABLE (that response code no longer exists in
    app.py). This exact payload is missing provenance.retrieval_context_hash, so it is
    now correctly rejected 400 INVALID_REQUEST for that reason -- a real, more specific
    validation failure than the old blanket rejection, not merely a status-code rename.
    """
    response = client.post(
        "/memory/write",
        json={
            "session_id": "s1",
            "content": {"text": "x", "entity_refs": [{"role": "subject", "entity_id": "e1"}]},
            "provenance": {"source_type": "user_stated", "purpose": "p"},
        },
        headers={**_auth("memory:write"), "Idempotency-Key": str(uuid.uuid4())},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


def test_tenant_scope_mismatch_rejected_403_never_404(client: TestClient) -> None:
    other_tenant_scope = _auth("admin:tenants", aud="dashanan-data-plane", tenant_id="tenant-b")
    response = client.get(f"/tenants/{_TENANT_ID}", headers=other_tenant_scope)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "TENANT_SCOPE_MISMATCH"


def test_platform_operator_credential_exempt_from_tenant_scope_match(client: TestClient) -> None:
    platform_scope = _auth("admin:tenants", aud="dashanan-control-plane", tenant_id="platform-op")
    create = client.post("/tenants", json={"display_name": "Acme"}, headers=platform_scope)
    tenant_id = create.json()["tenant_id"]
    response = client.get(f"/tenants/{tenant_id}", headers=platform_scope)
    assert response.status_code == 200


def test_readiness_report_reflects_postgres_availability(client: TestClient) -> None:
    response = client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert body["adapters"]["episodic"] == "degraded"
    assert body["adapters"]["working"] == "healthy"


# --------------------------------------------------------------------------
# DSHN-75 (CWE-209): a caught ZoneRepositoryError's raw driver/SQL exception
# text must never be echoed verbatim into a 503 ErrorResponse body -- only
# a generic, zone-scoped message may reach the client (security.py's own
# TenantAuthenticationError precedent: log the real reason server-side,
# return a generic message to the caller).
# --------------------------------------------------------------------------

_SENSITIVE_DRIVER_TEXT = (
    'psycopg.OperationalError: connection to server at "10.0.0.5" port 5432 '
    'failed: FATAL: password authentication failed for user "dashanan_admin"'
)


class _RaisingProvenanceRepository:
    """A real Protocol-shaped double that always fails like a real driver would."""

    def find_by_item_id(self, tenant_id: str, item_id: str) -> list[object]:
        from dashanan.domain.exceptions import ZoneRepositoryError

        raise ZoneRepositoryError("7-provenance", _SENSITIVE_DRIVER_TEXT)


class _RaisingWorkingRepository:
    """A real Protocol-shaped double that always fails like a real driver would."""

    def put(self, tenant_id: str, session_id: str, item_id: str, text: str, token_count: int) -> None:
        from dashanan.domain.exceptions import ZoneRepositoryError

        raise ZoneRepositoryError("1-working", _SENSITIVE_DRIVER_TEXT)


def test_get_item_provenance_never_leaks_raw_driver_text_on_503(
    app_context: AppContext,
) -> None:
    """DSHN-75: a ZoneRepositoryError's raw `reason` must not reach the response body."""
    app = app_module.create_app(app_context)
    with TestClient(app) as client:
        write_id = _write_one_item(client)

        app_context.postgres_available = True
        app_context.provenance_repository = _RaisingProvenanceRepository()  # type: ignore[assignment]

        response = client.get(
            f"/items/{write_id}/provenance", headers=_auth("context:read")
        )

    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "PROVENANCE_UNAVAILABLE"
    raw_body_text = response.text
    assert _SENSITIVE_DRIVER_TEXT not in raw_body_text
    assert "dashanan_admin" not in raw_body_text
    assert "10.0.0.5" not in raw_body_text
    assert "password authentication failed" not in raw_body_text
    assert "7-provenance" in body["error"]["message"]


def test_write_memory_never_leaks_raw_driver_text_on_503(app_context: AppContext) -> None:
    """DSHN-75: the same guarantee for the writeMemory/_do_write 503 path."""
    app_context.working_repository = _RaisingWorkingRepository()  # type: ignore[assignment]
    app = app_module.create_app(app_context)
    with TestClient(app) as client:
        response = client.post(
            "/memory/write",
            json={
                "session_id": "s1",
                "content": {"text": "hello world"},
                "provenance": {"source_type": "user_stated", "purpose": "qa-fixture"},
            },
            headers={**_auth("memory:write"), "Idempotency-Key": str(uuid.uuid4())},
        )

    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "WRITE_REJECTED_NOT_DURABLE"
    raw_body_text = response.text
    assert _SENSITIVE_DRIVER_TEXT not in raw_body_text
    assert "dashanan_admin" not in raw_body_text
    assert "10.0.0.5" not in raw_body_text
    assert "password authentication failed" not in raw_body_text
    assert "working" in body["error"]["message"]


def test_write_memory_batch_never_leaks_raw_driver_text_on_503(
    app_context: AppContext,
) -> None:
    """DSHN-75: the same guarantee via the writeMemoryBatch operationId (shared `_do_write`)."""
    app_context.working_repository = _RaisingWorkingRepository()  # type: ignore[assignment]
    app = app_module.create_app(app_context)
    with TestClient(app) as client:
        response = client.post(
            "/memory/write:batch",
            json={"items": [{
                "session_id": "s1", "content": {"text": "hello world"},
                "provenance": {"source_type": "user_stated", "purpose": "qa-fixture"},
            }]},
            headers={**_auth("memory:write"), "Idempotency-Key": str(uuid.uuid4())},
        )

    assert response.status_code == 202
    body = response.json()
    receipt = body["receipts"][0]
    assert receipt["status"] == "rejected"
    raw_body_text = response.text
    assert _SENSITIVE_DRIVER_TEXT not in raw_body_text
    assert "dashanan_admin" not in raw_body_text
    assert "10.0.0.5" not in raw_body_text
    assert "password authentication failed" not in raw_body_text
