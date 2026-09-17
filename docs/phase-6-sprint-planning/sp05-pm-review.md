# Dashanan — SP.0.5 PM Review: WSJF vs. Build-Order Reconciliation

**Author:** product-manager-agent · **Phase:** 6 (Sprint Planning, SP.0.5 review) · **Date:** 2026-09-17
**Inputs reviewed:** `backlog_draft.json` v1.0.0, `sprint_plan.json` v1.0.0, `docs/phase-2-validation/pm-wsjf-revalidation.md` v1.0.0 (my own WSJF ranking)

---

## 1. WSJF Priority Order (my ranking, Section 4/5 of pm-wsjf-revalidation.md)

Rank 1-7 locked scope: **FR-001 (8.33) > FR-010 (6.25) > FR-002 (6.00) > FR-009 (5.75) > FR-007 (4.80) > FR-006 (4.60) = FR-012 (4.60)**

## 2. Build-Order Groups (`sprint_plan.json.dependency_graph.build_order_groups`)

| Group | Stories | FRs | WSJF rank of each FR |
|---|---|---|---|
| 1 | DASH-STORY-001 | FR-009 | 4 |
| 2 | DASH-STORY-002 | FR-001 | 1 |
| 3 | 003, 005, 007 | FR-002, FR-007, FR-006 | 3, 5, 6 |
| 4 | 004, 006, 008 | FR-002(cap), FR-010, FR-012 | 2, 6 |
| 5 | 009 | FR-012(rotation) | 6 |

## 3. Reconciliation Verdict: No Contradiction — Two Explained Deviations

**Deviation A — FR-009 (rank 4) builds before FR-001 (rank 1).** This is architectural necessity, not a value miscall: every zone story's `depends_on` in `backlog_draft.json` roots at DASH-STORY-001 (Orchestrator), per the dependency graph's own `promotion_reason` — "DASH-STORY-001 (Orchestrator) is the sole root dependency for all zone stories since every write/read routes through its facade." A facade cannot be value-ranked against the things that register with it; it is infrastructure the #1-ranked item (FR-001) itself requires to be reachable. This exactly matches my own Section 6 finding in pm-wsjf-revalidation.md: "the HLD does not make this highest-WSJF item hardest to build; it makes it a natural consequence of building FR-009 correctly."

**Deviation B — FR-010 (rank 2) builds in Group 4, not Group 1.** DASH-STORY-006 (FR-010, the write-path provenance gate) depends on DASH-STORY-001 (Orchestrator) **and** DASH-STORY-005 (Zone 7 / FR-007, rank 5). This is a reasonable architectural necessity absent from my Phase 2 analysis (which only traced FR-010's dependency to the Orchestrator write path): a gate that rejects writes lacking a resolvable provenance entry cannot exist before the Provenance/Audit zone it gates against exists to receive that entry. The delay from Group 1 to Group 4 does not indicate the team is under-valuing the #2 WSJF item — it reflects that FR-010 is a *cross-cutting enforcement layer over* FR-007, not an independent build.

**No other WSJF-vs-build-order inversion exists.** FR-002 (rank 3), FR-007 (rank 5), FR-006 (rank 6) run in parallel in Group 3 — order-within-group is immaterial since none blocks another. FR-012 (rank 6, tied) correctly lands last (Group 4 for scoring, Group 5 for rotation) because it structurally consumes the outputs of FR-007 and FR-006 (Memory Score needs ProvenanceConfidence and TaskRelevance) — it cannot be built earlier regardless of its WSJF rank.

**Conclusion:** The dependency-graph build order does not contradict WSJF priority. Every case where build sequence departs from strict rank order is explained by a genuine, documented architectural dependency (facade-first, gate-after-target-zone, scorer-after-its-inputs) — not by the team deprioritizing high-value work. This is the same "no contradiction" verdict I reached independently in pm-wsjf-revalidation.md Section 6, now re-confirmed at the story-decomposition level.

## 4. Sprint 1 Goal Coherence — Confirmed

Sprint goal text covers: orchestrated write/read (FR-009), Working + Episodic Memory (FR-001, FR-002), hybrid retrieval (FR-006), provenance-gated writes (FR-007 + FR-010), Memory-Score-driven Active→Compressed rotation (FR-012). This is a 1:1 match to `locked_scope_frs` (7 FRs) and to my WSJF top-7 with **zero adjustment** — `sprint_plan.json` correctly states "WSJF ranks 1-7 exact match" as the scope source, corroborated independently by HLD §12F's architecture-fitness check. Both risk flags (RISK-01 OAQ-4 capacity backstop, RISK-02 Importance default-to-0.5) trace to gaps I flagged in pm-wsjf-revalidation.md Section 6 and are carried forward as explicit stories/notes rather than silently absorbed. Goal is coherent and fully traceable to validated scope.

## Change Log

| Date | Version | Change |
|---|---|---|
| 2026-09-17 | 1.0.0 | SP.0.5 review: WSJF-vs-build-order reconciliation and Sprint 1 goal coherence confirmation. |
