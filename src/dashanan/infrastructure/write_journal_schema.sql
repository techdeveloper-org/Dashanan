-- Provenance write-path WAL/outbox journal DDL (DASH-STORY-006, traces to FR-010).
--
-- Source of every structural decision below: HLD Section 7.2 (write path),
-- ADR-010 (Provenance durability -- write-ahead journal / Outbox pattern),
-- and HLD Section 10 threat S-2 (forged provenance).
--
-- This is a separate table from `provenance_records` (provenance_schema.sql,
-- DASH-STORY-005): ADR-010 is explicit that the journal is what the
-- Orchestrator appends to synchronously ("On every accepted write, the
-- Orchestrator appends the ProvenanceRecord to a LOCAL DURABLE APPEND-ONLY
-- JOURNAL and fsyncs BEFORE returning 202. A provenance-relay worker drains
-- the journal into Zone 7."), and Zone 7 itself is therefore never on the
-- synchronous write path. This is an initial CREATE, not an ALTER against
-- production data.
--
-- PII NOTE: every column here is write-path CONTROL METADATA -- a
-- caller-asserted source_type, a caller identity, a pre-hashed
-- retrieval_context_hash -- never the underlying fact payload a write
-- carries. No example row values or seed data are included here.

CREATE TABLE provenance_write_journal (
    tenant_id               TEXT         NOT NULL,
    write_id                TEXT         NOT NULL,
    item_id                 TEXT         NOT NULL,
    -- TEXT + CHECK rather than an ENUM, matching provenance_schema.sql's
    -- own source_zone column: the eight-zone value set is owned by
    -- dashanan.domain.zone.ZoneId, a different story's file, and this
    -- story must not introduce a second definition that could drift.
    source_zone              TEXT         NOT NULL,
    -- Also TEXT + CHECK rather than referencing provenance_schema.sql's
    -- `provenance_source_type` ENUM: that type is DASH-STORY-005's own
    -- file, and this journal must construct correctly whether or not
    -- that migration has run yet in a given environment (this story's
    -- file-disjointness requirement -- see the dev report's
    -- judgment-call list). A row only ever reaches this table once
    -- `ProvenanceWriteGate.submit_write` has already resolved
    -- source_type against the same six fixed values, so the two CHECK
    -- constraints are guaranteed to agree in practice.
    source_type              TEXT         NOT NULL,
    source_refs              TEXT[]       NOT NULL DEFAULT '{}',
    -- HLD Threat S-2: "source_type is ... recorded with the caller's
    -- identity and the retrieval_context hash."
    caller_identity           TEXT         NOT NULL,
    retrieval_context_hash    TEXT         NOT NULL,
    -- Caller-supplied replay/idempotency nonce (ProvenanceWriteGate's
    -- own hardening, this story's remediation pass): a verbatim replay
    -- of a previously accepted WriteRequest carries the same
    -- (tenant_id, idempotency_key) pair, so the UNIQUE constraint below
    -- is the database-level backstop behind
    -- ProvenanceWriteGate.submit_write's own find_by_idempotency_key
    -- lookup -- without a caller nonce, a captured valid WriteRequest
    -- could be replayed verbatim and accepted identically every time.
    idempotency_key           TEXT         NOT NULL,
    -- HLD Threat S-2: "user_stated requires an explicit host-side
    -- user-turn marker." Recorded even for non-user_stated rows (always
    -- FALSE there) so the S-2 binding is auditable per row, not just
    -- inferred from source_type. Genuineness is established BEFORE
    -- this row is ever inserted: ProvenanceWriteGate.submit_write
    -- verifies a host-signed UserTurnAttestation against a signing key
    -- the write path's caller never holds, so a TRUE value here always
    -- corresponds to a verified attestation, never an unverified
    -- caller claim alone.
    user_turn_marker          BOOLEAN      NOT NULL DEFAULT FALSE,
    written_at                TIMESTAMPTZ  NOT NULL,

    -- write_id is generated per accepted write (uuid4), unique on its
    -- own; tenant_id is included in the primary key to match every
    -- other zone table's tenant-scoped-PK convention in this codebase
    -- (episodic_schema.sql, provenance_schema.sql) rather than to
    -- disambiguate write_id, which does not collide across tenants.
    CONSTRAINT pk_provenance_write_journal PRIMARY KEY (tenant_id, write_id),

    -- Replay/idempotency backstop: the database itself refuses a second
    -- row for a (tenant_id, idempotency_key) pair even if a future
    -- write path somehow bypassed ProvenanceWriteGate's own
    -- in-application find_by_idempotency_key check.
    CONSTRAINT uq_provenance_write_journal_idempotency
        UNIQUE (tenant_id, idempotency_key),

    CONSTRAINT chk_provenance_write_journal_source_zone CHECK (
        source_zone IN (
            'working', 'episodic', 'semantic', 'procedural', 'entity',
            'retrieval_index', 'provenance', 'consolidation'
        )
    ),
    CONSTRAINT chk_provenance_write_journal_source_type CHECK (
        source_type IN (
            'user_stated', 'tool_output', 'llm_inferred',
            'summarized_from_n', 'imported', 'system_derived'
        )
    ),
    CONSTRAINT chk_provenance_write_journal_retrieval_context_hash_format
        CHECK (retrieval_context_hash ~ '^[0-9a-f]{64}$'),
    -- Defense-in-depth: the S-2 binding is enforced in
    -- ProvenanceWriteGate.submit_write BEFORE this INSERT is ever
    -- issued, but a database-level CHECK makes the invariant hold even
    -- against a future write path that reaches this table by a route
    -- other than the gate -- must-not-deviate item 3
    -- ("structurally unbypassable, never a per-call convention").
    CONSTRAINT chk_provenance_write_journal_user_stated_requires_marker CHECK (
        source_type <> 'user_stated' OR user_turn_marker = TRUE
    )
);

-- The dominant query shape a future provenance-relay worker (ADR-010,
-- out of this story's scope) needs is "every entry for this item_id, in
-- write order" -- the same lineage-scan shape provenance_schema.sql's
-- own index serves for Zone 7 itself.
CREATE INDEX idx_provenance_write_journal_item
    ON provenance_write_journal (tenant_id, item_id, written_at);

-- Append-only enforcement (WAL/outbox semantics, ADR-010): once a write
-- clears ProvenanceWriteGate and is journaled, that journal entry is
-- never edited or removed by this story's own write path. Mirrors
-- episodic_schema.sql's and provenance_schema.sql's identical pattern.
REVOKE UPDATE, DELETE ON provenance_write_journal FROM PUBLIC;
