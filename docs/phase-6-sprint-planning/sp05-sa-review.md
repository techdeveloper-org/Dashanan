# SP.0.5 — Solution Architect Feasibility Review of the Sprint 1 Backlog

| Field | Value |
|---|---|
| Document ID | SP05-SA-REVIEW-20260917-01 |
| Version | 1.0.0 |
| Reviewer | solution-architect (opus) |
| Date | 2026-09-17 |
| Inputs reviewed | `backlog_draft.json` v1.0.0, `sprint_plan.json` v1.0.0, `HLD.md` v1.0.0 (Sections 3.7, 3.8, 12A–12F, ADR-005/007/008/010/012/015, OAQ-1/3/4) |
| **Verdict** | **APPROVED WITH CONCERNS** — 2 must-fix before SP.2 estimation, 3 advisory |

This review is a feasibility check of the Sprint 1 backlog against the HLD this same agent authored. It does not re-derive architecture; it asks whether the sprint slice is buildable as written, and whether anything the HLD stated has been contradicted, lost, or silently absorbed.

---

## 1. Scope Match Against HLD Section 12F — CONFIRMED

Section 12F locked Sprint 1 as **Zones 1, 2, 6, 7 + FR-009 (orchestrator) + FR-010 + FR-012**.

| 12F element | FR | Story | Present |
|---|---|---|---|
| Orchestrator facade + routing | FR-009 | DASH-STORY-001 | Yes |
| Working Memory (Zone 1) | FR-001 | DASH-STORY-002 | Yes |
| Episodic Memory (Zone 2) | FR-002 | DASH-STORY-003 + DASH-STORY-004 | Yes |
| Retrieval-Index (Zone 6) | FR-006 | DASH-STORY-007 | Yes |
| Provenance/Audit (Zone 7) | FR-007 | DASH-STORY-005 | Yes |
| Provenance write-path enforcement | FR-010 | DASH-STORY-006 | Yes |
| Memory Score + rotation | FR-012 | DASH-STORY-008 + DASH-STORY-009 | Yes |

`locked_scope_frs` = {FR-001, FR-002, FR-006, FR-007, FR-009, FR-010, FR-012} is an **exact set match** to 12F, with no additions and no omissions. Both 12F gaps are carried forward explicitly rather than absorbed: the `Importance` explicit-tag-only default of 0.5 appears as AC-012-SCORE-1 on DASH-STORY-008, and the archive-has-no-destination constraint appears as AC-012-ROT-1 on DASH-STORY-009. That is precisely what 12F asked for ("This is fine and should be stated, not discovered").

**Advisory A-1 — FR-011 is partially consumed without a story.** 12F's table lists "Storage adapter ports | Yes (needed regardless)", but FR-011 sits in `future_sprints_backlog` with the qualifier "remaining zone coverage beyond Sprint 1 adapters". The Sprint 1 portion of FR-011 (the `ZoneRepository` port and the ADR-005 adapter for each of Zones 1, 2, 7) is therefore real work embedded inside DASH-STORY-001/002/003/005 with no story or story points of its own. This is defensible decomposition, but the port contract must be designed once in DASH-STORY-001, not three times — flag it as a design constraint on that story rather than leaving it implicit.

---

## 2. Dependency Build Order vs. HLD Architectural Constraints — ONE CONTRADICTION, ONE OVER-CONSTRAINT

The asserted order is Orchestrator → Zone 1 → {Zone 6, Zone 7} → Memory Score → Rotation. Against the HLD:

**Confirmed correct:**
- Every zone story depending on DASH-STORY-001 is right. HLD Section 2/3.1 makes the Orchestrator a Facade through which all reads and writes route; no zone is externally reachable without it.
- DASH-STORY-006 (write-path gate) depending on DASH-STORY-005 (Zone 7) is right — the gate cannot reject a write for a missing provenance record until the provenance store exists (ADR-010, Section 7.2/8.4).
- DASH-STORY-008 depending on DASH-STORY-007 is right — TaskRelevance and UserAffinity are Zone 6 similarity primitives (Section 3.7), so the score cannot be computed without the index.
- DASH-STORY-008 depending on DASH-STORY-005 is right — ProvenanceConfidence *is* the Zone 7 `confidence` field (Section 3.8), not a separately derived value.
- DASH-STORY-009 depending on DASH-STORY-008 is right, and stopping at `Compressed` matches 12F recommendation 2 exactly.

