-- Zone 3 Semantic Memory DDL (DASH-STORY-012, traces to FR-003).
--
-- Source of every structural decision below: HLD Section 3.4 (Zone 3
-- ownership, ADR-006; the object_ref NOT NULL CHECK constraint closing
-- PRD risk R-005) and HLD Section 3.10 (Zone 3 OWNS: SemanticEdge,
-- GeneralFact; Hard Rule 1: no two zones share a table; Hard Rule 2:
-- entity_id references only) and HLD Section 5, DSA Choices per
-- Component, row "Zone 3 Semantic": "Adjacency list as an edge table
-- with B-tree indexes on BOTH subject_ref and object_ref; Union-Find for
-- entity-alias merging" / "Bounded-depth BFS, O(V+E) within the depth
-- bound" / "Sparse graph (E ~ O(V)) so adjacency list beats a matrix's
-- O(V^2) space."
--
-- ADR-006: "One relational engine backing Zones 2, 3, 4, 5 and 7 ... with
-- SEPARATE SCHEMAS and separate roles per zone. Not shared tables; shared
-- engine." -- dashanan_semantic_role below is scoped to Zone 3's two
-- tables alone, the same per-zone-role pattern episodic_schema.sql
-- (dashanan_episodic_role) and provenance_schema.sql
-- (dashanan_provenance_role) already establish.
--
-- Unlike episodic_entries / provenance_records, Zone 3's tables are NOT
-- append-only: SemanticEdge/GeneralFact carry a `state` field in the
-- locked Active -> Compressed rotation state machine (HLD Section 6) and
-- a `score_terms` field the scoring sweep re-computes, both of which are
-- ordinary in-place UPDATEs, not new-row corrections -- there is
-- therefore no `REVOKE UPDATE` / append-only trigger here (see this
-- story's dev report, judgment-call list, for why the DSHN-55/DSHN-60
-- append-only remediation pattern does not transfer to this table).
--
-- This is an initial CREATE, not an ALTER against production data -- the
-- expand-contract migration pattern (database-schema-migration-core) does
-- not apply to a first creation.
--
-- PII NOTE: `qualifiers` and `statement` are Zone 3's own opaque payload
-- content (mirrors episodic_entries.payload); subject_ref/object_ref are
-- Zone 5 entity_id REFERENCES ONLY, never a copied attribute value (HLD
-- Section 3.10 Hard Rule 2). No example row values or seed data are
-- included here.

-- A fixed, never-changing value set (the Phase 0 locked rotation state
-- machine), matching episodic_schema.sql's identical `episode_state`
-- ENUM shape and value set -- ENUM is the correct choice here per
-- database-engineer's schema-design rule ("ENUM types for fixed value
-- sets only when the set truly never changes").
CREATE TYPE semantic_state AS ENUM ('active', 'compressed', 'archived');

