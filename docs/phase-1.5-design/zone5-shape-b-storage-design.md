# Zone 5 (Entity) Shape B Storage — Postgres Adapter Design (Sprint 5 candidate, DASH-STORY-029)

Status: **DRAFT — NOT IMPLEMENTATION-READY.** This document resolves the schema-shape and
privilege-model questions with reasoning grounded in the existing codebase, but leaves genuine
open items (Section 8) that need a human/BA decision or a load-bearing precedent (mirroring how
`connection-pooling-design.md` and `dpdp-crypto-shredding-full-erasure-design.md` each disclose
their own remaining gaps rather than overclaiming readiness).

Related: `docs/phase-1.5-design/dpdp-crypto-shredding-full-erasure-design.md` Section 2 (the
finding that surfaced this gap) and Section 3-5 (the `subject_item_index`/compliance-role pattern
this document extends to Zone 5), `docs/phase-1-architecture/HLD.md` ADR-006 (Zones 2/3/4/5/7
storage — PostgreSQL Shape B / SQLite Shape A, already APPROVED at the architecture level; this
document is the first concrete schema fulfilling that ADR for Zone 5), `src/dashanan/domain/zone.py`
(`ZoneId.ENTITY`), `src/dashanan/infrastructure/entity_memory_repository.py` (the Shape A adapter
this document's Shape B adapter must stay interface-compatible with), `src/dashanan/infrastructure/episodic_schema.sql`
and `src/dashanan/infrastructure/sql_episodic_repository.py` (the precedent this document mirrors
for schema/role conventions), `docs/phase-6-sprint-planning/backlog_draft.json`
`future_sprints_backlog.remaining_frs` `FR-011-ZONE-4-5-SHAPE-B` (the ORIGINAL, now-superseded stub
title this story's own creation narrowed to `FR-011-ZONE-4-SHAPE-B` — Zone 4 durability only; this
story itself is the canonical tracking vehicle for the Zone 5 half, not any FR-011 sub-id, see
Section 1.1).

---

## 1. Problem statement

FR-005 (SRS.md, verbatim): *"The system SHALL provide an Entity Memory zone holding per-entity
(person/project/system) profile records, each the canonical sole owner of that entity's own
attributes."* `EntityMemoryRepository` (Shape A, in-process `dict` storage) already implements
this in full, including `erase_entity` — a real, tested DPDP erasure leg
(`src/dashanan/infrastructure/entity_memory_repository.py`, called today by
`UnifiedSubjectErasureOrchestrator`). **Zone 5's erasure LOGIC already works.** What does not exist
anywhere in this codebase is a **Postgres-backed (Shape B) storage adapter for Zone 5** — confirmed
by inspection: `src/dashanan/infrastructure/` has `episodic_schema.sql`, `semantic_schema.sql`,
`provenance_schema.sql`, `zone8_consolidation_schema.sql`, but no `entity_schema.sql`. Under the
real API host's composition root, Zone 5 data today lives only in process memory — gone on every
restart, and structurally unreachable by `dpdp-crypto-shredding-full-erasure-design.md`'s own
Section 2 finding: *"a `DELETE /tenants/{t}/subjects/{s}` cascade that claims to erase Zone 4/5
content would be erasing data that has no durable existence to begin with in a real Shape B
deployment."*

This document proposes that adapter: a Postgres schema for Zone 5, a least-privilege role model
matching the DPDP design doc's Section 5 compliance-role pattern, and a `SqlEntityMemoryRepository`
that implements the exact same `ZoneRepository`/`erase_entity` interface
`UnifiedSubjectErasureOrchestrator` already calls today for Shape A — so that swapping the
composition root's Zone 5 builder from Shape A to Shape B requires no change to the orchestrator,
DASH-STORY-027's own erasure cascade wiring, or any other zone.

### 1.1 Scope: Zone 5 only, explicitly not Zone 4

**This document is scoped to Zone 5 (Entity) alone.** A prior planning pass conflated Zone 4
(Procedural) and Zone 5 (Entity) under one "Shape B storage prerequisite" framing
(`backlog_draft.json`'s original `FR-011-ZONE-4-5-SHAPE-B` stub title), which this session's own
plan-review corrected against the DPDP design doc's own text
(`dpdp-crypto-shredding-full-erasure-design.md` Section 1, "MAJOR CORRECTION" paragraph, and
Section 2's 2026-09-22 correction note): **Zone 4 has no subject-linkable field in its domain
model at all** (`Procedure`'s real field set carries no `subject_id`, `entity_id`, or any
subject-linkable reference — confirmed by `unified_subject_erasure_orchestrator.py`'s own
docstring, which this session independently re-verified is still true of the domain model as
written). Zone 4 is therefore **permanently excluded from the DPDP erasure rationale, regardless
of storage shape** — there is no erasure seam to build toward, Shape A or Shape B. Building a
`procedural_schema.sql` would be a legitimate *durability* improvement for Zone 4 on its own
merits (Shape A's in-process backstop-sweep store is also non-durable), but it would not resolve
any part of AC-013 or DASH-STORY-027's own scope, and this document does not attempt it — see
Section 9 for why a Zone 4 secondary scope was considered and deliberately left out rather than
folded in here.

Every schema, role, and interface decision below concerns **Zone 5 (Entity) only**. Where this
document references "the Shape B storage prerequisite," it means Zone 5's prerequisite
specifically, narrower than the original stub's title implied.

---

## 2. Existing precedent this design mirrors

`episodic_schema.sql` (Zone 2, DASH-STORY-003) and `sql_episodic_repository.py` establish this
project's own conventions for a Shape B zone adapter, independently confirmed by a second reading
for this document, EXCEPT convention 2, which is not an existing codebase precedent -- see the
correction directly beneath the numbered list before relying on it:

1. **Composite PK matching the domain key**, not a surrogate `id` column — Zone 2 uses
   `(tenant_id, session_id, seq)`; Zone 5's domain key (per `EntityMemoryRepository`'s own storage
   shape, `dict[(tenant_id, entity_id), dict[attribute_name, EntityAttributeRecord]]`) is
   `(tenant_id, entity_id, attribute_name)`.
2. **`subject_id` as a nullable column on every zone table.** Proposed here as a NEW pattern this
   document introduces for Zone 5 -- NOT cited as an existing, inspection-confirmed codebase
   convention. See the correction directly below this list for what this session's own re-reading
   of `episodic_schema.sql` and `semantic_schema.sql` actually found.
3. **Least-privilege, per-zone Postgres roles** (`dashanan_episodic_role`, `dashanan_provenance_role`
   — DSHN-60's P1 remediation narrowed these from a single shared role to one per zone, so a
   credential granted membership in one can never read/write another zone's table) plus, where
   the DPDP design doc's Section 5 compliance-role pattern applies, a distinct compliance-scoped
   role for the erasure path.
4. **`REVOKE ALL ... FROM PUBLIC`** as a defense-in-depth baseline, with the app role granted only
   the specific verbs it needs.
5. **A migration-role-agnostic ownership-reassignment tail** (`dashanan_schema_owner`) — this
   applies only to zones that need an append-only trigger's ownership hardened against
   `ALTER TABLE ... DISABLE TRIGGER`; Section 4 below explains why Zone 5 does not need this
   mechanism.

**Correction (2026-09-22, solution-architect adversarial review): convention 2 above was
originally, and incorrectly, presented as an existing codebase convention "independently confirmed
by inspecting `episodic_schema.sql`."** That claim is false and is corrected here rather than
silently rewritten. This session re-read both schema files directly:

- `episodic_schema.sql` (Zone 2) has **zero** occurrences of `subject_id`, and no other
  subject-linkable column either. There is no generic `subject_id` column anywhere in this
  codebase's existing Shape B schemas today.
- `semantic_schema.sql` (Zone 3) is the closest actual precedent, and it does NOT use a generic
  `subject_id` column: `semantic_edges` carries a domain-specific `subject_ref` field (indexed via
  `idx_semantic_edges_subject`, annotated in that file's own comment as fulfilling "ADR-006 India
  Layer['s] ... `subject_id` secondary index" for that table specifically), and `general_facts`
  **explicitly has no such column at all** — that file's own comment states plainly: "No
  `subject_ref` / `entity_id` column exists on this table by definition ... ADR-006's mandatory
  `subject_id` erasure index has no column to bind to here."
- The only place a literal, generic `subject_id` concept exists anywhere in this codebase's design
  material is the NOT-YET-BUILT `subject_item_index` table proposed in
  `dpdp-crypto-shredding-full-erasure-design.md` Section 3 — a design proposal, not a shipped
  schema.

**Corrected framing:** ADR-006's textual mandate ("a `subject_id` secondary index on every zone
table ... is MANDATORY, not optional," HLD.md Section 10 DPDP-3) is real and does apply to Zone 5.
What is NOT real is any existing, inspection-confirmed precedent for implementing that mandate as a
literal `subject_id` column. `semantic_schema.sql`'s `subject_ref`-based approach — a
domain-specific, table-appropriate column name satisfying the same ADR-006 mandate — is the closest
actual precedent, under a different name than this document originally implied. The literal
`subject_id TEXT` column this document's `entity_schema.sql` (Section 3 below) proposes for
`entity_attributes` is therefore a **NEW pattern this design document is introducing**, extending
the not-yet-built `subject_item_index` proposal from the DPDP design doc to give Zone 5 a concrete
ADR-006-mandated secondary index — not a pattern this document merely follows from prior art. Future
readers should not cite this document as evidence that a generic `subject_id` column is an
established codebase convention; only `semantic_schema.sql`'s `subject_ref` precedent and ADR-006's
own textual mandate may be cited that way.

`entity_schema.sql` (Section 3 below) follows conventions 1 and 4 directly, and introduces
convention 2 as new (per the correction above). Convention 3 and 5 are addressed in Section 5,
where Zone 5's actual mutability (not append-only) changes which parts of the Zone 2 precedent
apply.

---

## 3. Postgres schema for Zone 5

`EntityMemoryRepository`'s Shape A storage shape is the schema's direct source: one
`EntityAttributeRecord` per `(tenant_id, entity_id, attribute_name)`, each carrying its own
`value`, `provenance_id`, and `updated_at` — **not** a whole-entity JSON blob, because
`write_attribute`'s own must-not-deviate constraint (`entity_memory_repository.py`'s docstring)
requires that a single-attribute write "never performs a whole-record rewrite." A row-per-attribute
table preserves that property in Shape B exactly as the `dict`-per-attribute-name shape preserves
it in Shape A: an `UPDATE ... WHERE (tenant_id, entity_id, attribute_name) = (...)` touches exactly
one row, never a JSON column requiring read-modify-write of sibling attributes.

```sql
-- Zone 5 Entity Memory DDL (DASH-STORY-029 candidate, traces to FR-005 / HLD ADR-006).
--
-- Source of every structural decision below: HLD Section 3.6 (Zone 5 ownership) and this
-- document's Section 2 (precedent) and Section 3 (schema rationale). Mirrors
-- episodic_schema.sql's structural conventions where they apply; departs from them where Zone 5's
-- actual access pattern (point lookup + point update, not append-only range scan) differs -- see
-- Section 4 for why no append-only trigger is added here.
--
-- This is an initial CREATE, not an ALTER against production data.
--
-- PII NOTE: Zone 5 stores per-entity attribute values at runtime -- this is its whole purpose
-- (entity_memory_repository.py's own PII NOTE, reused verbatim here). This schema-shape story
-- includes no example row values or seed data.

CREATE TABLE entity_attributes (
    tenant_id       TEXT        NOT NULL,
    entity_id       TEXT        NOT NULL,
    attribute_name  TEXT        NOT NULL,
    value           TEXT        NOT NULL,
    provenance_id   TEXT        NOT NULL,
    subject_id      TEXT,
    updated_at      TIMESTAMPTZ NOT NULL,

    -- Composite PK matching EntityMemoryRepository's own domain key (Section 2 convention 1).
    -- A write_attribute call's UPDATE/INSERT touches exactly this one row -- the same "one dict
    -- entry per call" guarantee AC-005-2 requires of the Shape A adapter (must-not-deviate item
    -- 4, entity_memory_repository.py docstring).
    CONSTRAINT pk_entity_attributes PRIMARY KEY (tenant_id, entity_id, attribute_name),

    CONSTRAINT chk_entity_attributes_provenance_id_non_blank CHECK (length(trim(provenance_id)) > 0)
);

-- ADR-006's mandatory DPDP secondary index (Section 2 convention 2) -- feeds
-- dpdp-crypto-shredding-full-erasure-design.md Section 3's subject_item_index resolution.
-- Partial index: most attribute writes may not carry a subject_id (openapi.yaml's own
-- provenance.subject_id is nullable=true), so indexing only non-null rows keeps this index small
-- and avoids indexing every entity attribute a tenant has ever written.
CREATE INDEX idx_entity_attributes_subject_id
    ON entity_attributes (tenant_id, subject_id)
    WHERE subject_id IS NOT NULL;

-- Point-lookup path (AC-005-1, get_entity's O(1)-per-entity read): a covering index on the
-- entity's own key prefix. The PK's own B-tree already serves this (tenant_id, entity_id,
-- attribute_name is a valid prefix match for a (tenant_id, entity_id) lookup), so no separate
-- index is required here -- noted explicitly so a future reader does not add a redundant one.

-- Alias resolution (AC-005-3/AC-005-4): a second table, not a column on entity_attributes,
-- because one entity can have zero or many aliases and an alias is registered independently of
-- any particular attribute write (register_alias carries no provenance_id of its own --
-- entity_memory_repository.py's own docstring: "a pure indexing operation over already-
-- provenanced entity data").
CREATE TABLE entity_aliases (
    tenant_id   TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    alias       TEXT NOT NULL,
    registered_at TIMESTAMPTZ NOT NULL,

    CONSTRAINT pk_entity_aliases PRIMARY KEY (tenant_id, alias, entity_id)
    -- Composite PK, not (tenant_id, alias) alone: AliasTrie.insert (Shape A) allows the same
    -- alias to resolve to more than one entity_id (resolve_alias_prefix/resolve_exact_term both
    -- return a frozenset, not a single value) -- an alias is not unique per tenant, only per
    -- (tenant, alias, entity) triple.
);

-- AC-005-3's prefix-search path. B-tree on alias supports both exact match (AC-005-4) and
-- prefix search via the standard `alias LIKE 'prefix%'` / `alias >= 'prefix' AND alias <
-- 'prefix' || chr(0x10FFFF)` range-scan pattern -- no trigram/GIN index is needed at this
-- story's scope, since Zone 5's own AliasTrie precedent (Shape A) is a simple prefix trie, not a
-- fuzzy/typo-tolerant search; a GIN trigram index is a legitimate future optimization if prefix
-- search proves too slow at scale, not assumed necessary here.
CREATE INDEX idx_entity_aliases_prefix ON entity_aliases (tenant_id, alias);

-- Reverse lookup for erase_entity's alias cleanup (DSHN-70 fix, entity_memory_repository.py's
-- own erase_entity docstring: "purges entity_id from tenant_id's own AliasTrie"). The PK above
-- already covers (tenant_id, entity_id) via a non-leading-column scan only inefficiently; this
-- index makes "every alias for this entity_id, this tenant" a direct index lookup instead of a
-- PK-order scan filtered by a non-prefix column.
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
-- SELECT, INSERT, UPDATE, DELETE for the ordinary app role (Section 4 explains why Zone 5's own
-- writes AND ordinary application-level attribute overwrites need UPDATE, unlike Zone 2's
-- append-only INSERT-only grant) -- DELETE here covers a *non-DPDP* use case only (an
-- application-level "unset this attribute" operation, if one is ever added); the DPDP erasure
-- path uses the separate compliance role from Section 5, not this grant, per this document's own
-- Section 5 decision.
GRANT SELECT, INSERT, UPDATE, DELETE ON entity_attributes TO dashanan_entity_role;
GRANT SELECT, INSERT, DELETE ON entity_aliases TO dashanan_entity_role;
-- No UPDATE on entity_aliases: an alias registration is either present or absent (register_alias
-- inserts; there is no "rename an alias" operation in the Shape A interface), so UPDATE is never
-- needed and is not granted.
```

**Deviation from Zone 2's precedent, stated explicitly:** `entity_attributes` carries no
append-only trigger and no `dashanan_schema_owner` ownership-reassignment tail. Section 4 below
is the answer to the open question the DPDP design doc's Section 5 explicitly left for this future
story ("should this be append-only, and if so give it the same compliance-role escape hatch").

---

## 4. Is Zone 5 append-only? No — resolved here

`EntityMemoryRepository.write_attribute` (Shape A, read in full for this document) **overwrites**
`entity_attrs[attribute_name] = record` on every call — a mutable key-value store per attribute,
not an append-only log. This is structurally different from Zone 2 (Episodic), whose domain model
is a sequence of immutable events (`seq`-ordered), and from Zone 7 (Provenance), whose entire
purpose is an immutable audit chain. Zone 5's own FR-005 text — *"each the canonical sole owner of
that entity's own attributes"* — describes a current-state store (the entity's attributes *as they
are now*), not a history of every value an attribute has ever held.

**Decision: `entity_attributes` is an ordinary mutable table (`UPDATE`/`DELETE` both permitted
under the standard app role), not append-only.** No `trg_entity_attributes_append_only` trigger,
no `dashanan_schema_owner` ownership-hardening tail. This mirrors `semantic_schema.sql`'s own
precedent (`dpdp-crypto-shredding-full-erasure-design.md` Section 4's zone-by-zone table: *"Zone 3
... has no append-only trigger ... it uses an UPDATE-able `state` field ... no compliance-role
workaround needed here"*) rather than Zone 2's append-only precedent. Consequence for DPDP erasure
(Section 5 below): Zone 5's `erase_entity` equivalent can use an ordinary `DELETE`, and — per the
same reasoning that exempted Zone 3 from a compliance-role adapter — does **not strictly require**
a separate least-privilege compliance role the way Zone 2's append-only-bypass does. This document
still proposes one, for a reason distinct from append-only-bypass — see Section 5.

---

## 5. Least-privilege compliance-role adapter (DPDP design doc Section 5)

`dpdp-crypto-shredding-full-erasure-design.md` Section 5 proposes one project-wide
`dashanan_compliance_erasure_role`, narrowly granted `DELETE` only, with no `SELECT`/`INSERT`/
`UPDATE` grant — the erasure job resolves which rows to delete via `subject_item_index` under a
*different*, read-only credential, and hands this role only the resolved primary keys as bind
parameters, never letting it derive them itself. That design was written to give Zone 2's
append-only trigger an escape hatch. Section 4 above established that Zone 5 does not have an
append-only trigger to escape — so why does this document still propose extending that same role
to Zone 5?

**Decision: extend the existing `dashanan_compliance_erasure_role` (not a new, Zone-5-specific
role) with a `DELETE` grant on `entity_attributes`, even though Zone 5's own app role
(`dashanan_entity_role`) already has `DELETE` too.** Rationale, stated explicitly since this is a
genuine judgment call, not a structural requirement the way Zone 2's escape hatch was:

1. **Defense in depth for an irreversible, compliance-critical operation.** `erase_entity`
   permanently destroys attribute data. Running the DPDP cascade's Zone 5 leg under the same
   `dashanan_entity_role` credential the ordinary application write path uses means a credential
   compromise on the *ordinary* write path (e.g. a leaked app-role connection string) is also
   sufficient to run compliance-critical deletes — no additional barrier. Routing the erasure
   cascade's Zone 5 deletes through the same narrowly-scoped, no-SELECT/no-INSERT/no-UPDATE
   compliance role the cascade already uses for Zone 2 means a single compromised credential
   class (the compliance role) is the one thing that can run *any* zone's DPDP-triggered delete —
   a smaller, more auditable blast radius than "every zone's own app role can also delete for
   compliance reasons."
2. **Operational uniformity for the cascade job.** `SubjectErasureJob` (the design doc's Section 7
   job shape) already connects as `dashanan_compliance_erasure_role` for its Zone 2 leg. Having it
   switch credentials per zone — compliance role for Zone 2, ordinary app role for Zone 5 — adds
   real implementation complexity (a second Postgres connection/credential path in the same job)
   for a security benefit this document judges not worth the added surface. One role, one
   erasure-job credential, every zone's erasure delete goes through it.
3. **Not a structural requirement, disclosed as such.** Unlike Zone 2, nothing about Zone 5's own
   schema *forces* this — an ordinary `DELETE` under `dashanan_entity_role` would work fine
   mechanically. This is a security-posture choice, not a schema constraint, and a future
   implementer or reviewer could reasonably choose differently (e.g. skip the compliance-role hop
   for Zone 5 specifically, since it adds a credential-switch cost that Zone 2's actual escape-hatch
   requirement does not). Recorded as an explicit decision with its own rationale, not asserted as
   the only correct answer.

```sql
-- Extends dpdp-crypto-shredding-full-erasure-design.md Section 5's existing role -- NOT a new
-- Zone-5-specific role. This statement is additive to that design's own CREATE ROLE statement
-- (idempotent role creation is that document's own responsibility; this schema file only adds
-- the new grant once the role exists).
GRANT DELETE ON entity_attributes TO dashanan_compliance_erasure_role;
-- No SELECT, INSERT, or UPDATE grant here either -- identical posture to the Zone 2 grant this
-- extends: the erasure job resolves which (tenant_id, entity_id, attribute_name) rows to delete
-- via subject_item_index (read under a different, read-only credential) and hands this role only
-- the resolved keys as bind parameters.
```

`entity_aliases`' cleanup (the DSHN-70 fix -- removing an erased entity's own aliases) is a
separate `DELETE ... WHERE (tenant_id, entity_id) = (...)` against `entity_aliases`, run under the
same compliance role:

```sql
GRANT DELETE ON entity_aliases TO dashanan_compliance_erasure_role;
```

---

## 6. `SqlEntityMemoryRepository` — interface compatibility with DASH-STORY-027's cascade

`UnifiedSubjectErasureOrchestrator` (already built, DASH-STORY-025/DSHN-70) calls Zone 5's erasure
leg today via `EntityMemoryRepository.erase_entity(tenant_id, entity_id) -> tuple[str, ...]`
(returning the `item_id`s removed, in `f"{entity_id}:{attribute_name}"` form — the same convention
`write_attribute`'s own projection event payload uses). **This document's `SqlEntityMemoryRepository`
must expose the identical method signature and return-value shape**, so that DASH-STORY-027's own
orchestrator wiring — and any other caller already coded against `EntityMemoryRepository` — needs
**zero code changes** when the composition root's Zone 5 builder is switched from the Shape A
adapter to this Shape B one. This is the same "repository swap is invisible above the port"
guarantee `ZoneRepository`'s Protocol already gives every other zone (HLD Section 3, Repository
pattern).

Concretely, `SqlEntityMemoryRepository` (new file,
`src/dashanan/infrastructure/sql_entity_memory_repository.py`, not built by this docs-only pass)
must implement:

| `ZoneRepository`/Zone-5-specific method | Shape A behavior (existing) | Shape B behavior (this design) |
|---|---|---|
| `write_attribute(tenant_id, entity_id, attribute_name, value, provenance_id, updated_at=None, subject_id=None)` | Overwrites one `dict` entry; rejects blank `provenance_id` (422, `ERROR_MISSING_ATTRIBUTE_PROVENANCE_ID`) | `INSERT ... ON CONFLICT (tenant_id, entity_id, attribute_name) DO UPDATE SET value=..., provenance_id=..., subject_id=..., updated_at=...` — a single-row upsert, same one-row-touched guarantee. Same 422 rejection before any statement is executed. **New parameter (`subject_id`) added here, not present on the Shape A signature today** — see Section 8 item 1; Shape A's own signature would need the same addition for symmetry, out of this story's own scope to change. |
| `erase_entity(tenant_id, entity_id) -> tuple[str, ...]` | Pops the `dict` entry; purges aliases from the trie; holds the per-tenant lock for the whole operation | `DELETE FROM entity_attributes WHERE (tenant_id, entity_id) = (...) RETURNING attribute_name`, connecting as `dashanan_compliance_erasure_role` (Section 5); builds the same `f"{entity_id}:{attribute_name}"` tuple from the `RETURNING` rows; a second `DELETE FROM entity_aliases WHERE (tenant_id, entity_id) = (...)` under the same role/transaction. An empty result is a clean no-op, identical to Shape A. |
| `register_alias(tenant_id, entity_id, alias)` | Inserts into the tenant's trie + reverse-index set | `INSERT INTO entity_aliases ... ON CONFLICT DO NOTHING` (idempotent — the same alias/entity pair registered twice is a no-op, matching `AliasTrie.insert`'s own idempotent-insert behavior) |
| `get_entity(tenant_id, entity_id) -> EntityRecord \| None` | `dict` point lookup | `SELECT * FROM entity_attributes WHERE (tenant_id, entity_id) = (...)`, assembled into the same `EntityRecord` |
| `resolve_alias_prefix(tenant_id, prefix) -> frozenset[str]` | `AliasTrie.prefix_search` | `SELECT DISTINCT entity_id FROM entity_aliases WHERE tenant_id = ... AND alias LIKE prefix \|\| '%'` (parameterized, not string-concatenated — see rules/05-security-standards.md Section 3) |
| `resolve_exact_term(tenant_id, term) -> frozenset[str]` | `AliasTrie.exact_match` | `SELECT DISTINCT entity_id FROM entity_aliases WHERE (tenant_id, alias) = (...)` |
| `fetch(query: ZoneQuery) -> list[MemoryItem]` | Delegates to `resolve_alias_prefix` + `get_entity` | Identical delegation — no new query path, reusing the two methods above |

**Concurrency, corrected for Shape B (Postgres native, not a `threading.Lock`).** Shape A's
per-tenant `threading.Lock` (serializing a tenant's own attribute-dict AND alias-trie access
together, ADR-018) has no meaning across a connection pool with multiple worker processes/pods.
Postgres's own row-level locking on `entity_attributes`/`entity_aliases` (implicit in any
`UPDATE`/`DELETE`/upsert statement) is the Shape B equivalent isolation mechanism — a concurrent
`write_attribute` and `erase_entity` for the same `(tenant_id, entity_id)` are serialized by
Postgres's own MVCC/row-lock behavior at the statement level, not by an application-level lock
object. This is the same class of Shape A-to-Shape-B concurrency-model translation
`connection-pooling-design.md` performs for the composition root generally — not a new pattern
this document invents, but explicitly not yet load-tested or race-condition-audited at the level
`connection-pooling-design.md`'s own DASH-STORY-028-LOADTEST sub-task audits pool sizing (see
Section 8 item 3).

**`subject_id`-driven `subject_item_index` population.** Per `dpdp-crypto-shredding-full-erasure-design.md`
Section 3, every zone write that carries a `subject_id` must append a row to
`subject_item_index (tenant_id, subject_id, zone_id, item_id)` at the same write-gate boundary.
`SqlEntityMemoryRepository.write_attribute`'s new `subject_id` parameter (see table above and
Section 8 item 1) is what makes this possible for Zone 5 — Shape A's `write_attribute` has no such
parameter today, so a Shape A deployment's Zone 5 writes can never populate
`subject_item_index`, and `UnifiedSubjectErasureOrchestrator`'s Zone 5 leg (calling
`EntityMemoryRepository.erase_entity` directly by `entity_id`, not resolved via the index) does not
depend on that index for Zone 5 specifically today. This document's Shape B adapter is written to
populate the index going forward, but does not change how the orchestrator resolves Zone 5's
`entity_id` in the interim — see Section 8 item 2 for the open question this leaves.