**CONCERN C-1 (must fix) — AC-006 as worded contradicts HLD Section 3.7.**
AC-006 on DASH-STORY-007 reads "Given an item exists in any of Zones 1-5, 7, 8…". HLD Section 3.7 states the opposite for Zone 1: *"Zone 1 is deliberately NOT indexed in Zone 6… Indexing it would add embedding cost on the synchronous write path for content whose median lifetime is under one half-life. This decision is what keeps the write path's p99 at the Section 12D targets."* Section 12D reuses this as one of the two bounds that make Profile C's embedding budget feasible at all (`778M` vectors under PQ rather than an unbounded count).

Implemented literally, AC-006 forces Zone 1 into the index and invalidates both the write-path latency budget and the Profile C embedding sizing. The AC was inherited from SRS AC-006, so the defect is upstream of this backlog — but it must not be carried into an implementable story. **Required fix:** restate AC-006 as "Given an item exists in any *indexed* zone (Zones 2–5, 7, 8; Zone 1 is deliberately unindexed per HLD Section 3.7)…", and raise the SRS AC-006 wording as a traceability correction. In Sprint 1 the only indexed zones that exist are Zones 2 and 7.

**CONCERN C-2 (must fix) — DASH-STORY-004's dependency edge is wrong for its own acceptance criterion.**
`dependency_graph` gives DASH-STORY-004 `depends_on: ["DASH-STORY-003"]` only, and the INVEST note argues it "does not require FR-012's full rotation state machine to exist, only a capacity/age check on write." That reasoning holds against DASH-STORY-009 (the state machine) but not against DASH-STORY-008 (the score). AC-002-CAP-1 says the sweep evicts "the **lowest-ranked-by-MemoryScore** item(s)", and OAQ-4's whole point is that it "converts the Memory Score from a pure threshold function into a ranking function with a threshold fast-path." A ranking eviction cannot be built before the ranking function exists.

Two acceptable resolutions, either fine architecturally:
- **(a)** Add `DASH-STORY-008` to DASH-STORY-004's `depends_on`, moving it from build-order group 4 to group 5 alongside DASH-STORY-009; or
- **(b)** Keep the current edge and downgrade Sprint 1's backstop to age-ordered (MaxAge/FIFO) eviction, with MemoryScore-ranked eviction deferred — which still satisfies 12F's liveness requirement, since 12F only demands that the zone not grow unbounded.

Option (a) preserves the HLD's stated intent and is preferred. Option (b) is acceptable if group 5 becomes a schedule risk, but must then be written into the AC rather than discovered at implementation.

**CONCERN C-3 (advisory) — Zone 6 → Zone 1 is over-constrained, not incorrect.**
DASH-STORY-007 `depends_on: ["DASH-STORY-002"]` with the rationale "Zone 6/Zone 7 both depend on Zone 1 existing first." Per Section 3.7, Zone 6 indexes Zones 2/3/4/5/8 and explicitly not Zone 1, so Zone 6's real content dependency in Sprint 1 is **Zone 2 (DASH-STORY-003)**, not Zone 1. Likewise Zone 7 (Section 3.8) records provenance for facts in every zone and has no structural dependency on Zone 1 specifically.

Nothing breaks — Zone 1 lands in group 2 regardless, so the order is merely tighter than necessary — but the *stated reason* is architecturally wrong, and a wrong reason recorded in a dependency graph tends to be re-used. Correct the rationale, and consider re-pointing DASH-STORY-007's edge at DASH-STORY-003 so the graph reflects what Zone 6 actually indexes.

**No other contradiction found.** No Sprint 1 story crosses a bounded-context boundary the HLD drew, no story requires two components to share storage, and no story depends on an out-of-scope zone (3, 4, 5, 8).

---

## 3. OAQ-4 / DASH-STORY-004 Scoping — CONFIRMED CORRECT

HLD Section 12F states, verbatim: *"OAQ-4 is not optional for Sprint 1; it is the only thing preventing unbounded growth in a sprint with no archival destination. That elevates OAQ-4 from a nice-to-have to a Sprint 1 dependency."*

The backlog honours this in the three ways that matter:

1. **It is a standalone story, not a sub-task.** Folding it into DASH-STORY-003 or DASH-STORY-009 would have made it the first thing cut under sprint pressure, and its omission would not have failed any other story's AC — a silent liveness failure. Splitting it out is the right call and `RISK-01` records the reason.
2. **Its AC states the dependency in its own text,** with source `[DERIVED FROM HLD Section 12F / OAQ-4 — explicit Sprint 1 dependency, not silently deferred]`, and its Review sub-task explicitly re-checks that framing. DASH-STORY-009's AC-012-ROT-1 names DASH-STORY-004 as the sole bound on `Compressed` dwell, so the two stories are mutually anchored.
3. **Zone-2-only scoping is correct for Sprint 1,** even though OAQ-4 is written as a *per-zone* cap. Of the four zones in scope: Zone 1 is TTL-bounded and self-limiting (FR-001/AC-001); Zone 6 is a derived, rebuildable projection whose size follows its source zones (Section 3.7); Zone 7 is append-only by design and must not be capacity-evicted at all (Section 3.8, ADR-010, plus the CERT-In 180-day retention obligation in Section 12D). Zone 2 is the only zone in Sprint 1 that can grow without bound, because it is the only one receiving items that dwell in `Compressed` with no archival destination. Generalising the cap to other zones in Sprint 1 would have been scope inflation.

