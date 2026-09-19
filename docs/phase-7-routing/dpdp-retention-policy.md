# DPDP (Digital Personal Data Protection Act 2023) Retention & Erasure Policy

**Source of truth:** `docs/phase-7-routing/ar1_assignments.json` (routing gap `AR1-G3`, acceptance
criteria `AC-002-DPDP-1` / `AC-002-CAP-DPDP-1`), `docs/phase-1.5-api/openapi.yaml` (`NFR-006` DPDP-1/2/3
endpoints), and `docs/phase-0-output/PRD.md` (`NFR-006`). This file consolidates the DPDP obligations
already scattered across those three documents into one policy view — it does not define any new
obligation, and where an obligation is still an open decision rather than a shipped mechanism, that
status is stated explicitly below, not smoothed over.

## Current implementation status — read this first

**Update (2026-09-19, DSHN-60 docs-drift correction):** the "no shipped erasure mechanism to audit yet"
claim two paragraphs below is now partially superseded. Commit `d888536` shipped
`src/dashanan/infrastructure/dpdp_erasure_cascade.py` (`CrossZoneDpdpErasureCascade`), a real, tested,
Shape A adapter — but it is **materially narrower** than AR1-G3/AC-013's target-state design this document
describes, and closes a *different* obligation than the one this section is about:

- It is keyed by `item_id`, not `subject_id` — the `subject_id → item_id` resolution step this
  document's own "What is NOT yet specified" paragraph (below) already flags as unbuilt is still unbuilt.
- It cascades to Zone 6 (vector + lexical) and Zone 2 (episodic, via the pluggable `Zone2EvictionPort` —
  itself not yet backed by a real Postgres compliance-role adapter, only the seam for one). It does
  **not** touch Zones 1, 3, 4, 5, 8, and does **not** implement crypto-shredding (key destruction) —
  it performs an ordinary delete/evict call through existing zone ports.
- It exists to satisfy `AC-002-CAP-DPDP-1` (DASH-STORY-004: a forced Zone-2 capacity eviction must not
  bypass a pending erasure obligation) — a narrower, already-in-scope Sprint-1 concern — **not** to
  implement the full `DELETE /tenants/{tenant_id}/subjects/{subject_id}` cascade AC-013 and the workflow
  below describe. See `SRS.md` §4.1 for the full drift analysis.

**AR1-G3 itself remains open.** The rest of this section (below) still accurately describes that
routing-gap status for the FULL crypto-shredding, subject_id-keyed, all-8-zone cascade.

**DPDP erasure is currently a P2, review-only obligation, not an implemented, shipped mechanism.**
`ar1_assignments.json`'s routing gap `AR1-G3` records that no verified agent scored ≥0.75 as an
*implementer* of a DPDP erasure mechanism (the agents carrying `data-security-privacy-core` are
infrastructure-scoped and score below threshold for application-level erasure). Its
`recommended_action` is explicit: DASH-STORY-004 already carries a P1 *review* by
`security-defense-architect` covering cross-zone erasure consistency, extended as a non-blocking privacy
*consult* on DASH-STORY-003 — **"if the user wants DPDP erasure implemented as its own capability rather
than reviewed, it needs a new backlog story — it is not implicitly covered by any Sprint 1 assignment."**
This is also recorded as an open human decision in `ar1_assignments.json`'s open-decisions list: *"Decide
whether DPDP erasure needs its own story or stays a review-only obligation (AR1-G3)."*

Separately, `openapi.yaml` already **specifies** (not yet necessarily implemented — API spec is a
contract, not a deployment record) two DPDP-facing endpoints under the `admin:tenants` scope:
`DELETE /tenants/{tenant_id}/subjects/{subject_id}` (erasure cascade) and
`GET /tenants/{tenant_id}/subjects/{subject_id}/export` (data-portability export). This policy document
describes the target-state design those endpoints imply; it does not assert that AR1-G3 is closed.

## Retention schedule (target-state design)

