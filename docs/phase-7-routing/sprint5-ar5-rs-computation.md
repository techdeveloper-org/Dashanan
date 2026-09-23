# Phase 7 AR.5 — Reliability/Readiness Score Computation — Sprint 5 — 2026-09-22

**Formula (reused unchanged from Sprint 1-4):** `RS = sqrt(a x b)`, where `a` is a
citation/consistency check and `b` is an atomic-fact check, each in `[0,1]`.

**Scope note (added 2026-09-23):** This computation originally covered DASH-STORY-027 and
DASH-STORY-028 only, since DASH-STORY-029 (Zone 5 Shape B storage) was pulled into Sprint 5 scope
AFTER this AR.5 pass was first produced. **UPDATED 2026-09-23 (this addendum):** DASH-STORY-029 now
has its own dedicated self-check subsection below ("DASH-STORY-029's own AR.5 self-check") and a
combined bundle-wide figure — this file now covers all three Sprint 5 stories. The original
DASH-STORY-027/028-only Inputs/rule-of-three sections immediately below are left exactly as they
were first written (not deleted), per this file's own append-only convention; the DASH-STORY-029
subsection and the combined figure are new additions that build on top of them, not replacements.

## Inputs

- **a (citation/consistency):** `1.00`. Every score, agent name, file path, and dependency edge
  across `sprint5_ar0_routing_index.json`, `sprint5_ar1_assignments.json`,
  `sprint5_ar3_context_windows.json`, and `sprint5_implementation_execution_plan.json` was checked
  for internal consistency (see `sprint5-phase-c-gate-results.md`'s self-check) and found
  consistent — no orphaned sub-task, no score drifting between files, no cross-story edge omitted
  or fabricated.
- **b (atomic-fact check):** `1.00`. Every file-path/line-range-style citation traced back to a
  real, directly-read source this session itself opened (see `sprint5-phase-c-gate-results.md`).
  One LOW-severity confidence caveat was found (DASH-STORY-027's story-point estimate has no
  review-round history behind it) but is not a factual/citation defect and does not reduce `b`.

`RS = sqrt(1.00 x 1.00) = 1.00`

## Rule-of-three statistical floor

- AC count: **updated 2026-09-23, FINAL** (was stale at n=19 -- undercounted by one AC each for
  AC-027-DEV and AC-028-DEV/QA, per a direct recount of `sprint5_ar1_assignments.json`'s current
  `acceptance_criteria` arrays). AC-027-DEV-1/2/3/4 = 4, AC-027-QA-1/2/3 = 3,
  AC-027-REVIEW-1/2/3/4 = 4 (subtotal 11); AC-028-DEV-1/2/3/4 = 4, AC-028-QA-1/2/3/4 = 4,
  AC-028-REVIEW-1/2/3/4/5 = 5 (subtotal 13) — total unique AC IDs across
  `sprint5_ar1_assignments.json`'s DASH-STORY-027/028 Dev/QA/Review sub-tasks = 11+13 = **24**. The
  3 `AC-028-LOADTEST-*` IDs (a separate prerequisite sub-task) and DASH-STORY-029's own ACs (out of
  this pass's scope, see note above) are excluded from this n=24 figure.
  Rule-of-three floor at n=24: `3/24 = 0.125 -> 1 - 0.125 = 0.875`.
- `RS = 1.00` clears this floor.

## DASH-STORY-029's own AR.5 self-check (ADDED 2026-09-23)

**Scope:** DASH-STORY-029 (Zone 5 Shape B storage), design doc
`docs/phase-1.5-design/zone5-shape-b-storage-design.md` (v4, DRAFT, NOT IMPLEMENTATION-READY),
covering the same 4 Phase 7 bundle files as the 027/028 self-check above plus that design doc.

### Inputs

- **a (citation/consistency):** `1.00`. DASH-STORY-029's assignment record in
  `sprint5_ar1_assignments.json` (sub_tasks DASH-STORY-029-DEV/QA/REVIEW, `story_points: 8`,
  `assigned_agent`s database-engineer/integration-testing-engineer/consensus-agent) was checked for
  internal consistency against `sprint5_ar0_routing_index.json`'s `SPRINT5-CAND-003` entry,
  `sprint5_ar3_context_windows.json`'s DASH-STORY-029 context windows, and
  `sprint5_implementation_execution_plan.json`'s DASH-STORY-029 prompts — no orphaned sub-task, no
  score drift, no cross-story edge omitted or fabricated (this story's own `logging_note` confirms
  zero dependency edges to DASH-STORY-027/028, disjoint files).
- **b (atomic-fact check):** `1.00`. This pass independently re-opened DASH-STORY-029's own citations
  against the real files they claim to quote, not merely trusting the prior authoring session's own
  citations: `src/dashanan/infrastructure/entity_memory_repository.py` was directly read and
  confirmed to define `write_attribute` (line 166), `erase_entity` (line 242),
  `resolve_alias_prefix` (line 346), and `resolve_exact_term` (line 360) — exactly the four methods
  AC-029-DEV-1/2, AC-029-QA-1/3 cite as the Shape A interface DASH-STORY-029's `SqlEntityMemoryRepository`
  must match byte-for-byte. `docs/phase-1.5-design/zone5-shape-b-storage-design.md` was directly
  read and confirmed to contain Sections 1-9 as cited throughout `sprint5_ar1_assignments.json`'s
  DASH-STORY-029 acceptance criteria (Section 3 schema, Section 4 append-only analysis, Section 5
  compliance-role pattern, Section 6 interface-compatibility table, Section 8 disclosed open items —
  all present, none fabricated). No citation traced to a non-existent file, method, or section.

`RS = sqrt(1.00 x 1.00) = 1.00`

### Rule-of-three statistical floor (DASH-STORY-029 alone)

- AC count: 8 unique AC IDs — AC-029-DEV-1/2/3 = 3, AC-029-QA-1/2/3 = 3, AC-029-REVIEW-1/2 = 2
  (verified by grep of `sprint5_ar1_assignments.json`'s `ac_id` fields for this story; no
  AC-029-DEV-4/QA-4/REVIEW-3 exists).
  Rule-of-three floor at n=8: `3/8 = 0.375 -> 1 - 0.375 = 0.625`.
- `RS = 1.00` clears this floor.

### Combined bundle-wide figure (all three Sprint 5 stories)

- Combined AC count: the existing DASH-STORY-027/028 subtotal (n=24, from the "Rule-of-three
  statistical floor" section above) plus DASH-STORY-029's own 8 ACs = **32**.
  Rule-of-three floor at n=32: `3/32 = 0.09375 -> 1 - 0.09375 = 0.90625`.
- Combined `RS = 1.00` (all three stories' own `a`/`b` inputs are 1.00/1.00 on a self-check basis —
  see the same evidentiary-weight caveat in the section below, which applies equally to this figure)
  clears this combined floor.
- This combined n=32/floor=0.90625 figure is also recorded in
  `docs/phase-8-alignment/sprint5_ir1_agent_flags.json`'s `rs_computation_cross_check` field — see
  that file's own 2026-09-23 dated append for the cross-referenced restatement.

## Why this RS figure carries LESS evidentiary weight than Sprint 1-4's own RS=1.00 figures

**UPDATED 2026-09-23:** a real Phase C gate HAS now run (see "Real Phase C Gate results (2026-09-23
update)" immediately below) — this section's original text (retained, not deleted, per this file's
own append-only convention) describes the gap as it stood before that gate was dispatched. This is
the single most important thing to read in this file, and it is stated as plainly as
`sprint4-ar5-rs-computation.md` stated its own equivalent gap before Sprint 4's real gate closed
it:

1. **No paired-detector Phase C gate ran [HISTORICAL — see update below].** `a` and `b` above are
   this session's own single-pass self-check, not a dispatched `hallucination-detector` (NLI/
   FactScore) + `context-faithfulness-engineer` (RAGAS) census. See
   `sprint5-phase-c-gate-results.md`'s own honest-disclosure section.
