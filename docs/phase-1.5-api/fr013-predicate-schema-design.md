# FR-013 Predicate Schema Design — POST /memory/write Zone 3/5 Routing (GitHub #23)

Status: DESIGN COMPLETE, NOT IMPLEMENTATION-READY — pending user review. No implementation files
have been modified. Blocked on one open decision: `retrieval_context_hash` sourcing (Section 4.2
step 5), which blocks the entire Zone 3/5 persistence path. Everything else in this document is
implementation-ready.
Related: GitHub issue #23, GitHub issue #25 (separate, pre-existing `zone_hint` comparison bug,
disclosed during this review round, NOT fixed by this design), `docs/phase-1.5-api/openapi.yaml`,
`src/dashanan/api/schemas.py`, `src/dashanan/api/app.py`,
`src/dashanan/domain/entity_ownership_specification.py`

**Revision note (v5):** v1 (4/10) proposed a routing model contradicting the locked HLD truth
table. v2 (8.5/10) fixed routing by reusing the existing `CandidateFact`/`classify()` domain
primitives, but had a missing `tenant_id`, an incomplete `classify()` call, and undefined
provenance/atomicity semantics. v3 (8.8/10) fixed those, but was missing the routing gate that
keeps ordinary Zone 1/2/4 writes out of the classification path entirely, falsely claimed
`entity_refs` partitioning already exists, and understated how much of the persistence path
`retrieval_context_hash` actually blocks. v4 (9/10) fixed all three, but its routing gate compared
`zone_hint` against a bare `"semantic"` string instead of the real wire enum value `"3-semantic"`,
and its Section 4.1 file list never mentioned the OpenAPI-side changes (`WriteReceipt` schema,
`207` response) the doc's own Section 4.2 step 6 already specified in prose — leaving the OpenAPI
contract silently undocumented and divergent from the Pydantic model. This revision (v5) fixes
both. See the Change Log at the bottom for the full history.

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
| `docs/phase-1.5-api/openapi.yaml` | (a) Add `predicate` and `subject_scope` to `WriteMemoryRequest.content.properties` per Section 3 above. (b) Widen `WriteReceipt.status` to include `"partial"` and add `zone5_written`/`zone3_edges_written`/`zone3_edges_failed` to the `WriteReceipt` schema, mirroring Section 4.2 step 6's contract exactly — the OpenAPI contract is the source of truth every client generates against; a Pydantic-only change would silently diverge from it. (c) Document the new `207` response explicitly on the `POST /memory/write` (`writeMemory`) operation's `responses` block, alongside the existing `202`/`503`/`422` entries — **review Finding #2, this round, confirmed real: v4 specified the 207/WriteReceipt contract in prose (Section 4.2 step 6) but never listed these three OpenAPI edits as files to change, which would have left the contract undocumented and the Pydantic model silently diverged from it.** |
| `src/dashanan/api/schemas.py` | Add `predicate: str \| None` and `subject_scope: str \| None` to the Pydantic model backing `WriteMemoryRequest.content`. Widen `WriteReceipt.status` (currently `Literal["accepted"]`, `schemas.py:68`) to `Literal["accepted", "partial"]` and add the three new optional fields from Section 4.2 step 6, matching the OpenAPI change in (b) above field-for-field. |
| `src/dashanan/api/app.py` | Replace the unconditional `if body.content.entity_refs: return 422` (`_do_write`, lines 383-390) with the routing logic in 4.2 below. |
| A new/extended real HTTP-level integration test | Full matrix in Section 5. |

### 4.2 `_do_write` routing logic (reusing existing domain code, not reinventing it)

