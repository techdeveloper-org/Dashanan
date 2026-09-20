-- ADR-006 mandatory subject_id secondary index for zone8_manifest
-- (DASH-STORY-020, DSHN-69, traces to FR-008 / HLD Section 10 DPDP-3).
--
-- HLD Section 10, DPDP-3 (verbatim): "The cascade must reach eight
-- zones, the vector index, the lexical index and Zone 8 archives. This
-- is what makes the subject_id secondary index on every zone table
-- mandatory (ADR-006) -- without it the cascade is eleven full scans."
--
-- This is a MIGRATION against zone8_consolidation_schema.sql's existing
-- zone8_manifest table (DASH-STORY-018, a different story's owned
-- file) -- additive-only (ADD COLUMN / CREATE INDEX, both IF NOT
-- EXISTS), never an edit to that file's own CREATE TABLE statement,
-- per this story's file-disjointness requirement. See this story's dev
-- report, judgment-call list, for the corresponding shared_file_request
-- (the fully-integrated version of this column belongs on
-- ManifestEntry/ManifestPort/SqlManifestRepository themselves, written
-- in the same INSERT that already writes every other column).
--
-- PII NOTE: subject_id is the same class of opaque identifier
-- zone8_consolidation_schema.sql's own item_id/tenant_id columns
-- already are -- an index key, never decoded content.

ALTER TABLE zone8_manifest ADD COLUMN IF NOT EXISTS subject_id TEXT;

-- Partial index: subject_id is written by SqlManifestRepository.insert_batch's
-- own INSERT (dashanan.domain.consolidated_blob.ArchiveBatchItem.subject_id,
-- DASH-STORY-020's fully-integrated fix -- see SqlZone8SubjectIndexRepository's
-- own module docstring) only when the caller archived the row through
-- Zone8SubjectKeyedArchiver.archive_for_subject; a row written through
-- ArchiveEngine's un-keyed sweep path carries no subject_id at all. WHERE
-- subject_id IS NOT NULL keeps the index small and matches
-- idx_semantic_edges_subject's own "erasure key" index precedent in
-- semantic_schema.sql.
CREATE INDEX IF NOT EXISTS idx_zone8_manifest_subject
    ON zone8_manifest (tenant_id, subject_id)
    WHERE subject_id IS NOT NULL;