2. **Both underlying design docs remain formally NOT IMPLEMENTATION-READY, though both now have
   real, dedicated review history.** UPDATED 2026-09-22, FINAL (was: "connection-pooling-design.md
   is still v1, one authoring pass, no review rounds" -- stale after that doc's own dedicated
   2-round adversarial solution-architect review): `connection-pooling-design.md` is now v2.6,
   having been through its own dedicated 2-round review (round 1 found a real defect in its
   migration description and a missing Zone 8 scope; round 2 verified the fix and caught 2 further
   issues, both fixed; a third fix pass added an explicit rollback procedure per a subsequent
   consensus-agent finding, bumping to v2.2; further advanced by a mandatory-circuit-breaker round,
   a Retry-After/error-code disambiguation round, a load-test-prerequisite round, and a Change-Log-
   ordering fix; final verdict APPROVE). `dpdp-crypto-shredding-full-erasure-design.md` is
   v2.3, having been through 13 solution-architect rounds AND a separate 4-round consensus-agent
   failure-mode/retry/rollback/escalation review, both ending in clean APPROVE. Neither doc is
   formally marked IMPLEMENTATION-READY in its own Status line — that label is reserved for a doc
   that has also cleared a real dispatched Phase C hallucination/faithfulness gate, which has not
   run for either — but both have now had genuine adversarial review depth, closer in kind to
   `fr013-predicate-schema-design.md`'s own 7-round precedent than a single-pass draft. `RS=1.00`
   here certifies that THIS BUNDLE is internally consistent with THOSE DESIGN DOCS AS THEY
   CURRENTLY STAND — it does NOT certify that either design is implementation-ready, since neither
   has cleared a real Phase C gate. Those are two different claims, and conflating them would
   overstate this bundle's readiness.
3. **No Jira issues or GitHub issues exist yet for either story**, unlike every Sprint 1-4 item,
   which had at least a real Jira epic/story or a live GitHub issue as external grounding.

### Real Phase C Gate results (2026-09-23 update)

