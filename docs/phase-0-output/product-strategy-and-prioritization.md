# Dashanan — Product Strategy & Prioritization

**Author:** product-manager-agent (Domain 45) · **Phase:** 0 (BA/PM parallel track) · **Date:** 2026-09-17
**Note on skill routing:** the task's mandated skill filenames (`product-strategy-core`, `wsjf-prioritization-core`, `kano-model-core`) do not exist in the library. This output was produced from `agents/product-manager-agent/agent.md`'s actual mandatory skill set (`product-management-core`, `product-analytics-core`, `user-story-mapping-core`), which covers WSJF §4.1/§M1 and Kano classification directly. Flagging per the "never invent/silently skip" rule rather than fabricating the named files.
**Note on source PRD:** `Dashanan\docs\phase-0-output\PRD.md` (business-analyst-agent's parallel output) did not exist at time of writing. Scoring below is from the locked 8-zone taxonomy + 3 inferred cross-cutting FRs in `01-vision-and-prd.md`, including the Memory Score/rotation-algorithm FR called out explicitly in this task's instructions.

---

## 1. Product Vision & Strategy

**Vision:** Dashanan gives any AI system a memory architecture instead of a context window. Rather than one flat buffer that forgets the moment it overflows, Dashanan routes information across eight independently-governed zones — each with its own retention half-life, compression policy, and eviction rule — so a host AI's *effective* working context is bounded by relevance and provenance quality, not by token count.

**Strategic thesis:** the defensible differentiator is not "more storage," it is **trustworthy retrieval** — every recalled fact carries a Provenance/Audit record (Zone 7), so the engine can answer not just "what does the system remember" but "how sure should it be, and where did this come from." In a market where LLM memory add-ons compete mostly on recall volume, anti-hallucination-as-a-feature is the wedge. This is why Zone 7 is scored as a Kano Must-Be below, not a differentiator to defer.

**Sequencing logic:** ship the smallest slice that proves the *orchestration* claim — not just "we can store data in 8 buckets," but "we can score, rotate, retrieve, and cite it correctly." That requires: a fast-churn zone (Working), a chronological zone (Episodic), the retrieval layer that makes any zone queryable (Retrieval-Index), and the trust layer that makes retrieval usable without hallucination risk (Provenance/Audit) — plus the two cross-cutting engine pieces (unified API, Memory Score/rotation state machine) that make those four zones function as a system rather than four disconnected stores. Semantic/Procedural/Entity/Consolidation zones are real product value but are compounding refinements on top of a working core, not part of proving the core claim.

**Non-goals for Sprint 1 (confirmed against WSJF ranking below):** Semantic/Entity ownership-boundary enforcement, Procedural pattern learning, and Consolidation's sleep-cycle promotion are explicitly deferred — none scored in the top tier, and all three depend on the Retrieval-Index + Rotation Engine existing first, so building them earlier would be sequencing risk, not just lower value.

---

## 2. WSJF-Scored + Kano-Classified Priority Table

**Formula (per agent.md's Mathematical Delegation contract — delegated to `ba-pm-mathematics-expert`, shown here for auditability, not as a substitute for that delegation):**
`CoD = User-Business Value (UBV) + Time Criticality (TC) + Risk Reduction/Opportunity Enablement (RRO)`, each 1–10.
`WSJF = CoD / Job Size` (Job Size 1–10, story-point-scale relative sizing).

11 FRs scored: 8 zone FRs (`FR-ZONE-01..08`) + 3 cross-cutting FRs (`FR-CC-01..03`, inferred per this task's instruction to include the Memory Score/rotation-algorithm FR pending the BA's PRD).

| # | FR ID | Requirement | UBV | TC | RRO | CoD | Job Size | **WSJF** | Kano | Rationale (WSJF) |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | FR-ZONE-01 | Working Memory zone (turn/session scratch, shortest TTL) | 9 | 9 | 7 | 25 | 3 | **8.33** | Must-Be | Smallest job, and nothing else in the system functions without a live scratch layer — highest CoD-to-effort ratio in the set. |
| 2 | FR-ZONE-02 | Episodic Memory zone (chronological, time-anchored, absorbs former Temporal) | 9 | 8 | 7 | 24 | 4 | **6.00** | Performance | Second-cheapest zone to build (no embedding infra needed yet) and directly proves the "engine remembers across turns" claim. |
| 3 | FR-CC-01 | Memory Orchestrator: unified context-assembly API across zones | 9 | 8 | 6 | 23 | 4 | **5.75** | Must-Be | Without this, Working+Episodic are two disconnected stores, not an "engine" — TC is high because every later zone integration depends on this contract existing first. |
| 4 | FR-ZONE-07 | Provenance/Audit Memory zone (source attribution, confidence, edit history — anti-hallucination backbone) | 9 | 6 | 9 | 24 | 5 | **4.80** | Must-Be | RRO=9 (highest in the set): this is explicitly the anti-hallucination requirement flagged as first-class in the locked spec — a memory engine that can't say "how confident, from where" is a liability, not a feature gap. |
| 5 | FR-ZONE-06 | Retrieval-Index Memory zone (vector + lexical index over all other zones) | 8 | 7 | 8 | 23 | 5 | **4.60** | Performance | High RRO because it's the enabler for every future zone's recall quality; Job Size reflects real indexing infra (embedding pipeline, hybrid search) — appropriately larger than Working/Episodic. |
| 6 | FR-CC-02 | Memory Score & Rotation Policy Engine (6-term weighted score; Active→Compressed→Archived state machine) | 8 | 7 | 8 | 23 | 5 | **4.60** | Performance | This is the "load-bearing algorithm" per the locked spec — without it the 4 Sprint-1 zones are storage with no lifecycle. Tied with Retrieval-Index; both are infra multipliers, not standalone user value. |
| 7 | FR-CC-03 | Pluggable storage adapter layer (portability — embeddable + sidecar, any host AI) | 7 | 5 | 6 | 18 | 5 | **3.60** | Must-Be | Must-Be long-term ("reusable in any AI" is the product's core promise) but not urgent for a single-host Sprint-1 proof — TC correctly lower than the zones above. |
| 8 | FR-ZONE-05 | Entity Memory zone (per-entity profile records) | 6 | 4 | 5 | 15 | 5 | **3.00** | Performance | Depends on the Zone 3/5 ownership ADR being enforced at schema level (a Phase 1 deliverable) — premature before that lands. |
| 9 | FR-ZONE-03 | Semantic Memory zone (distilled cross-entity facts/relations) | 6 | 4 | 5 | 15 | 6 | **2.50** | Performance | Same ADR dependency as Zone 5; larger Job Size (graph-edge modeling, dedup logic) pushes it below Entity despite similar CoD. |
| 10 | FR-ZONE-08 | Consolidation Memory zone (long-term cross-session store, sleep-cycle promotion) | 6 | 3 | 6 | 15 | 7 | **2.14** | Performance | Needs Rotation Engine (`FR-CC-02`) fully proven first — it's the terminal state of a lifecycle that doesn't exist yet at Sprint 1. |
| 11 | FR-ZONE-04 | Procedural Memory zone (learned task procedures / tool-use patterns) | 5 | 3 | 4 | 12 | 6 | **2.00** | Delighter | Lowest UBV/RRO and no urgent dependency chain — genuinely differentiating for power users later, not core-loop value now. |

**Auditability note:** the scores above are PM-authored estimates for sequencing purposes, consistent with `product-management-core` §4.1's rank-ordering guidance. Per this agent's Operating Rule #1 and Mathematical Delegation contract, these are provisional inputs — final CoD normalization, weighting sensitivity, and any Kano asymmetry-coefficient computation (Berger et al. 1993 β_CS/β_DS, requiring ≥10 paired dysfunctional/functional response pairs per feature) are deferred to `ba-pm-mathematics-expert` before these numbers are treated as committed roadmap inputs rather than a sequencing proposal.

---

## 3. Sprint 1 Scope Validation

**Locked Sprint 1 scope (from `01-vision-and-prd.md` / this task's contract): Zones 1, 2, 6, 7 — Working, Episodic, Retrieval-Index, Provenance/Audit.**

**Verdict: CONFIRMED — WSJF ordering corroborates the locked scope, with one addition flagged, not a disagreement.**

The top 6 WSJF-ranked FRs are exactly: `FR-ZONE-01` (1st), `FR-ZONE-02` (2nd), `FR-CC-01` (3rd), `FR-ZONE-07` (4th), `FR-ZONE-06` (5th, tied), `FR-CC-02` (5th, tied). That is precisely the 4 locked zones plus the 2 cross-cutting engine components (`FR-CC-01` Orchestrator API, `FR-CC-02` Memory Score/Rotation Engine) that make those 4 zones operate as a system rather than 4 disconnected stores. No zone outside {1, 2, 6, 7} scores inside the top 6 — Entity (8th) is the next-highest and sits meaningfully below the cutoff.

**Flag (not a disagreement with the locked zone scope — an addition to Sprint 1's *task list* within that scope):** `FR-CC-01` and `FR-CC-02` are not zones and were not named in the original Zones-1/2/6/7 framing, but WSJF ranks both above 3 of the 4 locked zones' peers and — more importantly — Zones 1/2/6/7 have no way to interoperate or rotate without them. Recommend Sprint 1's actual backlog include `FR-CC-01` and `FR-CC-02` as first-class stories alongside the 4 zones, not as implicit "architecture that happens along the way." This is a scope-completeness flag for Phase 6 (Sprint Planning), not a challenge to the already-agreed zone selection.

Kano cross-check: 3 of the 4 locked zones (Working, Provenance/Audit) plus both cross-cutting engine pieces classify as **Must-Be** or sit on the Must-Be/Performance boundary — consistent with an MVP scope, since Kano theory says Must-Be attributes should ship first (their absence causes dissatisfaction disproportionate to their presence causing delight). Provenance/Audit's Must-Be classification directly corroborates this task's own hypothesis in the routing brief.

---

## 4. Open Items for Phase 1 (Solution Architect) / Phase 6 (Sprint Planning)

1. Confirm `FR-CC-01`/`FR-CC-02` are added as explicit Sprint 1 stories, not folded silently into the 4 zone stories.
2. Once `Dashanan\docs\phase-0-output\PRD.md` exists, re-score against the BA's actual FR-NNN numbering (this table used inferred zone/cross-cutting IDs in its absence) and reconcile any FR the BA's PRD adds that isn't captured here.
3. `ba-pm-mathematics-expert` to formalize CoD normalization and Kano asymmetry coefficients before these scores are cited outside this Phase 0 sequencing exercise.
