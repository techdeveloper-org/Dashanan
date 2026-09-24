-- Zone 5 Entity Memory DDL (DASH-STORY-029-DEV, traces to FR-005 / HLD ADR-006).
--
-- Source of every structural decision below:
-- docs/phase-1.5-design/zone5-shape-b-storage-design.md v4 (DRAFT, NOT
-- IMPLEMENTATION-READY -- see that document's Section 8 for the 5 disclosed
-- open items this schema does not silently resolve) Section 3 (schema) and
-- Section 5 (compliance-role grant). Mirrors episodic_schema.sql's
-- structural conventions where they apply (idempotent role creation,
-- REVOKE ALL ... FROM PUBLIC baseline); departs from them where Zone 5's
-- own access pattern (point lookup + point update, not append-only range
-- scan) differs -- see the design doc's Section 4 for why no append-only
-- trigger and no dashanan_schema_owner ownership-reassignment tail are
-- added here.
--
-- This is an initial CREATE, not an ALTER against production data.
--
-- PII NOTE: Zone 5 stores real per-tenant attribute values at runtime --
-- this is its whole purpose (entity_memory_repository.py's own PII NOTE,
-- reused verbatim here). This schema-shape file includes no example row
-- values or seed data.

CREATE TABLE entity_attributes (
    tenant_id       TEXT        NOT NULL,
    entity_id       TEXT        NOT NULL,
    attribute_name  TEXT        NOT NULL,
    value           TEXT        NOT NULL,
    provenance_id   TEXT        NOT NULL,
    subject_id      TEXT,
    updated_at      TIMESTAMPTZ NOT NULL,

    -- Composite PK matching EntityMemoryRepository's own domain key (design
    -- doc Section 2 convention 1). A write_attribute call's UPSERT touches
    -- exactly this one row -- the same "one dict entry per call" guarantee
    -- AC-005-2 requires of the Shape A adapter (must-not-deviate item 4,
    -- entity_memory_repository.py docstring).
    CONSTRAINT pk_entity_attributes PRIMARY KEY (tenant_id, entity_id, attribute_name),

    CONSTRAINT chk_entity_attributes_provenance_id_non_blank CHECK (length(trim(provenance_id)) > 0)
);

-- ADR-006's mandatory DPDP secondary index (design doc Section 2 convention
-- 2) -- feeds dpdp-crypto-shredding-full-erasure-design.md Section 3's
-- subject_item_index resolution. Partial index: most attribute writes may
-- not carry a subject_id (openapi.yaml's own provenance.subject_id is
-- nullable=true), so indexing only non-null rows keeps this index small and
-- avoids indexing every entity attribute a tenant has ever written.
CREATE INDEX idx_entity_attributes_subject_id
    ON entity_attributes (tenant_id, subject_id)
    WHERE subject_id IS NOT NULL;