A real, genuinely dispatched `hallucination-detector` + `context-faithfulness-engineer` pair ran
against the full three-story Sprint 5 bundle — see `sprint5-phase-c-gate-results.md`'s "Real Phase C
Gate (dispatched)" section for the full report. **Combined verdict: PASS.** Zero CRITICAL/HIGH
findings from either agent across 12 (hallucination-detector) + 14 (context-faithfulness-engineer)
independently sampled high-stakes claims, no disagreement between the two agents, and no finding
against any `must_not_deviate` item or security/legal-adjacent claim. One disclosed LOW-severity,
unverifiable-but-not-contradicted note (the dpdp design doc's "13 review rounds" figure could not be
independently confirmed from its own Change Log table alone).

**Honest, non-inflated evidentiary-weight characterization of this real gate (do not overstate it):**
this is a genuinely independent, separately dispatched qualitative review — a real second and third
pair of eyes with no access to this session's own reasoning, checking claims against real files
cold. It is NOT a numerically-computed statistical gate: neither agent could run an actual NLI/
RAGAS/SummaC/BERTScore/FactScore pipeline in this environment (no such pipeline exists here), and
both agents explicitly marked every numeric Output Format field `N/A — not computed` rather than
present a fabricated score. So this real gate closes gap 1 above in the sense that matters most
(genuine independent dispatch, not a same-session self-check) but does NOT itself constitute the
literal paired-detector statistical census Sprint 1-4's own RS=1.00 figures are sometimes read to
imply. Both agents also disclosed their own scope caveat: a targeted sample of the bundle's highest-
stakes claims, not exhaustive line-by-line coverage of every one of the bundle's hundreds of
narrative sentences.

**`a`/`b` inputs, recomputed from the real gate's actual findings (not assumed 1.00/1.00 by
self-check convention):** `a` (citation/consistency) = `1.00` — independently re-confirmed by two
separate dispatched agents finding zero contradicted citations across 26 combined sampled claims.
`b` (atomic-fact check) = `1.00` — same basis; every sampled fact traced to real, directly-verified
source with zero fabrications found. The one LOW-severity note (the "13 rounds" figure) does not
reduce either input, per the same LOW-severity/non-reducing convention the original self-check
above already used for DASH-STORY-027's own confidence caveat — an unverifiable-but-not-demonstrably-
false figure is a disclosed limitation, not a citation or atomic-fact defect.

`RS = sqrt(1.00 x 1.00) = 1.00` — now backed by a real dispatched gate for the DASH-STORY-027/028
scope, in addition to the self-check basis above.

## Verdict

**RS = 1.00 (self-check basis), CONDITIONAL — NOT equivalent to Sprint 1-4's paired-detector-backed
RS=1.00 figures.** Recommend, before this bundle is treated as execution-ready for a future Phase B
dispatch:
1. UPDATED 2026-09-23: a real Phase C gate (dispatched `hallucination-detector` +
   `context-faithfulness-engineer`) has now run -- PASS verdict, zero CRITICAL/HIGH findings -- see
   "Real Phase C Gate results (2026-09-23 update)" above and `sprint5-phase-c-gate-results.md`'s own
   "Real Phase C Gate (dispatched)" section for the full report. This item is resolved.
2. UPDATED 2026-09-23, FINAL: both design docs have now cleared their own dedicated review loops
   with a final clean APPROVE (connection-pooling-design.md v2.6; dpdp-crypto-shredding-full-
   erasure-design.md v2.3). This item is resolved -- remaining before either is formally marked
   IMPLEMENTATION-READY is a real dispatched Phase C gate (item 1 above), not a further design
   review round.
3. UPDATED 2026-09-23: all six user decisions raised across this Sprint 5 bundle now have a
   disposition (SP5-DEC-001/002 CONFIRMED, SP5-DEC-003 DEFERRED, SP5-DEC-004 SUPERSEDED in
   `sprint5_ar0_routing_index.json`; SP5-DEC-101 RESOLVED, SP5-DEC-102 SUPERSEDED in
   `sprint5_ar1_assignments.json`) -- no longer an "open decisions" item blocking dispatch.
4. UPDATED 2026-09-23: DASH-STORY-029 (pulled into scope after this file was first produced) now has
   its own dedicated AR.5 self-check ("DASH-STORY-029's own AR.5 self-check" section above,
   RS=1.00, floor 0.625 at n=8) and is included in this file's combined bundle-wide figure (n=32,
   floor 0.90625). This item is resolved on the AR.5-self-check-coverage question specifically --
   remaining before DASH-STORY-029 is formally treated as execution-ready alongside 027/028 is the
   same real dispatched Phase C gate (item 1 above) that all three stories still need, plus this
   file's own `per_item_cross_check`-style gap (see `sprint5_ir1_agent_flags.json`'s
   `per_item_cross_check_dash_story_029_disclosure`, which remains separately deferred).

This conditionality is carried forward into `sprint5_ir1_agent_flags.json`'s own
`execution_readiness` determination.