One reinforcement worth recording: OAQ-3 establishes a **structural score floor of `w3 + w6 = 1/3`** from the two non-decaying terms. Combined with Sprint 1's `Importance` default of 0.5, a user-stated fact carries `w3·0.5 + w6·1.0 = 0.0833 + 0.1667 = 0.25` from the non-decaying terms alone, which equals the adopted `ArchiveThreshold` (OAQ-1). This is exactly the class of item that can never fall far enough to rotate out — which is *why* the backstop is load-bearing rather than defensive. DASH-STORY-008's Review sub-task already flags OAQ-3; the connection to DASH-STORY-004 should be made explicit in one of the two stories.

---

## 4. 52 Story Points — DIRECTIONALLY PLAUSIBLE, WITH ONE STRUCTURAL UNDER-SCOPE

52 SP across 9 stories is plausible as a **draft order of magnitude** for this slice, and `RISK-04` correctly refuses to treat it as a commitment absent velocity history. Reviewing structure rather than precision:

**CONCERN C-4 (must fix) — the ADR-012 deadline-invalidation wiring is unowned by any story.**

HLD Section 12C, in the passage that makes the whole rotation design tractable, states the liability in terms this review will not soften: *"any mutation of a non-Recency term (Frequency on access, ProvenanceConfidence on conflict detection, Importance on operator override) invalidates the stored deadline and requires an `O(log N)` re-insert. This must be wired into **every** term-mutation path. Missing one produces items rotating on stale deadlines — a silent correctness bug, not a crash. It is called out in ADR-012's consequences and **belongs in the Sprint 1 definition of done**."*

No story owns this. DASH-STORY-008's sub-tasks cover score computation, the weight/threshold configuration surface and the `Importance` default. DASH-STORY-009's Dev sub-task covers the deadline-scheduled sweep and the gated transition. Neither covers the **invalidation hooks on the mutation paths**, which are cross-cutting and land in other stories' code: Frequency-on-access in the DASH-STORY-001 read path, ProvenanceConfidence-on-conflict in DASH-STORY-005/006, Importance-on-override in the DASH-STORY-008 configuration surface. Cross-cutting work with no owning story is exactly the work that does not get done, and here the failure mode is silent.

This is the answer to the question of whether 8 SP under-scopes the Memory Score engine. The six-term computation itself is genuinely small — Section 12C measures it at O(1), six multiply-adds — so 8 SP is generous for what DASH-STORY-008's sub-tasks describe. What is under-scoped is not the formula; it is the invalidation contract that surrounds it. **Required fix:** either add a tenth story ("deadline invalidation on non-Recency term mutation", ~3 SP, dependent on DASH-STORY-008, touching every mutation path) or add an explicit invalidation sub-task and +2–3 SP to DASH-STORY-008 with a Definition-of-Done entry naming all three mutation paths. A tenth story is preferred, for the same reason DASH-STORY-004 was split out.

**Advisory A-2 — embedding generation is unowned.** DASH-STORY-007 delivers a vector + lexical index and its similarity primitives, but nothing in Sprint 1 owns producing the vectors: the embedding call, the `EmbedThreshold` gate (Section 12D), the host-supplied `query_embedding` path (ADR-015) or the L1 query-embedding cache (Section 12E). AC-006 cannot pass without at least a minimal embedding path. At 8 SP, DASH-STORY-007 is already the joint-largest story and its 5 SP Dev sub-task lists partitioning, RRF fusion and cache keys with no embedding line item. Either name the embedding path in DASH-STORY-007 and re-estimate, or split it out. Compare DASH-STORY-001 at the same 8 SP for a routing facade and API surface: relative to each other, 001 looks generous and 007 looks light.

**Advisory A-3 — no Sprint 1 story carries a performance AC.** Section 12E's 97 ms read-path budget against the NFR-004 p99 of 150 ms, and the Section 12D write-path assumptions, appear in no acceptance criterion. Deferring performance verification while the sprint goal is to prove the loop end-to-end is a legitimate choice, but it should be a *stated* deferral, since three Sprint 1 design decisions (Zone 1 unindexed, dual-index parallel fan-out, provenance off the read critical path) exist solely to hit those budgets and will not be regression-protected without one.

