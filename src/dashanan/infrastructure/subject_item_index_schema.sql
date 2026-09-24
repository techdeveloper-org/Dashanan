-- subject_item_index: the real SubjectToItemIndex backing table (DASH-STORY-027-DEV).
--
-- Fulfils UnifiedSubjectErasureOrchestrator's SubjectToItemIndex Protocol
-- (dashanan.application.unified_subject_erasure_orchestrator), replacing
-- NullSubjectToItemIndex for zones that have a real subject_id ->
-- item_id[] resolution need at erasure time: today Zone 2 (episodic) and
-- Zone 6 (retrieval-index), per that Protocol's own "Zone 2/6 leg"
-- docstring. Zone 8 has its OWN separate subject index
-- (zone8_manifest_subject_index_migration.sql,
-- SqlZone8SubjectIndexRepository) and is not read through this table;
-- this table's own zone8 rows (written only as a durability bookkeeping
-- convenience by the Zone 8 erasure-durability finalize step, see
-- dashanan.application.zone8_erasure_durability) exist solely so that
-- finalize step has something durable to delete in the SAME transaction
-- as its Zone 7 promotion (design doc Section 7 step 6) -- they are never
-- read back by items_for_subject.
--
-- This is deliberately its own table, not a column bolted onto any
-- existing zone table, because it must support DELETE (per-zone
-- cleanup after a successful erasure, design doc Section 7 step 6) --
-- something several existing zone tables (episodic_entries,
-- zone8_manifest) structurally refuse via an append-only trigger. A
-- fresh, erasure-scoped index table sidesteps that conflict entirely
-- rather than reopening any append-only table's own protection.
--
-- PII NOTE: subject_id/item_id/zone are opaque routing identifiers only
-- (mirrors every other subject-index table in this codebase); no payload
-- content is ever stored here.

CREATE TABLE IF NOT EXISTS subject_item_index (
    tenant_id   TEXT        NOT NULL,
    subject_id  TEXT        NOT NULL,
    zone        TEXT        NOT NULL,
    item_id     TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT pk_subject_item_index PRIMARY KEY (tenant_id, zone, item_id),
    CONSTRAINT chk_subject_item_index_zone
        CHECK (zone IN ('zone1', 'zone2', 'zone3', 'zone5', 'zone6', 'zone8'))
);

-- items_for_subject's own read path: an indexed (tenant_id, subject_id)
-- lookup, mirroring idx_zone8_manifest_subject's identical rationale.
CREATE INDEX IF NOT EXISTS idx_subject_item_index_subject
    ON subject_item_index (tenant_id, subject_id);

-- Idempotent role creation (IF NOT EXISTS via pg_roles), mirroring every
-- other per-zone role's own creation pattern in this package.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dashanan_subject_index_role') THEN
        CREATE ROLE dashanan_subject_index_role NOLOGIN;
    END IF;
END
$$;

REVOKE ALL ON subject_item_index FROM PUBLIC;
GRANT SELECT, INSERT, DELETE ON subject_item_index TO dashanan_subject_index_role;

-- The compliance-role delete adapter (SqlZone2ComplianceEviction) and the
-- Zone 8 erasure-durability finalize step both need to remove rows here
-- as part of a single erasure transaction; dashanan_compliance_erasure_role
-- already exists by the time this file runs (entity_schema.sql creates it,
-- and this file is ordered after entity_schema.sql -- see
-- migration_runner.py's SCHEMA_FILES comment).
GRANT SELECT, DELETE ON subject_item_index TO dashanan_compliance_erasure_role;
