# API Testing Review — Dashanan Memory Orchestration API (openapi.yaml)

Reviewer: api-testing-engineer (Domain 42, skills: api-testing-core, contract-testing-core,
security-testing-ci-core, integration-testing-core)
Artifact reviewed: `docs/phase-1.5-api/openapi.yaml` (openapi 3.1.0, 31 operations, JWT bearer + mTLS)
Basis for security test plan: `docs/phase-1-architecture/HLD.md` Section 10, threats I-1/I-2/I-3

**2026-09-18 addendum:** This review's operation count (31) and every derived coverage number
below (|T(S)| = 112 triples, the 4-gaps/27-clean breakdown) were computed before
`/items:batchGet` was added to `openapi.yaml` (SRS Change Log 1.0.5, "OpenAPI batch-retrieve
endpoint"). The spec now has 32 operations. `/items:batchGet`'s own design-time testability has
not yet been assessed by this review — an open item, not silently folded into the 27-clean
figure below. The original 31-operation analysis and its triple count remain a real, dated
result and are left as authored rather than rewritten to an unverified "32".

---

## 1. C_api Triple Coverage — Derivation

### 1.1 Applicability note (must be stated before any number)

`api-testing-core` M1 defines `C_api = |T_exercised| / |T(S)|` where `T_exercised` is the set of
triples actually hit by a **running test suite**. No test suite exists yet for this artifact —
`openapi.yaml` was "just authored" per the dispatch brief, and no Karate/Newman/REST-assured
collection has been written against it. Per the skill's own applicability note (added
2026-09-07, issue #148): *"C_api ... is undefined, not merely zero-valued, at any earlier phase.
A downstream pipeline that needs a coverage-shaped gate before a test suite exists must define
its own formula explicitly under its own name... rather than reuse C_api and silently redefine
what 'exercised' means."*

This review therefore reports two distinct numbers, not one silently-redefined metric:

- **C_api (actual, as defined by M1) = 0 / 112 = 0.0000** — correct and expected at this phase;
  no test suite exists to exercise anything yet.
- **C_plan (testability coverage — a design-time metric, NOT C_api)** = the fraction of the 112
  triples for which this review confirms a test case is designable from the spec alone (schema
  present, status code documented, no missing information blocking test authoring). This is
  reported in §4 as the gap-analysis table, not as a coverage percentage, to avoid the exact
  conflation the applicability note warns against.

**Consequence for the Phase 1.5 consensus-agent gate:** if the gate's `C_api >= 0.85` threshold
is read literally against the M1 definition, this artifact currently reads **0.0 and is a HARD
REJECT** by that literal reading, because no suite exists. That is almost certainly not the
gate's intent (the gate is presumably meant to fire once `integration-testing-engineer`'s Pact
suite and this agent's Karate/Newman suite exist and run in CI). Flagging this explicitly rather
than either (a) fabricating an executed-coverage number or (b) silently reinterpreting C_api to
mean something else. Recommend the pipeline definition be corrected to name the design-time gate
separately (e.g. `C_plan >= 0.85`, evaluated against the gap table in §4) so `C_api` keeps its
one true meaning for the post-implementation gate.

### 1.2 Triple enumeration T(S) — full matrix

`T(S) = {(path, method, status) | path ∈ paths(S), method ∈ methods(path), status ∈ responses(path,method)}`.
Counted directly from `openapi.yaml`'s `responses:` blocks (each documented status code = 1
triple, regardless of shared `$ref` response bodies).