---

## 7. Migration runner integration

`src/dashanan/infrastructure/migration_runner.py` already applies `episodic_schema.sql`,
`semantic_schema.sql`, `provenance_schema.sql`, and `zone8_consolidation_schema.sql` in a fixed
order at Shape B startup (not read in full for this document — the runner's existence and its
existing per-zone schema list is confirmed by the file listing in
`src/dashanan/infrastructure/`). `entity_schema.sql` (Section 3) is a new entry in that same list.
This document does not specify the exact ordering position — that is an implementation-time
decision for whoever wires it in, constrained only by the fact that `entity_schema.sql`'s own
`GRANT ... TO dashanan_compliance_erasure_role` statement (Section 5) requires that role to already
exist, which means either `entity_schema.sql` must run after
`dpdp-crypto-shredding-full-erasure-design.md` Section 5's `CREATE ROLE dashanan_compliance_erasure_role`
statement has run (wherever that statement itself lands, since that design doc does not name a
specific schema file for it either — see Section 8 item 4), or `entity_schema.sql`'s own `GRANT`
statement must tolerate the role not existing yet (e.g. wrapped in the same idempotent
`DO $$ ... IF NOT EXISTS ... $$` pattern episodic_schema.sql already uses for role creation).

---

## 8. Open items — not resolved here

