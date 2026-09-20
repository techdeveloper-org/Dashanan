-- Zone 8 Consolidation Memory hot-manifest DDL (DASH-STORY-018, traces to FR-008).
--
-- Source of every structural decision below: HLD Section 3.9 (Zone 8,
-- ConsolidatedBlob), ADR-009 (object-store blobs + hot manifest index,
-- APPROVED), HLD Section 3.10 (RECEIVES rule, Hard Rule 1), and HLD
-- threat I-5 (consolidation cross-tenant merge control).
--
-- This table holds ONLY the hot-manifest index -- `item_id -> (blob_id,
-- byte_range, compression_generation)` -- never blob content itself,
-- which ADR-009 places in a separate object store
-- (`dashanan.infrastructure.local_object_store.LocalObjectStore` or a
-- production S3/GCS/Azure adapter behind the same `ObjectStorePort`).
-- This is an initial CREATE, not an ALTER against production data.
--
-- PII NOTE: every column here is manifest/index metadata -- a
-- content-addressed blob_id, a byte offset pair, an integer generation
-- counter, a source zone label -- never the underlying archived content
-- itself, which this table never stores. No example row values or seed
-- data are included here.

CREATE TABLE zone8_manifest (
    tenant_id               TEXT         NOT NULL,
    item_id                 TEXT         NOT NULL,
    -- Always a 64-character lowercase hex SHA-256 digest
    -- (dashanan.domain.consolidated_blob.compute_blob_id) -- never a
    -- caller-chosen identifier (must-not-deviate item 3: content-
    -- addressed, immutable, never overwritten).
    blob_id                  TEXT         NOT NULL,
    byte_start                BIGINT       NOT NULL,
    byte_end                  BIGINT       NOT NULL,
    compression_generation    INTEGER      NOT NULL DEFAULT 0,
    -- TEXT + CHECK rather than an ENUM, matching provenance_schema.sql's
    -- and write_journal_schema.sql's own source_zone column: the
    -- eight-zone value set is owned by dashanan.domain.zone.ZoneId, a
    -- different story's file, and this story must not introduce a
    -- second definition that could drift.
    source_zone              TEXT         NOT NULL,
    written_at                TIMESTAMPTZ  NOT NULL,

    -- AC-008-1's "single indexed manifest-table lookup": (tenant_id,
    -- item_id) as the primary key makes the dominant query
    -- (find_by_item_id) an O(1)/O(log n) PK point lookup, never a scan.
    CONSTRAINT pk_zone8_manifest PRIMARY KEY (tenant_id, item_id),

    CONSTRAINT chk_zone8_manifest_byte_range
        CHECK (byte_end > byte_start AND byte_start >= 0),
    CONSTRAINT chk_zone8_manifest_compression_generation
        CHECK (compression_generation >= 0),
    -- HLD Section 3.10's RECEIVES rule for Zone 8 (AC-008-2): only
    -- Zones 2 (episodic), 3 (semantic), 4 (procedural), 5 (entity) may
    -- archive into Consolidation Memory -- Zone 1 (Working) has no
    -- Archived state of its own, and Zones 6/7 (retrieval_index,
    -- provenance) are derived/audit tiers with no owned content.
    -- Enforced here as defense-in-depth alongside
    -- dashanan.domain.consolidated_blob.validate_source_zone, which is
    -- the application's own gate before a row ever reaches this INSERT.
    CONSTRAINT chk_zone8_manifest_source_zone CHECK (
        source_zone IN ('episodic', 'semantic', 'procedural', 'entity')
    ),
    CONSTRAINT chk_zone8_manifest_blob_id_format
        CHECK (blob_id ~ '^[0-9a-f]{64}$')
);

-- Append-only enforcement: a manifest row for an immutable, content-
-- addressed blob is itself never mutated once written (must-not-deviate
-- item 3). Mirrors write_journal_schema.sql's identical baseline
-- pattern (REVOKE, no trigger ceremony) -- this table carries no
-- recorded P1 remediation history of its own that would call for the
-- ownership-reassignment ceremony provenance_schema.sql's ownership
-- history required.
REVOKE UPDATE, DELETE ON zone8_manifest FROM PUBLIC;