| # | Path | Method | Status codes (n) | Triples |
|---|------|--------|-------------------|---------|
| 1 | /context/assemble | POST | 200,400,401,403,422,429,503 (7) | 7 |
| 2 | /context/assemble | GET | 200,400,401 (3) | 3 |
| 3 | /memory/search | POST | 200,400,401 (3) | 3 |
| 4 | /items/{item_id} | GET | 200,401,404 (3) | 3 |
| 5 | /items/{item_id} | PATCH | 202,401,404,409,422 (5) | 5 |
| 6 | /items/{item_id} | DELETE | 202,401,404 (3) | 3 |
| 7 | /items/{item_id}/provenance | GET | 200,401,404 (3) | 3 |
| 8 | /items/{item_id}/score:explain | POST | 200,401,404 (3) | 3 |
| 9 | /memory/write | POST | 202,400,401,409,422,429,503 (7) | 7 |
| 10 | /memory/write:batch | POST | 202,400,401,409,422 (5) | 5 |
| 11 | /writes/{write_id} | GET | 200,401,404 (3) | 3 |
| 12 | /zones | GET | 200,401,403 (3) | 3 |
| 13 | /zones/{zone} | GET | 200,401,403,404 (4) | 4 |
| 14 | /zones/{zone}/config | GET | 200,401,403 (3) | 3 |
| 15 | /zones/{zone}/config | PUT | 200,401,403,422 (4) | 4 |
| 16 | /zones/{zone}/sweep | POST | 202,401,403 (3) | 3 |
| 17 | /zones/{zone}/items | GET | 200,401,403 (3) | 3 |
| 18 | /zones/{zone}/reindex | POST | 202,401,403 (3) | 3 |
| 19 | /config/scoring | GET | 200,401,403 (3) | 3 |
| 20 | /config/scoring | PUT | 200,401,403,422 (4) | 4 |
| 21 | /jobs/{job_id} | GET | 200,401,403,404 (4) | 4 |
| 22 | /tenants | POST | 201,401,403,422 (4) | 4 |
| 23 | /tenants | GET | 200,401,403 (3) | 3 |
| 24 | /tenants/{tenant_id} | GET | 200,401,403,404 (4) | 4 |
| 25 | /tenants/{tenant_id} | PATCH | 200,401,403,404,422 (5) | 5 |
| 26 | /tenants/{tenant_id} | DELETE | 202,401,403,404 (4) | 4 |
| 27 | /tenants/{tenant_id}/subjects/{subject_id} | DELETE | 202,401,403,404 (4) | 4 |
| 28 | /tenants/{tenant_id}/subjects/{subject_id}/export | GET | 200,202,401,403,404 (5) | 5 |
| 29 | /healthz | GET | 200 (1) | 1 |
| 30 | /readyz | GET | 200,503 (2) | 2 |
| 31 | /metrics | GET | 200 (1) | 1 |

**Sum check (shown, not asserted):**
7+3+3+3+5+3+3+3 = 30 (rows 1-8)
30+7+5+3+3+4+3+4 = 59 (rows 9-16, running: 30+7=37,+5=42,+3=45,+3=48,+4=52,+3=55,+4=59)
59+3+3+3+4+4+4+3 = 83 (rows 17-24: 59+3=62,+3=65,+3=68,+4=72... )

Recomputing cleanly, one row at a time, running total:
7, 10, 13, 16, 21, 24, 27, 30, 37, 42, 45, 48, 52, 55, 59, 62, 65, 68, 71, 75, 79, 83, 86, 90, 95, 99, 103, 108, 109, 111, **112**

**|T(S)| = 112 triples across 31 operations.**

### 1.3 Reported coverage

```
T_exercised = 0   (no test suite exists yet — verified: no Karate/.feature, no Newman
                    collection, no REST-assured test class found under this artifact's
                    directory at review time)
C_api = 0 / 112 = 0.0000
```

**This is a REJECT if the Phase 1.5 gate is evaluated literally today.** The corrective action is
not to write a suite as part of this review (out of scope: this is a testability review of the
spec, not test implementation), but to (a) hand this gap table to whoever authors the Karate/
Newman suite next, and (b) get the pipeline's gate definition clarified per §1.1.

---

## 2. Pagination / Cursor Edge-Case Test Outline

Two operations are genuine cursor-paginated list resources per the spec's own M1 anti-pattern-2
discipline: `GET /context/assemble` (via `AssembleContextRequest.cursor` / `ContextAssembly.next_cursor`,
POST form) and `POST /memory/search` (`SearchMemoryRequest.cursor` / `SearchMemoryResponse.next_cursor`).
`POST /context/assemble`'s cursor is unusual — it continues the **same** `assembly_id`/score basis
rather than starting a fresh query, which is the main source of edge cases below.

