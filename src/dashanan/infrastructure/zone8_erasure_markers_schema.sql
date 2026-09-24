-- zone8_erasure_markers: the write-ahead log table for the Zone 8
-- step-3/step-4 durability boundary (DASH-STORY-027-DEV).
--
-- Backs `dashanan.application.zone8_erasure_durability.
-- SqlErasureMarkerStore`. A row here is durably appended (via
-- `connection.commit()`, an fsync barrier) BEFORE
-- `SubjectKeyStorePort.destroy_key` is called, and its `status` is
-- advanced through PENDING -> destroy_confirmed -> finalized as the
-- coordinator's own steps complete -- see that module's own docstring
-- for the full recovery-sweep contract this table exists to serve.
--
-- Deliberately mutable (UPDATE/DELETE both needed): unlike
-- episodic_entries/zone8_manifest, a marker's whole purpose is to be
-- advanced in place as its job progresses, so no append-only trigger is
-- applied here.
--
-- PII NOTE: item_ids/tenant_id/subject_id are opaque identifiers only;
-- no payload content is ever stored in this table.

CREATE TABLE IF NOT EXISTS zone8_erasure_markers (
    marker_id    TEXT        NOT NULL PRIMARY KEY,
    tenant_id    TEXT        NOT NULL,
    subject_id   TEXT        NOT NULL,
    item_ids     TEXT[]      NOT NULL DEFAULT '{}',
    status       TEXT        NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL,
    retry_count  INTEGER     NOT NULL DEFAULT 0,

    CONSTRAINT chk_zone8_erasure_markers_status
        CHECK (status IN ('pending', 'destroy_confirmed', 'finalized', 'failed')),
    CONSTRAINT chk_zone8_erasure_markers_retry_count_non_negative
        CHECK (retry_count >= 0)
);

-- The recovery sweep's own query (list_unfinalized): a partial index on
-- the two non-terminal states keeps the sweep's scan small in steady
-- state, where almost every marker is already finalized.
CREATE INDEX IF NOT EXISTS idx_zone8_erasure_markers_unfinalized
    ON zone8_erasure_markers (status)
    WHERE status NOT IN ('finalized', 'failed');

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dashanan_zone8_role') THEN
        CREATE ROLE dashanan_zone8_role NOLOGIN;
    END IF;
END
$$;

REVOKE ALL ON zone8_erasure_markers FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON zone8_erasure_markers TO dashanan_zone8_role;
