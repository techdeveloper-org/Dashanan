# Dashanan — WSJF Re-Validation Against the Authoritative 12-FR PRD (Phase 2)

**Author:** product-manager-agent (Domain 45) · **Phase:** 2 (Joint Blueprint Validation) · **Date:** 2026-09-17
**Skill applied:** `user-story-mapping-core` (primary, per this task), with `product-management-core` §4.1/§M1 WSJF mechanics carried forward from Phase 0.
**Inputs:** `docs/phase-0-output/product-strategy-and-prioritization.md` (Phase 0 WSJF table, 11 FRs), `docs/phase-0-output/PRD.md` (business-analyst-agent's authoritative 12-FR list, FR-001..FR-012), `docs/phase-1-architecture/HLD.md` (APPROVED-pending-consensus, v1.0.0).

---

## 1. Finding: FR-010 Was Genuinely Missed, Not Intentionally Excluded

The Phase 0 table (written before `PRD.md` existed) scored 11 FRs using inferred IDs (`FR-ZONE-01..08`, `FR-CC-01..03`) built from the locked vision doc alone. It explicitly flagged this gap in its own header note: *"Once PRD.md exists, re-score against the BA's actual FR-NNN numbering... reconcile any FR the BA's PRD adds that isn't captured here."*

Cross-referencing against `PRD.md` §7 confirms: **FR-010 (Cross-cutting provenance/audit obligation) has no corresponding entry anywhere in the Phase 0 table**, including under the inferred cross-cutting IDs. FR-007 (the Provenance/Audit *zone itself*) was scored — but FR-010, the PRD's distinct requirement that *every write to any of the 8 zones must register a Provenance/Audit entry at write time*, was not. This is a genuine omission, not a stated exclusion (no rationale for excluding it appears anywhere in the Phase 0 document). Corrected below.

## 2. ID Reconciliation (Phase 0 inferred IDs -> PRD authoritative IDs)

| PRD ID | Phase 0 inferred ID | Title |
|---|---|---|
| FR-001 | FR-ZONE-01 | Working Memory zone |
| FR-002 | FR-ZONE-02 | Episodic Memory zone |
| FR-003 | FR-ZONE-03 | Semantic Memory zone |
| FR-004 | FR-ZONE-04 | Procedural Memory zone |
| FR-005 | FR-ZONE-05 | Entity Memory zone |
| FR-006 | FR-ZONE-06 | Retrieval-Index Memory zone |
| FR-007 | FR-ZONE-07 | Provenance/Audit Memory zone |
| FR-008 | FR-ZONE-08 | Consolidation Memory zone |
| FR-009 | FR-CC-01 | Memory Orchestrator cross-zone routing |
| **FR-010** | **(missing)** | **Cross-cutting provenance/audit obligation — NEWLY SCORED** |
| FR-011 | FR-CC-03 | Pluggable storage-adapter interface |
| FR-012 | FR-CC-02 | Memory Score & rotation-state-machine algorithm |

All 11 previously-scored FRs keep their original UBV/TC/RRO/Job Size/WSJF/Kano scores unchanged — this is a completion pass, not a re-litigation of Phase 0's estimates.

## 3. FR-010 — WSJF Scoring (same methodology as Phase 0: CoD = UBV+TC+RRO, WSJF = CoD/Job Size)

| UBV | TC | RRO | CoD | Job Size | **WSJF** | Kano |
|---|---|---|---|---|---|---|
| 9 | 8 | 8 | 25 | 4 | **6.25** | Must-Be |

**Rationale:** UBV=9 — this is the PRD's stated enforcement mechanism for the product's entire anti-hallucination thesis (§5 Goal 5); without it, Zone 7 is an optional audit log instead of a guarantee. TC=8 — the PRD's own Sprint 1 scope note (§7) requires FR-010 to apply to the 4 Sprint-1 zones from day one, not a later hardening pass. RRO=8 — directly retires PRD Risk R-001 (memory poisoning / unattributed writes) at the write-path level, second only to FR-007 itself. Job Size=4 — smaller than a full zone build: it is an enforcement/validation layer across existing write paths (comparable in scope to FR-009's cross-cutting routing), not new storage. This places it immediately behind FR-001 in priority — consistent with it being, functionally, a write-path *gate* rather than a standalone feature.

## 4. Corrected 12-FR WSJF Table (re-ranked)

| Rank | FR ID | Requirement | UBV | TC | RRO | CoD | Job Size | **WSJF** | Kano |
|---|---|---|---|---|---|---|---|---|---|
| 1 | FR-001 | Working Memory zone | 9 | 9 | 7 | 25 | 3 | **8.33** | Must-Be |
| 2 | **FR-010** | **Cross-cutting provenance/audit obligation** | **9** | **8** | **8** | **25** | **4** | **6.25** | **Must-Be** |
| 3 | FR-002 | Episodic Memory zone | 9 | 8 | 7 | 24 | 4 | 6.00 | Performance |
| 4 | FR-009 | Memory Orchestrator cross-zone routing | 9 | 8 | 6 | 23 | 4 | 5.75 | Must-Be |
| 5 | FR-007 | Provenance/Audit Memory zone | 9 | 6 | 9 | 24 | 5 | 4.80 | Must-Be |
| 6 | FR-006 | Retrieval-Index Memory zone | 8 | 7 | 8 | 23 | 5 | 4.60 | Performance |
| 6 | FR-012 | Memory Score & Rotation Policy Engine | 8 | 7 | 8 | 23 | 5 | 4.60 | Performance |
| 8 | FR-011 | Pluggable storage-adapter interface | 7 | 5 | 6 | 18 | 5 | 3.60 | Must-Be |
| 9 | FR-005 | Entity Memory zone | 6 | 4 | 5 | 15 | 5 | 3.00 | Performance |
| 10 | FR-003 | Semantic Memory zone | 6 | 4 | 5 | 15 | 6 | 2.50 | Performance |
| 11 | FR-008 | Consolidation Memory zone | 6 | 3 | 6 | 15 | 7 | 2.14 | Performance |
| 12 | FR-004 | Procedural Memory zone | 5 | 3 | 4 | 12 | 6 | 2.00 | Delighter |

