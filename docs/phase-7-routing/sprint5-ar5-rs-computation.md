# Phase 7 AR.5 — Reliability/Readiness Score Computation — Sprint 5 — 2026-09-22

**Formula (reused unchanged from Sprint 1-4):** `RS = sqrt(a x b)`, where `a` is a
citation/consistency check and `b` is an atomic-fact check, each in `[0,1]`.

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

- AC count: **updated 2026-09-22 per solution-architect review round 7 finding 3** (was stale at
  15 -- round 6 added AC-027-REVIEW-4 without this file being updated). AC-027-DEV-1/2/3 = 3,
  AC-027-QA-1/2 = 2, AC-027-REVIEW-1/2/3/4 = 4, AC-028 equivalents (DEV-1/2/3, QA-1/2, REVIEW-1/2)
  = 3+2+2 = 7 — total unique AC IDs across `sprint5_ar1_assignments.json` = 3+2+4+7 = **16**.
  Rule-of-three floor at n=16: `3/16 = 0.1875 -> sqrt(0.8125 x 0.8125) = 0.8125`.
- `RS = 1.00` clears this floor.

## Why this RS figure carries LESS evidentiary weight than Sprint 1-4's own RS=1.00 figures

This is the single most important thing to read in this file, and it is stated as plainly as
`sprint4-ar5-rs-computation.md` stated its own equivalent gap before Sprint 4's real gate closed
it:

1. **No paired-detector Phase C gate ran.** `a` and `b` above are this session's own single-pass
   self-check, not a dispatched `hallucination-detector` (NLI/FactScore) + `context-faithfulness-
   engineer` (RAGAS) census. See `sprint5-phase-c-gate-results.md`'s own honest-disclosure section.
2. **Both underlying design docs remain formally NOT IMPLEMENTATION-READY, though both now have
   real, dedicated review history.** UPDATED 2026-09-22, FINAL (was: "connection-pooling-design.md
   is still v1, one authoring pass, no review rounds" -- stale after that doc's own dedicated
   2-round adversarial solution-architect review): `connection-pooling-design.md` is now v2.2,
   having been through its own dedicated 2-round review (round 1 found a real defect in its
   migration description and a missing Zone 8 scope; round 2 verified the fix and caught 2 further
   issues, both fixed; a third fix pass added an explicit rollback procedure per a subsequent
   consensus-agent finding, bumping to v2.2; final verdict APPROVE). `dpdp-crypto-shredding-full-erasure-design.md` is
   v2.2, having been through 13 solution-architect rounds AND a separate 4-round consensus-agent
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

## Verdict

**RS = 1.00 (self-check basis), CONDITIONAL — NOT equivalent to Sprint 1-4's paired-detector-backed
RS=1.00 figures.** Recommend, before this bundle is treated as execution-ready for a future Phase B
dispatch:
1. A real Phase C gate (dispatched `hallucination-detector` + `context-faithfulness-engineer`),
   per `sprint5-phase-c-gate-results.md`'s own recommendation.
2. UPDATED 2026-09-22, FINAL: both design docs have now cleared their own dedicated review loops
   with a final clean APPROVE (connection-pooling-design.md v2.2; dpdp-crypto-shredding-full-
   erasure-design.md v2.2). This item is resolved -- remaining before either is formally marked
   IMPLEMENTATION-READY is a real dispatched Phase C gate (item 1 above), not a further design
   review round.
3. Resolution of the six open user decisions raised across this Sprint 5 bundle (SP5-DEC-001
   through 004 in `sprint5_ar0_routing_index.json`; SP5-DEC-101/102 in `sprint5_ar1_assignments.json`).

This conditionality is carried forward into `sprint5_ir1_agent_flags.json`'s own
`execution_readiness` determination.