Matching `dpdp-crypto-shredding-full-erasure-design.md`'s own posture of disclosing genuine gaps
rather than glossing over them:

1. **`write_attribute`'s new `subject_id` parameter is Shape-B-only as proposed.** Adding it only
   to `SqlEntityMemoryRepository` and not to `EntityMemoryRepository` (Shape A) means the two
   adapters' method signatures diverge, which `ZoneRepository`'s own Protocol may or may not
   tolerate depending on how strictly it is typed (not verified against the Protocol's actual
   definition by this document — a real risk this document flags rather than assumes away).
   Whether Shape A's signature should gain the same optional `subject_id` parameter (for
   interface symmetry, even though Shape A deployments are explicitly out of scope for the DPDP
   compliance workflow per this codebase's own established convention,
   `episodic_schema.sql`'s comment: *"A DPDP erasure workflow ... is explicitly OUT OF SCOPE for
   this [Shape A] story"*) is a real design decision this document does not make.
2. **`subject_item_index`-driven resolution vs. direct `entity_id` calls for Zone 5, reconciled or
   not.** `UnifiedSubjectErasureOrchestrator` today calls `EntityMemoryRepository.erase_entity`
   directly by `entity_id`, not via `subject_item_index` resolution — the orchestrator apparently
   already has (or assumes) a `subject_id -> entity_id` mapping from elsewhere. Whether that
   existing resolution path should be replaced by `subject_item_index` once Zone 5 writes populate
   it (for consistency with Zones 1/2/3/6's resolution, per the DPDP design doc's own Section 3),
   or left as-is with `subject_item_index` populated for Zone 5 but not actually consulted for
   Zone 5's own erasure trigger, is not resolved here — this needs a read of
   `unified_subject_erasure_orchestrator.py`'s actual Zone 5 call site (not performed by this
   docs-only pass) to answer correctly rather than guessed at.
