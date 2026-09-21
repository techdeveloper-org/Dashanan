# FR-013 Predicate Schema Design — POST /memory/write Zone 3/5 Routing (GitHub #23)

Status: DESIGN ONLY — pending user review. No implementation files have been modified.
Related: GitHub issue #23, `docs/phase-1.5-api/openapi.yaml`, `src/dashanan/api/schemas.py`,
`src/dashanan/api/app.py`, `src/dashanan/domain/entity_ownership_specification.py`

**Revision note (v2):** the first draft of this document was reviewed and found genuinely
incorrect on the routing model (score 4/10). This revision replaces it. The root cause of v1's
errors: it did not discover `src/dashanan/domain/entity_ownership_specification.py`'s existing
`CandidateFact`/`classify()` pair before designing a new routing scheme from scratch — that module
already implements HLD Section 3.4's locked truth table correctly, and already models `predicate`
as a single fact-level field, not a per-entity-reference one. This revision maps the wire schema
onto that existing, tested, locked domain code instead of reinventing it.

---

## 1. Problem Statement (unchanged from v1, still accurate)

GitHub #23: `POST /memory/write` cannot reach Zone 3 (`SemanticEdge`/`GeneralFact`) or Zone 5
(entity-attribute) because `WriteMemoryRequest.content` has no `predicate` field and no
`subject_scope` field. `_do_write` (`api/app.py:383-390`) unconditionally rejects any write where
`content.entity_refs` is non-empty with `422 ZONE_3_5_PREDICATE_UNRESOLVABLE`.

---

## 2. The HLD-locked routing truth table (verified against source, not restated from memory)

`docs/phase-1-architecture/HLD.md:234-249` (Section 3.4) is unambiguous, and
`src/dashanan/domain/entity_ownership_specification.py` already implements it exactly as an
executable spec — `classify(fact: CandidateFact, ...)` at line 216, verified read in full:

