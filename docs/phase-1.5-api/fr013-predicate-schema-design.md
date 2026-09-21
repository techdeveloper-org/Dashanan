# FR-013 Predicate Schema Design — POST /memory/write Zone 3/5 Routing (GitHub #23)

Status: IMPLEMENTATION-READY — pending user review. No implementation files have been modified.
Every open decision raised across six review rounds is resolved with evidence from this
codebase's own existing, established precedents — none were invented in the abstract.
Related: GitHub issue #23, GitHub issue #25 (separate, pre-existing `zone_hint` comparison bug,
disclosed during v5's review round, NOT fixed by this design), `docs/phase-1.5-api/openapi.yaml`,
`src/dashanan/api/schemas.py`, `src/dashanan/api/app.py`,
`src/dashanan/domain/entity_ownership_specification.py`, `src/dashanan/domain/write_gate.py`,
`src/dashanan/infrastructure/provenance_schema.sql`

**Revision note (v7):** v1 through v6 progressively fixed the routing model, `CandidateFact`/
`classify()` usage, provenance/atomicity semantics, `zone_hint` wire-value normalization, the
OpenAPI contract's own documentation of itself, `retrieval_context_hash` sourcing, the 400-vs-422
status decision, and event-publishing parity (score progression 4 -> 8.5 -> 8.8 -> 9 -> 9.5 ->
9.8/10 — full history in the Change Log at the bottom). v6's one remaining micro-gap: it declared
`retrieval_context_hash` as a caller-supplied string with no explicit format constraint or
conditional-requiredness wording, even though this codebase's own database layer
(`provenance_schema.sql:93`) already enforces `^[0-9a-f]{64}$` for this exact field. This revision
(v7) closes that gap by declaring the identical pattern at the OpenAPI/Pydantic layer too, and
makes the conditional-requiredness (Zone 3/5 required, Zone 1/2/4 optional/unread) explicit
alongside `predicate`/`subject_scope`'s own existing wording.

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
False`); plus one new field on `WriteMemoryRequest.provenance` (`ProvenanceWriteBlock`), resolved
in Section 4.2 step 5:

```yaml
# On WriteMemoryRequest.content:
predicate:
  type: string
  description: >-
    CandidateFact.predicate (HLD 3.4). A single fact-level relation/attribute
    name -- NOT per entity_refs entry, since exactly one predicate value
    applies to the whole write regardless of how many subject/object pairs
    classify() expands it into. Required whenever entity_refs contains at
    least one subject-role entry (i.e. |subjects| >= 1); the server returns
    400 INVALID_REQUEST if a subject entity_ref is present without it
    (Section 4.2 step 5b). Ignored (not read) when |subjects| == 0 -- that
    branch is GeneralFact, which has no predicate field.
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
    which never touches this field). 400 INVALID_REQUEST if entity_refs is
    empty, zone_hint normalizes to Zone 3, and subject_scope is
    missing/blank (Section 4.2 step 5b).

