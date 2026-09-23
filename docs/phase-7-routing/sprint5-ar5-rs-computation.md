# Phase 7 AR.5 — Reliability/Readiness Score Computation — Sprint 5 — 2026-09-22

**Formula (reused unchanged from Sprint 1-4):** `RS = sqrt(a x b)`, where `a` is a
citation/consistency check and `b` is an atomic-fact check, each in `[0,1]`.

**Scope note (added 2026-09-23):** This computation covers DASH-STORY-027 and DASH-STORY-028 only.
DASH-STORY-029 (Zone 5 Shape B storage) was pulled into Sprint 5 scope AFTER this AR.5 pass was
produced, so it is NOT covered by the RS=1.00 figure or the rule-of-three floor below — it requires
its own separate AR.5/Phase-C coverage before being treated as execution-ready alongside 027/028.

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

## Verdict

**RS = 1.00 (self-check basis), CONDITIONAL — NOT equivalent to Sprint 1-4's paired-detector-backed
RS=1.00 figures.** Recommend, before this bundle is treated as execution-ready for a future Phase B
dispatch:
1. A real Phase C gate (dispatched `hallucination-detector` + `context-faithfulness-engineer`),
   per `sprint5-phase-c-gate-results.md`'s own recommendation.
2. UPDATED 2026-09-23, FINAL: both design docs have now cleared their own dedicated review loops
   with a final clean APPROVE (connection-pooling-design.md v2.6; dpdp-crypto-shredding-full-
   erasure-design.md v2.3). This item is resolved -- remaining before either is formally marked
   IMPLEMENTATION-READY is a real dispatched Phase C gate (item 1 above), not a further design
   review round.
3. UPDATED 2026-09-23: all six user decisions raised across this Sprint 5 bundle now have a
   disposition (SP5-DEC-001/002 CONFIRMED, SP5-DEC-003 DEFERRED, SP5-DEC-004 SUPERSEDED in
   `sprint5_ar0_routing_index.json`; SP5-DEC-101 RESOLVED, SP5-DEC-102 SUPERSEDED in
   `sprint5_ar1_assignments.json`) -- no longer an "open decisions" item blocking dispatch. DASH-
   STORY-029 (pulled into scope after this file was produced) still needs its own AR.5/Phase-C
   coverage before being treated as execution-ready alongside 027/028 -- see the scope note above.

This conditionality is carried forward into `sprint5_ir1_agent_flags.json`'s own
`execution_readiness` determination.
