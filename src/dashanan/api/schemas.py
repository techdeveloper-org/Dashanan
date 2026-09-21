"""Pydantic request/response models mirroring docs/phase-1.5-api/openapi.yaml.

Locked contract (must-not-deviate item 2): this module mirrors
openapi.yaml's `components.schemas` field-for-field; it never adds,
removes, or renames a documented field. Several schemas openapi.yaml
itself leaves loosely typed (`ZoneConfig`, `ScoringConfig`, `content`
inside `WriteMemoryRequest`) stay permissive `dict[str, object]`-backed
here rather than this module inventing stricter typing openapi.yaml does
not itself state.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ErrorDetail(BaseModel):
    code: str
    message: str
    trace_id: str | None = None


class ErrorResponse(BaseModel):
    success: Literal[False] = False
    data: None = None
    error: ErrorDetail
    timestamp: datetime


class ProvenanceWriteBlock(BaseModel):
    source_type: Literal[
        "user_stated", "tool_output", "llm_inferred",
        "summarized_from_n", "imported", "system_derived",
    ]
    source_refs: list[str] = Field(default_factory=list)
    importance: float | None = None
    purpose: str
    subject_id: str | None = None


class EntityRef(BaseModel):
    role: Literal["subject", "object"]
    entity_id: str


class WriteMemoryContent(BaseModel):
    model_config = ConfigDict(extra="allow")

    text: str | None = None
    zone_hint: str | None = None
    occurred_at: datetime | None = None
    task_signature_hash: str | None = None
    entity_refs: list[EntityRef] = Field(default_factory=list)
    index_reverse: bool = False


class WriteMemoryRequest(BaseModel):
    session_id: str
    content: WriteMemoryContent
    provenance: ProvenanceWriteBlock


class WriteReceipt(BaseModel):
    write_id: str
    status: Literal["accepted"] = "accepted"
    accepted_at: datetime


class BatchWriteReceiptItem(BaseModel):
    index: int
    status: Literal["accepted", "rejected"]
    write_id: str | None = None
    error: ErrorResponse | None = None


class WriteMemoryBatchRequest(BaseModel):
    items: list[WriteMemoryRequest] = Field(min_length=1, max_length=100)


class WriteStatus(BaseModel):
    write_id: str
    status: Literal["accepted", "indexed", "failed"]
    item_id: str | None = None
    failure_reason: str | None = None


class ScoreTerms(BaseModel):
    recency: float
    frequency: float
    importance: float
    task_relevance: float
    user_affinity: float
    provenance_confidence: float


class Provenance(BaseModel):
    source_type: str
    source_refs: list[str] = Field(default_factory=list)
    confidence: float
    conflict_status: str
    invalidation_flag: bool
    write_timestamp: datetime
    status: Literal["durable", "pending"] | None = None


class MemoryItemModel(BaseModel):
    item_id: str
    source_zone: str
    payload: Any
    token_count: int
    score: float
    score_terms: ScoreTerms
    lifecycle_state: Literal["Active", "Compressed", "Archived"]
    provenance: Provenance | None = None


class AssembleContextRequest(BaseModel):
    session_id: str
    task: str | None = None
    query_embedding: list[float] | None = None
    token_budget: int = Field(gt=0, le=262_144)
    zones: list[str] | None = None
    max_items: int = Field(default=50, gt=0, le=500)
    min_provenance_conf: float = 0.0
    include_provenance: bool = True
    as_of: datetime | None = None
    cursor: str | None = None


class ContextAssembly(BaseModel):
    assembly_id: str
    assembled_at: datetime
    items: list[MemoryItemModel]
    token_count_total: int
    token_budget: int
    items_considered: int
    items_returned: int
    degraded: bool
    zones_unavailable: list[str] = Field(default_factory=list)
    retrieval_mode: Literal["hybrid_rrf", "vector_only", "lexical_only", "working_only"]
    next_cursor: str | None = None
    trace_id: str


class UpdateItemRequest(BaseModel):
    content: dict[str, Any]
    provenance: ProvenanceWriteBlock


class ScoreExplanation(BaseModel):
    item_id: str
    score: float
    score_terms: ScoreTerms
    weights: dict[str, float]
    lifecycle_state: str
    predicted_crossings: dict[str, datetime | None] = Field(default_factory=dict)


class ZoneSummary(BaseModel):
    zone: str
    health: Literal["healthy", "degraded", "down"]
    item_count: int
    capacity_utilization: float
    adapter_binding: str
    last_sweep_at: datetime | None = None


class ZoneDetail(ZoneSummary):
    rotation_stats: dict[str, float] = Field(default_factory=dict)


class ZoneConfigModel(BaseModel):
    zone: str
    lambda_zone: float | None = None
    promote_threshold: float
    compress_threshold: float
    archive_threshold: float
    capacity_cap: int
    max_age_seconds: int | None = None
    idle_ttl_seconds: int | None = None
    adapter_binding: str


class ScoringConfigModel(BaseModel):
    w1: float
    w2: float
    w3: float
    w4: float
    w5: float
    w6: float
    promote_threshold: float = 0.70
    compress_threshold: float
    archive_threshold: float = 0.25


class JobAccepted(BaseModel):
    job_id: str
    status: Literal["queued"] = "queued"


class JobStatus(BaseModel):
    job_id: str
    status: Literal["queued", "running", "succeeded", "failed"]
    progress: float | None = None
    error: ErrorResponse | None = None


class CreateTenantRequest(BaseModel):
    display_name: str
    embedding_residency: Literal["in-region", "global"] = "in-region"


class UpdateTenantRequest(BaseModel):
    display_name: str | None = None
    embedding_residency: Literal["in-region", "global"] | None = None


class TenantModel(BaseModel):
    tenant_id: str
    display_name: str
    embedding_residency: str
    created_at: datetime


class TenantListResponse(BaseModel):
    items: list[TenantModel]
    next_cursor: str | None = None


class SubjectExportBundle(BaseModel):
    subject_id: str
    exported_at: datetime
    items: list[MemoryItemModel] = Field(default_factory=list)
    provenance_chains: list[dict[str, Any]] = Field(default_factory=list)


class ReadinessReport(BaseModel):
    ready: bool
    adapters: dict[str, Literal["healthy", "degraded", "down"]]


class SearchMemoryRequest(BaseModel):
    query: str
    query_embedding: list[float] | None = None
    zones: list[str] | None = None
    cursor: str | None = None
    page_size: int = Field(default=50, gt=0, le=500)


class SearchMemoryResponse(BaseModel):
    items: list[MemoryItemModel]
    next_cursor: str | None = None
    retrieval_mode: Literal["hybrid_rrf", "vector_only", "lexical_only"] | None = None


class BatchGetItemsRequest(BaseModel):
    item_ids: list[str] = Field(min_length=1, max_length=100)


class BatchGetItemReceiptItem(BaseModel):
    item_id: str
    status: Literal["found", "not_found"]
    item: MemoryItemModel | None = None
    error: ErrorResponse | None = None