(Ranks 6/6 tied at WSJF 4.60, as in Phase 0.)

## 5. Sprint 1 Scope Re-Validation

**Locked Sprint 1 scope, now confirmed against `PRD.md` §7 and `HLD.md` §12F verbatim:** Orchestrator (FR-009) + Zones 1/2/6/7 (FR-001, FR-002, FR-006, FR-007) + the cross-cutting provenance obligation as it applies to those four zones (FR-010) + the Memory Score/rotation algorithm (FR-012). Seven FRs.

**Result: the corrected WSJF ranking is an EXACT match to the locked Sprint 1 scope.** Ranks 1–7 are precisely {FR-001, FR-010, FR-002, FR-009, FR-007, FR-006, FR-012} — no FR outside this set scores in the top 7; FR-011 (rank 8, WSJF 3.60) is the next-highest and sits meaningfully below the cutoff. This is a stronger corroboration than the Phase 0 table produced (which needed a "flag" to justify including FR-CC-01/02): with FR-010 correctly scored, **no flag is needed** — the locked scope and the WSJF ordering now agree without adjustment.

FR-010's rank-2 position is itself informative: it confirms the PRD's own framing that the provenance obligation is not a Zone-7-adjacent afterthought but the second-most-valuable single piece of work in the entire 12-FR backlog, behind only Working Memory (the zone nothing else can function without).

## 6. HLD Build-Order Feasibility Check (does the architecture make the highest-WSJF FRs easiest to build first?)

**Verdict: CONFIRMED — no contradiction. The HLD's dependency ordering is consistent with, and in one respect actively reinforces, the WSJF priority ordering.** Two qualifications are flagged below, both raising Sprint 1 *complexity* rather than reversing *sequencing*.

**Confirming evidence from HLD.md:**
- **§12F "Sprint 1 Architectural Coherence Check"** verifies the exact locked slice (Zones 1/2/6/7 + FR-009 + FR-010 + FR-012) stands alone and concludes: *"the Sprint 1 slice is architecturally coherent: it proves orchestration, scoring, hybrid retrieval and provenance end to end."* This is the HLD independently re-deriving the same scope this WSJF pass converges on, from an architecture-fitness angle rather than a value angle.
- **FR-010 (rank 2) is architecturally cheap to build first, not deferred as risk.** ADR-010 (Transactional Outbox / WAL) and §3.8 make provenance enforcement a write-path gate rather than a new subsystem: writes without a resolvable `source_type` are rejected `422` at the API boundary (line 1122) *before* any zone sees the fact. This means FR-010 is enforced structurally by the Orchestrator's existing write path (FR-009, rank 4) rather than bolted on after zones exist — architecturally, FR-010's dependency is "Orchestrator write path must exist," which Sprint 1 builds anyway. The HLD does not make this highest-WSJF item hardest to build; it makes it a natural consequence of building FR-009 correctly.
- **FR-001 (rank 1, Working Memory) is explicitly the cheapest, most load-bearing build** per ADR-005 (in-process LRU / Redis, O(1) sub-millisecond), consistent with it also being Phase 0/2's #1 WSJF item on both effort and value grounds.

**Two flagged gaps (complexity additions, not sequencing contradictions) — HLD §12F's own language:**
1. **`Importance` term has no data source within Sprint 1** (its inference path depends on Zone 4/Procedural, which is out of scope). HLD's own recommendation — ship `Importance` as explicit-tag-only, default 0.5 — is accepted as sufficient; it does not require re-sequencing FR-012 above FR-004, since FR-004 remains correctly ranked last (12th) regardless.
2. **`archive` has no destination in Sprint 1** (Zone 8/Consolidation, rank 11, is correctly out of scope). HLD's mitigation is that `Active -> Compressed` completes but items dwell in `Compressed` indefinitely, which makes the Zone 2 capacity cap (OAQ-4) a **hard Sprint 1 dependency that was not visible in either Phase 0's or this pass's Job Size estimate for FR-002 or FR-012.** This does not change WSJF ranking (FR-002 and FR-012 remain correctly prioritized), but it is a real scope-completeness note for Phase 6 (Sprint Planning): budget OAQ-4's capacity-cap implementation as part of the FR-002/FR-012 Sprint 1 stories, not as a separate later task.

## 7. Summary for Consensus Gate

- FR-010 omission: **confirmed genuine miss**, now scored (WSJF 6.25, rank 2, Kano Must-Be) and reconciled into the full 12-FR table above.
- Sprint 1 locked scope (Zones 1/2/6/7 + Orchestrator + FR-010 + FR-012): **WSJF-confirmed without qualification** — exact match on the top 7 ranked FRs.
- HLD build-order vs. WSJF priority: **no contradiction** — FR-010 and FR-001 are both cheap-to-build-first under the chosen architecture (ADR-005, ADR-010). Two complexity flags (Importance term default, OAQ-4 as a hard Sprint 1 dependency) are carried forward to Phase 6 Sprint Planning as scope-completeness notes, not as objections to this ordering.

---

## Change Log

| Date | Version | Change |
|---|---|---|
| 2026-09-17 | 1.0.0 | Phase 2 re-validation: added FR-010, reconciled Phase 0 inferred IDs to PRD.md authoritative FR-001..FR-012, re-ranked WSJF, validated HLD build-order against WSJF priority. |
