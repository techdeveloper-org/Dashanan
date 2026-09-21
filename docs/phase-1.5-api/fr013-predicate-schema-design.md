# FR-013 Predicate Schema Design — POST /memory/write Zone 3/5 Routing (GitHub #23)

Author: solution-architect (Domain: system-design, clean-architecture, event-driven-architecture,
api-design-core, cloud-security-core)
Status: DESIGN ONLY — pending user review. No files have been modified as part of this document.
Related: GitHub issue #23, `docs/phase-1.5-api/openapi.yaml`, `src/dashanan/api/schemas.py`,
`src/dashanan/api/app.py`

---

## 1. Problem Statement

GitHub #23: `POST /memory/write` cannot reach Zone 3 (SemanticEdge) or Zone 5 (entity-attribute)
because `WriteMemoryRequest.content` has no `predicate` field.

Today, `content` carries exactly `{text, zone_hint, occurred_at, task_signature_hash, entity_refs
[{role: enum[subject,object], entity_id}], index_reverse}`. `entity_refs` can already express
which entities participate in a write and in what role (subject vs object), via the
EntityOwnershipSpecification branch logic (HLD 3.4). What it cannot express is the *relation
name* itself — the value a Zone 3 write needs for `SemanticEdge.predicate` and a Zone 5 write
needs for `EntityAttributeRecord.attribute_name`. Both target domain objects require a non-blank
predicate/attribute_name at construction time, and the wire contract has no field that supplies
one.

The current server behavior (`_do_write`, `api/app.py`, lines 373-391) reflects this gap
directly: any write where `content.entity_refs` is non-empty is unconditionally rejected with
`422 ZONE_3_5_PREDICATE_UNRESOLVABLE`. Zone 3 and Zone 5 writes are therefore unreachable through
the public API today, regardless of caller intent — this is the real, verified defect, not a
theoretical one.

This document proposes three schema options that close the gap, records the judge panel's
comparative scoring, states the recommended option, and lists the files a future implementation
pass will touch. No schema, code, or test file is changed by this document.

---

## 2. Proposed Options

### Option 1 — Flat `predicate: str` field on WriteMemoryRequest.content (minimal wire diff)

**schema_change**

In `docs/phase-1.5-api/openapi.yaml`, `WriteMemoryRequest.content.properties`, add one new
optional sibling property next to the existing `text`/`zone_hint`/`occurred_at`/
`task_signature_hash`/`entity_refs`/`index_reverse`:

```yaml
predicate:
  type: string
  description: >-
    The relation name for a Zone 3/5 write, dual-purposed by the
    EntityOwnershipSpecification (HLD 3.4) branch entity_refs selects:
    - |subjects|>=1 and |objects|>=1 (edge branch): this is the
      SemanticEdge.predicate (e.g. "located_in"); `text` is unused for
      this branch, and `object_ref` comes from the entity_refs entry
      with role=object.
    - |subjects|==1 and |objects|==0 (entity-attribute branch): this is
      reused as EntityAttributeRecord.attribute_name (e.g.
      "preferred_language"); `text` supplies EntityAttributeRecord.value.
    - |subjects|==0 (GeneralFact branch): predicate is not applicable;
      omit it, `text` alone supplies GeneralFact.statement.
  # Required when entity_refs contains at least one subject-role entry;
  # the server returns 422 if a subject entity_ref is present without it.
```

No other `WriteMemoryRequest.content` field changes. Zone 1/2/4 writes never populate `predicate`
and are byte-for-byte unaffected — it is purely additive and optional at the JSON Schema level
(not added to `WriteMemoryRequest.content`'s own `required` list, only conditionally required by
server-side validation once `entity_refs` carries a subject).

**do_write_routing**

`_do_write` (Orchestrator write path) already inspects `WriteMemoryRequest.content.entity_refs`
to run EntityOwnershipSpecification (HLD 3.4) before this change; the only new branch input is
reading `content.predicate` where it previously had nothing:

1. Count subject-role and object-role entries in `entity_refs` (existing logic, unchanged).
2. `|subjects|==0` -> GeneralFact branch (unchanged): construct
   `GeneralFact(statement=content.text, subject_scope=<existing derivation>, ...)`.
   `content.predicate` is ignored if present (server MAY 422 if it's set here, to keep the
   contract unambiguous).
