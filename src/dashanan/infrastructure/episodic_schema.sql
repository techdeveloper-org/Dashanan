-- Zone 2 Episodic Memory DDL (DASH-STORY-003, traces to FR-002).
--
-- Source of every structural decision below: HLD Section 3.3 (Zone 2
-- ownership) and HLD Section 5, DSA Choices per Component, row "Zone 2
-- Episodic": "Append-only table + B-tree PK (tenant, session, seq) + BRIN
-- index on occurred_at" / "Keyset (cursor) pagination; range scan" /
-- "Dominant query is a time range. BRIN is ~1000x smaller than B-tree for
-- naturally-ordered timestamps. Keyset is O(1) per page vs offset's
-- O(offset) (api-design-core M1)."
--
-- This is an initial CREATE, not an ALTER against production data -- the
-- expand-contract migration pattern (database-schema-migration-core) does
-- not apply to a first creation.
--
-- PII NOTE: Zone 2 is the Bell-LaPadula-flagged PII-bearing zone. This
-- story is schema-shape only; no example row values or seed data are
-- included here.

-- A fixed, never-changing value set (the Phase 0 locked rotation state
-- machine) -- ENUM is the correct choice here per database-engineer's
-- schema-design rule ("ENUM types for fixed value sets only when the set
-- truly never changes").
CREATE TYPE episode_state AS ENUM ('active', 'compressed', 'archived');

CREATE TABLE episodic_entries (
    tenant_id     TEXT            NOT NULL,
    session_id    TEXT            NOT NULL,
    episode_id    TEXT            NOT NULL,
    seq           BIGINT          NOT NULL,
    occurred_at   TIMESTAMPTZ     NOT NULL,
    written_at    TIMESTAMPTZ     NOT NULL,
    payload       TEXT            NOT NULL,
    actors        TEXT[]          NOT NULL DEFAULT '{}',
    token_count   INTEGER         NOT NULL,
    state         episode_state   NOT NULL DEFAULT 'active',
    score_terms   JSONB           NOT NULL DEFAULT '{}',

    -- Composite B-tree PK (HLD Section 5, DSA row 2). Also the sole path
    -- AC-002-DPDP-1's "locatable" definition permits: an equality lookup
    -- on these three columns, never a range scan on occurred_at.
    CONSTRAINT pk_episodic_entries PRIMARY KEY (tenant_id, session_id, seq),

    -- episode_id is a deterministic function of the PK triple
    -- (Episode.episode_id_for in the domain layer), so it is unique by
    -- construction; the constraint makes that a database-enforced
    -- invariant rather than an application-only assumption.
    CONSTRAINT uq_episodic_entries_episode_id UNIQUE (episode_id),

    CONSTRAINT chk_episodic_entries_seq_non_negative CHECK (seq >= 0),
    CONSTRAINT chk_episodic_entries_token_count_positive CHECK (token_count > 0),
    CONSTRAINT chk_episodic_entries_written_after_occurred CHECK (written_at >= occurred_at)
);

-- BRIN, not B-tree, for the recency range-scan path (HLD Section 5:
-- "~1000x smaller than a B-tree for naturally-ordered timestamps").
-- occurred_at is naturally ordered because seq (part of the PK, and thus
-- of physical insertion order under append-only writes) is monotonically
-- increasing per session at write time, which is the precondition BRIN
-- needs to be effective.
CREATE INDEX idx_episodic_entries_occurred_at_brin
    ON episodic_entries
    USING BRIN (occurred_at);

-- Append-only enforcement (HLD Section 3.3, must-not-deviate item 4) --
-- HIGH remediation, DSHN-55 P1 security review (attempt 1): this table
-- had the identical defect the review found in provenance_schema.sql
-- (Zone 7) -- a bare `REVOKE UPDATE, DELETE ... FROM PUBLIC` does not
-- bind the table's OWNER (PostgreSQL grants the owner implicit ALL
-- PRIVILEGES regardless of any REVOKE), and no distinct, less-privileged
-- role existed for the application to connect as instead of the owner.
-- The same three controls provenance_schema.sql now applies close the
-- gap here:
--
--   1. A BEFORE UPDATE OR DELETE trigger rejects the statement
--      unconditionally, regardless of which role's privileges executed
--      it (owner included) -- see provenance_schema.sql's identical
--      trigger comment for the full rationale, including attempt 2's
--      correction of attempt 1's false claim that only "administrative
--      access outside the threat model" could bypass it (it could not:
--      ordinary table OWNERSHIP grants ALTER implicitly, which the
--      re-review used to disable this exact trigger).
--   2. The same distinct, least-privileged `dashanan_app_role`
--      (created idempotently, shared across both zones) is granted only
--      SELECT and INSERT here -- never UPDATE or DELETE. The
--      REVOKE ... FROM PUBLIC below remains as a defense-in-depth layer
--      for any other role that was never explicitly granted anything.
--      As in provenance_schema.sql, this role is NOLOGIN; wiring an
--      actual login role's membership into it is deployment's
--      responsibility and out of this schema-only story's scope.
--   3. Ownership of `episodic_entries` and of its trigger function is
--      reassigned to the same dedicated, NOLOGIN, never-granted-out
--      `dashanan_schema_owner` role at the end of this file, exactly as
--      in provenance_schema.sql -- see that file's long comment above
--      its trigger definition for the full rationale and the honestly-
--      stated CREATEROLE residual limitation. This is what closes the
--      re-review's reproduced exploit against this table (ALTER TABLE
--      episodic_entries DISABLE TRIGGER, run by the table owner).
--
-- Deployment note: migration tooling MUST run this file under a
-- migration/DDL role. That role no longer needs to be "separate from the
-- app role" to remain safe -- control 3 above reassigns ownership away
-- from it unconditionally, as the file's own last step. It does still
-- need ordinary CREATEROLE-level privilege to create the idempotent
-- roles above and to briefly self-grant, then revoke, membership in
-- `dashanan_schema_owner`.
--
-- A DPDP erasure workflow (AC-002-DPDP-1, P2 advisory consult per
-- AR1-G3) is explicitly OUT OF SCOPE for this story. It is not built
-- here. Should a future story implement it, that is expected to run
-- under a separate, narrowly-scoped compliance role authorized for a
-- PK-only DELETE -- not by widening this application role's grants, and
-- not by altering or dropping the append-only trigger below.
CREATE FUNCTION reject_episodic_entries_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        'episodic_entries is append-only: % is not permitted (HLD 3.3, must-not-deviate item 4)',
        TG_OP;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_episodic_entries_append_only
    BEFORE UPDATE OR DELETE ON episodic_entries
    FOR EACH ROW
    EXECUTE FUNCTION reject_episodic_entries_mutation();