| `\|subjects\|` | `\|objects\|` | `index_reverse` | Zone 5 write | Zone 3 write | `classify()` return |
|---|---|---|---|---|---|
| 1 | 0 | n/a | **Mandatory** | None | `ZoneFiveOnlyRouting(reason=SINGLE_SUBJECT_NO_OBJECT)` |
| 1 | ≥1 | `False` (default) | **Mandatory** | None | `ZoneFiveOnlyRouting(reason=SINGLE_SUBJECT_INDEX_REVERSE_FALSE)` |
| 1 | ≥1 | `True` | **Mandatory** | One `SemanticEdge` per object | `SemanticEdgeRouting(edges=(...))` |
| 0 | n/a | n/a | None | One `GeneralFact` | `GeneralFactRouting(fact=...)` |
| >1 | 0 | n/a | None | No edge (empty cross-product; `classify()`'s own documented judgment call, not an error) | `SemanticEdgeRouting(edges=())` |
| >1 | ≥1 | n/a | None | One `SemanticEdge` per `(subject, object)` pair | `SemanticEdgeRouting(edges=(...))` |

**Critical, independently verified correction from v1:** `classify()`'s return value tells the
caller what to do in **Zone 3 only**. It is a pure marker for "no Zone 3 write" in the
`ZoneFiveOnlyRouting` case — the docstring at `entity_ownership_specification.py:182-186` states
this explicitly: *"this module performs no Zone 5 write, stub, or fake of one... any Zone 5
attribute write is that caller's own separate FR-005 concern, entirely outside this module."*

This means the API handler **cannot** decide whether to do a Zone 5 write by branching on
`classify()`'s return type alone. It must independently apply the same `|subjects|==1` condition
from the truth table above to decide "also write Zone 5," in parallel with calling `classify()` to
decide "also write Zone 3." v1's `do_write_routing` sections for all three options conflated these
two decisions into one branch (e.g. treating `subjects>=1 and objects>=1` as unconditionally "Zone
3 only," silently dropping the mandatory Zone 5 write for the `\|subjects\|==1` sub-case) — that
was the review's Finding #1, confirmed accurate.

---

## 3. Recommended wire schema

Two new fields on `WriteMemoryRequest.content`, both optional at the JSON Schema level
(conditionally required by server-side validation once `entity_refs` is non-empty), mapping
directly onto `CandidateFact`'s own existing fields (`entity_ownership_specification.py:122-128`:
`subjects`, `objects`, `predicate: str = ""`, `subject_scope: str = ""`, `index_reverse: bool =
False`):

```yaml
predicate:
  type: string
  description: >-
    CandidateFact.predicate (HLD 3.4). A single fact-level relation/attribute
    name -- NOT per entity_refs entry, since exactly one predicate value
    applies to the whole write regardless of how many subject/object pairs
    classify() expands it into. Required whenever entity_refs contains at
    least one subject-role entry (i.e. |subjects| >= 1); the server returns
    422 if a subject entity_ref is present without it. Ignored (not read)
    when |subjects| == 0 -- that branch is GeneralFact, which has no
    predicate field.
subject_scope:
  type: string
  description: >-
    CandidateFact.subject_scope / GeneralFact.subject_scope (HLD 3.4,
    |subjects|==0 branch only). GeneralFact.subject_scope is a required,
    non-blank field on the domain object (semantic_memory.py:167,185) with
    no existing derivation anywhere in this codebase -- v1 of this document
    incorrectly claimed an "existing derivation"; there is none (review
    Finding #4, confirmed). Required whenever entity_refs is empty AND the
    caller intends a GeneralFact write (as opposed to a Zone 1/2/4 write,
    which never touches this field). 422 if entity_refs is empty, zone_hint
    does not resolve to Zone 1/2/4, and subject_scope is missing/blank.
```

`entity_refs` and `index_reverse` are unchanged — they already carry exactly `CandidateFact.subjects`
/ `CandidateFact.objects` (via `role` partitioning) and `CandidateFact.index_reverse`.

This corrects v1 Finding #2 (ambiguous per-entry `predicate`): a single fact-level field has no
"which entry does it come from" ambiguity in the >1-subject/multi-edge case, because
`CandidateFact.predicate` genuinely is one value applied identically to every edge `classify()`
produces from a multi-subject/multi-object cross-product — this is not a simplification, it is
what the domain code already does (`entity_ownership_specification.py:280-289`, one shared
`predicate=fact.predicate` per constructed edge).

---

## 4. Implementation plan (NOT YET DONE — pending user re-review)

### 4.1 Files to change

| File | Change |
|---|---|
| `docs/phase-1.5-api/openapi.yaml` | Add `predicate` and `subject_scope` to `WriteMemoryRequest.content.properties` per Section 3 above. |
| `src/dashanan/api/schemas.py` | Add `predicate: str \| None` and `subject_scope: str \| None` to the Pydantic model backing `WriteMemoryRequest.content`. |
| `src/dashanan/api/app.py` | Replace the unconditional `if body.content.entity_refs: return 422` (`_do_write`, lines 383-390) with the routing logic in 4.2 below. |
| A new/extended real HTTP-level integration test | Full matrix in Section 5. |

### 4.2 `_do_write` routing logic (reusing existing domain code, not reinventing it)

1. Partition `body.content.entity_refs` into `subjects: tuple[str, ...]` / `objects: tuple[str,
   ...]` by `role` (existing logic already does this partitioning today for the 422 check; keep
   it).
2. Build `entity_ownership_specification.CandidateFact(subjects=subjects, objects=objects,
   predicate=body.content.predicate or "", statement=body.content.text or "",
   subject_scope=body.content.subject_scope or "", index_reverse=body.content.index_reverse)`.
   Read `CandidateFact.__post_init__` (lines 122-150, already read in full during this review) for
   its own validation before assuming this construction cannot raise — it does raise on
   structural issues (e.g. blank refs); those become 400s, not 422s, mirroring how
   `SemanticEdge.__post_init__`/`GeneralFact.__post_init__` errors are already handled elsewhere
   per this module's own docstring reference.
3. **Zone 5 write (independent of the Zone 3 decision — apply the truth table's own condition
   directly, do not derive it from `classify()`'s return type):** if `len(subjects) == 1`,
   construct and write an `EntityAttributeRecord` (`attribute_name=body.content.predicate,
   value=body.content.text, entity_id=subjects[0]`, plus whatever provenance/tenant fields
   `EntityAttributeRecord` requires — read `entity_record.py`'s constructor in full before
   implementing, not assumed here) via
   `ctx.conflict_aware_entity_repository.write_attribute(...)`. This fires for BOTH the
   `\|objects\|==0` case and the `\|objects\|>=1` case, per the truth table — this is the exact
   correction for review Finding #1.
4. **Zone 3 write:** call `classify(candidate_fact)`. On `SemanticEdgeRouting(edges=...)`, call
   `ctx.semantic_repository.insert_edge(edge, ...)` once per edge. On
   `GeneralFactRouting(fact=...)`, call `ctx.semantic_repository.insert_general_fact(fact, ...)`
   once. On `ZoneFiveOnlyRouting(...)`, do nothing further for Zone 3 (step 3 already handled Zone
   5 if applicable).
5. **Provenance/conflict wiring (review Finding #3, only partially resolvable in this document):**
   `ConflictAwareSemanticRepository.insert_edge`/`insert_general_fact` and
   `ConflictAwareEntityMemoryRepository.write_attribute` (`conflict_aware_zone_writes.py`, read in
   full during this revision) all require `provenance_id`, `source_type`, and
   `retrieval_context_hash` as call parameters — these already run the FR-013 conflict sweep
   internally (`insert_edge`'s own docstring, "AC-022-1: the sweep runs... BEFORE `edge` is
   persisted"), so **no separate conflict-detection wiring is needed beyond calling these methods
   with real arguments** (correcting v1's silence on this — the sweep is not a separate step, it
   is inside these two repository methods already). `provenance_id` is a fresh UUID (mirrors
   `write_id` generation already in `_do_write`); `source_type` is already resolved via the
   existing `_resolve_source_type(body)` call at the top of `_do_write`. **`retrieval_context_hash`
   has no existing source anywhere in the current request schema or `_do_write`** — this is a
   genuinely open question, not resolved here: either (a) add a `retrieval_context_hash` field to
   the wire contract (another schema change, scoped separately from `predicate`/`subject_scope`
   above), or (b) derive/hash something already present in the request server-side. This decision
   needs the user/maintainer to pick, not this document.
6. **Atomicity (review Finding #3, flagged not resolved):** step 3 and step 4 above can each
   independently succeed or fail (e.g. Zone 5 write succeeds, Zone 3 edge write then fails). The
   existing `AR1-S3-G3` limitation (`api.composition`'s own module docstring: one shared
   `psycopg.Connection`, no real transaction/rollback coordination across zone writes today)
   already applies to the existing Zone 1 durability-mirror pattern in `_do_write` (lines 450-456,
   which explicitly tolerates and logs a partial-failure case rather than rolling back). The Zone
   3/5 path introduces the same class of partial-write risk across two zones instead of one. This
   document does not propose a fix (a real transaction or a compensating-write pattern is FR-015
   pooling-adjacent scope, per AR1-S3-G3's own note) — it flags this as a known, inherited
   limitation the implementation must not silently paper over.
7. **Event publishing:** confirm during implementation whether Zone 1/2/4 writes in `_do_write`
   publish any event today (not verified in this design pass) and, if so, whether Zone 3/5 writes
   need parity — flagged as an implementation-time check, not assumed either way here.

---

## 5. Test matrix (review Finding #5 — full 8-case matrix, up from v1's 4 cases)

All as real HTTP-level integration tests, mirroring
`tests/integration/test_api_real_postgres_e2e_dash3.py`'s Scenario style (real Postgres, real
`TestClient`, not mocks):

1. `\|subjects\|==0`, `subject_scope` populated → `GeneralFact` written to Zone 3, no Zone 5 write.
2. `\|subjects\|==1`, `\|objects\|==0` → Zone 5 `EntityAttributeRecord` written, no Zone 3 write.
3. `\|subjects\|==1`, `\|objects\|>=1`, `index_reverse=False` (default) → Zone 5 write only, no
   Zone 3 edge, even though `objects` is non-empty (this is the exact case v1 got wrong).
4. Same as 3 but `index_reverse=True` → Zone 5 write AND a Zone 3 `SemanticEdge`, both present.
5. `\|subjects\|>1`, `\|objects\|>=1` → cross-product `SemanticEdge`s (no Zone 5 write — multiple
   subjects, per the truth table).
6. `\|subjects\|>1`, `\|objects\|==0` → `classify()`'s own documented empty-cross-product case
   (`SemanticEdgeRouting(edges=())`) — assert no edge is written and no error is raised.
7. Missing/blank `predicate` when `\|subjects\|>=1` → 422/400 (exact code TBD at implementation
   time), no partial write.
8. Missing/blank `subject_scope` when `\|subjects\|==0` and a `GeneralFact` write is intended →
   422/400, no partial write.
9. Object-only `entity_refs` (no subject-role entries, only object-role) → confirm this collapses
   to the `\|subjects\|==0` GeneralFact branch per the truth table (objects are only meaningful
   paired with a subject), not silently dropped.
10. Repository failure mid-write (e.g. `insert_edge` raises after the Zone 5 write already
    succeeded in case 4) → assert the response reflects the real partial-failure state per Section
    4.2 step 6 (not silently reported as full success), and that provenance/conflict state is
    consistent with what actually got persisted.

---

## 6. What this revision does NOT resolve (explicitly, not silently)

- `retrieval_context_hash` sourcing (Section 4.2 step 5) — needs a maintainer decision.
- Exact HTTP status code for predicate/subject_scope validation failures (400 vs 422) — should
  match whatever convention `SemanticEdge.__post_init__`/`GeneralFact.__post_init__` errors
  already use elsewhere in this handler once that's confirmed during implementation.
- Multi-zone write atomicity (Section 4.2 step 6) — inherited limitation, not fixed here.
- Event-publishing parity for the new write paths (Section 4.2 step 7) — unverified, flagged for
  implementation-time confirmation.

---

## Change Log

| Date | Change |
|---|---|
| 2026-09-21 | v1: Initial design (3 competing schema options). Reviewed, scored 4/10 -- routing model contradicted the locked HLD 3.4 truth table, predicate placement was ambiguous, implementation plan and test matrix were incomplete. |
| 2026-09-21 | v2 (this revision): Replaced the routing model by reusing the existing, locked `CandidateFact`/`classify()` domain primitives instead of reinventing routing. Corrected predicate to a single fact-level field. Added `subject_scope`. Expanded the implementation plan to cover provenance/conflict wiring, atomicity, and event-publishing parity. Expanded the test matrix from 4 to 10 cases. Still DESIGN ONLY -- no implementation performed. |
