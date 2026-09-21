"""Wire/API layer (DASH-STORY-023, FR-014): the real FastAPI host closing DSHN-60.

Traces to FR-014 in SRS.md. FR-014 (verbatim, SRS.md Section 3.1): "The
system SHALL expose docs/phase-1.5-api/openapi.yaml's 32 already-designed
operationIds through a real, running host application -- gRPC primary,
FastAPI REST gateway secondary (HLD Section 2 Communication Matrix,
ADR-004) -- so that a host AI can actually call Dashanan over the network,
closing the DSHN-60 gap `composition_root.py`'s own module docstring
documents: 'this codebase has no api/routes module or host application
anywhere that actually invokes this composition root.'"

Module layout:
    composition.py: The ONLY place in this package that constructs a
        `MemoryOrchestrator`, `SqlProvenanceRepository`, or
        `SqlEpisodicRepository` -- always through
        `dashanan.infrastructure.composition_root`'s own builder
        functions (AC-023-2), never a bare constructor call.
    security.py: JWT bearer-token verification (the REST gateway's
        companion to Shape B's mTLS transport layer, HLD Section 2) and
        the HLD Threat S-1 `TenantCredential` this host mints for every
        verified caller.
    state.py: The in-process, Shape A bookkeeping this host layer itself
        owns (write-status/job tracking, the zone-admin registry, the
        scoring-config store, the tenant control-plane record, and the
        item registry `getItem`/`batchGetItems`/`updateItem`/`deleteItem`
        read back) -- API-layer aggregation state, not a new zone
        adapter (must-not-deviate item 1).
    schemas.py: Pydantic request/response models mirroring
        docs/phase-1.5-api/openapi.yaml's `components.schemas` (locked;
        this story is the caller, not the designer, of that contract).
    app.py: The FastAPI application wiring all 32 operationIds
        (AC-023-1).
    main.py: The uvicorn entry point.

Scope note (must-not-deviate item 1): this story does not stand up real
Postgres/Redis/Qdrant/OpenSearch/S3 backing infrastructure -- that is
FR-015 (DASH-STORY-024)'s scope. `composition.py`'s own docstring records
exactly which operationIds degrade (documented `503`/`ServiceDegraded`/
`WriteRejectedNotDurable` responses openapi.yaml itself already specifies
for this exact scenario) when no live Postgres is reachable, rather than
failing to start or silently faking success.
"""

from __future__ import annotations