-- Idempotent role creation (IF NOT EXISTS via pg_roles, since PostgreSQL
-- has no `CREATE ROLE IF NOT EXISTS`): this role is shared with
-- provenance_schema.sql's Zone 7 remediation, so either migration file
-- may run first without erroring on a duplicate role.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dashanan_app_role') THEN
        CREATE ROLE dashanan_app_role NOLOGIN;
    END IF;
END
$$;

REVOKE ALL ON episodic_entries FROM PUBLIC;
GRANT SELECT, INSERT ON episodic_entries TO dashanan_app_role;
REVOKE UPDATE, DELETE ON episodic_entries FROM PUBLIC;

-- Control 3 (DSHN-55 P1 re-review, attempt 2): identical mechanism to
-- provenance_schema.sql's -- see that file's matching comment block for
-- the full rationale, including why the trigger FUNCTION's ownership
-- must move too (a trigger has no independent ownership in PostgreSQL;
-- DROP TRIGGER / ALTER TABLE ... DISABLE TRIGGER are gated by table
-- ownership, reassigned below, but the function it calls is
-- independently owned and its owner could otherwise neuter it via
-- CREATE OR REPLACE FUNCTION without ever touching the table).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'dashanan_schema_owner'
    ) THEN
        CREATE ROLE dashanan_schema_owner NOLOGIN;
    END IF;
END
$$;

GRANT dashanan_schema_owner TO CURRENT_USER;
-- Same grant-option caveat as provenance_schema.sql's identical block --
-- see that file's comment for the full rationale.
DO $$
BEGIN
    GRANT CREATE ON SCHEMA public TO dashanan_schema_owner;
    ALTER TABLE episodic_entries OWNER TO dashanan_schema_owner;
    ALTER FUNCTION reject_episodic_entries_mutation() OWNER TO dashanan_schema_owner;
EXCEPTION WHEN insufficient_privilege THEN
    RAISE WARNING 'Could not reassign episodic_entries / its trigger '
        'function to dashanan_schema_owner (%): the migration role lacks '
        'CREATE ... WITH GRANT OPTION on schema "public". Controls 1 and '
        '2 above still apply in full; re-run this migration under a role '
        'that owns schema "public" (or the database) to close the '
        'remaining ownership gap. See provenance_schema.sql''s matching '
        'comment for the full rationale.', SQLERRM;
END
$$;
REVOKE dashanan_schema_owner FROM CURRENT_USER;