**Stories reviewed and found reasonably scoped as written:** DASH-STORY-002 (5), DASH-STORY-003 (5), DASH-STORY-004 (3), DASH-STORY-005 (5), DASH-STORY-009 (5). DASH-STORY-006 at 5 SP is a watch-item rather than a flag — a WAL/outbox durability barrier with a structurally unbypassable 422 gate is more infrastructure than its point count suggests, though the gate itself is narrow.

Net effect of the fixes above: roughly **+3 to +6 SP** against the 52 SP draft, concentrated in C-4 and A-2. `RISK-03`'s observation that the three 8-SP stories sit in build-order groups 1, 3 and 4 rather than bunching remains true after these adjustments.

---

## 5. Summary of Required Actions

| ID | Severity | Action | Owner |
|---|---|---|---|
| C-1 | Must fix | Restate AC-006 to exclude Zone 1 from the index (HLD Section 3.7); raise SRS AC-006 as an upstream traceability correction | scrum-master-agent + business-analyst-agent |
| C-2 | Must fix | Add `DASH-STORY-008` to DASH-STORY-004's `depends_on` (preferred), or downgrade the Sprint 1 backstop to age-ordered eviction in the AC | scrum-master-agent |
| C-4 | Must fix | Give the ADR-012 deadline-invalidation wiring an owning story (~3 SP) or an explicit DASH-STORY-008 sub-task with +2–3 SP and a Definition-of-Done entry naming all three mutation paths | scrum-master-agent |
| C-3 | Advisory | Correct the Zone 6 dependency rationale; consider re-pointing DASH-STORY-007 at DASH-STORY-003 | scrum-master-agent |
| A-1 | Advisory | Record the `ZoneRepository` port + ADR-005 adapter contract as a DASH-STORY-001 design constraint (FR-011's Sprint 1 share) | solution-architect |
| A-2 | Advisory | Name the embedding path (`EmbedThreshold`, ADR-015 `query_embedding`, L1 cache) in DASH-STORY-007 and re-estimate, or split it out | scrum-master-agent |
| A-3 | Advisory | State the performance-verification deferral explicitly, or add one p99 AC covering the Section 12E read path | product-manager-agent |

With C-1, C-2 and C-4 resolved, the Sprint 1 slice is architecturally feasible and matches Section 12F's coherence claim: it proves orchestration, scoring, hybrid retrieval and provenance end to end, which is exactly what the sprint goal commits to.

---

### COORDINATION OUTPUT

**Status:** `COMPLETE`
**Agent:** solution-architect

**Decisions Made:**
1. Sprint 1 scope APPROVED WITH CONCERNS — the 9-story FR set is an exact match to HLD Section 12F, and both 12F gaps are carried as explicit ACs rather than absorbed.
2. Build order does not contradict the HLD, with one genuine defect (DASH-STORY-004 needs the Memory Score it ranks by) and one incorrect-but-harmless rationale (Zone 6 does not index Zone 1).
3. OAQ-4 / DASH-STORY-004 is correctly scoped as a hard Sprint 1 dependency, standalone, Zone-2-only, per 12F.
4. 52 SP is directionally plausible; the structural under-scope is not the Memory Score formula (O(1) per 12C) but the unowned ADR-012 deadline-invalidation wiring that 12C places in the Sprint 1 definition of done.

**Documents Produced:**
- `docs/phase-6-sprint-planning/sp05-sa-review.md` — architectural feasibility review for the SP.0.5 consensus gate

**Issues / Obstacles:**

| Severity | Description | Action Taken |
|---|---|---|
| MAJOR | AC-006 would index Zone 1, contradicting HLD Section 3.7 and the Section 12D embedding budget | C-1 raised; SRS correction requested |
| MAJOR | DASH-STORY-004 ranks by MemoryScore but does not depend on DASH-STORY-008 | C-2 raised with two acceptable resolutions |
| MAJOR | ADR-012 deadline invalidation on non-Recency term mutation is unowned by any story | C-4 raised; new story or +2–3 SP recommended |
| MINOR | Zone 6 dependency rationale architecturally wrong; embedding path unowned; no performance AC | C-3, A-1, A-2, A-3 raised as advisory |

**Summary:** The Sprint 1 backlog matches the HLD's own Section 12F coherence check exactly and handles both flagged gaps honestly. Three defects must be fixed before SP.2 estimation — one AC that contradicts a load-bearing architecture decision, one missing dependency edge, and one cross-cutting requirement the HLD explicitly assigned to the Sprint 1 definition of done that no story currently owns. None require re-scoping the sprint.

**Next Review Point:** after scrum-master-agent applies C-1/C-2/C-4, before the SP.0.5 consensus gate closes.