CREATE TABLE semantic_edges (
    tenant_id     TEXT            NOT NULL,
    edge_id       TEXT            NOT NULL,
    subject_ref   TEXT            NOT NULL,
    predicate     TEXT            NOT NULL,
    -- Nullable at the column level so the CHECK constraint below is the
    -- mechanism that actually performs the rejection -- AC-003-SCHEMA-1's
    -- literal wording is "the database-level CHECK constraint rejects
    -- it," and must-not-deviate item 2 names "a DB CHECK/partial-unique
    -- constraint" as the sanctioned enforcement shape.
    object_ref    TEXT,
    qualifiers    JSONB           NOT NULL DEFAULT '{}',
    state         semantic_state  NOT NULL DEFAULT 'active',
    score_terms   JSONB           NOT NULL DEFAULT '{}',

    CONSTRAINT pk_semantic_edges PRIMARY KEY (tenant_id, edge_id),

    -- AC-003-SCHEMA-1 / must-not-deviate item 2: a SemanticEdge with
    -- object_ref IS NULL is rejected at the database level, making the
    -- R-005 non-duplication regression structurally impossible rather
    -- than review-dependent (HLD Section 3.4: "a single-subject-no-object
    -- fact is by definition Entity-owned"). This is the ONLY constraint
    -- this story's must-not-deviate list requires; it is deliberately not
    -- widened into a broader partial-unique constraint (e.g. de-duplicating
    -- identical (subject_ref, predicate, object_ref) triples) since HLD
    -- Section 3.4 does not specify that as part of the non-duplication
    -- guarantee -- see the dev report's judgment-call list.
    CONSTRAINT chk_semantic_edges_object_ref_not_null CHECK (object_ref IS NOT NULL)
);

-- Two-sided B-tree per HLD Section 5, DSA row 3: forward traversal
-- ("what does subject_ref relate to") and reverse traversal ("what points
-- at object_ref" -- HLD's own worked example's index_reverse=true case,
-- "who lives in <ENTITY_B>?") are both first-class query shapes this
-- adapter's find_edges_by_subject / find_edges_by_object methods serve.
CREATE INDEX idx_semantic_edges_subject
    ON semantic_edges (tenant_id, subject_ref);
CREATE INDEX idx_semantic_edges_object
    ON semantic_edges (tenant_id, object_ref);

-- ADR-006 India Layer: "a subject_id secondary index on every zone table
-- ... is MANDATORY, not optional." idx_semantic_edges_subject above IS
-- that mandatory index for semantic_edges -- subject_ref is this table's
-- subject-scoped erasure key, so a DPDP cascade deletes by an indexed
-- (tenant_id, subject_ref) lookup, never a full table scan.

CREATE TABLE general_facts (
    tenant_id      TEXT            NOT NULL,
    fact_id        TEXT            NOT NULL,
    statement      TEXT            NOT NULL,
    subject_scope  TEXT            NOT NULL,
    state          semantic_state  NOT NULL DEFAULT 'active',
    score_terms    JSONB           NOT NULL DEFAULT '{}',

    CONSTRAINT pk_general_facts PRIMARY KEY (tenant_id, fact_id)

    -- No subject_ref / entity_id column exists on this table by
    -- definition (HLD Section 3.4: GeneralFact is selected precisely
    -- when |subjects| == 0 -- "world knowledge owned by no entity"), so
    -- ADR-006's mandatory subject_id erasure index has no column to bind
    -- to here: there is no subject entity for a subject-scoped cascade to
    -- key on. See this story's dev report, judgment-call list.
);

-- AC-003-OWN-1 / HLD Section 3.10 Hard Rule 1 ("no two zones share a
-- table"): semantic_edges and general_facts are the only two tables this
-- file creates, and must-not-deviate item 1 forbids adding a third owned
-- entity type here or anywhere else in this story's scope.

-- Idempotent role creation (IF NOT EXISTS via pg_roles, since PostgreSQL
-- has no `CREATE ROLE IF NOT EXISTS`): scoped to Zone 3's two tables
-- alone, per ADR-006's "separate roles per zone" -- a credential granted
-- membership here can never also read/write episodic_entries or
-- provenance_records (the same isolation rationale as those two files'
-- own per-zone roles).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dashanan_semantic_role') THEN
        CREATE ROLE dashanan_semantic_role NOLOGIN;
    END IF;
END
$$;

-- Least privilege (application-security-core): SELECT/INSERT/UPDATE only
-- -- UPDATE is granted here (unlike episodic_entries' / provenance_records'
-- SELECT/INSERT-only append-only roles) because state and score_terms are
-- ordinary in-place mutations for this zone, not new-row corrections (see
-- this file's opening comment). DELETE is never granted: a future DPDP
-- subject-scoped erasure cascade is expected to run under a separate,
-- narrowly-scoped compliance role authorized for an indexed
-- (tenant_id, subject_ref) DELETE, mirroring episodic_schema.sql's
-- identical "Zone 2 leg" note -- not by widening this application role's
-- grants.
REVOKE ALL ON semantic_edges, general_facts FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON semantic_edges, general_facts TO dashanan_semantic_role;

-- dashanan_semantic_role is NOLOGIN by design: the application connects
-- as its own LOGIN role and is GRANTed membership in this role -- never
-- logs in as this role directly. Wiring an actual login role's membership
-- is a composition-root concern out of this schema-only story's scope
-- (the same boundary `SqlEpisodicRepository` / `SqlProvenanceRepository`'s
-- docstrings already draw for their own per-zone roles); deployment MUST
-- perform that wiring.