| Case | Endpoint | Setup | Expected |
|------|----------|-------|----------|
| PG-1 | `/context/assemble` | First call, no `cursor` field sent | 200; `next_cursor` present iff `items_considered > items_returned`; `assembly_id` set |
| PG-2 | `/context/assemble` | Second call with `cursor` from PG-1, same `session_id`/`task` | 200; same `assembly_id` and score basis as PG-1 (per spec note); `items_returned` continues past PG-1's set, no duplicate items |
| PG-3 | `/context/assemble` | `cursor` value from a **different** `assembly_id`/session reused | 400 or 422 (spec does not document a specific status for this — **GAP**, see §4) |
| PG-4 | `/context/assemble` | `cursor` present but `items_considered == items_returned` (nothing left) | 200; `next_cursor` absent/null; empty or terminal page, not an error |
| PG-5 | `/memory/search` | `page_size` at boundary values 1 and 500 (schema min/max) | 200 for both; 500 must not be rejected, 501+ must 400 |
| PG-6 | `/memory/search` | `page_size` = 0 or negative | 400 (schema `minimum: 1` violation) |
| PG-7 | `/memory/search` | `cursor: null` explicitly vs field omitted entirely | Both treated as "first page" — schema marks `cursor` `nullable: true`, must verify server treats omission and explicit null identically |
| PG-8 | `/memory/search` | Malformed/tampered opaque cursor string | 400 with a stable `error.code` (not a 500) — **not explicitly documented; treat as UnprocessableEntity/BadRequest gap, see §4** |
| PG-9 | `/zones/{zone}/items` | `page_size` default (50) omitted vs explicit 50 | Identical result set — verifies default wiring |
| PG-10 | `/context/assemble` GET form | `token_budget` at min(1) and max(262144) boundary | 200 at both; 262145 → 400 |

---

## 3. BOLA/IDOR Test Plan (HLD threats I-1, I-2, I-3)

**Threat basis (HLD §10):** I-1 cross-tenant retrieval via shared ANN index; I-2 `UserAffinity`
as a soft score term that can out-score a zero-affinity penalty; I-3 shared caches as a
cross-tenant existence oracle. The spec's mitigation is architectural (`tenant_id` derives only
from the verified JWT claim, never from any request body/path — confirmed absent from every
schema in `components/schemas`) plus per-tenant physical partitioning (ADR-013/ADR-007, outside
this API layer). The API-layer test obligation is to prove the **boundary itself never leaks**,
regardless of what's true underneath.

Caller model for every test below: **Tenant A** holds a valid JWT (`tenant_id=A`, correct scope).
**Victim resource** belongs to **Tenant B**. Attack = Tenant A's token used against Tenant B's
resource ID, guessed or enumerated.