| Zone | PII retention trigger | Notes |
|---|---|---|
| Zone 1 (Working) | TTL-primary, shortest of all zones | Highest churn; PII here is the shortest-lived by construction (FR-001) |
| Zone 2 (Episodic) | Capacity cap + MaxAge forced-archival backstop (OAQ-4, DASH-STORY-004) | Eviction ordering is lowest-MemoryScore-first; **eviction must satisfy, not bypass, a pending DPDP erasure obligation** (`AC-002-CAP-DPDP-1`) — a scheduled eviction cannot be used to sidestep an already-requested erasure |
| Zones 3-5, 8 | Not in Sprint 1 scope | Retention policy for these zones is deferred with the zones themselves |
| Zone 6 (Retrieval-Index) | Erasure cascade target | Any crypto-shredding erasure must propagate here — `openapi.yaml` NFR-006/DPDP-2 explicitly names "both retrieval indices" (vector + lexical) as in-scope for the erasure cascade |
| Zone 7 (Provenance/Audit) | **Not erased by DPDP requests** | Provenance/audit history is the anti-hallucination backbone (FR-007); DPDP erasure targets the PII payload, not the fact that a now-erased item once existed — exact audit-log-preservation boundary is `[NEEDS INPUT]`, not yet specified in available docs, and must be resolved before AR1-G3 is closed as an implementation (not just a review) |

## Right-to-forget workflow (as specified in `openapi.yaml`, target-state)

1. `DELETE /tenants/{tenant_id}/subjects/{subject_id}` — `admin:tenants` scope required (see
   `docs/phase-1.5-api/rbac-matrix.md`).
2. Per `openapi.yaml`'s comment at NFR-006/DPDP-2/3: cascades across **all 8 zones**, **both retrieval
   indices** (vector + lexical), and **Zone 8 archives**, via crypto-shredding (key destruction, not
   row-by-row delete — consistent with `subject_id` being indexed at write time per `ADR-006`, so the
   erasure path can locate every record without a full-corpus scan).
3. Purpose-limitation tagging (`purpose` field, `DPDP-1`) must already exist on every write for the
   erasure path to know what may legitimately have been retained versus what predates purpose-limitation
   enforcement — this is a write-time obligation (`FR-010` cross-cutting provenance/audit enforcement),
   not something the erasure path can retroactively reconstruct.
4. `GET /tenants/{tenant_id}/subjects/{subject_id}/export` — the data-portability counterpart, same
   `admin:tenants` scope, same `subject_id` index.

**What is NOT yet specified:** the audit-log-preservation boundary (item 5 above — how the Provenance
zone (7) records that an erasure occurred without itself retaining the erased PII), and whether the
erasure cascade is synchronous or dispatched as one of the async admin jobs already polled via
`GET /jobs/{job_id}` (`openapi.yaml` comments it as a candidate: "Async admin job status (sweep, reindex,
DPDP erasure)" — the erasure job type is listed but its endpoint/job contract is not fully specified in
the reviewed docs). Both are `[NEEDS INPUT]` for whoever picks up AR1-G3's open decision.

## Cross-zone eviction interaction (`AC-002-CAP-DPDP-1`)

Per `ar1_assignments.json`: forced eviction from Zone 2 under the capacity/MaxAge backstop is a
P1-triggered concern because it "touches the storage-adapter/eviction surface on a PII-bearing zone,"
and the acceptance criterion requires that "forced eviction leave no partially-erased PII across Zone 2
and any indexing/audit zone that referenced it" — i.e. a routine capacity eviction and a DPDP erasure
request racing against the same item must not leave Zone 6's index or Zone 7's provenance record
pointing at PII that Zone 2 has already dropped, or vice versa. This is currently a P1 **review**
obligation on DASH-STORY-004 (`security-defense-architect`, 0.79 review fit), not an implemented
locking/coordination mechanism.

## Erasure verification runbook (target-state — closes doc-gap #5, does not close AR1-G3)

