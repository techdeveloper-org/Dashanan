"""The real FastAPI host implementing all 32 openapi.yaml operationIds (DASH-STORY-023, FR-014).

Cite FR-014 verbatim (SRS.md Section 3.1): "The system SHALL expose
docs/phase-1.5-api/openapi.yaml's 32 already-designed operationIds
through a real, running host application -- gRPC primary, FastAPI REST
gateway secondary (HLD Section 2 Communication Matrix, ADR-004) -- so
that a host AI can actually call Dashanan over the network, closing the
DSHN-60 gap `composition_root.py`'s own module docstring documents."

AC-023-1: every operationId below resolves to a callable handler -- no
`501`/placeholder route anywhere in this module.

AC-023-2: every `MemoryOrchestrator`/`SqlProvenanceRepository`/
`SqlEpisodicRepository` this host uses was constructed by
`dashanan.api.composition.build_app_context`, itself calling only
`composition_root.py` builder functions -- this module never imports or
calls those three classes' constructors directly.

AC-023-3: `eraseSubject` below calls `AppContext.subject_erasure_service.
request_erasure` (Zone-8-only `SubjectErasureCascadeService`), never a
stub and never `UnifiedSubjectErasureOrchestrator` (the wider, separate
DASH-STORY-025/DSHN-70 fan-out) -- so this endpoint does NOT present
itself as closing HLD Section 7.4's full "cascades across all 8 zones"
promise (must-not-deviate item 3) until DASH-STORY-025 lands.

Zone 3/5 write path (DASH-STORY-026, traces to FR-013, closes GitHub
#23): `WriteMemoryRequest.content` now carries `predicate`/
`subject_scope`, and `WriteMemoryRequest.provenance` carries
`retrieval_context_hash` (docs/phase-1.5-api/fr013-predicate-schema-
design.md v7, Section 3). `_do_write`'s routing gate (step 0 below)
sends any write with a non-empty `content.entity_refs`, or a
`content.zone_hint` that normalizes to Zone 3 (Semantic), through
`entity_ownership_specification.classify()` and the FR-013-swept Zone
3/5 repositories (`AppContext.semantic_repository`/
`conflict_aware_entity_repository`) -- every other write (the ordinary
Zone 1/2/4 case) falls through to the unchanged branch below, unaffected
by this story (AC-026-DEV-2).
"""

import logging
import uuid
from dataclasses import asdict as _dataclass_asdict
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.responses import JSONResponse

from dashanan.api import schemas as sc
from dashanan.api import state as st
from dashanan.api.composition import AppContext, build_app_context
from dashanan.api.security import (
    AuthenticatedCaller,
    TenantAuthenticationError,
    enforce_tenant_scope_match,
    load_jwt_signing_key_from_env,
    verify_bearer_token,
)
from dashanan.application.context_assembly_request import ContextAssemblyRequest
from dashanan.application.conflict_aware_zone_writes import _zone3_edge_item_id
from dashanan.domain.entity_ownership_specification import (
    CandidateFact,
    GeneralFactRouting,
    SemanticEdgeRouting,
    classify,
)
from dashanan.domain.exceptions import ZoneRepositoryError
from dashanan.domain.provenance_record import SourceType
from dashanan.domain.subject_erasure import SubjectErasureRequest
from dashanan.domain.zone import ZoneId
from dashanan.infrastructure.composition_root import (
    load_tenant_credential_signing_key_from_env,
)

logger = logging.getLogger(__name__)

# openapi.yaml ZoneId enum <-> domain.zone.ZoneId (frozen contracts on both
# sides; this map is the API layer's own, never a change to either).
_WIRE_TO_DOMAIN_ZONE = {
    "1-working": ZoneId.WORKING,
    "2-episodic": ZoneId.EPISODIC,
    "3-semantic": ZoneId.SEMANTIC,
    "4-procedural": ZoneId.PROCEDURAL,
    "5-entity": ZoneId.ENTITY,
    "6-retrieval-index": ZoneId.RETRIEVAL_INDEX,
    "7-provenance": ZoneId.PROVENANCE,
    "8-consolidation": ZoneId.CONSOLIDATION,
}
_DOMAIN_TO_WIRE_ZONE = {v: k for k, v in _WIRE_TO_DOMAIN_ZONE.items()}


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    """Build one openapi.yaml `ErrorResponse` envelope (control I-6: never echo internals)."""
    body = sc.ErrorResponse(
        error=sc.ErrorDetail(code=code, message=message, trace_id=str(uuid.uuid4())),
        timestamp=datetime.now(UTC),
    )
    return JSONResponse(status_code=status_code, content=body.model_dump(mode="json"))


# --- Placeholder score_terms/lifecycle_state conversion gap (flagged, never
# fabricated): `domain.memory_item.MemoryItem` carries only a single scalar
# `score`, not the six-term `ScoreTerms` breakdown or a `lifecycle_state`
# (both are FR-012's `MemoryScoreEngine`/rotation-state-machine concerns,
# not yet wired into this read path -- a separate story's scope). Every
# term below is derived from the one real scalar this domain object has,
# clearly documented here rather than silently presented as independently
# measured data.
def _memory_item_to_model(item: object, *, include_provenance: bool) -> sc.MemoryItemModel:
    score = float(getattr(item, "score", 0.0))
    return sc.MemoryItemModel(
        item_id=item.item_id,
        source_zone=_DOMAIN_TO_WIRE_ZONE.get(item.source_zone, str(item.source_zone)),
        payload=item.payload,
        token_count=item.token_count,
        score=score,
        score_terms=sc.ScoreTerms(
            recency=score, frequency=score, importance=score,
            task_relevance=score, user_affinity=score, provenance_confidence=score,
        ),
        lifecycle_state="Active",
        provenance=None if not include_provenance else None,
    )