| Test ID | Endpoint | Attack | Expected (per spec's `NotFound` design contract) | Threat ref |
|---------|----------|--------|---------------------------------------------------|------------|
| BOLA-1 | `GET /items/{item_id}` | Tenant A token, `item_id` belonging to Tenant B | **404**, not 403 — spec's `NotFound` response explicitly states cross-tenant existence must "never be distinguished from true absence, to avoid a tenant-existence oracle" | I-1, I-3 |
| BOLA-2 | `PATCH /items/{item_id}` | Tenant A token, Tenant B's `item_id`, valid body | 404 (not 401/403/200) | I-1 |
| BOLA-3 | `DELETE /items/{item_id}` | Tenant A token, Tenant B's `item_id` | 404 | I-1 |
| BOLA-4 | `GET /items/{item_id}/provenance` | Tenant A token, Tenant B's `item_id` | 404 — provenance chain must not leak even record count | I-1, T-2 adjacent |
| BOLA-5 | `POST /items/{item_id}/score:explain` | Tenant A token, Tenant B's `item_id` | 404 — must not leak `score_terms`/`user_affinity` cross-tenant (direct I-2 test: if this ever returned 200, `UserAffinity` cross-tenant scoring would be externally observable) | I-2 |
| BOLA-6 | `GET /writes/{write_id}` | Tenant A token, Tenant B's `write_id` (UUID, unguessable by design but must still be enforced server-side, not just obscured) | 404 | I-1 |
| BOLA-7 | `GET /jobs/{job_id}` | Tenant A token, Tenant B's `job_id` | 404 | I-1 |
| BOLA-8 | `GET /zones/{zone}` and `/zones/{zone}/items` | Tenant A `admin:zones` token against zone data — verify `ZoneId` is a fixed global enum (not tenant-scoped) so zone-level admin reads are correctly tenant-partitioned **within** the zone response body, not just at the path | check that `ZoneSummary`/`ZoneItemsPage` items returned are Tenant-A-only, never mixed | I-1 |
| BOLA-9 | `GET /tenants/{tenant_id}` | Tenant A `admin:tenants` token, Tenant B's `tenant_id` in path | Spec puts `tenant_id` **in the path** here (control-plane exception) — must verify `admin:tenants` scope is itself tenant-A-restricted or is a platform-level cross-tenant scope by design; if the latter, this is NOT a BOLA bug but must be explicitly asserted as intended in the test, not assumed | ADR-013 boundary |
| BOLA-10 | `DELETE /tenants/{tenant_id}/subjects/{subject_id}` | Tenant A token, correct `tenant_id=A` but Tenant B's `subject_id` (guessing another tenant's data subject inside A's own partition is a *different*, intra-tenant IDOR) | 404 — subject must resolve only within the caller's own tenant partition | I-1 adjacent (subject-level IDOR, not cross-tenant) |
| BOLA-11 | `GET /tenants/{tenant_id}/subjects/{subject_id}/export` | Same as BOLA-10 | 404 — export bundle must not leak Tenant B subject existence via timing or status-code difference (I-3-style oracle) | I-3 |
| BOLA-12 (timing) | `GET /items/{item_id}` | Compare response latency for (a) Tenant A's own real `item_id`-not-found vs (b) Tenant B's real existing `item_id` queried by Tenant A | Latency distributions must not be statistically distinguishable — a fast-path cache hit on (b) vs a slow miss on (a) is exactly the I-3 timing oracle the spec's cache-key-prefixing mitigation is meant to close; this must be measured, not assumed closed by the architecture doc alone | I-3 |
| BOLA-13 | `POST /context/assemble`, `POST /memory/search` | Tenant A token; craft `zones`/`query`/`query_embedding` designed to be a near-neighbor of a known Tenant-B-only fact | 200 with **zero** Tenant B items in `items[]`; this is the end-to-end I-1 regression test the unit-level BOLA-1..7 tests don't cover, since assembly/search never take an `item_id` at all — the leak path here is entirely inside retrieval, not authorization | I-1 (primary), I-2 |
| BOLA-14 | `POST /context/assemble` | Tenant A token; item exists in Tenant A's own partition with real `user_affinity` — confirm response payload's `score_terms.user_affinity` is computed against Tenant-A-only interaction history, not global | Value must be explainable as intra-tenant only (needs a provenance/fixture-based assertion, not just "not 0") | I-2 |

**Test data note (DPDP/api-testing-core §9.4):** all `item_id`/`subject_id`/`write_id` values in
this plan must be synthetic UUIDs generated per-test-run; do not use real tenant data even under
tenant A's own valid scope.

---

## 4. Gap Analysis — Missing/Ambiguous Traceability and Coverage Blockers

| Endpoint / Area | Gap | Severity | FR-NNN traceability status |
|---|---|---|---|
| `/context/assemble` POST, stale/foreign `cursor` (PG-3) | No documented status code for a `cursor` that doesn't belong to the current `assembly_id`/session — spec text describes the *semantics* (Section 7.1 continuation) but not the *error contract* | Medium | FR-009 traced, but error case untraced |
| `/memory/search`, malformed opaque `cursor` (PG-8) | Same gap — no explicit `400`/`422` mapping documented for a corrupted cursor token | Medium | FR-006 traced, error case untraced |
| `/tenants/{tenant_id}` family | `tenant_id` appears as a **path** parameter here (only place in the API tenant_id is not purely JWT-derived) — the spec doesn't state whether `admin:tenants` scope is itself global (platform operator) or per-tenant-restricted; BOLA-9 cannot be scored pass/fail without this being resolved first | High (blocks a whole test's expected-result definition) | NFR-006 traced; scope-semantics untraced |
| `/zones/{zone}/*` admin endpoints | `ZoneId` enum (`1-working` … `8-consolidation`) is **not tenant-scoped in the schema** — need confirmation whether zone admin views are per-tenant (filtered server-side from the caller's JWT) or genuinely global/platform-level; affects BOLA-8's pass criterion | High | FR-012/NFR-009 traced; tenant-scoping of admin reads untraced |
| Idempotency-Key description ("resend with different body returns 409") | Testable, but the exact diff algorithm (full-body hash vs semantic diff) isn't specified — a test asserting 409 on "same key, semantically-equivalent-but-differently-serialized body" (e.g. key order) needs this clarified | Low | Traceable via api-design-core anti-pattern 2 comment; implementation-detail gap only |
| `503 ServiceDegraded` on `/context/assemble` | Response schema reused generic `ErrorResponse`, but the operation description says the body may still carry `degraded:true` inside a **200**, not just inside the 503 error envelope — the two degraded-signaling paths (200-with-degraded-flag vs 503-error) are not mutually exclusive in the text and need an explicit precedence test | Medium | NFR-004 `[ASSUMED — OAQ-13]`, doubly untraced pending that confirmation |
| `WriteRejectedNotDurable` (503) on `/memory/write` | Correctly documented as the sole accepted SPOF; testable directly, no gap | — | FR-009/010/013 fully traced |
| `/healthz`, `/readyz`, `/metrics` | No `FR-NNN` traceability by design (explicitly noted as a deployment/operability concern, not silently omitted) — acceptable, not a gap | — | Explicitly N/A, correctly documented as such |
| Whole-spec: `x-consensus-gate-status: PENDING` | The spec's own header flags itself as pending gate approval — consistent with the C_api=0 finding in §1; this review's REJECT-by-literal-C_api reading is the expected state for a PENDING artifact, not a surprise finding | Informational | — |

**Endpoints requiring gap-fill before a Karate/Newman suite can reach 100% design-time
testability (C_plan):** `/context/assemble` (cursor error path), `/memory/search` (cursor error
path), `/tenants/{tenant_id}` family (scope-semantics), `/zones/{zone}/*` family (tenant-scoping
of admin reads). All other 27 operations are testable as documented — no blocking gap.

---

## 5. Summary

- **|T(S)| = 112 triples**, fully enumerated in §1.2, derivation shown and re-summed for
  verification.
- **C_api = 0 / 112 = 0.0000** — correct per M1's definition given no test suite exists yet;
  this is a literal REJECT against the Phase 1.5 `C_api >= 0.85` gate if applied today, and the
  gate's intended trigger point should be clarified (recommend renaming the design-time check to
  `C_plan` per §1.1 rather than reusing `C_api`), per api-testing-core's explicit anti-pattern-1
  and applicability-note guidance against ever asserting or implying a non-zero coverage number
  without an executed suite behind it.
- **Pagination/cursor edge cases**: 10 test cases outlined for `/context/assemble` and
  `/memory/search` (§2); 2 of them (PG-3, PG-8) hit undocumented error contracts (see §4).
- **BOLA/IDOR plan**: 14 test cases across every tenant-scoped endpoint (§3), directly tied to
  HLD threats I-1/I-2/I-3, including one end-to-end retrieval-level test (BOLA-13) and one
  timing-oracle test (BOLA-12) that unit-level per-endpoint tests alone would miss.
- **4 High/Medium-severity gaps** block full design-time testability on 4 of 31 operations; the
  remaining 27 are fully testable as specified.