3. `|subjects|==1` and `|objects|==0` -> Entity-attribute branch (Zone 5, mandatory per
   HLD 3.4): construct `EntityAttributeRecord(entity_id=entity_refs[subject].entity_id,
   attribute_name=content.predicate, value=content.text, provenance_id=<from provenance
   block>, ...)`. This is the new dependency this schema option introduces: the branch now reads
   `content.predicate` instead of having no source for `attribute_name` at all (today's real gap
   per issue #23).
4. `|subjects|>=1` and `|objects|>=1` -> SemanticEdge branch (Zone 3): construct
   `SemanticEdge(subject_ref=entity_refs[subject].entity_id, predicate=content.predicate,
   object_ref=entity_refs[object].entity_id, ...)`. If `content.index_reverse` is also true and
   this is the single-subject/single-object case, the entity-attribute branch (step 3) ALSO
   fires alongside this one per the existing `index_reverse` semantics already documented in the
   file (both use the same `content.predicate` value for their respective
   `attribute_name`/`predicate` slots — no ambiguity since they are the same relation name in
   both roles).
5. Routing itself (which zone-write call the Orchestrator dispatches to which ZoneRepository
   port) is unchanged; only the payload each branch constructs gains a populated
   `predicate`/`attribute_name` source that does not exist today.

Validation gate: `_do_write` (or an upstream request validator) rejects with 422 when
`entity_refs` has `>=1` subject-role entry and `content.predicate` is missing/blank, since
branches 3 and 4 both require it now.

**tradeoffs**

Self-score against the three stated criteria:

1. Minimal wire-contract churn / does it break existing Zone 1/2/4 shapes: STRONG. Exactly one
   new optional property added to `WriteMemoryRequest.content`; `required` array for content's
   own sub-shape is untouched (`predicate` is not JSON-Schema-required, only conditionally
   required by server logic tied to `entity_refs`, which Zone 1/2/4 callers never populate with
   subject-role entries today). Existing Zone 1 (episodic-adjacent text), Zone 2
   (`occurred_at`), Zone 4 (`task_signature_hash`) request bodies validate identically before
   and after — zero regeneration needed for any client that doesn't write Zone 3/5. This is the
   cheapest possible diff: one field, no restructuring of `content` into a `oneOf`/discriminated
   union.
2. Backward compatibility: STRONG for existing callers (additive optional field, old clients
   omit it and nothing changes for their zones). WEAK for callers who were already sending
   `entity_refs` with a subject to attempt Zone 5 writes pre-fix (per issue #23, this presumably
   never worked/was silently mis-routed) — but since it never worked, there's no working
   behavior to break; this is a pure bug-fix addition, not a breaking change to a previously
   functioning contract.
3. Can it express BOTH a SemanticEdge write AND an entity-attribute write without a second
   field: PARTIAL, by deliberate double-duty overload rather than clean separation. It reuses
   `predicate` as `SemanticEdge.predicate` for the edge branch and as
   `EntityAttributeRecord.attribute_name` for the entity-attribute branch, and reuses the
   pre-existing `text` field as `EntityAttributeRecord.value`. This avoids adding
   `attribute_name`/`value` as separate fields, satisfying the letter of "no second field" — but
   it is a semantic overload: `predicate` and `text` mean different things depending on which
   EntityOwnershipSpecification branch `entity_refs` happens to select, which is implicit and
   must be documented carefully (done above) and enforced server-side (422 gate) rather than
   being self-evident from the schema shape itself. A reviewer should weigh this against the
   alternative of a real discriminated union (`oneOf` keyed by a `write_type` field), which would
   be more self-documenting and less wire-diff-minimal — that tradeoff is exactly what makes
   this "flat field, minimal diff" option a genuine but imperfect fit for the third criterion,
   and is why it is not scored as a full win there.

Overall: best fit when the priority is genuinely minimizing wire churn and preserving Zone 1/2/4
compatibility with zero client-visible change; weakest on self-documentation/explicitness of the
dual-purpose field, which is the real cost being traded for that minimalism.

---

### Option 2 — Nested `relation` object on WriteMemoryRequest.content — `{predicate, target_entity_id}`

**schema_change**

Add one new, optional, additive property to `WriteMemoryRequest.content` in
`docs/phase-1.5-api/openapi.yaml` (alongside the existing `text`, `zone_hint`, `occurred_at`,
`task_signature_hash`, `entity_refs`, `index_reverse`):

```yaml
relation:
  type: object
  description: >-
    Present when the write is Zone 3/5 ownership-routed (HLD 3.4's
    EntityOwnershipSpecification gate, keyed off `entity_refs`).
    Required whenever `entity_refs` is non-empty; absent/null for
    Zone 1/2/4 writes (unchanged, existing shapes).
  required: [predicate]
  properties:
    predicate:
      type: string
      description: >-
        Overloaded by the ownership-routing outcome: SemanticEdge.predicate
        when the |subjects|>=1 and |objects|>=1 branch fires (edge write),
        or EntityAttributeRecord.attribute_name when the |objects|==0
        branch fires (entity-attribute write). Same wire field, two
        possible domain destinations -- the classification gate, not the
        client, decides which.
    target_entity_id:
      type: string
      nullable: true
      description: >-
        SemanticEdge.object_ref for an edge write (should mirror the
        entity_refs role=object entry's entity_id). Null/omitted for an
        entity-attribute write, since |objects|==0 by definition and
        there is no target entity -- the attribute's own value is
        already carried by the existing `content.text` field, not by
        this one.
```

No existing property is renamed, retyped, or made required-where-it-wasn't. `entity_refs` keeps
its current shape (role/entity_id pairs) and continues to supply the subject_ref / object_ref
candidates; `relation` supplies only what `entity_refs` structurally cannot: the predicate name.

**do_write_routing**

In `api/app.py`'s `_do_write`, replace the current unconditional `if body.content.entity_refs:
return 422 ZONE_3_5_PREDICATE_UNRESOLVABLE` with a routing branch keyed on both `entity_refs`
and the new `relation`:

1. If `entity_refs` is empty -> unchanged today's behavior (Zone 1/2/4 path via `zone_hint`, or
   GeneralFact-eligible text with no `entity_refs` at all -- out of this option's scope).
2. If `entity_refs` is non-empty but `relation` is missing or `relation.predicate` is blank ->
   keep the existing 422 `ZONE_3_5_PREDICATE_UNRESOLVABLE` (now genuinely "caller didn't supply
   a predicate," not "entity_refs is structurally unsupported").
3. If `entity_refs` is non-empty and `relation.predicate` is set, split `entity_refs` by role:
   - `subjects` = entries with `role="subject"`, `objects` = entries with `role="object"`
   - `len(subjects) >= 1 and len(objects) >= 1` -> EntityOwnershipSpecification's edge branch:
     construct `SemanticEdge(subject_ref=subjects[0].entity_id, predicate=relation.predicate,
     object_ref=relation.target_entity_id or objects[0].entity_id, ...)`, write to Zone 3 (plus
     the existing mandatory Zone 5 attribute write / optional `index_reverse` edge, per HLD 3.4
     -- unchanged by this option).
   - `len(subjects) >= 1 and len(objects) == 0` -> EntityOwnershipSpecification's no-object
     branch: construct `EntityAttributeRecord(entity_id=subjects[0].entity_id,
     attribute_name=relation.predicate, value=body.content.text, provenance_id=<from provenance
     block>, ...)`, write to Zone 5.
   - `len(subjects) == 0` -> unchanged existing GeneralFact-eligible path (statement=text,
     subject_scope from wherever it's sourced today), not gated by `relation` at all since
     GeneralFact has no predicate.

This is a pure branch-widening of the existing single `if entity_refs: reject` line -- no other
handler, no other zone's code path, is touched.

**tradeoffs**

Scored honestly against the three criteria:

1. Minimal wire-contract churn (Zone 1/2/4 write shapes): HIGH marks. `relation` is a single new,
   optional, sibling property under `content`. `text`, `zone_hint`, `occurred_at`,
   `task_signature_hash`, `index_reverse` are untouched -- their types, requiredness, and
   semantics don't change. A Zone 1/2/4 caller that never sets `entity_refs` never sets or sees
   `relation` either, so its request/response shape is byte-for-byte identical to today.
2. Backward compatibility: HIGH marks. Old clients that send `entity_refs` without `relation`
   get the exact same 422 they get today (just a more precise underlying reason). Old clients
   that never send `entity_refs` are completely unaffected. No field is removed, renamed, or has
   its `required` status tightened on an existing shape.
3. Expresses both a SemanticEdge write AND an entity-attribute write without a second NEW
   top-level field: MOSTLY yes, with two honest caveats:
   - It only avoids a second *content-level* field by reusing the already-existing
     `content.text` as `EntityAttributeRecord.value` for the no-object branch. That reuse is a
     little uncomfortable: `text` was documented as free text / Zone 1-2 payload, and is now
     being asked to double as a Zone 5 attribute's opaque value with no schema hint that it's
     playing that role -- a reader has to already know the |objects|==0 routing rule to
     understand why `text` means "the attribute value" here.
   - `relation` itself has two sub-fields (`predicate`, `target_entity_id`), and
     `target_entity_id` is meaningless/null in exactly one of the two branches this option is
     meant to unify -- that's a mild schema smell (an always-present property that's
     structurally unused half the time), not a clean single-field solution. There is also a
     latent double-source-of-truth risk for the edge branch: `relation.target_entity_id` and the
     `entity_refs` role="object" entry can disagree, since `entity_refs` already carries a full
     object entity_id. The design note above says `target_entity_id` "should mirror" that entry,
     which pushes a consistency check into `_do_write` (reject if they conflict) that this schema
     alone doesn't enforce -- an implementation-time cost flagged here rather than hidden.

Net: this option clears the churn and backward-compatibility bars cleanly, and clears the
"one field expresses both write kinds" bar only if the `content.text`-as-value reuse and the
`target_entity_id`/`entity_refs` redundancy are accepted as known, documented trade-offs rather
than treated as free.

---

### Option 3 — Extend `entity_refs[]` item with an optional `predicate` sibling property

Reuse the existing role-discriminated array instead of adding a new top-level `content` field.

**schema_change**

In `docs/phase-1.5-api/openapi.yaml`, `WriteMemoryRequest.content.entity_refs.items.properties`
gains one new optional property:

```yaml
predicate:
  type: string
  description: >-
    The relation/attribute key this entry contributes. When |subjects|>=1
    and |objects|>=1 (Zone 3 SemanticEdge branch), this is the edge's
    `predicate` (e.g. "located_in"). When |subjects|==1 and |objects|==0
    (Zone 5 entity-attribute branch), this is the EntityAttributeRecord's
    `attribute_name`. Absent/ignored on the |subjects|==0 GeneralFact
    branch. No change to `content`'s own `required` list (still
    [session_id, content, provenance] unaffected) and `entity_refs` stays
    optional at the content level -- existing Zone 1/2/4 callers that omit
    entity_refs entirely are untouched.
