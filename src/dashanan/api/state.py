"""API-layer, in-process Shape A bookkeeping this host owns directly.

None of the registries here is a zone adapter (must-not-deviate item 1):
each tracks bookkeeping the HOST APPLICATION itself is responsible for --
write-status polling, async admin-job status, the zone-admin/scoring
config surface (FR-012, NFR-009 -- "per-deployment configuration, never
compiled-in constants"), the tenant control-plane record, and an item
registry this host's own `getItem`/`batchGetItems`/`updateItem`/
`deleteItem`/`score:explain` handlers read back (real state, written by
this same host's `writeMemory` handler -- not a placeholder).

Concurrency note (flagged gap, never fabricated): every store here uses a
single `threading.Lock` per registry, matching `WorkingMemoryLRURepository`
's own per-structure locking discipline, but is entirely in-process --
Shape A. A Shape B deployment with more than one host process needs this
state to move to Redis/Postgres (FR-015 scope); this module does not
pretend otherwise.
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime

from dashanan.domain.zone import ZoneId


@dataclass(slots=True)
class RegisteredItem:
    """One item this host has written, keyed by `(tenant_id, item_id)`.

    The minimal shape `getItem`/`batchGetItems`/`updateItem`/`deleteItem`/
    `explainItemScore` need to serve a read-back -- populated only by
    this host's own `writeMemory`/`writeMemoryBatch` handlers.
    """

    item_id: str
    tenant_id: str
    source_zone: ZoneId
    payload: str
    token_count: int
    score: float
    lifecycle_state: str
    provenance: dict[str, object]
    deleted: bool = False


class ItemRegistry:
    """Thread-safe in-process index of items this host has written."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[tuple[str, str], RegisteredItem] = {}

    def put(self, item: RegisteredItem) -> None:
        """Register (or overwrite) `item`, keyed by `(item.tenant_id, item.item_id)`."""
        with self._lock:
            self._items[(item.tenant_id, item.item_id)] = item

    def get(self, tenant_id: str, item_id: str) -> RegisteredItem | None:
        """Return the registered item, or `None` if unknown or a different tenant wrote it.

        Never distinguishes "does not exist" from "belongs to another
        tenant" (mirrors openapi.yaml `NotFound`'s own documented
        anti-tenant-existence-oracle rationale).
        """
        with self._lock:
            item = self._items.get((tenant_id, item_id))
            return item if item is not None and not item.deleted else None

    def mark_deleted(self, tenant_id: str, item_id: str) -> bool:
        """Soft-delete the item; returns `True` if it existed and was not already deleted."""
        with self._lock:
            item = self._items.get((tenant_id, item_id))
            if item is None or item.deleted:
                return False
            item.deleted = True
            return True


@dataclass(slots=True)
class WriteStatusEntry:
    write_id: str
    status: str
    item_id: str | None = None
    failure_reason: str | None = None


class WriteStatusStore:
    """Tracks `POST /v1/memory/write`'s own `write_id -> status` (`GET /v1/writes/{write_id}`)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, WriteStatusEntry] = {}

    def record(self, entry: WriteStatusEntry) -> None:
        with self._lock:
            self._entries[entry.write_id] = entry

    def get(self, write_id: str) -> WriteStatusEntry | None:
        with self._lock:
            return self._entries.get(write_id)


@dataclass(slots=True)
class AdminJobEntry:
    job_id: str
    status: str
    progress: float | None = None
    error_message: str | None = None


class AdminJobStore:
    """Tracks async admin jobs (`forceZoneSweep`/`reindexZone`/`deleteTenant`, `GET /v1/jobs/{job_id}`)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, AdminJobEntry] = {}

    def record(self, entry: AdminJobEntry) -> None:
        with self._lock:
            self._jobs[entry.job_id] = entry

    def get(self, job_id: str) -> AdminJobEntry | None:
        with self._lock:
            return self._jobs.get(job_id)


@dataclass(slots=True)
class ZoneConfigEntry:
    """FR-012/NFR-009: per-deployment zone configuration, never a compiled-in constant."""

    zone: ZoneId
    lambda_zone: float | None
    promote_threshold: float
    compress_threshold: float
    archive_threshold: float
    capacity_cap: int
    max_age_seconds: int | None
    idle_ttl_seconds: int | None
    adapter_binding: str


