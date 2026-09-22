# AC-013 Full DPDP Crypto-Shredding Erasure Cascade — Design (GitHub #TBD, Sprint 5 candidate)

Status: **DRAFT — NOT IMPLEMENTATION-READY.** Several items below are genuine open decisions,
not resolved with existing-codebase evidence the way `fr013-predicate-schema-design.md`'s were.
This document states plainly where it is complete and where it is not, rather than overclaiming
readiness to look more finished than it is.

Related: SRS.md AC-013 (`NFR-006`), SRS.md §4.1 "AC-013 Implementation Scope Note" (2026-09-19
docs-drift correction — the authoritative statement of what's shipped today),
`docs/phase-1-architecture/HLD.md` Section 10 (DPDP-1..6), OAQ-10 (§11),
`docs/phase-7-routing/dpdp-retention-policy.md` (Sprint 1's own scoping doc, its "Open decision"
blocking AR1-G3), `src/dashanan/domain/zone8_crypto_shredding.py`,
`src/dashanan/infrastructure/dpdp_erasure_cascade.py`, `docs/phase-1.5-api/openapi.yaml` (`DELETE
/tenants/{tenant_id}/subjects/{subject_id}`, `GET .../export`). **Added 2026-09-22 (v2.0,
solution-architect review round 6 — see Section 1's major correction):**
`src/dashanan/application/subject_erasure_cascade.py`,
`src/dashanan/application/zone8_crypto_shredding_store.py`,
`src/dashanan/application/unified_subject_erasure_orchestrator.py`,
`src/dashanan/infrastructure/sql_zone8_subject_index_repository.py`, `src/dashanan/api/app.py`'s
`eraseSubject` handler (AC-023-3) — all real, already-built prior art the v1-v1.4 drafts never
read or cited.

---

## 1. Problem statement

AC-013 (SRS.md, verbatim): *"Given a data subject requests erasure, When the erasure cascade
executes across all 8 zones plus indices and archives, Then the subject's payload content becomes
permanently unrecoverable via crypto-shredding while the Zone 7 provenance chain structure
(hashes, timestamps) remains intact and verifiable."*

**MAJOR CORRECTION (2026-09-22, solution-architect review round 6, finding: false "what ships
today" baseline).** The paragraph below this note originally described only
`CrossZoneDpdpErasureCascade` and concluded the cascade "does not touch Zones 1, 3, 4, 5, 8" and
that the `DELETE` endpoint is "contract-only... no handler wires them." **Both claims were false.**
A prior sprint (DASH-STORY-020, DASH-STORY-025/DSHN-70) already built substantially more than this
document's v1-v1.4 drafts accounted for, confirmed by direct read of the real modules and their
own module docstrings:

- **`DELETE /tenants/{tenant_id}/subjects/{subject_id}` IS wired.** `src/dashanan/api/app.py`'s
  `eraseSubject` handler (operation_id `eraseSubject`, its own AC-023-3 docstring) calls
  `AppContext.subject_erasure_service.request_erasure(...)` for real, today.
- **`SubjectErasureCascadeService`** (`src/dashanan/application/subject_erasure_cascade.py`) is a
  real, tested Facade that DOES call `zone8_crypto_shredding.py`'s primitives for real, via
  `Zone8SubjectKeyedArchiver.erase_subject` (`zone8_crypto_shredding_store.py`) — genuine
  crypto-shredding, not a stub, tracked through a real `SubjectErasureJob`/`GET /jobs/{job_id}`
  lifecycle. **This is currently the ONLY leg the live endpoint actually invokes.**
- **`UnifiedSubjectErasureOrchestrator`** (`src/dashanan/application/
  unified_subject_erasure_orchestrator.py`, DASH-STORY-025/DSHN-70) is a SEPARATE, already-built
  Facade that fans out to Zone 8 (`SubjectErasureCascadeService`), Zone 3
  (`SqlSemanticRepository.delete_edges_by_subject`), Zone 5 (`EntityMemoryRepository.erase_entity`),
  and Zone 2/6 (`CrossZoneDpdpErasureCascade`, via a `SubjectToItemIndex` Protocol seam) — **but its
  own module docstring and `app.py`'s AC-023-3 comment both confirm it is NOT wired into the live
  endpoint yet.** `app.py` calls the narrower Zone-8-only service, not this orchestrator.
- **The `SubjectToItemIndex` Protocol already exists** as the exact dependency-inversion seam this
  document's own Section 3 (below) independently proposed as a new `subject_item_index` table.
  `NullSubjectToItemIndex` (the shipped default) resolves every subject to zero items, so the
  orchestrator's Zone 2/6 leg is genuinely invoked on every call but currently fulfils nothing —
  an honest, already-disclosed limit in that module's own docstring, not a shortcut this document
  needs to rediscover.
- **Zone 4 is deliberately, permanently excluded**, not merely blocked on infrastructure: the
  orchestrator's own docstring states `Procedure`'s real field set carries no `subject_id`,
  `entity_id`, or any subject-linkable reference at all — there is no seam to erase through.
- **Zone 1 is not yet touched by either mechanism** — genuinely missing, this document's Section 2
  finding about Zone 1 was directionally right for the wrong reason (not a storage-shape blocker
  like Zone 4/5, simply never built).
- **Zone 5's real remaining blocker is narrower than v1-v1.4 stated**: `EntityMemoryRepository`
  (Shape A, in-memory) already has a real `erase_entity` method the orchestrator already calls —
  Zone 5 erasure WORKS today for Shape A deployments. The genuine gap is only a Shape B (Postgres)
  adapter for Zone 5, not Zone 5 erasure logic itself. Zone 4 has no such adapter question at all
  (see above — no seam exists, Shape B or otherwise).

**Consequence for this document's own scope:** the real remaining gap AC-013 needs is
substantially NARROWER than a from-scratch build. It is now: (a) wire
`UnifiedSubjectErasureOrchestrator` into `app.py`'s `eraseSubject` handler in place of (or
extending) the narrower `SubjectErasureCascadeService`; (b) implement a real `SubjectToItemIndex`
(this document's Section 3 proposal, reframed as fulfilling that existing Protocol rather than
inventing new architecture); (c) add a Zone 1 leg to the orchestrator, which has none today; (d)
Section 6's Zone 7 audit-record question remains open exactly as before. Sections 3-9 below are
revised accordingly where they assumed a from-scratch build; see each section's own 2026-09-22
correction notes.

This design proposes the target-state architecture for that narrower remaining gap. It does
**not** implement anything, and per Section 6 below it does not resolve every open question — some
are legitimately unresolved and are named as such rather than glossed over.

---

## 2. A prerequisite finding this design surfaces (new, not previously documented)

Before proposing the erasure mechanism itself, a structural gap needs to be named because it
changes what "erase Zone 4/5" can even mean:

**Zones 4 (Procedural) and 5 (Entity) have no Postgres-backed (Shape B) storage adapter today.**
Confirmed by inspection: `src/dashanan/infrastructure/` has `episodic_schema.sql`,
`semantic_schema.sql`, `provenance_schema.sql`, `zone8_consolidation_schema.sql` — there is no
`procedural_schema.sql` or `entity_schema.sql`. `in_memory_procedural_memory_repository.py` and
`entity_memory_repository.py` (Zone 5's own docstring: *"Shape A only... a durable/distributed
Shape B adapter is a separate follow-on"*) are in-process, non-durable stores. Under the real API
host's composition root, Zone 4/5 data currently lives only in process memory.

**Consequence for this design:** a `DELETE /tenants/{t}/subjects/{s}` cascade that claims to erase
Zone 4/5 content would be erasing data that has no durable existence to begin with in a real Shape
B deployment — there is nothing between two restarts to erase. This design therefore treats
**Shape B (Postgres) adapters for Zones 4 and 5 as a hard prerequisite**, not part of this story's
own scope (that is FR-011/FR-015-adjacent infrastructure work, tracked as a separate dependency
below in Section 7), and scopes AC-013's *own* cascade work to the zones that already have durable
storage (Zones 1, 2, 3, 6, 7, 8) plus a forward-compatible interface so Zone 4/5 slot in once their
Shape B adapters exist. Silently pretending Zone 4/5 erasure is "done" without durable storage
underneath it would satisfy the letter of a test but not the actual DPDP obligation — not proposed
here.

**Correction (2026-09-22, Section 1's major correction above):** this finding's TECHNICAL substance
(no Postgres schema for Zone 4/5) remains accurate and independently re-confirmed. Two refinements:
Zone 4 is now known to be permanently excluded regardless of storage shape (no subject-linkable
field exists in `Procedure` at all, per `unified_subject_erasure_orchestrator.py`'s own docstring)
— it is not merely "blocked," it has no erasure seam to build against, Shape A or B. Zone 5's
erasure LOGIC already exists and already runs today for Shape A deployments
(`EntityMemoryRepository.erase_entity`, already called by `UnifiedSubjectErasureOrchestrator`) —
only a Shape B adapter is the real remaining Zone 5 gap, not erasure logic itself.

---

## 3. `subject_id -> item_id[]` resolution/index layer

**Correction (2026-09-22, Section 1's major correction above):** this section's proposal is not
new architecture — `unified_subject_erasure_orchestrator.py` already defines the exact seam this
section needs to fulfil: a `SubjectToItemIndex` Protocol (`items_for_subject(tenant_id,
subject_id) -> tuple[str, ...]`), currently satisfied only by `NullSubjectToItemIndex` (returns
zero items always). The table proposed below is this design's recommended CONCRETE
implementation of that already-defined Protocol, not a from-scratch index design — reframed,
substance unchanged.

**Decision: a dedicated subject-index table in the Shape B (Postgres) store, not a Zone 6/7
side-index.** ADR-006 already mandates a `subject_id` secondary index on every zone table (HLD
§11 DPDP-3: *"without it the cascade is eleven full scans"*), and `openapi.yaml`'s schema already
carries `subject_id` on write requests (line 1430: `WriteMemoryRequest.provenance.subject_id`,
`nullable: true`, "Data-subject identifier for the DPDP erasure/export index (ADR-006)"). The
field exists at the wire layer; nothing populates or indexes it yet.

Proposed table (Shape B only — Shape A/dev remains explicitly out of scope for a real compliance
workflow, matching this codebase's existing Shape A/B split convention):

```sql
CREATE TABLE subject_item_index (
    tenant_id   TEXT NOT NULL,
    subject_id  TEXT NOT NULL,
    zone_id     SMALLINT NOT NULL,   -- 1..8, ZoneId enum ordinal
    item_id     TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, subject_id, zone_id, item_id)
);
CREATE INDEX idx_subject_item_index_lookup ON subject_item_index (tenant_id, subject_id);
```

Written to at the same write-gate boundary every zone write already passes through (this mirrors
`write_gate.py`'s existing "durable at the write barrier" pattern, OAQ-8) — whenever
`ProvenanceWriteBlock.subject_id` is populated on a write, an index row is appended for that
write's `(zone_id, item_id)`. This is additive (a new table, a new write-gate side-effect) and
does not change any existing zone's own schema or the FR-013 write path.

**Open question, not resolved here:** whether every zone write is *required* to carry a
`subject_id` (making the index always-complete) or whether it stays optional as `openapi.yaml`
currently declares it (`nullable: true`), in which case pre-existing rows written before this
design lands have no index entry and cannot be reached by a future erasure request. This is a real
product/legal decision (does DPDP obligate backfilling `subject_id` for historical data?), not an
engineering one, and is listed as an open item in Section 6.

---

## 4. Crypto-shredding vs. ordinary delete, per zone

**Correction (2026-09-22, Section 1's major correction above):** the table below's Zone 3, 5, and
8 rows describe mechanisms that ALREADY EXIST and already run today via
`UnifiedSubjectErasureOrchestrator` (Zone 3: `SqlSemanticRepository.delete_edges_by_subject`;
Zone 5: `EntityMemoryRepository.erase_entity`, Shape A; Zone 8:
`SubjectErasureCascadeService`/`Zone8SubjectKeyedArchiver`, already wired into the live endpoint).
This section's original framing presented them as proposed-but-unbuilt; they are confirmed-built,
just not yet all reachable from one wired call path (Section 1). The table's substance (which
mechanism per zone) was independently correct despite that framing error.

`zone8_crypto_shredding.py` intentionally provides pure primitives only; per ADR-009 only Zone 8
has per-subject envelope encryption today (object-store blobs, versioned, object-locked — a
genuine WORM store where in-place deletion is not an option, which is exactly why crypto-shredding
was invented for it). Zones 1-5, 7 are ordinary mutable/insertable Postgres rows (Zone 2 excepted,
see below) with no such WORM constraint.

**Decision: crypto-shredding is scoped to Zone 8 only; Zones 1, 3, 4, 5 are erased by ordinary
row deletion (or crypto-shredding is added to them only if a future compliance requirement
demands it — not asserted as needed here).** Rationale: DPDP-2's own text (HLD §10) frames
crypto-shredding as the resolution to a *specific* conflict — Zone 7's append-only-ness versus the
erasure right — not as a blanket policy for every zone. Zone 8 shares that same WORM conflict
(object-locked bucket) and is the only other zone where a physical delete is architecturally
unavailable. Extending crypto-shredding to Zones 1/3/4/5, which support ordinary `DELETE`, would
add real key-management complexity (key storage, rotation, per-record vs. per-subject granularity)
without a corresponding technical constraint forcing it. This mirrors DPDP-2's own reasoning
rather than inventing a new one.

Zone-by-zone erasure mechanism:

| Zone | Mechanism | Notes |
|---|---|---|
| 1 (Working) | Ordinary delete from the Shape B hot-buffer (Redis) or in-process store | Short TTL already limits exposure; a subject erasure request should still force-evict any live entry. |
| 2 (Episodic) | Compliance-role `DELETE`, bypassing the append-only trigger (Section 5) | Already the pattern this codebase's own `episodic_schema.sql` comments anticipate. |
| 3 (Semantic) | Ordinary `DELETE` | `semantic_schema.sql` has **no** append-only trigger (confirmed by inspection — it uses an UPDATE-able `state` field for FR-013 conflict marking instead) — no compliance-role workaround needed here, `app_login_user`-scoped delete suffices once the row is identified via Section 3's index. |
| 4 (Procedural) | Ordinary `DELETE`, once a Shape B adapter exists (Section 2 prerequisite) | Blocked on the prerequisite; not buildable against today's in-memory-only store. |
| 5 (Entity) | Ordinary `DELETE`, once a Shape B adapter exists (Section 2 prerequisite) | Same blocker as Zone 4. |
| 6 (Retrieval-Index) | Existing mechanism, unchanged | `RetrievalIndexEviction` (`dpdp_erasure_cascade.py`) already does this correctly today — reused, not rebuilt. |
| 7 (Provenance/Audit) | **Not erased** — see Section 5 | Structural record survives by design (AC-013's own text). |
| 8 (Consolidation) | Crypto-shredding: destroy the subject's `zone8_crypto_shredding` key via a new `SubjectKeyStorePort.destroy_key` call | `zone8_crypto_shredding.py`'s own module docstring already names this exact port and this exact call as the intended orchestration seam — it does not exist yet (no `SubjectKeyStorePort` implementation was found in `src/dashanan/application/` beyond the Protocol name referenced in the docstring; this needs verification against `zone8_crypto_shredding_store.py` at implementation time, not assumed complete here). |

**Consequence:** the cascade ordering principle `dpdp_erasure_cascade.py`'s docstring already
established (search-surface-first, so a retry never leaves PII newly searchable) generalizes, as a
GENERAL principle covering all 6 zones a full 8-zone AC-013 implementation will eventually touch,
to: **Zone 6 first, then Zones 1/3/4/5/2 (searchable/mutable stores), then Zone 8 last
(crypto-shred, irreversible).** **This story's own cascade job (Section 7) touches only the subset
of that general ordering that already has durable storage today — Zone 6, then Zones 1/3/2, then
Zone 8 last — per Section 2's prerequisite scoping.** Zones 4/5 are listed here only because they
are part of the general principle for a reader planning the eventual full cascade once the Section
2 Shape B prerequisite lands; they are NOT part of this story's own iteration order (corrected
2026-09-22, solution-architect review round 4 finding 11 — an earlier draft of this paragraph
implied Zone 4/5 were touched by this story's own job, contradicting Section 2's scoping and every
downstream artifact's `must_not_deviate` guarantee). Zone 8's key destruction remains the one truly
irreversible step in the whole cascade, so it should only fire after every reversible-if-retried
leg (in this story's scope: Zones 6/1/3/2) has already succeeded, not before.

---

## 5. Postgres compliance-role adapters for append-only zones (Zone 2; Zone 4/5 once they exist)

Zone 2's `trg_episodic_entries_append_only` trigger already documents its own escape hatch
(`episodic_schema.sql`, read in full): a **separate, narrowly-scoped compliance role**, distinct
from `app_login_user`, authorized for a **PK-only `DELETE`** — never a widened application-role
grant, never a trigger removal.

Proposed compliance role (Shape B migration, additive to the existing per-zone least-privilege
role set the migration runner already applies):

```sql
CREATE ROLE dashanan_compliance_erasure_role LOGIN;
GRANT USAGE ON SCHEMA public TO dashanan_compliance_erasure_role;
GRANT DELETE ON episodic_entries TO dashanan_compliance_erasure_role;
-- No SELECT, INSERT, or UPDATE grant -- this role can only delete rows it is
-- given the exact (tenant_id, item_id) primary key for, resolved beforehand
-- via Section 3's subject_item_index (which this role also does NOT have
-- SELECT on -- the resolution step runs under app_login_user or a read-only
-- compliance-reader role, and the item_id list is handed to this role's
-- session as parameters, never derived by it).
```

The erasure job (Section 7) connects as this role *only* for the Zone 2 (and future Zone 4/5)
delete statements, and as `app_login_user`/`compliance_reader` for everything else — a real
privilege boundary, not a comment-only convention. Same pattern applies to Zone 4/5's future
schemas once Section 2's prerequisite lands: their `procedural_schema.sql`/`entity_schema.sql`
should be authored with the same "should this be append-only, and if so give it the same
compliance-role escape hatch" question asked explicitly during that future story, not assumed
either way here.

---

## 6. Zone 7 audit-log-preservation boundary — status: `ADOPTED AS WORKING ASSUMPTION (2026-09-22, user decision)`

**UPDATE 2026-09-22 (user decision):** the option below is now adopted as a working assumption for
implementation purposes. The underlying legal question — whether a `source_type: erasure_event`
record referencing `subject_id` is itself "retained personal data" under DPDP — is NOT resolved by
this decision and remains a tracked follow-up, same posture as OAQ-10 (Section 8). This is an
engineering scoping decision (build against this shape), not a legal determination.

`dpdp-retention-policy.md`'s own text already states the exact shape of the open question: how
does Zone 7 record "an erasure occurred for subject X" without retaining the erased PII itself?

This design proposes one concrete option, now adopted as a working assumption:

**Option (adopted as working assumption, not legally confirmed):** on cascade completion, write a new Zone 7 provenance record
whose `source_type` is a new literal (e.g. `"erasure_event"`) and whose content field holds only
`{subject_id, erased_zones: [...], erased_at, cascade_job_id}` — structural metadata already
established as non-PII by this codebase's own classification (item/tenant/job identifiers, not
payload content) — chained into the existing hash chain like any other provenance record, so the
audit trail shows an erasure happened, when, and for which zones, without holding a byte of the
erased content itself. This keeps Zone 7 append-only and untouched by the cascade (Section 4's
"not erased" row), consistent with AC-013's own "Zone 7 provenance chain structure... remains
intact" clause.

**What remains genuinely open:** whether a `source_type: erasure_event` record referencing
`subject_id` is itself something DPDP would consider "retained personal data about the subject"
(the subject identifier persists in Zone 7 forever, even though no payload does) — this is a legal
question this document explicitly declines to answer, matching OAQ-10's own posture (Section 8).

---

## 7. Cascade job design: sync vs. async, and the erasure job's shape

**Correction (2026-09-22, Section 1's major correction above):** the async job shape proposed
below already exists — TWICE. `SubjectErasureCascadeService` has its own real
`SubjectErasureJob`/`SubjectErasureJobStore`/`GET /jobs/{job_id}`-shaped lifecycle for its Zone-8
leg, and `UnifiedSubjectErasureOrchestrator` composes a SEPARATE, combined job store for the
cross-zone outcome (deliberately separate from Zone 8's own bookkeeping, per that module's own
constructor docstring). This section's real remaining job is therefore not "design a new job
component," but: wire the orchestrator's already-real job lifecycle into the same `GET
/jobs/{job_id}` endpoint `app.py` already exposes for the Zone-8-only service, add the
`subject_item_index` cleanup call (step 6 below) into the orchestrator's existing per-zone loop,
and add the still-missing Zone 1 leg to that same loop. The narrative below is kept as the
requirements this wiring must satisfy, not as a from-scratch component design.

**Decision: async, using the existing `GET /jobs/{job_id}` contract `openapi.yaml` already
anticipates** (its own comment: *"Async admin job status (sweep, reindex, ...)"* already lists
erasure as a candidate). A cascade touching up to 6 zones in THIS story's own scope (Zones
1/2/3/6/7/8 — Section 2) plus a compliance-role connection for Zone 2, potentially over an
unbounded number of items for a long-lived subject, is not a bounded-latency operation suitable
for a synchronous request — this matches `openapi.yaml`'s own `202 JobAccepted` response already
declared on `DELETE /tenants/{tenant_id}/subjects/{subject_id}`.

Proposed job shape (new application-layer component, `SubjectErasureJob`, composing):
1. Resolve `subject_id -> item_id[]` per zone via Section 3's index (read-only) — this story's own
   index only ever has rows for Zones 1/2/3/6/7/8, since nothing writes a Zone 4/5 row until the
   Section 2 prerequisite lands, so no explicit Zone 4/5 skip logic is needed in this step.
2. For each zone THIS STORY'S OWN JOB actually iterates over, in cascade order (Section 4's
   general ordering principle, restricted to this story's in-scope subset: 6, then 1/3/2, then 8
   last — corrected 2026-09-22, solution-architect review round 4 finding 11; Section 4's own
   "Zones 1/3/4/5/2" wording is the GENERAL principle for a future full 8-zone cascade, not this
   story's own iteration list):
   - Attempt every item's erasure for that zone.
   - On a per-item failure, record it and continue (matches this codebase's own established
     fail-safe-isolation pattern in `Zone2CapacityBackstopSweep._execute_one`) — one bad item must
     not abort the whole subject's cascade.
3. After all zones except 8 succeed for all items, destroy the Zone 8 subject key (irreversible,
   last step, per Section 4).
4. Write the Zone 7 erasure-event record (Section 6's proposed option), if adopted.

**Durability guarantee for the step-3/step-4 boundary (added 2026-09-22, consensus-agent
failure-mode review finding 2).** Step 3 (key destruction) is irreversible; step 4 (the Zone 7
audit record of that destruction) is not yet durable at the moment step 3 fires. A crash between
the two leaves an undetectable gap: data is gone, and nothing records that it happened. This
design adopts the same durability pattern `ADR-010` (HLD.md Section on Provenance durability —
write-ahead journal / Outbox pattern) already uses for the ordinary write path, applied here in
reverse order relative to a normal write:
- **Before** step 3 fires, the job durably appends an "erasure in progress" marker to the same
  local append-only journal ADR-010 already fsyncs to (`{subject_id, zone: 8, cascade_job_id,
  status: "key_destroy_in_progress", intended_at}`), fsynced before step 3 is invoked. This marker
  is the durability barrier, exactly as ADR-010's write-ahead entry is the durability barrier for
  an ordinary write — the marker exists whether or not step 3 or step 4 ever completes.
- Step 3 (key destroy) executes.
- **After** step 3 succeeds, step 4 finalizes: the marker is confirmed/promoted into the real Zone
  7 erasure-event record (Section 6's proposed option) and the journal entry is marked drained,
  mirroring the relay's own drain-and-acknowledge semantics.
- If the process crashes between step 3 and step 4's finalize, the journal still holds the
  "in_progress" marker. On restart, the job recovery sweep (mirroring the existing provenance
  relay's own backlog-drain behavior on recovery, HLD.md Section 8.4) checks Zone 8's key store to
  determine which of two states the marker is in, and handles each explicitly (corrected
  2026-09-22, consensus-agent failure-mode/retry/rollback/escalation round-2 re-review finding 1 —
  the prior version of this rule only covered the confirmed-complete case and left the
  unconfirmed case with no route out, see below):
  - **Key store CONFIRMS destruction already executed.** The sweep retries ONLY the finalize
    (step 4). Step 3 must never be re-attempted in this state — there is nothing left to destroy.
  - **Key store does NOT yet confirm destruction** (the crash landed before or during step 3
    itself, or the confirmation call is transiently unavailable). The sweep is ALLOWED to retry
    step 3 itself, under the SAME bounded k=2/exponential-backoff policy already defined below for
    item-level cascade retries. This is safe specifically because Zone 8 key-destroy is idempotent
    (stated above: "a spurious second destroy call against an already-destroyed key is a no-op at
    worst"). The **corrected, narrower rule** replaces the prior absolute: **step 3 must never be
    re-attempted once the key store CONFIRMS destruction; if destruction is unconfirmed, step 3
    may be safely retried under the bounded policy, since destroy is idempotent.** The prior
    absolute wording ("step 3 must never be re-attempted once a marker exists") directly
    contradicted this document's own idempotency claim and left a job with an unconfirmed marker
    stuck in `key_destroy_in_progress` forever, with no route to a terminal state — that
    contradiction is what this correction resolves.
  - If the bounded retries are exhausted with destruction still unconfirmed either way (the key
    store cannot be reached, or repeated destroy attempts fail without a clear confirm/deny), the
    job reaches the same terminal `FAILED` state defined below for exhausted item-level retries,
    with an explicit escalation to the named human compliance owner (the same `escalation_target`
    used throughout this section) — it does not remain stuck in `key_destroy_in_progress`
    indefinitely.
- **Asymmetry, stated explicitly:** the key destruction itself (step 3) is irreversible and cannot
  be retried or rolled back once it fires. The AUDIT of that destruction (step 4, and the marker
  written before step 3) is fully recoverable and retriable even though the event it records is
  not — a crash leaves a durable, detectable trace (the marker, in `status:
  "key_destroy_in_progress"`) rather than a silent gap, and the finalize step can be safely
  retried to completion independent of how many times it needs to run.
- **Zone 8 `subject_item_index` row cleanup durability (resolved 2026-09-22, consensus-agent
  failure-mode round-2 re-review finding 1 — the prior draft left this implicit).** Step 6 below
  deletes Zone 8's `subject_item_index` rows only after its key-destroy (step 3) succeeds. This
  delete is a SEPARATE write from the marker/finalize mechanism above — it is not automatically
  covered by step 4's finalize merely because both happen after step 3. Resolution: the Zone 8
  index-row delete is performed as PART OF step 4's finalize transaction (the same transaction
  that promotes the marker into the real Zone 7 erasure-event record), not as an independent,
  un-journaled write. If that finalize transaction itself fails partway (index rows deleted but
  the Zone 7 record not yet written, or vice versa), the recovery sweep's finalize retry (above)
  re-runs the whole finalize step idempotently — re-deleting already-deleted index rows is a
  no-op, and the Zone 7 erasure-event record write must itself be idempotent (keyed on
  `cascade_job_id`, so a retried finalize never appends a duplicate audit record). Zone 8's index
  cleanup therefore rides along with step 4 as one atomic finalize operation, precisely so the
  recovery sweep's existing single finalize-retry path covers both effects without needing a
  second recovery mechanism.
5. Job status (`GET /jobs/{job_id}`) reports per-zone success/failure counts, not just a binary
   done/not-done — mirroring FR-013's own `WriteReceipt` partial-status precedent (`207`/
   `status="partial"` pattern in `fr013-predicate-schema-design.md` §4.2 step 6) rather than a
   silent full-success claim when some items failed.
6. **`subject_item_index` row cleanup (added 2026-09-22, solution-architect review round 1
   finding 2 -- a real gap the original design left unaddressed).** After a zone's items are all successfully
   erased in step 2, this zone's own `subject_item_index` rows for that `subject_id` are deleted
   as part of the same step, not left in place. Rationale: the index row itself asserts "subject S
   had item I in zone Z" — retaining that assertion after I no longer exists means the index is
   itself retained personal data referencing the subject, a DPDP concern independent of and
   additional to OAQ-10's Zone-7-audit-record question (Section 6). A row for an item that failed
   erasure (step 2's per-item failure path) is deliberately NOT deleted, so the job's own
   per-zone failure report (step 5) and any retry (see "Not resolved here" below) can still find
   it. Zone 8's index rows are deleted only after its key-destroy (step 3) succeeds, consistent
   with the "last, irreversible" ordering.

**Retry policy for a partially-failed cascade (resolved 2026-09-22, consensus-agent failure-mode
review finding 1 — this was previously left as "not resolved here", which for a DPDP erasure
story means an item can be stuck indefinitely in "erasure attempted, not completed," exactly the
failure this story exists to prevent).**

- **Only failed items re-run, never the whole job.** Step 2's per-item, per-zone fail-safe
  isolation already means successful legs are done and durably reflected (their `subject_item_index`
  rows are already deleted, step 6); re-running a zone or the whole job would re-attempt
  already-erased items for no benefit and, for Zone 2's compliance-role delete adapter, would be an
  avoidable extra privileged operation against rows that no longer exist. Retry scope is exactly
  the set of `(zone_id, item_id)` pairs whose `subject_item_index` row is still present after step
  2 (step 6's own "kept for retry visibility" rows are the retry work list, not merely a diagnostic
  artifact).
- **Bounded retry count with backoff: this engagement's existing k=2 bounded-retry convention
  (reused from the Dev/QA/Review sub-task retry policy, `sprint5_implementation_execution_plan.json`
  `retry_policy`) is adopted for a partially-failed cascade's own item-level retries, with one
  compliance-specific addition: unlike the k=2 Dev/QA retry (which re-dispatches an agent
  immediately), a cascade item retry runs on an exponential backoff (base 30s, doubling, capped at
  10 minutes between attempts) because a per-item erasure failure here is far more likely to be a
  transient storage/lock contention issue than an engineering defect, and hammering a Postgres
  compliance-role connection or the Zone 8 key store immediately after a failure risks compounding
  the same transient condition. k=2 (not a higher bound) is kept rather than justifying a larger
  number for this job specifically: a compliance-critical erasure that still fails after 2 backed-off
  attempts is a signal worth a human compliance owner's attention sooner, not a signal to keep
  quietly retrying — matching this project's own general posture (`retry_policy` for review
  verdicts) that persistent failure escalates rather than loops indefinitely.
- **Explicit terminal state.** After the 2 retries are exhausted for any item, the job's own status
  transitions to a terminal `FAILED` state (not left `IN_PROGRESS` indefinitely) with the specific
  failed `(zone_id, item_id)` pairs and their last error visible via `GET /jobs/{job_id}`'s existing
  per-zone success/failure reporting (step 5) — the partial-erasure state is never silently hidden.
  A job reaching terminal `FAILED` is escalated to a named human compliance owner (the same
  `escalation_target` this project's `retry_policy` already names for a second REJECTED verdict,
  reused here for the same reason: this is no longer an engineering-remediable condition once
  bounded retries are exhausted) rather than left open with no defined path out. The job does NOT
  auto-close or report success while any item remains in this terminal-failed state; a subject with
  a terminal-`FAILED` erasure job is a DPDP compliance incident, not a background task to leave
  running.

**Still not resolved here:** whether an in-flight erasure job blocks new writes for that
`subject_id` during its run (a write racing an in-progress erasure could re-populate an item the
cascade already passed). This is a real concurrency question, independent of the retry policy above;
flagged, not designed.

---

## 8. Legal/regulatory disclaimer (carries OAQ-10 forward, unresolved)

This design proposes a **technical mechanism**: crypto-shredding for Zone 8, ordinary
compliance-scoped deletion for Zones 1-5, index-driven resolution, and a Zone 7 audit trail that
survives the cascade. **It does not resolve, and does not claim to resolve, OAQ-10** — HLD §11's
own extensive research (citing the DPDP Act's Section 12, the EDPB's own unsettled position on
crypto-shredding under GDPR Article 17, and the explicit absence of any Indian regulatory guidance
on the question) already states plainly that whether key destruction constitutes DPDP-compliant
erasure requires **real legal confirmation, not an engineering judgment call**. Nothing in this
document, its future implementation, or its test suite should be read, logged, or documented as
asserting legal sufficiency. Any story that implements this design must carry the same disclaimer
forward into its own acceptance criteria and Definition of Done, exactly as `zone8_crypto_shredding.py`'s
existing MUST-NOT-DEVIATE item 2 already requires of that module.

---

## 9. Definition of Done, mapped to AC-013's exact wording

| AC-013 clause | This design's coverage | Status |
|---|---|---|
| "the erasure cascade executes across all 8 zones" | Zones 1, 2, 3, 6, 7 (untouched by design), 8 covered; Zones 4, 5 blocked on the Section 2 prerequisite (no durable store exists to erase from) | **Partial** — cannot be fully done without the prerequisite landing first |
| "plus indices and archives" | Zone 6 (both indices) reused unchanged; Zone 8 (archive) crypto-shredded | Covered |
| "the subject's payload content becomes permanently unrecoverable via crypto-shredding" | True only for Zone 8; Zones 1-5 use ordinary deletion, which is also "permanently unrecoverable" but not "via crypto-shredding" in the literal sense | **Textual tension** — flagged in Section 4's rationale; a BA/SRS clarification on whether AC-013's "via crypto-shredding" phrase is scoped to Zone 8 specifically or intended as a blanket mechanism is a real open item, not assumed resolved by this design's Section 4 decision |
| "the Zone 7 provenance chain structure... remains intact and verifiable" | Zone 7 untouched by the cascade; Section 6's proposed erasure-event record is additive only | Covered, pending Section 6's adoption decision |
| Implicit in "erasure" — the `subject_id -> item_id[]` index itself (Section 3) must not remain as retained personal data after a successful erasure (added 2026-09-22, solution-architect review round 1 finding 2) | Section 7 step 6: each zone's own `subject_item_index` rows are deleted immediately after that zone's items are successfully erased; Zone 8's rows wait for its key-destroy (step 3); a failed item's row is deliberately kept for retry visibility | Covered |

**Overall: this design is not implementation-ready.** It resolves the mechanism-selection
questions (Sections 3-5, 7) with reasoning grounded in the existing codebase, but leaves three
items genuinely open (Section 2's prerequisite, Section 4's AC-013 textual-scope tension, Section
6's Zone 7 record adoption decision) that need a human/BA decision before a Sprint 5 story can be
scored implementation-ready the way FR-013's design eventually was.

---

## Change Log

| Date | Change |
|---|---|
| 2026-09-22 | v1 (DRAFT): Initial design produced for Sprint 5 planning. Surfaced the Zone 4/5 Shape B storage prerequisite (Section 2, not previously documented anywhere in the repo). Proposed subject_item_index (Section 3), zone-by-zone erasure mechanism selection (Section 4), the Zone 2 compliance-role pattern extended to future Zone 4/5 (Section 5), a proposed-not-adopted Zone 7 audit-record option (Section 6), an async job design reusing the existing `GET /jobs/{job_id}` contract (Section 7), and carried OAQ-10 forward unresolved (Section 8). Marked DRAFT, not IMPLEMENTATION-READY, pending human review of the three open items in Section 9. |
| 2026-09-22 | v1.1 (solution-architect review round 1, finding 2): Added Section 7 step 6 (`subject_item_index` row cleanup after successful per-zone erasure) -- the index itself would otherwise remain retained personal data after erasure, a gap the initial draft left unaddressed. |
| 2026-09-22 | v1.2 (solution-architect review round 2, finding 4): Added a Section 9 Definition-of-Done row for the v1.1 `subject_item_index` cleanup, so the document's own DoD summary reflects it. |
| 2026-09-22 | v1.3 (solution-architect review round 4, finding 11): Rewrote Section 4's "Consequence" paragraph and Section 7's job-order text (steps 1-2) to explicitly distinguish the GENERAL future-8-zone-cascade ordering principle (which legitimately lists Zones 1/3/4/5/2) from THIS STORY's own actual iteration scope (Zones 1/3/2 only, plus Zone 6 first and Zone 8 last) -- the prior wording could be read as claiming this story's own job touches Zone 4/5, contradicting Section 2's own scoping and the bundle-wide must_not_deviate guarantee. |
| 2026-09-22 | v1.4 (solution-architect review round 5, finding 14): Added these Change Log rows themselves -- rounds 1-4's fixes to this document had not been logged here, unlike `fr013-predicate-schema-design.md`'s own 7-row review-round Change Log precedent this project otherwise follows. No further content change; documentation-currency fix only. |
| 2026-09-22 | v2.0 (solution-architect review round 6, MAJOR): Corrected a false "what ships today" baseline in Section 1 -- `DELETE /tenants/{tenant_id}/subjects/{subject_id}` IS already wired (not contract-only), and three already-built, already-real modules (`subject_erasure_cascade.py`, `zone8_crypto_shredding_store.py`, `unified_subject_erasure_orchestrator.py`) were never read or cited by v1-v1.4, causing this document to propose rebuilding work that already exists rather than wiring/extending it. Section 1 now documents the real current state; Sections 2-7 each carry a 2026-09-22 correction note reframing their proposals against that real state (Section 3's index becomes a concrete `SubjectToItemIndex` implementation, not new architecture; Section 4's zone-by-zone mechanisms are confirmed-built, not proposed; Section 7's job design already exists twice and needs wiring, not designing). Story re-scoped and re-estimated accordingly in `backlog_draft.json`/`sprint5_ar1_assignments.json` (see those files' own 2026-09-22 round-6 correction notes). Still DRAFT, not IMPLEMENTATION-READY -- this correction narrows the remaining gap, it does not close it. |
| 2026-09-22 | v2.1 (consensus-agent failure-mode/retry/rollback/escalation review, REJECTED verdict remediation): Section 7 finding 1 -- replaced "not resolved here: exact retry policy" with a concrete policy (only failed items re-run; bounded k=2 retry with exponential backoff, base 30s/cap 10min; explicit terminal `FAILED` state visible via `GET /jobs/{job_id}`, escalated to a named human compliance owner rather than left open indefinitely). Section 7 finding 2 -- added an explicit durability guarantee for the step-3 (irreversible Zone 8 key destroy) / step-4 (Zone 7 audit record) boundary, reusing ADR-010's write-ahead-journal/outbox pattern (marker written before step 3, confirmed/finalized after), so a crash between the two leaves a durable, detectable trace instead of a silent gap; documented the asymmetry that key destruction itself is irreversible while the audit of it is fully recoverable/retriable. Both fixes close real gaps a separate consensus-agent review lens found that the architecture-conformance review rounds 1-9 above had not caught. |
| 2026-09-22 | v2.2 (consensus-agent failure-mode/retry/rollback/escalation round-2 re-review, verdict upgraded REJECT -> APPROVE WITH CHANGES, finding 1 -- the last remaining substantive gap): Section 7's recovery-sweep rule previously covered only the case where step 3 (Zone 8 key destroy) is CONFIRMED complete after a crash, and separately stated step 3 "must never be re-attempted once a marker exists" -- an absolute that, read together with the doc's own idempotency claim ("a spurious second destroy call against an already-destroyed key is a no-op at worst"), left a crash landing before/during step 3 with an unconfirmed marker stuck in `key_destroy_in_progress` forever, with no route to the terminal `FAILED` state. Fixed by narrowing the rule: step 3 must never be re-attempted once the key store CONFIRMS destruction; if unconfirmed, step 3 may be safely retried under the same bounded k=2/backoff policy, since destroy is idempotent -- and after those retries are exhausted with no confirmation either way, the job now reaches the same terminal `FAILED` state (v2.1's fix) with explicit escalation, rather than staying stuck indefinitely. Also resolved, per the same finding, whether step 6's Zone 8 `subject_item_index` row cleanup is covered by the same recoverable finalize (step 4) mechanism: it is -- the index-row delete is now stated explicitly as part of step 4's finalize transaction, idempotent on retry, rather than left as an implicit, separately-durable write. |
| 2026-09-22 | v2.3 (user decision): Section 6's Zone 7 audit-record status changed from `[NEEDS INPUT]` to `ADOPTED AS WORKING ASSUMPTION` -- the user chose to build against the proposed non-PII `erasure_event` record shape now, with legal confirmation of whether it constitutes retained personal data tracked as a follow-up, same posture as OAQ-10 (Section 8). This is an engineering scoping decision, not a legal determination; the underlying legal question remains open. |