```

No new top-level field is added to `WriteMemoryRequest.content` itself -- `role`'s existing
`enum:[subject,object]` on the same item object remains the ownership discriminant; `predicate`
merely rides alongside it per entry.

**do_write_routing**

`_do_write` reads `content.entity_refs`, partitions by role into `subjects[]`/`objects[]`
(unchanged from today's EntityOwnershipSpecification logic), then branches purely on
`len(subjects)`/`len(objects)`:

1. `len(subjects)>=1` and `len(objects)>=1` -> Zone 3 path: for each (subject,object) pair
   needing an edge, read `predicate` off the corresponding `entity_refs` entry (reject with 400
   if blank/missing -- `SemanticEdge.__post_init__`-equivalent validation) and construct
   `SemanticEdge(subject_ref=subject.entity_id, predicate=entry.predicate,
   object_ref=object.entity_id, qualifiers={}, ...)` via the Zone 3 repository. If
   `index_reverse` is also true, this is layered on top of case 2's mandatory Zone 5 write per
   the existing Section 3.4 note, not a replacement for it.
2. `len(subjects)==1` and `len(objects)==0` -> Zone 5 path: read `predicate` off that single
   entry (reject with 400 if blank -- becomes `EntityAttributeRecord.attribute_name`),
   `content.text` becomes `EntityAttributeRecord.value`, and the repository call proceeds
   through `EntityMemoryRepository` as today.
3. `len(subjects)==0` -> Zone 3 GeneralFact path: unchanged, `predicate` is not consulted;
   `statement=content.text` (`subject_scope`'s own gap is a separate pre-existing issue, not
   touched here).
4. `entity_refs` absent/empty entirely -> unchanged Zone 1/2/4 behavior via `zone_hint`, exactly
   as today; `predicate` is never read.

**tradeoffs**

Wire-contract churn: minimal-to-none. Adds one optional string property nested inside
`entity_refs` items, which Zone 1/2/4 callers never populate today -- their request/response
shapes are byte-for-byte unaffected, and JSON-Schema-wise the addition is fully additive (no
`required`-list change anywhere).

Backward compatibility: full. Old clients that never sent `entity_refs`, or sent it without
`predicate`, keep working exactly as before (Zone 3/5 writes were already unreachable pre-fix, so
there is no prior "no predicate" behavior to preserve on that path -- only Zone 1/2/4
compatibility matters, and it's untouched).

Single-field expressiveness for both target writes: mostly yes -- one field, keyed by the
pre-existing subject/object-count branch, supplies both `SemanticEdge.predicate` and
`EntityAttributeRecord.attribute_name` without a second new field. Caveat: the field's meaning is
overloaded (relation-name in the edge branch vs. attribute-key in the entity branch), which is a
minor semantic cost the reviewer should weigh against the alternative of two separately-named
fields; it does not, however, require any additional wire-contract surface to resolve.

---

## 3. Judge Panel — Scoring Table and Rationale

Verified against the real files (`docs/phase-1.5-api/openapi.yaml` lines 1418-1449 for
`WriteMemoryRequest.content`; `src/dashanan/domain/semantic_memory.py` for
`SemanticEdge`/`GeneralFact`; `src/dashanan/domain/entity_record.py` for
`EntityAttributeRecord`; `src/dashanan/api/app.py` lines 373-391 for the existing `_do_write` 422
gate on any `entity_refs`-bearing write).

Confirmed base facts: `content` today has exactly `{text, zone_hint, occurred_at,
task_signature_hash, entity_refs[{role:enum[subject,object], entity_id}], index_reverse}`;
`entity_refs` carries no predicate anywhere; `SemanticEdge` requires a non-blank
`predicate`+`object_ref`; `EntityAttributeRecord` requires a non-blank `attribute_name`;
`_do_write` today unconditionally 422s any write where `content.entity_refs` is truthy
(`ZONE_3_5_PREDICATE_UNRESOLVABLE`) — all three proposals' framing of "today's real gap" is
accurate.

All three proposals are additive-only against Zone 1/2/4 (none of the existing `content`
properties or `content`'s own top-level `required` list changes), so `backward_compat_score` is
essentially tied at 9/10 for all three (not 10, since it's impossible to claim "fully" when Zone
3/5 behavior itself is being defined for the first time, i.e. no prior "working" contract to be
fully compatible with beyond the additive-field guarantee).

Where they diverge is churn size and true single-field expressiveness:

- Option 1 (flat `content.predicate`) adds exactly one new scalar property directly on
  `content` — small, but it does grow `content`'s own property list, the surface every Zone
  1/2/4 client's schema/codegen tooling walks.
- Option 2 (`content.relation` object) is the largest diff: a whole new nested object type with
  TWO properties (`predicate` and `target_entity_id`). The option's own tradeoffs section admits
  this is "an always-present property that's structurally unused half the time" and creates a
  redundant/conflicting source of truth against `entity_refs`' own object entry. Critically,
  `target_entity_id` is a genuine second field alongside `predicate`, which directly violates the
  stated "ONE field, no second field needed" criterion — the option only avoids a second
  *content-level* field by pushing that second field one level down into `relation`, which
  doesn't satisfy the letter or spirit of the criterion. This tanks its `dual_expressiveness_score`.
- Option 3 (add `predicate` to `entity_refs[].items`) is the most contained change: it adds one
  optional scalar sibling to the existing `role`/`entity_id` pair inside `entity_refs`, an array
  Zone 1/2/4 callers never populate at all — so it doesn't even grow `content`'s own top-level
  property list, only extends a substructure that is already 100% scoped to Zone 3/5 traffic. It
  expresses both branches (`SemanticEdge.predicate` vs `EntityAttributeRecord.attribute_name`)
  with exactly one new field, reusing the pre-existing `text` for the attribute value exactly as
  Option 1 does — same overload cost, but smaller and more localized wire diff.

Scoring: Option 1 = 8+9+8 = 25. Option 2 = 6+9+3 = 18 (the second-field violation is the
deciding factor). Option 3 = 9+9+8 = 26.

Winner: Option 3 ("Extend entity_refs[] item with an optional predicate sibling property"), by a
narrow margin over Option 1, driven by strictly smaller/more-contained wire-contract churn while
matching Option 1 on backward compatibility and single-field expressiveness. Option 2 is clearly
last due to introducing a real second field (`target_entity_id`) that violates the explicit
"no second field" requirement, and a larger, more structurally awkward diff.

| Option | wire_contract_churn_score | backward_compat_score | dual_expressiveness_score | total |
|---|---|---|---|---|
| 1. Flat `predicate: str` on `content` (minimal wire diff) | 8 | 9 | 8 | 25 |
| 2. Nested `relation` object on `content` — `{predicate, target_entity_id}` | 6 | 9 | 3 | 18 |
| 3. Extend `entity_refs[]` item with an optional `predicate` sibling property | 9 | 9 | 8 | **26** |

---

## 4. Recommended Option

**Recommended option: Extend `entity_refs[]` item with an optional `predicate` sibling property
(reuse the existing role-discriminated array instead of adding a new top-level content field)**

In plain language: instead of bolting a new field onto the top of `WriteMemoryRequest.content`
(Option 1) or introducing a whole new nested object with two sub-fields (Option 2), Option 3 adds
`predicate` right next to the `role`/`entity_id` pair that already lives inside each
`entity_refs` entry. That array is already 100% Zone 3/5 territory — a Zone 1/2/4 caller never
touches it — so this is the smallest possible surface to add the missing information to. It still
supplies exactly what both target writes need (the edge's `predicate` and the attribute's
`attribute_name`) from a single field, matching Option 1's expressiveness while beating it on
containment, and it avoids the real second field (`target_entity_id`) that dragged Option 2's
score down for breaking the "no second field" requirement.

---

## 5. Implementation Plan (NOT YET DONE — pending user review)

No file listed below has been modified by this document. Once the option above is approved, the
implementation pass will touch:

| File | Change |
|---|---|
| `docs/phase-1.5-api/openapi.yaml` | Add the optional `predicate` property to `WriteMemoryRequest.content.entity_refs.items.properties`, per Section 2, Option 3 `schema_change` above. |
| `src/dashanan/api/schemas.py` | Add the corresponding optional `predicate: str \| None` field to the `entity_refs` item Pydantic model backing `WriteMemoryRequest` (the model currently at line 60, `class WriteMemoryRequest(BaseModel)`, and its nested entity-ref item type). |
| `src/dashanan/api/app.py` | Replace the unconditional `if body.content.entity_refs: return 422 ZONE_3_5_PREDICATE_UNRESOLVABLE` (lines 373-391 in `_do_write`) with the subject/object-count branch logic and per-branch `predicate` validation described in Section 2, Option 3 `do_write_routing` above. |
| A new/extended real HTTP-level integration test | Mirroring `tests/integration/test_api_real_postgres_e2e_dash3.py`'s Scenario style, covering: (a) Zone 3 edge write with `entity_refs` subjects>=1/objects>=1 and a populated `predicate`; (b) Zone 5 entity-attribute write with subjects==1/objects==0 and a populated `predicate`; (c) 400/422 rejection when a subject-role `entity_refs` entry is present without `predicate`; (d) Zone 1/2/4 writes unaffected (no `entity_refs`, unchanged response shape). |

---

## Change Log

| Date | Change |
|---|---|
| 2026-09-21 | Initial design document authored for GitHub #23 (FR-013 predicate schema). No implementation performed. |