3. **Postgres-native concurrency for Zone 5 is unaudited.** Section 6's claim that Postgres
   row-level locking serializes concurrent `write_attribute`/`erase_entity` correctly is reasoned
   from general Postgres MVCC behavior, not verified against this specific schema's actual
   transaction boundaries (e.g. whether `erase_entity`'s two-statement delete against
   `entity_attributes` and `entity_aliases` needs to be wrapped in an explicit transaction to avoid
   a partial-erasure race with a concurrent `register_alias` — not designed here). A real
   concurrency audit, mirroring `connection-pooling-design.md`'s own load-test discipline, is
   future work.
4. **Where `CREATE ROLE dashanan_compliance_erasure_role` itself lives is still undecided.**
   `dpdp-crypto-shredding-full-erasure-design.md` Section 5 proposes the role but does not name a
   specific schema file that creates it (it appears there as a standalone SQL block, not attached
   to `episodic_schema.sql` or any other file). This document's Section 5 `GRANT` statement assumes
   the role already exists by the time `entity_schema.sql` runs, which is only safe once that
   open question is resolved by whichever story actually implements DASH-STORY-027's Section 5
   compliance-role mechanism.
5. **No load-test or real-Postgres integration test exists for this schema.** Mirroring
   `connection-pooling-design.md`'s own DASH-STORY-028-LOADTEST precedent, this document's schema
   and role grants are reasoned from the existing codebase's conventions, not validated against a
   real running Postgres instance. `DASH-STORY-029-QA` (Section-level acceptance criteria, see
   `backlog_draft.json`) is where that validation is expected to happen, not this design pass.