# On WriteMemoryRequest.provenance (ProvenanceWriteBlock):
retrieval_context_hash:
  type: string
  pattern: '^[0-9a-f]{64}$'
  description: >-
    SHA-256 hex of the query/task this write was made under, pre-hashed by
    the caller -- this host never receives raw retrieval-context text
    (matches domain.write_gate.WriteRequest.retrieval_context_hash's own,
    already-established semantics exactly; see Section 4.2 step 5). The
    ^[0-9a-f]{64}$ pattern is not a new convention invented for this field --
    it is the exact format this codebase's own database layer already
    enforces for this same field (provenance_schema.sql:93,
    CHECK (retrieval_context_hash ~ '^[0-9a-f]{64}$'), and identically for
    write_journal_schema.sql:93 and zone8_consolidation_schema.sql's
    blob_id). Declaring it here lets a malformed value fail fast at the
    wire boundary (400) instead of only at the database CHECK constraint.
    Required whenever a Zone 3/5 write is intended (entity_refs non-empty,
    or zone_hint normalizes to Zone 3); optional and unread for Zone 1/2/4
    writes -- existing callers that never populate it are unaffected
    (matches predicate/subject_scope's own backward-compatibility
    guarantee in Section 3 above).
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
| `docs/phase-1.5-api/openapi.yaml` | (a) Add `predicate` and `subject_scope` to `WriteMemoryRequest.content.properties`, and `retrieval_context_hash` to `WriteMemoryRequest.provenance`'s schema, per Section 3 above. (b) Widen `WriteReceipt.status` to include `"partial"` and add `zone5_written`/`zone3_edges_written`/`zone3_edges_failed` to the `WriteReceipt` schema, mirroring Section 4.2 step 6's contract exactly — the OpenAPI contract is the source of truth every client generates against; a Pydantic-only change would silently diverge from it. (c) Document the new `207` response explicitly on the `POST /memory/write` (`writeMemory`) operation's `responses` block, alongside the existing `202`/`503`/`400`/`422` entries. |
| `src/dashanan/api/schemas.py` | Add `predicate: str \| None` and `subject_scope: str \| None` to the Pydantic model backing `WriteMemoryRequest.content`. Add `retrieval_context_hash: str \| None = Field(default=None, pattern=r"^[0-9a-f]{64}$")` to `ProvenanceWriteBlock` (`schemas.py:33-40`) -- the same `^[0-9a-f]{64}$` format this codebase's own DB layer already enforces (`provenance_schema.sql:93`), so a malformed value is rejected by FastAPI's own request validation before `_do_write` even runs, not only by the eventual DB `CHECK` constraint. Widen `WriteReceipt.status` (currently `Literal["accepted"]`, `schemas.py:68`) to `Literal["accepted", "partial"]` and add the three new optional fields from Section 4.2 step 6, matching the OpenAPI change in (b) above field-for-field. |
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
   `_resolve_source_type(body)` call at the top of `_do_write`.

   **`retrieval_context_hash` — RESOLVED this round (review blocker #1, fifth round).** This field
   is not new or unprecedented in this codebase: `src/dashanan/domain/write_gate.py`'s
   `WriteRequest.retrieval_context_hash` (lines 346-348, a different, higher-level write path this
   API host does not currently use) already establishes its exact semantics: *"SHA-256 hex of the
   query/task this write was made under, pre-hashed by the caller (this dataclass never receives
   raw retrieval context text)."* This settles the (a)-vs-(b) question from earlier rounds: it must
   be **caller-supplied** (option a), never server-derived (option b) — a server cannot reconstruct
   a hash of retrieval-context text it was explicitly designed never to receive (the same PII
   constraint `write_gate.py`'s own module docstring states at lines 26-31). **Decision: add
   `retrieval_context_hash: str | None` as a new field on `WriteMemoryRequest.provenance`**
   (`ProvenanceWriteBlock` in `src/dashanan/api/schemas.py:33-41`, which today has
   `source_type`/`source_refs`/`importance`/`purpose`/`subject_id` — corrected 2026-09-21,
   hallucination-detector gate; the prior "only source_type/purpose" claim undercounted the real
   field set, though the operative conclusion was still correct: none of the five existing fields
   covers `retrieval_context_hash`), matching
   `write_gate.py`'s own field name and semantics exactly rather than inventing a new convention.
   **Conditionally required, exactly like `predicate`/`subject_scope` (Section 3): required
   (server-side, via step 0's routing gate — not a JSON-Schema-level `required` on `provenance`
   itself, which would break every existing Zone 1/2/4 caller) whenever a Zone 3/5 write is
   intended; optional and unread for Zone 1/2/4 writes, which never populate it and remain
   byte-for-byte unaffected.** **Format:** `^[0-9a-f]{64}$`, matching the exact format this
   codebase's own database layer already enforces for this same field
   (`provenance_schema.sql:93`) — declared at the OpenAPI/Pydantic layer too (Section 3, Section
   4.1's `schemas.py` row) so a malformed value fails fast at the wire boundary instead of only at
   the DB `CHECK` constraint. This is now a concrete schema change alongside `predicate`/
   `subject_scope`, not an open question — added to Section 3's schema and Section 4.1's file list
   below.
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
5b. **Predicate/subject_scope validation status code — RESOLVED this round (review blocker #2,
   fifth round).** `_do_write`'s sibling handler `assemble_context` (`app.py:175-193`, read in
   full) already establishes the exact precedent for "a domain constructor raises `ValueError` on
   caller input": `except (ValueError, KeyError) as exc: return _error(400, "INVALID_REQUEST",
   str(exc))`. `CandidateFact.__post_init__` raising on a blank `tenant_id`/`subjects`/`objects`
   entry, and `SemanticEdge.__post_init__`/`GeneralFact.__post_init__` raising on a blank
   `predicate`/`statement`/`subject_scope`, are the exact same class of error this precedent
   already covers. **Decision: `400 INVALID_REQUEST`, not `422`**, with `str(exc)` as the message,
   mirroring `assemble_context`'s handling verbatim rather than introducing a second convention for
   the same error class within the same file. (`422` remains reserved for the pre-existing
   `ZONE_3_5_PREDICATE_UNRESOLVABLE` semantics this design removes, and for FastAPI's own automatic
   request-body-schema validation — neither applies here, since `predicate`/`subject_scope` are
   valid at the JSON Schema level, optional strings, and only become invalid via the domain
   object's own business-rule validation.)
7. **Event publishing — RESOLVED this round (review blocker #3, fifth round): no parity work is
   needed.** Read `src/dashanan/api/composition.py` in full for this: `AppContext.event_bus` is
   unconditionally constructed as `NoOpEventBus()` (`composition.py:177`) for every deployment
   configuration this codebase has today — there is no real event-publishing path anywhere in the
   API host to achieve parity with. Confirmed further: `ConflictAwareSemanticRepository`/
   `ConflictAwareEntityMemoryRepository` (`conflict_aware_zone_writes.py`, both constructors read
   in full) accept no `event_bus` parameter at all, unlike `EntityMemoryRepository`/
   `InMemoryProceduralMemoryRepository` elsewhere in `composition.py` which do (and which are
   themselves wired to the same no-op instance). "Verify parity at implementation time" from v5 is
   therefore already answered: there is nothing to make parity with, and nothing new to wire.

---

## 5. Test matrix (review Finding #5, first round; expanded again this round — 14 cases)

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
7. Missing/blank `predicate` when `\|subjects\|>=1` → `400 INVALID_REQUEST` (Section 4.2 step 5b),
   no partial write.
8. Missing/blank `subject_scope` when `\|subjects\|==0` and a `GeneralFact` write is intended →
   `400 INVALID_REQUEST` (Section 4.2 step 5b), no partial write.
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
12. Missing/blank `retrieval_context_hash` when a Zone 3/5 write is intended (`entity_refs`
    non-empty or `zone_hint` normalizes to Zone 3) → `400 INVALID_REQUEST` (Section 4.2 step 5b's
    same precedent applies), no partial write.
13. Malformed `retrieval_context_hash` (wrong length, uppercase hex, non-hex characters) when a
    Zone 3/5 write is intended → rejected by FastAPI's own request validation (the `^[0-9a-f]{64}$`
    `Field(pattern=...)` from Section 4.2 step 5) before `_do_write` even runs — a `422` from
    Pydantic's own schema validation, distinct from the `400`s above which come from domain-object
    construction after the request already parsed successfully. Also confirms a well-formed but
    syntactically-valid-looking value (64 lowercase hex chars) that doesn't match the DB's own
    `provenance_schema.sql:93` `CHECK` is impossible by construction, since both layers share the
    identical pattern.

---

## 6. What remains genuinely out of scope (by design, not oversight)

As of v6, every open question raised across five review rounds is resolved with evidence from this
codebase's own existing code — none invented in the abstract:

- `retrieval_context_hash` sourcing → resolved (Section 4.2 step 5): caller-supplied, matching
  `domain.write_gate.WriteRequest.retrieval_context_hash`'s own established semantics.
- 400 vs 422 status code → resolved (Section 4.2 step 5b): `400 INVALID_REQUEST`, matching
  `assemble_context`'s own existing precedent for a domain-constructor `ValueError`.
- Event-publishing parity → resolved (Section 4.2 step 7): not applicable — `event_bus` is
  unconditionally a no-op everywhere in this codebase today, and the conflict-aware repositories
  don't accept one at all.
- The routing gate for ordinary Zone 1/2/4 writes (Section 4.2 step 0) and the exact
  partial-failure response contract — `207`, widened `WriteReceipt.status`, and the
  `zone5_written`/`zone3_edges_written`/`zone3_edges_failed` fields (Section 4.2 step 6) — were
  resolved in v4/v5 and remain unchanged.

Only one item remains explicitly, permanently out of scope by design: **true cross-zone atomicity
(2-phase-commit/saga) for the Zone 5 + Zone 3 write pair.** This is not an oversight — it is
tracked under the existing, pre-dating `AR1-S3-G3` limitation (connection-pooling-adjacent, FR-015
scope), and this design's fail-fast-plus-honest-partial-reporting contract (Section 4.2 step 6) is
the deliberate, infrastructure-free substitute for it, not a placeholder pending a future decision.

---

## Change Log

| Date | Change |
|---|---|
| 2026-09-21 | v1: Initial design (3 competing schema options). Reviewed, scored 4/10 -- routing model contradicted the locked HLD 3.4 truth table, predicate placement was ambiguous, implementation plan and test matrix were incomplete. |
| 2026-09-21 | v2: Replaced the routing model by reusing the existing, locked `CandidateFact`/`classify()` domain primitives instead of reinventing routing. Corrected predicate to a single fact-level field. Added `subject_scope`. Expanded the implementation plan to cover provenance/conflict wiring, atomicity, and event-publishing parity. Expanded the test matrix from 4 to 10 cases. Reviewed, scored 8.5/10 -- CandidateFact construction omitted the required `tenant_id`, the `classify()` call omitted its required `edge_id_factory`/`fact_id_factory` keyword args, multi-edge provenance-per-edge semantics were undefined, and atomicity was flagged but no concrete failure contract was proposed; the "8-case" heading also didn't match the actual 10-case matrix. |
| 2026-09-21 | v3: Fixed all five v2 review findings (`tenant_id`, `classify()` factory args, per-edge provenance, an atomicity decision, the "8-case" heading typo). Reviewed, scored 8.8/10 -- the routing gate for ordinary Zone 1/2/4 writes was missing entirely (every write would have incorrectly entered classification and failed on a missing `subject_scope`), v3 falsely claimed `entity_refs` role-partitioning "already exists" in `_do_write` when it does not, and v3 understated `retrieval_context_hash` as blocking "one step" when it actually blocks the entire persistence path. |
| 2026-09-21 | v4: Added the missing routing gate (Section 4.2 step 0) so ordinary Zone 1/2/4 writes provably never enter the classification path. Corrected the false "already exists" claim about `entity_refs` partitioning -- it is new code this implementation must write. Corrected the `retrieval_context_hash` framing to state it blocks the whole persistence path, not one isolated step. Committed to the exact partial-failure response contract the second round asked for: `207 Multi-Status`, a widened `WriteReceipt.status: Literal["accepted","partial"]`, and new `zone5_written`/`zone3_edges_written`/`zone3_edges_failed` fields. Expanded the test matrix from 10 to 12 cases. Reviewed, scored 9/10 -- the routing gate compared `zone_hint` against a bare `"semantic"` string instead of the real wire enum value `"3-semantic"` (`openapi.yaml`'s `ZoneId` enum), and Section 4.1's file list never mentioned the OpenAPI-side changes (`WriteReceipt` schema widening, the new `207` response) that Section 4.2 step 6 already specified in prose. |
| 2026-09-21 | v5: Fixed the routing gate to normalize `zone_hint` via the existing `_WIRE_TO_DOMAIN_ZONE` map (`app.py:71-80`) against `ZoneId.SEMANTIC`, never a bare string. Disclosed, as a separate GitHub issue (#25) rather than silently fixing in-scope, a genuinely separate pre-existing bug this review surfaced: `_do_write`'s existing `zone_hint == "procedural"`/`"episodic"` comparisons don't match their own OpenAPI-contract wire values either, and don't use `_WIRE_TO_DOMAIN_ZONE`. Added the missing OpenAPI-side file changes to Section 4.1. Status line now explicitly reads "DESIGN COMPLETE, NOT IMPLEMENTATION-READY" pending the `retrieval_context_hash` decision. Reviewed, scored 9.5/10 -- three items still explicitly open: `retrieval_context_hash` sourcing, the 400-vs-422 status code decision, and Zone 3/5 event-publishing parity verification. |
| 2026-09-21 | v6: Closed all three remaining open items, each with evidence from this codebase's own existing code, not invented. `retrieval_context_hash`: resolved as caller-supplied (new field on `WriteMemoryRequest.provenance`), matching `domain.write_gate.WriteRequest.retrieval_context_hash`'s own already-established semantics exactly (`write_gate.py:346-348`). 400 vs 422: resolved as `400 INVALID_REQUEST`, matching the existing `assemble_context` handler's own precedent (`app.py:192-193`) for a domain-constructor `ValueError`. Event-publishing parity: resolved as not applicable -- `AppContext.event_bus` is unconditionally `NoOpEventBus()` everywhere in this codebase today (`composition.py:177`), and the conflict-aware repositories don't accept an `event_bus` parameter at all. Added `retrieval_context_hash` to Section 3's schema and Section 4.1's file list. Added a new step 5b and test case 12. Expanded the test matrix from 12 to 13 cases. Status line now reads "IMPLEMENTATION-READY." Reviewed, scored 9.8/10 -- one micro-gap: `retrieval_context_hash` had no explicit format constraint or conditional-requiredness wording, even though `provenance_schema.sql:93` already enforces `^[0-9a-f]{64}$` for this exact field at the DB layer. |
| 2026-09-21 | v7: Declared the identical `^[0-9a-f]{64}$` pattern at the OpenAPI (`pattern:`) and Pydantic (`Field(pattern=...)`) layers for `retrieval_context_hash`, reusing this codebase's own already-established DB-layer format rather than inventing a new one -- so a malformed value fails fast at the wire boundary (Pydantic `422`) instead of only at the eventual DB `CHECK` constraint. Made the conditional-requiredness (Zone 3/5 required via the routing gate, Zone 1/2/4 optional/unread, existing callers unaffected) explicit in Section 3's schema block and Section 4.2 step 5's prose, matching `predicate`/`subject_scope`'s own existing wording. Added test case 13 for the malformed-format scenario. Expanded the test matrix from 13 to 14 cases. Reviewed, scored 9.8/10. |
| 2026-09-21 | v7.1 (this revision, minor): Sprint 4's real Phase C hallucination-detector gate (against the Sprint 4 planning bundle, not a new user review round of this doc) found and fixed one real, LOW-severity inaccuracy inherited from this doc: Section 4.2 step 5 claimed `ProvenanceWriteBlock` (`schemas.py:33-40`) "today has only `source_type`/`purpose`" -- the real field set is five fields (`source_type`, `source_refs`, `importance`, `purpose`, `subject_id`, `schemas.py:33-41`), undercounted in the prior text. The operative conclusion (no existing field covers `retrieval_context_hash`) was still correct; only the field-count claim was wrong. No design decision changes as a result. |