_DEFAULT_ZONE_CONFIG: dict[ZoneId, ZoneConfigEntry] = {
    ZoneId.WORKING: ZoneConfigEntry(
        ZoneId.WORKING, 0.20, 0.70, 0.40, 0.25, 200, None, 1800, "WorkingMemoryLRURepository"
    ),
    ZoneId.EPISODIC: ZoneConfigEntry(
        ZoneId.EPISODIC, 0.05, 0.70, 0.40, 0.25, 100_000, None, None, "SqlEpisodicRepository"
    ),
    ZoneId.SEMANTIC: ZoneConfigEntry(
        ZoneId.SEMANTIC, 0.02, 0.70, 0.40, 0.25, 500_000, None, None, "SqlSemanticRepository"
    ),
    ZoneId.PROCEDURAL: ZoneConfigEntry(
        ZoneId.PROCEDURAL, 0.01, 0.70, 0.40, 0.25, 50_000, None, None,
        "InMemoryProceduralMemoryRepository",
    ),
    ZoneId.ENTITY: ZoneConfigEntry(
        ZoneId.ENTITY, 0.02, 0.70, 0.40, 0.25, 200_000, None, None, "EntityMemoryRepository"
    ),
    ZoneId.RETRIEVAL_INDEX: ZoneConfigEntry(
        ZoneId.RETRIEVAL_INDEX, None, 0.70, 0.40, 0.25, 500_000, None, None,
        "InMemoryVectorIndex+InMemoryLexicalIndex",
    ),
    ZoneId.PROVENANCE: ZoneConfigEntry(
        ZoneId.PROVENANCE, None, 0.70, 0.40, 0.25, 1_000_000, None, None,
        "SqlProvenanceRepository",
    ),
    ZoneId.CONSOLIDATION: ZoneConfigEntry(
        ZoneId.CONSOLIDATION, None, 0.70, 0.40, 0.25, 10_000_000, None, None,
        "Zone8ConsolidationStore",
    ),
}


class ZoneConfigStore:
    """Per-tenant zone configuration (FR-012, NFR-009), seeded from documented HLD defaults."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._configs: dict[tuple[str, ZoneId], ZoneConfigEntry] = {}

    def get(self, tenant_id: str, zone: ZoneId) -> ZoneConfigEntry:
        with self._lock:
            key = (tenant_id, zone)
            if key not in self._configs:
                default = _DEFAULT_ZONE_CONFIG[zone]
                self._configs[key] = ZoneConfigEntry(**asdict(default))
            return self._configs[key]

    def put(self, tenant_id: str, entry: ZoneConfigEntry) -> None:
        with self._lock:
            self._configs[(tenant_id, entry.zone)] = entry


@dataclass(slots=True)
class ScoringConfigEntry:
    w1: float = 0.30
    w2: float = 0.20
    w3: float = 0.20
    w4: float = 0.15
    w5: float = 0.10
    w6: float = 0.05
    promote_threshold: float = 0.70
    compress_threshold: float = 0.45
    archive_threshold: float = 0.25


class ScoringConfigStore:
    """Per-tenant Memory Score weights/thresholds (FR-012, NFR-009)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._configs: dict[str, ScoringConfigEntry] = {}

    def get(self, tenant_id: str) -> ScoringConfigEntry:
        with self._lock:
            return self._configs.setdefault(tenant_id, ScoringConfigEntry())

    def put(self, tenant_id: str, entry: ScoringConfigEntry) -> None:
        with self._lock:
            self._configs[tenant_id] = entry


@dataclass(slots=True)
class TenantRecord:
    tenant_id: str
    display_name: str
    embedding_residency: str
    created_at: datetime


class TenantRegistry:
    """Control-plane tenant records (NFR-006), keyed by `tenant_id`."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tenants: dict[str, TenantRecord] = {}

    def create(self, record: TenantRecord) -> None:
        with self._lock:
            self._tenants[record.tenant_id] = record

    def get(self, tenant_id: str) -> TenantRecord | None:
        with self._lock:
            return self._tenants.get(tenant_id)

    def list_all(self) -> list[TenantRecord]:
        with self._lock:
            return list(self._tenants.values())

    def update(self, tenant_id: str, **changes: object) -> TenantRecord | None:
        with self._lock:
            record = self._tenants.get(tenant_id)
            if record is None:
                return None
            for key, value in changes.items():
                if value is not None:
                    setattr(record, key, value)
            return record

    def delete(self, tenant_id: str) -> bool:
        with self._lock:
            return self._tenants.pop(tenant_id, None) is not None
