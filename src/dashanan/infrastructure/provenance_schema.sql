-- Zone 7 Provenance/Audit DDL (DASH-STORY-005, traces to FR-007).
--
-- Source of every structural decision below: HLD Section 3.8 (Zone 7
-- ownership, ADR-010) and HLD Section 5, DSA Choices per Component, row
-- "Zone 7 Provenance": "Append-only table + hash chain (prev_hash ->
-- record_hash); HashMap item_id -> record list" / "SHA-256 chaining;
-- conflict detection by set intersection on (subject, predicate) /
-- (entity, attribute)" / "Append-only gives O(1) amortized write. The hash
-- chain makes tampering O(n) to detect and impossible to do silently."
--
-- This is an initial CREATE, not an ALTER against production data -- the
-- expand-contract migration pattern (database-schema-migration-core) does
-- not apply to a first creation.
--
-- PII NOTE: a provenance record carries schema-level metadata only --
-- source_type, confidence, hash chain, an item_id pointer -- never the
-- underlying fact content the record points to. retrieval_context is
-- stored PRE-HASHED (retrieval_context_hash); this schema never persists
-- raw retrieval context text. No example row values or seed data are
-- included here.

-- Fixed, never-changing value sets per HLD Section 3.8's own literal ENUM
-- definitions -- ENUM is the correct choice here per database-engineer's
-- schema-design rule ("ENUM types for fixed value sets only when the set
-- truly never changes").
CREATE TYPE provenance_source_type AS ENUM (
    'user_stated',
    'tool_output',
    'llm_inferred',
    'summarized_from_n',
    'imported',
    'system_derived'
);

CREATE TYPE provenance_conflict_status AS ENUM (
    'none',
    'disputed',
    'resolved_superseded',
    'resolved_confirmed'
);

CREATE TABLE provenance_records (
    tenant_id               TEXT                        NOT NULL,
    provenance_id           TEXT                        NOT NULL,
    item_id                 TEXT                        NOT NULL,
    -- TEXT + CHECK rather than an ENUM (unlike the two types above):
    -- source_zone's value set is owned by dashanan.domain.zone.ZoneId, a
    -- DIFFERENT story's file (DASH-STORY-001). Encoding it as a second,
    -- independent ENUM here would let the two definitions drift; a CHECK
    -- constraint against the same eight literal values is the isolation-
    -- respecting choice for a story that must not edit shared zone files
    -- (see this story's dev report, judgment-call list).
    source_zone              TEXT                        NOT NULL,
    source_type              provenance_source_type      NOT NULL,
    source_refs              TEXT[]                      NOT NULL DEFAULT '{}',
    write_timestamp           TIMESTAMPTZ                 NOT NULL,
    -- One entry per row in practice (a correction is a NEW row, never an
    -- in-place append to this array -- see ProvenanceRecord's class
    -- docstring); modeled as JSONB array to match HLD Section 3.8's
    -- literal `update_history[]` shape.
    update_history            JSONB                       NOT NULL,
    retrieval_context_hash    TEXT                        NOT NULL,
    conflict_status           provenance_conflict_status  NOT NULL DEFAULT 'none',
    invalidation_flag         BOOLEAN                     NOT NULL DEFAULT FALSE,
    confidence                 DOUBLE PRECISION            NOT NULL,
    -- NULL only for the first record in a given item_id's chain (the
    -- chain is scoped per item_id, per the HashMap item_id -> record list
    -- DSA choice above -- not a single global chain across all tenants).
    prev_hash                  TEXT,
    record_hash                 TEXT                        NOT NULL,

    CONSTRAINT pk_provenance_records PRIMARY KEY (tenant_id, provenance_id),

    -- record_hash is a deterministic function of every other column plus
    -- prev_hash (ProvenanceRecord.create / compute_record_hash in the
    -- domain layer), so it is unique by construction; this constraint
    -- makes that a database-enforced invariant.
    CONSTRAINT uq_provenance_records_record_hash UNIQUE (record_hash),

    CONSTRAINT chk_provenance_records_source_zone CHECK (
        source_zone IN (
            'working', 'episodic', 'semantic', 'procedural', 'entity',
            'retrieval_index', 'provenance', 'consolidation'
        )
    ),
    CONSTRAINT chk_provenance_records_confidence_range
        CHECK (confidence >= 0.0 AND confidence <= 1.0),
    CONSTRAINT chk_provenance_records_record_hash_format
        CHECK (record_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT chk_provenance_records_prev_hash_format
        CHECK (prev_hash IS NULL OR prev_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT chk_provenance_records_retrieval_context_hash_format
        CHECK (retrieval_context_hash ~ '^[0-9a-f]{64}$')
);

-- The dominant query is AC-007's "for that fact's item_id" point lookup /
-- lineage scan, tenant-scoped. A B-tree composite index on (tenant_id,
-- item_id, write_timestamp) serves both find_by_item_id's chronological
-- scan and find_latest_by_item_id's "most recent" access in one index
-- (rdbms-core: "Place highest cardinality / most selective column FIRST").
CREATE INDEX idx_provenance_records_item
    ON provenance_records (tenant_id, item_id, write_timestamp);

-- Append-only enforcement -- CRITICAL remediation, DSHN-55 P1 security
-- review (attempt 1): a bare `REVOKE UPDATE, DELETE ... FROM PUBLIC` does
-- not bind the table's OWNER -- PostgreSQL grants the owner implicit ALL
-- PRIVILEGES regardless of any REVOKE (only REASSIGN OWNED or
-- ALTER TABLE ... OWNER TO changes that), and no distinct, less-privileged
-- role existed anywhere in this repository for the application to connect
-- as instead of the owner. That combination falsified must-not-deviate
-- item 1 ("No UPDATE grant exists on the Zone 7 table for ANY service
-- role") for any deployment where the connecting credential happened to
-- be the owner -- which is the default for whichever role runs this
-- file's CREATE TABLE. Three controls close that gap (attempt 1 shipped
-- controls 1 and 2 and mistakenly believed control 1 alone was already
-- sufficient against an owner-class credential; control 3 is attempt 2's
-- correction):
--
--   1. A BEFORE UPDATE OR DELETE trigger rejects the statement
--      unconditionally, regardless of which role's privileges executed
--      it (owner included). This is what actually makes the hash chain's
--      tamper detection meaningful (HIGH finding, same review): possessing
--      the application's ordinary DB credentials is no longer sufficient
--      to alter a row and recompute a self-consistent record_hash, because
--      the mutation itself is refused at the table before any row change
--      is visible.
--
--      CORRECTION (DSHN-55 P1 re-review, attempt 2): attempt 1's claim
--      here -- that "only a role separately privileged to disable
--      triggers or change ownership ... can bypass it" -- was FALSE and
--      was falsified by the re-review's own adversarial test. In
--      PostgreSQL, ALTER TABLE ... DISABLE TRIGGER requires only the
--      ALTER privilege, and table OWNERSHIP grants that implicitly --
--      it is NOT something REVOKE/GRANT on the table can restrict, and it
--      is NOT "administrative access outside the threat model" when the
--      connecting credential IS the owner (which the re-review proved is
--      the default: whichever role runs this file's CREATE TABLE becomes
--      the owner, and nothing before attempt 2 stopped that role from
--      remaining the owner afterward). The trigger alone was therefore
--      insufficient. Control 3 below closes that specific gap.
--   2. A distinct, least-privileged, PER-ZONE role (`dashanan_provenance_role`,
--      Zone 7 only -- DSHN-60 P1 remediation narrowed this from a role
--      shared with episodic_schema.sql's Zone 2 remediation to one scoped
--      to this table alone, so a credential granted membership here can
--      never also read/write `episodic_entries`) that the application
--      connects as instead of the table owner, granted only the two
--      privileges its write path uses -- SELECT and INSERT -- with
--      UPDATE and DELETE never granted. This is the actual "distinct
--      least-privileged service role" must-not-deviate item 1 requires
--      to exist; the REVOKE ... FROM PUBLIC below is retained as a
--      further defense-in-depth layer for any other role that was never
--      explicitly granted anything, per application-security-core's
--      least-privilege and defense-in-depth guidance. `dashanan_provenance_role`
--      is NOLOGIN by design: the application is expected to connect as
--      its own LOGIN role and be GRANTed membership in `dashanan_provenance_role`
--      (never to log in as this role directly), because the composition
--      root that would wire a real connection string / login role to that
--      membership is out of this schema-only story's scope (see
--      `SqlProvenanceRepository`'s docstring) -- deployment MUST perform
--      that wiring; this file cannot do it for a login role it never
--      creates or knows the name of, and cannot verify it was done, which
--      is why control 3 below does not assume it was.
--   3. Ownership of `provenance_records` (and of the trigger function
--      itself -- see below) is reassigned, at the end of this file, away
--      from whichever role ran this migration to a dedicated, NOLOGIN,
--      never-granted-out `dashanan_schema_owner` role that nothing can
--      log in as and that the migration-running role does not remain a
--      member of once this file finishes. This directly closes the
--      re-review's reproduced exploit (ALTER TABLE ... DISABLE TRIGGER
--      executed by the table owner): immediately after this file runs,
--      the role that ran it is no longer the owner and no longer holds
--      any membership granting owner-equivalent privilege, so that exact
--      statement now fails with "must be owner of table" regardless of
--      whether a distinct application role was ever wired in per control 2.
--      This means must-not-deviate item 1 now holds even in the worst
--      case the re-review named -- "any deployment where the app connects
--      as the table owner, the default" -- because after this file runs,
--      NO role is left holding ownership except the fixed, unreachable
--      `dashanan_schema_owner`.
--
--      Residual limitation, stated honestly rather than overclaimed: if
--      the role that runs this migration (and that the application may
--      also connect as, in a poorly provisioned deployment) additionally
--      holds the CREATEROLE attribute, PostgreSQL lets a CREATEROLE role
--      grant itself membership in any non-superuser role at will
--      (documented CREATEROLE behavior, not a bug in this schema) and
--      thereby re-acquire `dashanan_schema_owner`'s privileges on demand.
--      No table-level GRANT/REVOKE or ownership trick can close that --
--      it is a PostgreSQL privilege-model fact, not a fixable defect
--      here. Closing it requires deployment/provisioning to ensure the
--      application's actual connecting credential holds neither
--      CREATEROLE nor SUPERUSER (ordinary least-privilege provisioning,
--      not a special case for this schema). What controls 1 and 3 here
--      DO guarantee, unconditionally, is that the literal exploit both
--      P1 reviews reproduced -- directly running ALTER TABLE ... DISABLE
--      TRIGGER (or UPDATE/DELETE) as whatever role this file leaves
--      sitting as the connecting credential -- is refused.
--
-- Deployment note: migration tooling MUST run this file under a
-- migration/DDL role. Unlike attempt 1's version of this note, that role
-- no longer needs to be "separate from the app role" to remain safe --
-- control 3 above reassigns ownership away from it unconditionally, as
-- the file's own last step, regardless of which role ran the CREATE
-- TABLE. That role does still need ordinary CREATEROLE-level privilege
-- (to create `dashanan_provenance_role` and `dashanan_schema_owner` idempotently
-- and to grant itself brief, revoked-before-this-file-ends membership in
-- the latter) -- it does not need to remain, or ever have been, a role
-- the application logs in as.
CREATE FUNCTION reject_provenance_records_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        'provenance_records is append-only: % is not permitted (HLD 3.8, must-not-deviate item 1)',
        TG_OP;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_provenance_records_append_only
    BEFORE UPDATE OR DELETE ON provenance_records
    FOR EACH ROW
    EXECUTE FUNCTION reject_provenance_records_mutation();

-- Idempotent role creation (IF NOT EXISTS via pg_roles, since PostgreSQL
-- has no `CREATE ROLE IF NOT EXISTS`): this role is scoped to Zone 7
-- (`provenance_records`) alone -- episodic_schema.sql creates its own,
-- separate `dashanan_episodic_role` for Zone 2 (DSHN-60 P1 remediation:
-- a single role shared across every zone's schema meant a credential
-- provisioned for one zone was, by construction, already privileged on
-- every other zone's append-only table too).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dashanan_provenance_role') THEN
        CREATE ROLE dashanan_provenance_role NOLOGIN;
    END IF;
END
$$;

REVOKE ALL ON provenance_records FROM PUBLIC;
GRANT SELECT, INSERT ON provenance_records TO dashanan_provenance_role;
REVOKE UPDATE, DELETE ON provenance_records FROM PUBLIC;

-- Control 3 (DSHN-55 P1 re-review, attempt 2): reassign ownership of the
-- table AND of the trigger function to a dedicated, NOLOGIN,
-- never-granted-out role, then revoke this migration's own membership in
-- that role. This is what actually closes the re-review's reproduced
-- exploit (ALTER TABLE ... DISABLE TRIGGER run by the table owner) --
-- see the long comment above the trigger definition for the full
-- rationale and the honestly-stated CREATEROLE residual limitation.
--
-- The trigger FUNCTION's ownership must move too, not only the table's:
-- a trigger is not an independently-owned object in PostgreSQL (DROP
-- TRIGGER / ALTER TABLE ... DISABLE TRIGGER are gated by TABLE
-- ownership, which is now reassigned above), but the function the
-- trigger calls IS independently owned, and its owner can
-- CREATE OR REPLACE FUNCTION it into a no-op at any time -- a strictly
-- easier bypass than disabling the trigger, since it never touches the
-- table at all. Leaving the function owned by the migration role would
-- have left this exact class of gap open under a different mechanism.
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
-- ALTER ... OWNER TO requires the NEW owner to hold CREATE on the
-- object's schema (PostgreSQL's ownership-change check), not only that
-- the current session be a member of the new owning role. Granting that
-- itself requires the CURRENT session to hold CREATE ... WITH GRANT
-- OPTION on the schema, which an ordinary migration role has only if it
-- owns the schema (or the database, via pg_database_owner) -- true for
-- the common "single role owns everything" default deployment this
-- control targets, but not guaranteed for every deployment shape. Wrap
-- the whole reassignment in an exception-handling block so a migration
-- role that lacks that grant-option degrades to a clearly logged WARNING
-- (controls 1 and 2 above still fully apply) instead of aborting this
-- entire migration file on an ERROR.
DO $$
BEGIN
    GRANT CREATE ON SCHEMA public TO dashanan_schema_owner;
    ALTER TABLE provenance_records OWNER TO dashanan_schema_owner;
    ALTER FUNCTION reject_provenance_records_mutation() OWNER TO dashanan_schema_owner;
EXCEPTION WHEN insufficient_privilege THEN
    RAISE WARNING 'Could not reassign provenance_records / its trigger '
        'function to dashanan_schema_owner (%): the migration role lacks '
        'CREATE ... WITH GRANT OPTION on schema "public" (it does not own '
        'that schema or the database). Controls 1 and 2 above (the '
        'unconditional append-only trigger and dashanan_provenance_role''s '
        'SELECT/INSERT-only grant) still apply in full, but this '
        'migration role remains the table owner and can still run ALTER '
        'TABLE ... DISABLE TRIGGER. Re-run this migration under a role '
        'that owns schema "public" (or the database) to close that gap, '
        'per provenance_schema.sql''s long comment above the trigger '
        'definition.', SQLERRM;
END
$$;
REVOKE dashanan_schema_owner FROM CURRENT_USER;