0. **Routing gate, with explicit wire-value normalization (review Finding #1, third round for the
   gate itself; review Finding #1, fourth round for the normalization correction below):** this
   whole procedure (steps 1-6) applies ONLY when `body.content.entity_refs` is non-empty, OR
   `body.content.zone_hint` normalizes to Zone 3 (a caller explicitly requesting a
   `GeneralFact`-only write with `entity_refs` empty — the `\|subjects\|==0` branch of the truth
   table in Section 2). In every other case, skip steps 1-6 entirely and fall through unchanged to
   today's existing Zone 1/2/4 branch logic (`_do_write` lines 397-435, untouched by this design).
   This is a hard gate, evaluated before step 1, not an implicit consequence of `subjects` happening
   to end up empty.

   **Normalization rule (do not compare `zone_hint` against a bare string):**
   `openapi.yaml:1431` declares `WriteMemoryRequest.content.zone_hint` as `$ref: ZoneId`, and
   `ZoneId`'s real enum (`openapi.yaml:1213-1215`) is `[1-working, 2-episodic, 3-semantic,
   4-procedural, 5-entity, 6-retrieval-index, 7-provenance, 8-consolidation]` — the canonical wire
   value for Zone 3 is `"3-semantic"`, not `"semantic"`. `app.py` already has the correct
   wire-to-domain mapping for exactly this problem: `_WIRE_TO_DOMAIN_ZONE` (`app.py:71-80`,
   `"3-semantic" -> ZoneId.SEMANTIC`). This gate uses that existing map:
   `_WIRE_TO_DOMAIN_ZONE.get(body.content.zone_hint) == ZoneId.SEMANTIC`, never a bare
   `zone_hint == "semantic"` string comparison.

   **Separate, pre-existing defect noted but explicitly NOT fixed by this design (out of scope,
   filed as GitHub #25):** `_do_write`'s existing `zone_hint == "procedural"` /
   `zone_hint == "episodic"` comparisons (lines 398, 412) compare against bare strings that do NOT
   match their own OpenAPI-contract-valid wire values (`"4-procedural"`/`"2-episodic"`) and do NOT
   use the already-existing `_WIRE_TO_DOMAIN_ZONE` map either — a schema-compliant client may never
   be able to reach those branches today. This is a real, separate bug this design's step 0 must
   not repeat (hence using `_WIRE_TO_DOMAIN_ZONE` above, not a bare-string comparison), but fixing
   the existing Zone 2/4 comparisons is #25's scope, not this document's.
1. **Correction of a factual error from v3 (review Finding #2, third round):** `body.content.entity_refs`
   is NOT currently partitioned by `role` anywhere in `_do_write` — the only existing logic
   touching it today is the single `if body.content.entity_refs:` unconditional-422 check at line
   383. v3 incorrectly stated this partitioning "already exists." It does not; it is new code this
   implementation pass must write: partition `body.content.entity_refs` into
   `subjects: tuple[str, ...]` / `objects: tuple[str, ...]` by each entry's `role` field.
2. Build `entity_ownership_specification.CandidateFact(tenant_id=caller.tenant_id,
   subjects=subjects, objects=objects, predicate=body.content.predicate or "",
   statement=body.content.text or "", subject_scope=body.content.subject_scope or "",
   index_reverse=body.content.index_reverse)`. `tenant_id` is `CandidateFact`'s first field, no
   default (`entity_ownership_specification.py:121`) — v2's first draft of this step omitted it
   (review Finding #1 of the second round, confirmed real); `caller.tenant_id` is already
   available in `_do_write`'s signature, used identically by every existing branch. Read
   `CandidateFact.__post_init__` (lines 130-152) for its own validation before assuming this
   construction cannot raise — it does raise on structural issues (blank `tenant_id` or a blank
   entry in `subjects`/`objects`); those become 400s, not 422s, mirroring how
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
4. **Zone 3 write:** call `classify(candidate_fact, edge_id_factory=lambda: str(uuid.uuid4()),
   fact_id_factory=lambda: str(uuid.uuid4()))`. `classify()`'s real signature is keyword-only on
   both factories (`entity_ownership_specification.py:216-220`) — v2's first draft of this step
   called `classify(candidate_fact)` with no factories, which does not match the real signature
   (review Finding #2 of the second round, confirmed real). `uuid.uuid4()` mirrors the existing
   `write_id = str(uuid.uuid4())` pattern already at the top of `_do_write`; no new ID scheme is
   introduced. On `SemanticEdgeRouting(edges=...)`, call `ctx.semantic_repository.insert_edge(edge,
   ...)` once per edge, per step 5's provenance rule below. On `GeneralFactRouting(fact=...)`, call
   `ctx.semantic_repository.insert_general_fact(fact, ...)` once. On `ZoneFiveOnlyRouting(...)`, do
   nothing further for Zone 3 (step 3 already handled Zone 5 if applicable).
5. **Provenance/conflict wiring, including per-edge semantics (review Finding #3, first round;
   Finding #4, second round):** `ConflictAwareSemanticRepository.insert_edge`/`insert_general_fact`
   and `ConflictAwareEntityMemoryRepository.write_attribute` (`conflict_aware_zone_writes.py`, read
   in full) all require `provenance_id`, `source_type`, and `retrieval_context_hash` as call
   parameters — these already run the FR-013 conflict sweep internally (`insert_edge`'s own
   docstring, "AC-022-1: the sweep runs... BEFORE `edge` is persisted"), so **no separate
   conflict-detection wiring is needed beyond calling these methods with real arguments** (the
   sweep is not a separate step, it is inside these two repository methods already).
   **Multi-edge provenance is one fresh `provenance_id` PER EDGE, never shared or reused across
   edges in the same write request** — this is not a stylistic choice, it is required by the
   sweep's own keying: `_zone3_edge_item_id(subject_ref, predicate, object_ref)`
   (`conflict_aware_zone_writes.py:155-161`) derives a distinct `item_id` per `(subject, predicate,
   object)` triple, and `ProvenanceRecord.create` ties one `provenance_id` to one `item_id`'s own
   hash chain (Zone 7, HLD Section 3.8). Reusing a single `provenance_id` across multiple distinct
   `item_id`s would corrupt that per-item chain invariant. Each `insert_edge` call in step 4's loop
   therefore generates its own fresh `provenance_id` (same `uuid.uuid4()` pattern as `edge_id`
   above), and the single Zone 5 write in step 3 gets its own separate fresh `provenance_id` too —
   three independent identifiers in the worst case (one Zone 5 attribute write plus N Zone 3 edge
   writes), never one shared value. `source_type` is already resolved via the existing
   `_resolve_source_type(body)` call at the top of `_do_write`. **`retrieval_context_hash` has no
   existing source anywhere in the current request schema or `_do_write`** — this remains a
   genuinely open question, not resolved here: either (a) add a `retrieval_context_hash` field to
   the wire contract (a schema change scoped separately from `predicate`/`subject_scope` above), or
   (b) derive/hash something already present in the request server-side. **Correction of an
   overclaim from v3 (review Finding #3, third round):** this is not merely "one step blocked
   while every other step is implementation-ready" — `retrieval_context_hash` is a mandatory
   parameter on every one of `insert_edge`/`insert_general_fact`/`write_attribute`, so it blocks
   the entire Zone 3/5 persistence path (steps 3-6 all call into one of these three methods).
   Steps 0-2 and 4's `classify()` call are genuinely implementable and testable independent of it
   (routing/classification produces domain objects without touching a repository), but no actual
   Zone 3/5 write can be committed until this is decided. This decision is a hard prerequisite for
   the persistence half of this implementation, not a parallel/independent track.
6. **Atomicity — recommended decision (review Finding #5, second round: a decision was requested,
   not just a flag):** step 3 and step 4 can each independently succeed or fail (e.g. the Zone 5
   write in step 3 succeeds, a Zone 3 edge write in step 4's loop then fails). No 2-phase-commit or
   saga/compensating-transaction mechanism exists in this codebase today, and building one is
   FR-015 pooling-adjacent scope (`AR1-S3-G3`'s own note: one shared `psycopg.Connection`, no
   cross-zone transaction coordination) — genuinely out of scope for this fix. **Recommended
   decision, requiring no new infrastructure:** order writes Zone 5 first, then Zone 3 (already the
   plan's step order); if Zone 5 fails, return the existing `503 WRITE_REJECTED_NOT_DURABLE`
   immediately and attempt no Zone 3 write, exactly mirroring the existing `except
   ZoneRepositoryError` handling at `_do_write` lines 436-445. If Zone 5 succeeds but a later Zone 3
   edge write fails partway through the loop, do NOT attempt to roll back the already-persisted
   Zone 5 write or any already-persisted Zone 3 edges from earlier loop iterations (no rollback
   mechanism exists to do this safely); instead return a distinct, honestly-labeled response so the
   caller can see exactly what state actually landed, mirroring the existing Zone 1
   durability-mirror pattern's own choice (`_do_write` lines 450-456: log and continue rather than
   silently claim full success). This is a fail-fast-plus-honest-reporting contract, not true
   atomicity — genuine cross-zone atomicity remains explicitly out of scope and tracked under the
   existing `AR1-S3-G3` limitation, not newly invented or silently promised here.

   **Exact response contract (decided, per review request — not left as an "either/or"):**
   - Zone 5 write fails (step 3): `503 WRITE_REJECTED_NOT_DURABLE`, identical to the existing
     `except ZoneRepositoryError` response shape at `_do_write` lines 442-445. No Zone 3 attempt is
     made; nothing new to report beyond what that existing response already carries.
   - Zone 5 succeeds and all Zone 3 writes (if any) succeed: the existing `202` + `WriteReceipt`
     shape, unchanged, with the three new fields below populated to reflect full success (so a
     caller inspecting the body doesn't need to special-case "old" vs "new" receipts).
   - Zone 5 succeeds but one or more Zone 3 writes fail partway through step 4's loop: `207
     Multi-Status`. Chosen over reusing `202` with a body flag because this is a materially
     different, actually-partial outcome — a caller that only checks the status code (a common,
     reasonable integration pattern) must not be able to mistake this for full success, which a
     `2xx` status shared with the success case would risk. `207` is already meaningful HTTP
     semantics for "the operation as a whole is not a single pass/fail," and does not require
     inventing a new non-standard status code.
   - **`WriteReceipt` schema addition** (`src/dashanan/api/schemas.py:66-69`, read in full):
     `WriteReceipt.status` today is `Literal["accepted"] = "accepted"` — this must widen to
     `Literal["accepted", "partial"] = "accepted"` so a `207` response's body is distinguishable
     from a `202` body by its `status` field too, not only by the HTTP status code (defense against
     a caller that logs/stores the body without also checking the status code). Three further new
     fields, all optional/default-empty so existing Zone 1/2/4 receipts (which never populate them)
     are unaffected:
     ```
     zone5_written: bool = False
     zone3_edges_written: list[str] = []   # item_ids of edges that committed
     zone3_edges_failed: list[str] = []    # item_ids of edges that were attempted and failed
     ```
     `zone3_edges_written`/`zone3_edges_failed` use the same `_zone3_edge_item_id` values step 5
     already computes per edge, so the caller can identify exactly which `(subject, predicate,
     object)` triples landed versus which need a retry.
7. **Event publishing:** confirm during implementation whether Zone 1/2/4 writes in `_do_write`
   publish any event today (not verified in this design pass) and, if so, whether Zone 3/5 writes
   need parity — flagged as an implementation-time check, not assumed either way here.

---

## 5. Test matrix (review Finding #5, first round; expanded again this round — 12 cases)

All as real HTTP-level integration tests, mirroring
`tests/integration/test_api_real_postgres_e2e_dash3.py`'s Scenario style (real Postgres, real
`TestClient`, not mocks):

0. **Routing-gate regression guard (new this round, Section 4.2 step 0):** an ordinary Zone 1/2/4
   write (`entity_refs` empty, `zone_hint` in `{working, episodic, procedural}` or absent) behaves
   identically before and after this change — it must NOT enter `CandidateFact`/`classify()` at
   all, and must NOT 422/400 on a missing `subject_scope` it was never asked to supply. This is
   the single most important regression guard this change introduces, since a routing-gate bug
   here breaks every existing write, not just new Zone 3/5 traffic.
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
    succeeded in case 4) → assert `207` with `status="partial"`, `zone5_written=True`, and
    `zone3_edges_failed` non-empty per Section 4.2 step 6's exact response contract — not silently
    reported as `202`/`"accepted"` full success.
11. Zone 5 write itself fails (step 3, before any Zone 3 attempt) → assert `503
    WRITE_REJECTED_NOT_DURABLE`, identical shape to the existing `except ZoneRepositoryError`
    response, and that no Zone 3 write was attempted (`zone3_edges_written` and
    `zone3_edges_failed` both empty, not merely absent from the body).

---

## 6. What this revision does NOT resolve (explicitly, not silently)

- `retrieval_context_hash` sourcing (Section 4.2 step 5) — needs a maintainer decision. This blocks
  the entire persistence path (steps 3-6, every call into `insert_edge`/`insert_general_fact`/
  `write_attribute`), not just one isolated step — corrected from v3's understated framing.
- Exact HTTP status code for predicate/subject_scope validation failures (400 vs 422) — should
  match whatever convention `SemanticEdge.__post_init__`/`GeneralFact.__post_init__` errors
  already use elsewhere in this handler once that's confirmed during implementation.
- Event-publishing parity for the new write paths (Section 4.2 step 7) — unverified, flagged for
  implementation-time confirmation.

Resolved this round (previously open): the routing gate for ordinary Zone 1/2/4 writes (Section
4.2 step 0), and the exact partial-failure response contract — `207`, widened `WriteReceipt.status`,
and the `zone5_written`/`zone3_edges_written`/`zone3_edges_failed` fields (Section 4.2 step 6).
True cross-zone atomicity (2PC/saga) remains explicitly out of scope under `AR1-S3-G3`, by design,
not as an oversight.

---

## Change Log

| Date | Change |
|---|---|
| 2026-09-21 | v1: Initial design (3 competing schema options). Reviewed, scored 4/10 -- routing model contradicted the locked HLD 3.4 truth table, predicate placement was ambiguous, implementation plan and test matrix were incomplete. |
| 2026-09-21 | v2: Replaced the routing model by reusing the existing, locked `CandidateFact`/`classify()` domain primitives instead of reinventing routing. Corrected predicate to a single fact-level field. Added `subject_scope`. Expanded the implementation plan to cover provenance/conflict wiring, atomicity, and event-publishing parity. Expanded the test matrix from 4 to 10 cases. Reviewed, scored 8.5/10 -- CandidateFact construction omitted the required `tenant_id`, the `classify()` call omitted its required `edge_id_factory`/`fact_id_factory` keyword args, multi-edge provenance-per-edge semantics were undefined, and atomicity was flagged but no concrete failure contract was proposed; the "8-case" heading also didn't match the actual 10-case matrix. |
| 2026-09-21 | v3: Fixed all five v2 review findings (`tenant_id`, `classify()` factory args, per-edge provenance, an atomicity decision, the "8-case" heading typo). Reviewed, scored 8.8/10 -- the routing gate for ordinary Zone 1/2/4 writes was missing entirely (every write would have incorrectly entered classification and failed on a missing `subject_scope`), v3 falsely claimed `entity_refs` role-partitioning "already exists" in `_do_write` when it does not, and v3 understated `retrieval_context_hash` as blocking "one step" when it actually blocks the entire persistence path. |
| 2026-09-21 | v4: Added the missing routing gate (Section 4.2 step 0) so ordinary Zone 1/2/4 writes provably never enter the classification path. Corrected the false "already exists" claim about `entity_refs` partitioning -- it is new code this implementation must write. Corrected the `retrieval_context_hash` framing to state it blocks the whole persistence path, not one isolated step. Committed to the exact partial-failure response contract the second round asked for: `207 Multi-Status`, a widened `WriteReceipt.status: Literal["accepted","partial"]`, and new `zone5_written`/`zone3_edges_written`/`zone3_edges_failed` fields. Expanded the test matrix from 10 to 12 cases. Reviewed, scored 9/10 -- the routing gate compared `zone_hint` against a bare `"semantic"` string instead of the real wire enum value `"3-semantic"` (`openapi.yaml`'s `ZoneId` enum), and Section 4.1's file list never mentioned the OpenAPI-side changes (`WriteReceipt` schema widening, the new `207` response) that Section 4.2 step 6 already specified in prose. |
| 2026-09-21 | v5 (this revision): Fixed the routing gate to normalize `zone_hint` via the existing `_WIRE_TO_DOMAIN_ZONE` map (`app.py:71-80`) against `ZoneId.SEMANTIC`, never a bare string. Disclosed, as a separate GitHub issue (#25) rather than silently fixing in-scope, a genuinely separate pre-existing bug this review surfaced: `_do_write`'s existing `zone_hint == "procedural"`/`"episodic"` comparisons don't match their own OpenAPI-contract wire values either, and don't use `_WIRE_TO_DOMAIN_ZONE`. Added the missing OpenAPI-side file changes to Section 4.1: widening `WriteReceipt.status` and adding the three new response fields in `openapi.yaml` itself (not just `schemas.py`), and documenting the new `207` response on the `writeMemory` operation. Status line now explicitly reads "DESIGN COMPLETE, NOT IMPLEMENTATION-READY" pending the `retrieval_context_hash` decision, rather than an ambiguous "DESIGN ONLY." Still no implementation performed. |