---

## 9. Why Zone 4 durability was considered and left out of this document

The task that produced this document explicitly permitted an optional, clearly-labeled Zone 4
Postgres-storage secondary scope, "durability-only, NOT part of the erasure rationale." This
document does not include one, for a reason worth stating rather than silently omitting: Zone 4's
own Shape A store (`in_memory_procedural_memory_repository.py`) already has a documented backstop
mechanism (`Zone2CapacityBackstopSweep`'s own pattern, referenced elsewhere in this codebase for
Zone 4's rotation/capacity handling), and no Sprint 5 story, AC, or disclosed gap currently
motivates Zone 4 durability the way AC-013/DASH-STORY-027 motivates Zone 5's. Adding a Zone 4
schema here — even labeled secondary — would re-introduce exactly the Zone 4/5 conflation this
session's own plan-review corrected once already (see Section 1.1). If Zone 4 durability becomes a
real, separately-motivated need in a future sprint, it should get its own design document with its
own rationale, not ride along inside a document whose entire reason for existing is Zone 5's
DPDP-erasure-reachability gap.

---

## Change Log

| Date | Change |
|---|---|
| 2026-09-22 | v1 (DRAFT): Initial design for DASH-STORY-029 (Sprint 5 candidate), closing the Zone 5 Shape B storage gap `dpdp-crypto-shredding-full-erasure-design.md` Section 2 surfaced. Proposed `entity_schema.sql` (Section 3), resolved the append-only-vs-mutable question left open for a future story by the DPDP design doc's Section 5 (Section 4: Zone 5 is mutable, no append-only trigger), extended the existing `dashanan_compliance_erasure_role` to Zone 5 with an explicit defense-in-depth rationale rather than a structural requirement (Section 5), specified `SqlEntityMemoryRepository`'s required interface compatibility with `UnifiedSubjectErasureOrchestrator`'s already-built Zone 5 call site (Section 6), and disclosed 5 open items (Section 8) plus an explicit rationale for leaving Zone 4 out of scope (Section 9). Marked DRAFT, NOT IMPLEMENTATION-READY. |
| 2026-09-22 | v2 (DRAFT, adversarial-review correction, solution-architect): Section 2's convention 2 previously claimed "`subject_id` as a nullable column on every zone table" was an EXISTING codebase convention, "independently confirmed" by inspecting `episodic_schema.sql`. FALSE, and corrected in place rather than silently rewritten: `episodic_schema.sql` has zero occurrences of `subject_id` and no subject-linkable column at all; `semantic_schema.sql` uses a domain-specific `subject_ref` field (not a generic `subject_id` column), and its own `general_facts` table explicitly documents having no such column. The only place a generic `subject_id` concept exists in this codebase's design material is the NOT-YET-BUILT `subject_item_index` table proposed in `dpdp-crypto-shredding-full-erasure-design.md` Section 3. Section 2 now labels the literal `subject_id` column on `entity_attributes` as a NEW pattern this document introduces -- extending that not-yet-built proposal and fulfilling ADR-006's real textual mandate for a subject-scoped secondary index -- rather than as an inspection-confirmed existing convention; `semantic_schema.sql`'s `subject_ref` approach is cited as the actual (domain-specific-name) precedent. No change to Section 3's `entity_schema.sql` DDL itself, Section 4-9's content, or this story's scope/story points -- this is a documentation-accuracy correction only. Still marked DRAFT, NOT IMPLEMENTATION-READY. |
| 2026-09-22 | v3 (DRAFT, round-5 solution-architect review remediation): the header "Related:" citation block still cited `FR-011-ZONE-4-5-SHAPE-B` as the current, live stub id this story narrows -- stale, since this story's own creation narrowed that stub to `FR-011-ZONE-4-SHAPE-B` (Zone 4 durability only), making the original combined id superseded/historical rather than live. Section 1.1 already correctly framed this; the header block now carries the same "original, now-superseded" framing. No other content changed. |