-- Point-lookup path (AC-005-1, get_entity's O(1)-per-entity read): the PK's
-- own B-tree already serves this (tenant_id, entity_id, attribute_name is a
-- valid prefix match for a (tenant_id, entity_id) lookup), so no separate
-- index is added here -- noted explicitly so a future reader does not add a
-- redundant one (design doc Section 3).

-- Alias resolution (AC-005-3/AC-005-4): a second table, not a column on
-- entity_attributes, because one entity can have zero or many aliases and
-- an alias is registered independently of any particular attribute write
-- (register_alias carries no provenance_id of its own --
-- entity_memory_repository.py's own docstring: "a pure indexing operation
-- over already-provenanced entity data").
CREATE TABLE entity_aliases (
    tenant_id     TEXT        NOT NULL,
    entity_id     TEXT        NOT NULL,
    alias         TEXT        NOT NULL,
    registered_at TIMESTAMPTZ NOT NULL,

    -- Composite PK, not (tenant_id, alias) alone: AliasTrie.insert (Shape A)
    -- allows the same alias to resolve to more than one entity_id
    -- (resolve_alias_prefix/resolve_exact_term both return a frozenset, not
    -- a single value) -- an alias is not unique per tenant, only per
    -- (tenant, alias, entity) triple.
    CONSTRAINT pk_entity_aliases PRIMARY KEY (tenant_id, alias, entity_id)
);

-- AC-005-3's prefix-search path. B-tree on alias supports both exact match
-- (AC-005-4) and prefix search via a parameterized `alias LIKE %s || '%%'`
-- range-scan pattern.
CREATE INDEX idx_entity_aliases_prefix ON entity_aliases (tenant_id, alias);

-- Reverse lookup for erase_entity's alias cleanup (DSHN-70 fix,
-- entity_memory_repository.py's own erase_entity docstring: "purges
-- entity_id from tenant_id's own AliasTrie"). Makes "every alias for this
-- entity_id, this tenant" a direct index lookup instead of a PK-order scan
-- filtered by a non-prefix column.
CREATE INDEX idx_entity_aliases_by_entity ON entity_aliases (tenant_id, entity_id);

-- Idempotent role creation, mirroring episodic_schema.sql's convention exactly.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dashanan_entity_role') THEN
        CREATE ROLE dashanan_entity_role NOLOGIN;
    END IF;
END
$$;

REVOKE ALL ON entity_attributes FROM PUBLIC;
REVOKE ALL ON entity_aliases FROM PUBLIC;
-- SELECT, INSERT, UPDATE, DELETE for the ordinary app role (design doc
-- Section 4: Zone 5's own writes AND ordinary application-level attribute
-- overwrites need UPDATE, unlike Zone 2's append-only INSERT-only grant) --
-- DELETE here covers a *non-DPDP* use case only (an application-level
-- "unset this attribute" operation, if one is ever added); the DPDP
-- erasure path uses the separate compliance role below, not this grant,
-- per the design doc's own Section 5 decision.
GRANT SELECT, INSERT, UPDATE, DELETE ON entity_attributes TO dashanan_entity_role;
GRANT SELECT, INSERT, DELETE ON entity_aliases TO dashanan_entity_role;
-- No UPDATE on entity_aliases: an alias registration is either present or
-- absent (register_alias inserts; there is no "rename an alias" operation
-- in the Shape A interface), so UPDATE is never needed and is not granted.

-- Design doc Section 5: extend the DPDP design doc's existing
-- `dashanan_compliance_erasure_role` (dpdp-crypto-shredding-full-erasure-
-- design.md Section 5) with a DELETE grant on both Zone 5 tables -- NOT a
-- new, Zone-5-specific role (MUST-NOT-DEVIATE: "does not create a second,
-- Zone-5-specific compliance role"). No SELECT, INSERT, or UPDATE grant
-- here either -- identical posture to the Zone 2 grant this extends: the
-- erasure job resolves which (tenant_id, entity_id, attribute_name) /
-- (tenant_id, entity_id) rows to delete via subject_item_index (read under
-- a different, read-only credential) and hands this role only the
-- resolved keys as bind parameters.
--
-- Design doc Section 8 item 4 (open item, disclosed rather than silently
-- resolved): the DPDP design doc's own CREATE ROLE statement does not name
-- a specific schema file it runs from, so this file's own GRANT must
-- tolerate the role not existing yet at the time this file is applied --
-- the idempotent guard below creates it NOLOGIN (no password is known to
-- this file) if absent, so this GRANT never fails migration order
-- regardless of which schema file's turn it is. A future login/password
-- binding for this role (if ever required beyond the DELETE-only,
-- resolved-primary-keys usage the design doc specifies) is that other
-- story's own responsibility, not this file's.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'dashanan_compliance_erasure_role'
    ) THEN
        CREATE ROLE dashanan_compliance_erasure_role NOLOGIN;
    END IF;
END
$$;

GRANT DELETE ON entity_attributes TO dashanan_compliance_erasure_role;
GRANT DELETE ON entity_aliases TO dashanan_compliance_erasure_role;

-- FIX (found 2026-09-24 by a real-role integration-test run against a real
-- Postgres 16 container): PostgreSQL requires SELECT privilege on any
-- column referenced in a DELETE statement's WHERE clause or RETURNING
-- clause, in addition to DELETE privilege on the table -- confirmed
-- empirically: a role holding ONLY DELETE (no SELECT) gets "permission
-- denied for table entity_attributes" on sql_entity_memory_repository.py's
-- erase_entity DELETE ... WHERE tenant_id = %s AND entity_id = %s
-- RETURNING attribute_name statement, even after a correct SET ROLE. This
-- is NOT a broadening of what the role can do -- it can still only ever
-- see the rows its own WHERE clause already names, never an independent
-- SELECT * scan -- it is the minimum grant PostgreSQL requires for the
-- already-scoped DELETE to execute and return its RETURNING rows at all.
-- Without this, DASH-STORY-029's own AC-029-DEV-2/QA-1 (erase_entity
-- actually running under this role) cannot pass against a real
-- least-privilege deployment. The comment above this block ("No SELECT,
-- INSERT, or UPDATE grant here either") described the intended posture
-- correctly but did not anticipate this PostgreSQL mechanics requirement;
-- corrected here rather than silently, per this design doc's own
-- disclose-don't-silently-resolve convention.
GRANT SELECT ON entity_attributes TO dashanan_compliance_erasure_role;
GRANT SELECT ON entity_aliases TO dashanan_compliance_erasure_role;

-- No append-only trigger and no dashanan_schema_owner ownership-
-- reassignment tail here -- design doc Section 4's explicit decision:
-- `EntityMemoryRepository.write_attribute` overwrites its dict entry on
-- every call (a mutable current-state store, not an append-only log), so
-- entity_attributes is an ordinary mutable table with no escape hatch to
-- harden against `ALTER TABLE ... DISABLE TRIGGER` -- there is no trigger
-- to disable.