**This runbook describes how erasure completeness would be verified once AR1-G3's open decision is
resolved and an implementation exists.** It is written now so the verification procedure is not
invented ad hoc post-implementation, but per the "current implementation status" section above, none of
the steps below can be executed against a real system today — there is no shipped erasure mechanism to
audit yet. Each step below is marked `[TARGET]`.

Given a completed erasure request for `subject_id` under `tenant_id`:

1. `[TARGET]` **Zone 1 (Working).** Confirm no live key referencing `subject_id` remains in the Redis hot
   store for that tenant: `SCAN` the tenant's Zone 1 keyspace for any value whose `subject_id` field
   matches: `redis-cli --scan --pattern "zone1:{tenant_id}:*" | xargs -I{} redis-cli HGET {} subject_id`,
   expect zero matches. (Low-risk zone given TTL-primary eviction, but a request that lands mid-TTL must
   still be checked, not assumed expired.)
2. `[TARGET]` **Zones 2-5, 8 (structured store).** Query PostgreSQL directly for any row keyed to
   `subject_id` still holding non-tombstoned PII: `SELECT count(*) FROM zone_items WHERE tenant_id = ?
   AND subject_id = ? AND crypto_shred_key IS NOT NULL`, expect `0` — a crypto-shredded row's encryption
   key column is null by definition of the crypto-shredding mechanism (item 2 of the workflow above), so
   a non-null key on a supposedly-erased row is the audit failure signal.
3. `[TARGET]` **Zone 6 vector index (Qdrant).** Confirm no vector point tagged with `subject_id` remains
   in the tenant's physically partitioned collection: the Qdrant `scroll` API filtered on the
   `subject_id` payload field against the tenant's collection, expect zero points returned.
4. `[TARGET]` **Zone 6 lexical index (OpenSearch).** Symmetric check: `GET
   /{tenant_collection}/_search` with a `term` query on `subject_id`, expect `hits.total.value == 0`.
5. `[TARGET]` **Zone 8 archives (object store).** Confirm the versioned, object-locked archive objects
   for `subject_id` are either absent or their encryption key has been destroyed (crypto-shredding
   applies to object-store content the same as structured-store rows, per item 2 of the workflow above) —
   check via the object store's key-management audit log for a key-destruction event tied to
   `subject_id`'s archive key ID, not by attempting to read the (now unrecoverable) object content itself.
6. `[TARGET]` **Zone 7 (Provenance/Audit) — the boundary this runbook must NOT cross.** Confirm the
   provenance chain's structural record (hashes, timestamps, an entry noting an erasure occurred) remains
   intact and verifiable (per `AC-013`'s existing requirement), while confirming the erased PII payload
   itself is not recoverable from Zone 7. **This step is blocked on the audit-log-preservation boundary
   already flagged `[NEEDS INPUT]` above** — the exact query to run here cannot be specified until that
   boundary is defined, so this step remains a placeholder pending that decision, not a false claim of
   completeness.
7. `[TARGET]` **Cross-zone race check.** Re-run steps 1-6 a second time after the capacity/MaxAge
   backstop sweep (`AC-002-CAP-DPDP-1`) has had a chance to run at least once post-erasure, to catch the
   race condition already named in "Cross-zone eviction interaction" above — a forced eviction racing an
   erasure must not leave one zone erased and another still referencing the pre-erasure state.

**Audit command reference (illustrative, not yet runnable):** each `[TARGET]` step above names the
concrete check; a future implementer should wire these into a single `dashanan-admin verify-erasure
--tenant <id> --subject <id>` command that runs all 7 checks and reports pass/fail per zone, rather than
requiring an auditor to run each check by hand.

## Open decision (blocks closing AR1-G3)

> Decide whether DPDP erasure needs its own backlog story (implementation) or stays a review-only
> obligation (`AR1-G3`, `ar1_assignments.json`). Until this decision is made, treat every item above
> under "right-to-forget workflow" as a **specified API contract**, not a **verified running mechanism**.
