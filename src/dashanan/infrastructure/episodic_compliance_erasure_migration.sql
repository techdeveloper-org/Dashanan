-- Zone 2 compliance-role DPDP erasure migration (DASH-STORY-027-DEV).
--
-- episodic_schema.sql's own long comment names this exact follow-on
-- before it was ever built: "A DPDP erasure workflow ... is explicitly
-- OUT OF SCOPE for this story. It is not built here. Should a future
-- story implement it, that is expected to run under a separate,
-- narrowly-scoped compliance role authorized for a PK-only DELETE --
-- not by widening this application role's grants, and not by altering
-- or dropping the append-only trigger below." This migration is that
-- future story, and it honours both constraints literally:
--   1. It widens no grant on dashanan_episodic_role -- only
--      dashanan_compliance_erasure_role (already created by
--      entity_schema.sql for the Zone 5 compliance-role leg) gets a new
--      grant here.
--   2. It does not drop trg_episodic_entries_append_only. It replaces
--      the trigger FUNCTION body (CREATE OR REPLACE FUNCTION, the same
--      mechanism-widening pattern zone8_manifest_subject_index_migration.
--      sql uses for an additive schema change against a frozen file) so
--      that ONE additional, narrowly-scoped case is allowed: a DELETE
--      issued while the session's current_user is exactly
--      dashanan_compliance_erasure_role. Every UPDATE, and every DELETE
--      from any other role (the app login role included), is still
--      unconditionally rejected exactly as before -- this is a precise
--      widening of the exception, not a removal of the protection.
--
-- item_id mapping for Zone 2 (judgment call, disclosed): episodic_entries
-- has no item_id column of its own; episode_id is unique per tenant and
-- deterministic (this table's own comment: "a deterministic function of
-- the PK triple"), so SqlZone2ComplianceEviction's evict(tenant_id,
-- item_id) treats item_id as episode_id -- the same identifier
-- CrossZoneDpdpErasureCascade.fulfil_pending_erasure already receives as
-- its own item_id for the Zone 2/6 leg. This is a PK-only delete
-- (uq_episodic_entries_episode_id + tenant_id), never a range scan.
--
-- PII NOTE: this file's own DDL never reads or logs episodic_entries
-- payload content.

-- reject_episodic_entries_mutation() is owned by dashanan_schema_owner
-- (episodic_schema.sql's own control 3) precisely so its owner cannot
-- unilaterally neuter it via CREATE OR REPLACE FUNCTION -- this
-- migration needs that same brief, revoked-afterward membership
-- episodic_schema.sql's own file already uses for the identical reason.
GRANT dashanan_schema_owner TO CURRENT_USER;

CREATE OR REPLACE FUNCTION reject_episodic_entries_mutation()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' AND current_user = 'dashanan_compliance_erasure_role' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION
        'episodic_entries is append-only: % is not permitted (HLD 3.3, must-not-deviate item 4)',
        TG_OP;
END;
$$ LANGUAGE plpgsql;

ALTER FUNCTION reject_episodic_entries_mutation() OWNER TO dashanan_schema_owner;

REVOKE dashanan_schema_owner FROM CURRENT_USER;

GRANT DELETE ON episodic_entries TO dashanan_compliance_erasure_role;

-- FIX (found 2026-09-24 by a real-role integration-test run against a real
-- Postgres 16 container, not caught by DASH-STORY-027-QA's own tests since
-- they never actually connected as dashanan_compliance_erasure_role via
-- SET ROLE against this table -- see sql_zone2_compliance_eviction.py's
-- own DELETE FROM episodic_entries WHERE tenant_id = %s AND episode_id = %s
-- statement): PostgreSQL requires SELECT privilege on any column referenced
-- in a DELETE statement's WHERE clause (or RETURNING clause), in addition to
-- DELETE privilege on the table -- confirmed empirically: a role holding
-- ONLY DELETE (no SELECT) gets "permission denied for table episodic_entries"
-- on this exact WHERE-clause shape, even after a correct SET ROLE. This is
-- NOT a broadening of what the role can do -- it can still only ever see the
-- rows its own WHERE clause already names by primary key, never an
-- independent SELECT * scan -- it is the minimum grant PostgreSQL requires
-- for the already-scoped DELETE ... WHERE tenant_id = %s AND episode_id = %s
-- statement to execute at all. Without this, the Zone 2 leg of the real
-- DPDP erasure cascade (AC-013) cannot run against a real least-privilege
-- deployment.
GRANT SELECT ON episodic_entries TO dashanan_compliance_erasure_role;