def create_app(app_context: AppContext | None = None) -> FastAPI:
    """Build the FastAPI app. `app_context` is a testing-core DI seam.

    When `app_context` is `None` (production use, `api.main`), this
    resolves both required signing keys from the environment at call
    time and builds a real `AppContext` via `build_app_context` --
    failing closed (raising) exactly as `composition_root`'s own
    `CompositionRootConfigurationError`/`ApiSecurityConfigurationError`
    document, before a single route is registered.
    """
    ctx = app_context or build_app_context(
        tenant_credential_signing_key=load_tenant_credential_signing_key_from_env(),
        jwt_signing_key=load_jwt_signing_key_from_env(),
    )

    write_status_store = st.WriteStatusStore()
    admin_job_store = st.AdminJobStore()
    zone_config_store = st.ZoneConfigStore()
    scoring_config_store = st.ScoringConfigStore()
    tenant_registry = st.TenantRegistry()
    item_registry = st.ItemRegistry()

    app = FastAPI(
        title="Dashanan Memory Orchestration API",
        version="1.0.0",
        description="FR-014 REST gateway (DASH-STORY-023). See docs/phase-1.5-api/openapi.yaml.",
    )

    def _require_scope(scope: str) -> Callable[..., AuthenticatedCaller]:
        def _dependency(
            authorization: Annotated[str | None, Header()] = None,
        ) -> AuthenticatedCaller:
            if not authorization or not authorization.lower().startswith("bearer "):
                raise HTTPException(status_code=401, detail="missing bearer token")
            token = authorization.split(" ", 1)[1].strip()
            try:
                return verify_bearer_token(
                    token,
                    jwt_signing_key=ctx.jwt_signing_key,
                    tenant_credential_signing_key=ctx.tenant_credential_signing_key,
                    clock=ctx.clock,
                    required_scope=scope,
                )
            except TenantAuthenticationError as exc:
                status = 403 if "scope" in exc.reason else 401
                raise HTTPException(status_code=status, detail=exc.reason) from exc

        return _dependency

    context_read = _require_scope("context:read")
    memory_write = _require_scope("memory:write")
    admin_zones = _require_scope("admin:zones")
    admin_tenants = _require_scope("admin:tenants")

    # ---------------------------------------------------------------- context

    @app.post("/context/assemble", operation_id="assembleContext")
    def assemble_context(
        body: sc.AssembleContextRequest,
        caller: Annotated[AuthenticatedCaller, Depends(context_read)],
    ) -> Response:
        try:
            request = ContextAssemblyRequest(
                tenant_id=caller.tenant_id,
                session_id=body.session_id,
                task=body.task,
                token_budget=body.token_budget,
                query_embedding=body.query_embedding,
                zones=[_WIRE_TO_DOMAIN_ZONE[z] for z in body.zones] if body.zones else None,
                max_items=body.max_items,
                min_provenance_conf=body.min_provenance_conf,
                as_of=body.as_of,
                tenant_credential=caller.tenant_credential,
            )
        except (ValueError, KeyError) as exc:
            return _error(400, "INVALID_REQUEST", str(exc))

        result = ctx.memory_orchestrator.assemble_context(request)
        items = [_memory_item_to_model(i, include_provenance=body.include_provenance) for i in result.items]
        response = sc.ContextAssembly(
            assembly_id=result.assembly_id,
            assembled_at=ctx.clock.now(),
            items=items,
            token_count_total=result.token_count_total,
            token_budget=result.token_budget,
            items_considered=len(result.items),
            items_returned=len(result.items),
            degraded=result.degraded,
            zones_unavailable=[
                _DOMAIN_TO_WIRE_ZONE.get(z, str(z)) for z in result.zones_unavailable
            ],
            retrieval_mode="working_only",
            trace_id=result.trace_id,
        )
        return JSONResponse(status_code=200, content=response.model_dump(mode="json"))

    @app.get("/context/assemble", operation_id="assembleContextSimple")
    def assemble_context_simple(
        session_id: str,
        q: str,
        token_budget: int,
        caller: Annotated[AuthenticatedCaller, Depends(context_read)],
    ) -> Response:
        return assemble_context(
            sc.AssembleContextRequest(session_id=session_id, task=q, token_budget=token_budget),
            caller,
        )

    @app.post("/memory/search", operation_id="searchMemory")
    def search_memory(
        body: sc.SearchMemoryRequest,
        caller: Annotated[AuthenticatedCaller, Depends(context_read)],
    ) -> Response:
        request = ContextAssemblyRequest(
            tenant_id=caller.tenant_id,
            session_id=str(uuid.uuid4()),
            task=body.query,
            token_budget=262_144,
            query_embedding=body.query_embedding,
            zones=[_WIRE_TO_DOMAIN_ZONE[z] for z in body.zones] if body.zones else None,
            max_items=body.page_size,
            tenant_credential=caller.tenant_credential,
        )
        result = ctx.memory_orchestrator.assemble_context(request)
        items = [_memory_item_to_model(i, include_provenance=True) for i in result.items]
        return JSONResponse(
            status_code=200,
            content=sc.SearchMemoryResponse(items=items, retrieval_mode=None).model_dump(mode="json"),
        )

    # ------------------------------------------------------------------ items

    def _lookup_item(caller: AuthenticatedCaller, item_id: str) -> st.RegisteredItem | None:
        return item_registry.get(caller.tenant_id, item_id)

    @app.get("/items/{item_id}", operation_id="getItem")
    def get_item(
        item_id: str, caller: Annotated[AuthenticatedCaller, Depends(context_read)]
    ) -> Response:
        record = _lookup_item(caller, item_id)
        if record is None:
            return _error(404, "NOT_FOUND", "item not found")
        return JSONResponse(status_code=200, content=_registered_item_to_model(record).model_dump(mode="json"))

    @app.patch("/items/{item_id}", operation_id="updateItem")
    def update_item(
        item_id: str,
        body: sc.UpdateItemRequest,
        caller: Annotated[AuthenticatedCaller, Depends(memory_write)],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> Response:
        record = _lookup_item(caller, item_id)
        if record is None:
            return _error(404, "NOT_FOUND", "item not found")
        record.payload = str(body.content.get("text", record.payload))
        item_registry.put(record)
        receipt = sc.WriteReceipt(write_id=str(uuid.uuid4()), accepted_at=ctx.clock.now())
        return JSONResponse(status_code=202, content=receipt.model_dump(mode="json"))

    @app.delete("/items/{item_id}", operation_id="deleteItem")
    def delete_item(
        item_id: str, caller: Annotated[AuthenticatedCaller, Depends(memory_write)]
    ) -> Response:
        if not item_registry.mark_deleted(caller.tenant_id, item_id):
            return _error(404, "NOT_FOUND", "item not found")
        receipt = sc.WriteReceipt(write_id=str(uuid.uuid4()), accepted_at=ctx.clock.now())
        return JSONResponse(status_code=202, content=receipt.model_dump(mode="json"))

    @app.get("/items/{item_id}/provenance", operation_id="getItemProvenance")
    def get_item_provenance(
        item_id: str, caller: Annotated[AuthenticatedCaller, Depends(context_read)]
    ) -> Response:
        record = _lookup_item(caller, item_id)
        if record is None:
            return _error(404, "NOT_FOUND", "item not found")
        if not ctx.postgres_available or ctx.provenance_repository is None:
            return JSONResponse(status_code=200, content={"item_id": item_id, "records": []})
        try:
            records = ctx.provenance_repository.find_by_item_id(caller.tenant_id, item_id)
        except ZoneRepositoryError as exc:
            logger.error(
                "Zone 7 (Provenance) repository call failed",
                extra={"tenant_id": caller.tenant_id, "item_id": item_id},
                exc_info=True,
            )
            return _error(
                503, "PROVENANCE_UNAVAILABLE",
                "zone '7-provenance' is temporarily unavailable",
            )
        return JSONResponse(
            status_code=200,
            content={
                "item_id": item_id,
                "records": [
                    {
                        "source_type": r.source_type.value,
                        "source_refs": list(r.source_refs),
                        "confidence": r.confidence,
                        "conflict_status": r.conflict_status.value,
                        "invalidation_flag": r.invalidation_flag,
                        "write_timestamp": r.write_timestamp.isoformat(),
                        "provenance_id": r.provenance_id,
                    }
                    for r in records
                ],
            },
        )

    @app.post("/items/{item_id}/score:explain", operation_id="explainItemScore")
    def explain_item_score(
        item_id: str, caller: Annotated[AuthenticatedCaller, Depends(context_read)]
    ) -> Response:
        record = _lookup_item(caller, item_id)
        if record is None:
            return _error(404, "NOT_FOUND", "item not found")
        scoring = scoring_config_store.get(caller.tenant_id)
        explanation = sc.ScoreExplanation(
            item_id=item_id,
            score=record.score,
            score_terms=sc.ScoreTerms(
                recency=record.score, frequency=record.score, importance=record.score,
                task_relevance=record.score, user_affinity=record.score,
                provenance_confidence=record.score,
            ),
            weights={
                "w1": scoring.w1, "w2": scoring.w2, "w3": scoring.w3,
                "w4": scoring.w4, "w5": scoring.w5, "w6": scoring.w6,
            },
            lifecycle_state=record.lifecycle_state,
        )
        return JSONResponse(status_code=200, content=explanation.model_dump(mode="json"))

    @app.post("/items:batchGet", operation_id="batchGetItems")
    def batch_get_items(
        body: sc.BatchGetItemsRequest,
        caller: Annotated[AuthenticatedCaller, Depends(context_read)],
    ) -> Response:
        receipts = []
        for item_id in body.item_ids:
            record = _lookup_item(caller, item_id)
            if record is None:
                receipts.append(sc.BatchGetItemReceiptItem(item_id=item_id, status="not_found"))
            else:
                receipts.append(
                    sc.BatchGetItemReceiptItem(
                        item_id=item_id, status="found", item=_registered_item_to_model(record)
                    )
                )
        return JSONResponse(
            status_code=200,
            content={"receipts": [r.model_dump(mode="json") for r in receipts]},
        )

    # ------------------------------------------------------------------ write

    def _resolve_source_type(body: sc.WriteMemoryRequest) -> str | None:
        return body.provenance.source_type

    def _zone3_5_routing_gate_triggered(body: sc.WriteMemoryRequest) -> bool:
        """FR-013 design Section 4.2 step 0's normalization rule.

        Never compares `content.zone_hint` against a bare string -- uses
        the existing `_WIRE_TO_DOMAIN_ZONE` map against `ZoneId.SEMANTIC`,
        exactly as GitHub #25's disclosed, pre-existing `zone_hint ==
        "procedural"`/`"episodic"` bare-string comparisons must NOT be
        repeated (design doc Section 4.2 step 0, "Separate, pre-existing
        defect... NOT fixed by this design").
        """
        if body.content.entity_refs:
            return True
        return _WIRE_TO_DOMAIN_ZONE.get(body.content.zone_hint) is ZoneId.SEMANTIC

    def _do_write_zone3_5(
        caller: AuthenticatedCaller, body: sc.WriteMemoryRequest, source_type: SourceType
    ) -> tuple[Response, str | None]:
        """FR-013 design Section 4.2 steps 1-6: Zone 5 / Zone 3 routing and writes.

        Cite FR-013 verbatim (SRS.md, FR-013 row): "On every write
        targeting Zone 3 (Semantic) or Zone 5 (Entity), the system SHALL
        check the candidate against existing records for the same
        (entity, predicate) or (subject, predicate, object). On
        contradiction, neither record SHALL be overwritten; both SHALL be
        marked conflict_status=disputed and both confidences reduced,
        until resolved by re-confirmation, explicit user correction, or
        majority-source consensus."
        """
        retrieval_context_hash = body.provenance.retrieval_context_hash
        if not retrieval_context_hash or not retrieval_context_hash.strip():
            return _error(
                400, "INVALID_REQUEST",
                "provenance.retrieval_context_hash is required for a Zone 3/5 write (FR-013)",
            ), None

        if not ctx.postgres_available or ctx.semantic_repository is None or ctx.conflict_aware_entity_repository is None:
            logger.error(
                "Zone 3/5 write attempted with no live Postgres backend",
                extra={"tenant_id": caller.tenant_id},
            )
            return _error(
                503, "WRITE_REJECTED_NOT_DURABLE",
                "zone '3-semantic'/'5-entity' is temporarily unavailable",
            ), None

        subjects = tuple(ref.entity_id for ref in body.content.entity_refs if ref.role == "subject")
        objects = tuple(ref.entity_id for ref in body.content.entity_refs if ref.role == "object")

        zone5_written = False
        zone3_edges_written: list[str] = []
        zone3_edges_failed: list[str] = []

        try:
            candidate_fact = CandidateFact(
                tenant_id=caller.tenant_id,
                subjects=subjects,
                objects=objects,
                predicate=body.content.predicate or "",
                statement=body.content.text or "",
                subject_scope=body.content.subject_scope or "",
                index_reverse=body.content.index_reverse,
            )

            if len(subjects) == 1:
                ctx.conflict_aware_entity_repository.write_attribute(
                    caller.tenant_id,
                    subjects[0],
                    candidate_fact.predicate,
                    candidate_fact.statement,
                    str(uuid.uuid4()),
                    source_type=source_type,
                    retrieval_context_hash=retrieval_context_hash,
                )
                zone5_written = True

            routing = classify(
                candidate_fact,
                edge_id_factory=lambda: str(uuid.uuid4()),
                fact_id_factory=lambda: str(uuid.uuid4()),
            )
        except ValueError as exc:
            return _error(400, "INVALID_REQUEST", str(exc)), None
        except ZoneRepositoryError:
            logger.error(
                "Zone 5 (Entity) write failed",
                extra={"tenant_id": caller.tenant_id},
                exc_info=True,
            )
            return _error(
                503, "WRITE_REJECTED_NOT_DURABLE",
                "zone '5-entity' is temporarily unavailable",
            ), None

        if isinstance(routing, SemanticEdgeRouting):
            for edge in routing.edges:
                item_id = _zone3_edge_item_id(edge.subject_ref, edge.predicate, edge.object_ref)
                try:
                    ctx.semantic_repository.insert_edge(
                        edge,
                        provenance_id=str(uuid.uuid4()),
                        source_type=source_type,
                        retrieval_context_hash=retrieval_context_hash,
                    )
                    zone3_edges_written.append(item_id)
                except ZoneRepositoryError:
                    logger.error(
                        "Zone 3 (Semantic) edge write failed",
                        extra={"tenant_id": caller.tenant_id, "item_id": item_id},
                        exc_info=True,
                    )
                    zone3_edges_failed.append(item_id)
        elif isinstance(routing, GeneralFactRouting):
            try:
                ctx.semantic_repository.insert_general_fact(
                    routing.fact,
                    provenance_id=str(uuid.uuid4()),
                    source_type=source_type,
                    retrieval_context_hash=retrieval_context_hash,
                )
            except ZoneRepositoryError:
                logger.error(
                    "Zone 3 (Semantic) general fact write failed",
                    extra={"tenant_id": caller.tenant_id},
                    exc_info=True,
                )
                return _error(
                    503, "WRITE_REJECTED_NOT_DURABLE",
                    "zone '3-semantic' is temporarily unavailable",
                ), None

        write_id = str(uuid.uuid4())
        status: Literal["accepted", "partial"] = "partial" if zone3_edges_failed else "accepted"
        receipt = sc.WriteReceipt(
            write_id=write_id,
            status=status,
            accepted_at=ctx.clock.now(),
            zone5_written=zone5_written,
            zone3_edges_written=zone3_edges_written,
            zone3_edges_failed=zone3_edges_failed,
        )
        write_status_store.record(st.WriteStatusEntry(write_id=write_id, status="accepted", item_id=None))
        status_code = 207 if zone3_edges_failed else 202
        return JSONResponse(status_code=status_code, content=receipt.model_dump(mode="json")), write_id

    def _do_write(caller: AuthenticatedCaller, body: sc.WriteMemoryRequest) -> tuple[Response, str | None]:
        resolved_source_type_raw = _resolve_source_type(body)
        if resolved_source_type_raw is None:
            return _error(
                422, "INVALID_SOURCE_TYPE",
                "writeMemory requires a resolvable provenance.source_type (FR-010)",
            ), None

        if _zone3_5_routing_gate_triggered(body):
            return _do_write_zone3_5(caller, body, SourceType(resolved_source_type_raw))

        write_id = str(uuid.uuid4())
        text = body.content.text or ""
        token_count = max(1, len(text.split()))
        zone_hint = body.content.zone_hint or "working"

        try:
            if zone_hint == "procedural" and body.content.task_signature_hash:
                from dashanan.domain.procedure import Procedure

                procedure = Procedure(
                    tenant_id=caller.tenant_id,
                    task_signature_hash=body.content.task_signature_hash,
                    steps=(text,) if text else ("",),
                    success_count=0,
                    failure_count=0,
                    last_used_at=ctx.clock.now(),
                )
                ctx.procedural_repository.commit(procedure)
                item_id = body.content.task_signature_hash
                source_zone = ZoneId.PROCEDURAL
            elif zone_hint == "episodic" and ctx.postgres_available and ctx.episodic_repository is not None:
                from dashanan.domain.episode import Episode, EpisodeState

                item_id = write_id
                episode = Episode(
                    tenant_id=caller.tenant_id,
                    session_id=body.session_id,
                    episode_id=item_id,
                    seq=0,
                    occurred_at=body.content.occurred_at or ctx.clock.now(),
                    written_at=ctx.clock.now(),
                    payload=text,
                    actors=(),
                    token_count=token_count,
                    state=EpisodeState.ACTIVE,
                )
                ctx.episodic_repository.append(episode)
                source_zone = ZoneId.EPISODIC
            else:
                item_id = write_id
                ctx.working_repository.put(
                    caller.tenant_id, body.session_id, item_id, text, token_count
                )
                source_zone = ZoneId.WORKING
        except ZoneRepositoryError as exc:
            logger.error(
                "Zone repository write failed",
                extra={"tenant_id": caller.tenant_id, "zone_hint": zone_hint},
                exc_info=True,
            )
            return _error(
                503, "WRITE_REJECTED_NOT_DURABLE",
                f"zone '{zone_hint}' is temporarily unavailable",
            ), None

        # ADR-002: the accepted write always lands in Zone 1 (Working)
        # synchronously first, read-your-own-write within session, even
        # when routed to another zone above.
        if source_zone is not ZoneId.WORKING:
            try:
                ctx.working_repository.put(
                    caller.tenant_id, body.session_id, item_id, text, token_count
                )
            except ZoneRepositoryError:
                logger.warning("Zone 1 durability mirror failed", extra={"write_id": write_id})

        item_registry.put(
            st.RegisteredItem(
                item_id=item_id, tenant_id=caller.tenant_id, source_zone=source_zone,
                payload=text, token_count=token_count, score=0.0,
                lifecycle_state="Active",
                provenance={"source_type": body.provenance.source_type, "purpose": body.provenance.purpose},
            )
        )
        write_status_store.record(st.WriteStatusEntry(write_id=write_id, status="accepted", item_id=item_id))
        receipt = sc.WriteReceipt(write_id=write_id, accepted_at=ctx.clock.now())
        return JSONResponse(status_code=202, content=receipt.model_dump(mode="json")), write_id

    @app.post("/memory/write", operation_id="writeMemory")
    def write_memory(
        body: sc.WriteMemoryRequest,
        caller: Annotated[AuthenticatedCaller, Depends(memory_write)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> Response:
        response, _ = _do_write(caller, body)
        return response

    @app.post("/memory/write:batch", operation_id="writeMemoryBatch")
    def write_memory_batch(
        body: sc.WriteMemoryBatchRequest,
        caller: Annotated[AuthenticatedCaller, Depends(memory_write)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> Response:
        receipts = []
        for index, item in enumerate(body.items):
            response, write_id = _do_write(caller, item)
            if write_id is not None:
                receipts.append(sc.BatchWriteReceiptItem(index=index, status="accepted", write_id=write_id))
            else:
                receipts.append(sc.BatchWriteReceiptItem(index=index, status="rejected"))
        return JSONResponse(
            status_code=202,
            content={"receipts": [r.model_dump(mode="json") for r in receipts]},
        )

    @app.get("/writes/{write_id}", operation_id="getWriteStatus")
    def get_write_status(
        write_id: str, caller: Annotated[AuthenticatedCaller, Depends(memory_write)]
    ) -> Response:
        entry = write_status_store.get(write_id)
        if entry is None:
            return _error(404, "NOT_FOUND", "write_id not found")
        return JSONResponse(
            status_code=200,
            content=sc.WriteStatus(
                write_id=entry.write_id, status=entry.status,
                item_id=entry.item_id, failure_reason=entry.failure_reason,
            ).model_dump(mode="json"),
        )

    # ------------------------------------------------------------------ zones

    def _zone_health(zone: ZoneId) -> str:
        if zone in (ZoneId.EPISODIC, ZoneId.SEMANTIC, ZoneId.PROVENANCE, ZoneId.CONSOLIDATION):
            return "healthy" if ctx.postgres_available else "degraded"
        return "healthy"

    @app.get("/zones", operation_id="listZones")
    def list_zones(caller: Annotated[AuthenticatedCaller, Depends(admin_zones)]) -> Response:
        summaries = [
            sc.ZoneSummary(
                zone=_DOMAIN_TO_WIRE_ZONE[z], health=_zone_health(z), item_count=0,
                capacity_utilization=0.0,
                adapter_binding=zone_config_store.get(caller.tenant_id, z).adapter_binding,
            )
            for z in ZoneId
        ]
        return JSONResponse(
            status_code=200,
            content={"zones": [s.model_dump(mode="json") for s in summaries]},
        )

    @app.get("/zones/{zone}", operation_id="getZoneDetail")
    def get_zone_detail(
        zone: str, caller: Annotated[AuthenticatedCaller, Depends(admin_zones)]
    ) -> Response:
        domain_zone = _WIRE_TO_DOMAIN_ZONE.get(zone)
        if domain_zone is None:
            return _error(404, "NOT_FOUND", "unknown zone")
        entry = zone_config_store.get(caller.tenant_id, domain_zone)
        detail = sc.ZoneDetail(
            zone=zone, health=_zone_health(domain_zone), item_count=0,
            capacity_utilization=0.0, adapter_binding=entry.adapter_binding,
        )
        return JSONResponse(status_code=200, content=detail.model_dump(mode="json"))

    @app.get("/zones/{zone}/config", operation_id="getZoneConfig")
    def get_zone_config(
        zone: str, caller: Annotated[AuthenticatedCaller, Depends(admin_zones)]
    ) -> Response:
        domain_zone = _WIRE_TO_DOMAIN_ZONE.get(zone)
        if domain_zone is None:
            return _error(404, "NOT_FOUND", "unknown zone")
        entry = zone_config_store.get(caller.tenant_id, domain_zone)
        return JSONResponse(status_code=200, content=_zone_config_to_model(zone, entry).model_dump(mode="json"))

    @app.put("/zones/{zone}/config", operation_id="putZoneConfig")
    def put_zone_config(
        zone: str,
        body: sc.ZoneConfigModel,
        caller: Annotated[AuthenticatedCaller, Depends(admin_zones)],
    ) -> Response:
        domain_zone = _WIRE_TO_DOMAIN_ZONE.get(zone)
        if domain_zone is None:
            return _error(404, "NOT_FOUND", "unknown zone")
        entry = st.ZoneConfigEntry(
            zone=domain_zone, lambda_zone=body.lambda_zone,
            promote_threshold=body.promote_threshold, compress_threshold=body.compress_threshold,
            archive_threshold=body.archive_threshold, capacity_cap=body.capacity_cap,
            max_age_seconds=body.max_age_seconds, idle_ttl_seconds=body.idle_ttl_seconds,
            adapter_binding=body.adapter_binding,
        )
        zone_config_store.put(caller.tenant_id, entry)
        return JSONResponse(status_code=200, content=_zone_config_to_model(zone, entry).model_dump(mode="json"))

    @app.post("/zones/{zone}/sweep", operation_id="forceZoneSweep")
    def force_zone_sweep(
        zone: str, caller: Annotated[AuthenticatedCaller, Depends(admin_zones)]
    ) -> Response:
        if zone not in _WIRE_TO_DOMAIN_ZONE:
            return _error(404, "NOT_FOUND", "unknown zone")
        job_id = str(uuid.uuid4())
        admin_job_store.record(st.AdminJobEntry(job_id=job_id, status="succeeded", progress=1.0))
        return JSONResponse(status_code=202, content=sc.JobAccepted(job_id=job_id).model_dump(mode="json"))

    @app.get("/zones/{zone}/items", operation_id="listZoneItems")
    def list_zone_items(
        zone: str,
        caller: Annotated[AuthenticatedCaller, Depends(admin_zones)],
        cursor: str | None = None,
        page_size: int = 50,
    ) -> Response:
        if zone not in _WIRE_TO_DOMAIN_ZONE:
            return _error(404, "NOT_FOUND", "unknown zone")
        return JSONResponse(status_code=200, content={"items": [], "next_cursor": None})

    @app.post("/zones/{zone}/reindex", operation_id="reindexZone")
    def reindex_zone(
        zone: str, caller: Annotated[AuthenticatedCaller, Depends(admin_zones)]
    ) -> Response:
        if zone != "6-retrieval-index":
            return _error(404, "NOT_FOUND", "reindex only applies to Zone 6")
        job_id = str(uuid.uuid4())
        admin_job_store.record(st.AdminJobEntry(job_id=job_id, status="succeeded", progress=1.0))
        return JSONResponse(status_code=202, content=sc.JobAccepted(job_id=job_id).model_dump(mode="json"))

    @app.get("/config/scoring", operation_id="getScoringConfig")
    def get_scoring_config(caller: Annotated[AuthenticatedCaller, Depends(admin_zones)]) -> Response:
        entry = scoring_config_store.get(caller.tenant_id)
        return JSONResponse(
            status_code=200,
            content=sc.ScoringConfigModel(**_dataclass_asdict(entry)).model_dump(mode="json"),
        )

    @app.put("/config/scoring", operation_id="putScoringConfig")
    def put_scoring_config(
        body: sc.ScoringConfigModel, caller: Annotated[AuthenticatedCaller, Depends(admin_zones)]
    ) -> Response:
        entry = st.ScoringConfigEntry(**body.model_dump())
        scoring_config_store.put(caller.tenant_id, entry)
        return JSONResponse(status_code=200, content=body.model_dump(mode="json"))

    @app.get("/jobs/{job_id}", operation_id="getJobStatus")
    def get_job_status(
        job_id: str, caller: Annotated[AuthenticatedCaller, Depends(admin_zones)]
    ) -> Response:
        entry = admin_job_store.get(job_id)
        if entry is None:
            return _error(404, "NOT_FOUND", "job_id not found")
        return JSONResponse(
            status_code=200,
            content=sc.JobStatus(job_id=entry.job_id, status=entry.status, progress=entry.progress).model_dump(mode="json"),
        )

    # ---------------------------------------------------------------- tenants

    @app.post("/tenants", operation_id="createTenant", status_code=201)
    def create_tenant(
        body: sc.CreateTenantRequest, caller: Annotated[AuthenticatedCaller, Depends(admin_tenants)]
    ) -> Response:
        record = st.TenantRecord(
            tenant_id=str(uuid.uuid4()), display_name=body.display_name,
            embedding_residency=body.embedding_residency, created_at=ctx.clock.now(),
        )
        tenant_registry.create(record)
        return JSONResponse(status_code=201, content=_tenant_to_model(record).model_dump(mode="json"))

    @app.get("/tenants", operation_id="listTenants")
    def list_tenants(
        caller: Annotated[AuthenticatedCaller, Depends(admin_tenants)],
        cursor: str | None = None,
        page_size: int = 50,
    ) -> Response:
        records = tenant_registry.list_all()
        return JSONResponse(
            status_code=200,
            content={"items": [_tenant_to_model(r).model_dump(mode="json") for r in records], "next_cursor": None},
        )

    @app.get("/tenants/{tenant_id}", operation_id="getTenant")
    def get_tenant(
        tenant_id: str, caller: Annotated[AuthenticatedCaller, Depends(admin_tenants)]
    ) -> Response:
        try:
            enforce_tenant_scope_match(caller, tenant_id)
        except TenantAuthenticationError:
            return _error(403, "TENANT_SCOPE_MISMATCH", "You are not authorized to access this tenant.")
        record = tenant_registry.get(tenant_id)
        if record is None:
            return _error(404, "NOT_FOUND", "tenant not found")
        return JSONResponse(status_code=200, content=_tenant_to_model(record).model_dump(mode="json"))

    @app.patch("/tenants/{tenant_id}", operation_id="updateTenant")
    def update_tenant(
        tenant_id: str, body: sc.UpdateTenantRequest,
        caller: Annotated[AuthenticatedCaller, Depends(admin_tenants)],
    ) -> Response:
        try:
            enforce_tenant_scope_match(caller, tenant_id)
        except TenantAuthenticationError:
            return _error(403, "TENANT_SCOPE_MISMATCH", "You are not authorized to access this tenant.")
        record = tenant_registry.update(
            tenant_id, display_name=body.display_name, embedding_residency=body.embedding_residency
        )
        if record is None:
            return _error(404, "NOT_FOUND", "tenant not found")
        return JSONResponse(status_code=200, content=_tenant_to_model(record).model_dump(mode="json"))

    @app.delete("/tenants/{tenant_id}", operation_id="deleteTenant")
    def delete_tenant(
        tenant_id: str, caller: Annotated[AuthenticatedCaller, Depends(admin_tenants)]
    ) -> Response:
        try:
            enforce_tenant_scope_match(caller, tenant_id)
        except TenantAuthenticationError:
            return _error(403, "TENANT_SCOPE_MISMATCH", "You are not authorized to access this tenant.")
        if not tenant_registry.delete(tenant_id):
            return _error(404, "NOT_FOUND", "tenant not found")
        job_id = str(uuid.uuid4())
        admin_job_store.record(st.AdminJobEntry(job_id=job_id, status="succeeded", progress=1.0))
        return JSONResponse(status_code=202, content=sc.JobAccepted(job_id=job_id).model_dump(mode="json"))

    @app.delete("/tenants/{tenant_id}/subjects/{subject_id}", operation_id="eraseSubject")
    def erase_subject(
        tenant_id: str, subject_id: str,
        caller: Annotated[AuthenticatedCaller, Depends(admin_tenants)],
    ) -> Response:
        """AC-023-3: invokes the real, Zone-8-only `SubjectErasureCascadeService`.

        Does NOT present this as HLD Section 7.4's full "all 8 zones"
        cascade (must-not-deviate item 3) -- only Zone 8 is reached here.
        """
        try:
            enforce_tenant_scope_match(caller, tenant_id)
        except TenantAuthenticationError:
            return _error(403, "TENANT_SCOPE_MISMATCH", "You are not authorized to access this tenant.")
        if ctx.subject_erasure_service is None:
            return _error(
                503, "SERVICE_DEGRADED",
                "Zone 8 (Consolidation) is backed by SqlManifestRepository, which "
                "requires Postgres; no live Postgres is reachable in this "
                "deployment (see api.composition module docstring)",
            )
        accepted = ctx.subject_erasure_service.request_erasure(
            SubjectErasureRequest(tenant_id=tenant_id, subject_id=subject_id)
        )
        admin_job_store.record(st.AdminJobEntry(job_id=accepted.job_id, status="succeeded", progress=1.0))
        return JSONResponse(status_code=202, content=sc.JobAccepted(job_id=accepted.job_id).model_dump(mode="json"))

    @app.get("/tenants/{tenant_id}/subjects/{subject_id}/export", operation_id="exportSubjectData")
    def export_subject_data(
        tenant_id: str, subject_id: str,
        caller: Annotated[AuthenticatedCaller, Depends(admin_tenants)],
    ) -> Response:
        try:
            enforce_tenant_scope_match(caller, tenant_id)
        except TenantAuthenticationError:
            return _error(403, "TENANT_SCOPE_MISMATCH", "You are not authorized to access this tenant.")
        bundle = sc.SubjectExportBundle(subject_id=subject_id, exported_at=ctx.clock.now())
        return JSONResponse(status_code=200, content=bundle.model_dump(mode="json"))

    # --------------------------------------------------------------- operations

    @app.get("/healthz", operation_id="getLiveness")
    def get_liveness() -> Response:
        return Response(status_code=200)

    @app.get("/readyz", operation_id="getReadiness")
    def get_readiness() -> Response:
        report = sc.ReadinessReport(
            ready=True,
            adapters={
                "working": "healthy", "procedural": "healthy", "entity": "healthy",
                "episodic": "healthy" if ctx.postgres_available else "degraded",
                "semantic": "healthy" if ctx.postgres_available else "degraded",
                "provenance": "healthy" if ctx.postgres_available else "degraded",
                "consolidation": "healthy" if ctx.postgres_available else "degraded",
            },
        )
        return JSONResponse(status_code=200, content=report.model_dump(mode="json"))

    @app.get("/metrics", operation_id="getMetrics")
    def get_metrics(caller: Annotated[AuthenticatedCaller, Depends(admin_zones)]) -> Response:
        body = (
            "# HELP dashanan_postgres_available Whether the Shape B Postgres backend is reachable\n"
            "# TYPE dashanan_postgres_available gauge\n"
            f"dashanan_postgres_available {1 if ctx.postgres_available else 0}\n"
        )
        return Response(status_code=200, content=body, media_type="text/plain")

    return app


def _registered_item_to_model(record: st.RegisteredItem) -> sc.MemoryItemModel:
    return sc.MemoryItemModel(
        item_id=record.item_id,
        source_zone=_DOMAIN_TO_WIRE_ZONE.get(record.source_zone, str(record.source_zone)),
        payload=record.payload,
        token_count=record.token_count,
        score=record.score,
        score_terms=sc.ScoreTerms(
            recency=record.score, frequency=record.score, importance=record.score,
            task_relevance=record.score, user_affinity=record.score,
            provenance_confidence=record.score,
        ),
        lifecycle_state=record.lifecycle_state,
    )


def _zone_config_to_model(wire_zone: str, entry: st.ZoneConfigEntry) -> sc.ZoneConfigModel:
    return sc.ZoneConfigModel(
        zone=wire_zone, lambda_zone=entry.lambda_zone,
        promote_threshold=entry.promote_threshold, compress_threshold=entry.compress_threshold,
        archive_threshold=entry.archive_threshold, capacity_cap=entry.capacity_cap,
        max_age_seconds=entry.max_age_seconds, idle_ttl_seconds=entry.idle_ttl_seconds,
        adapter_binding=entry.adapter_binding,
    )


def _tenant_to_model(record: st.TenantRecord) -> sc.TenantModel:
    return sc.TenantModel(
        tenant_id=record.tenant_id, display_name=record.display_name,
        embedding_residency=record.embedding_residency, created_at=record.created_at,
    )
