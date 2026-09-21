# Phase 7 Reliability/Readiness Score (RS) Computation — Sprint 4 — 2026-09-21

## Scope note (read before the Inputs section)

Sprint 2's and Sprint 3's RS documents (`sprint2-ar5-rs-computation.md`,
`sprint3-ar5-rs-computation.md`) both source their `a`/`b` inputs from a dedicated,
on-disk Phase C gate-results artifact (`sprint2-phase-c-gate-results.md`,
`sprint3-phase-c-gate-results.md`), each itself the output of a paired-detector run
(C-1 hallucination-detector for NLI faithfulness + FactScore, cross-checked by a C-2
RAGAS Faithfulness pass). **No `sprint4-phase-c-gate-results.md` exists in this repo.**
The task brief that requested this document states the bundle (`sprint4_ar0_routing_index.json`,
`sprint4_ar1_assignments.json`, `sprint4_ar3_context_windows.json`,
`sprint4_implementation_execution_plan.json`) is "all Phase-C-approved" — this document
accepts that as the given premise of the task it was asked to compute against, but it
is **not** independently re-derived from a paired-detector artifact the way Sprint 2/3's
`a`/`b` were. This is disclosed as a methodology gap rather than papered over: `a` and
`b` below are this session's own single-reviewer citation/consistency check (performed
while reading all four bundle files in full for this task), not a two-detector census.
This is a materially weaker evidentiary basis than Sprint 2/3 carried, and the Verdict
section says so plainly rather than reporting an unqualified 1.00.

## Inputs

- **a (citation/consistency self-check, in place of C-1 NLI faithfulness):** **1.00** —
  this session read `sprint4_ar0_routing_index.json`, `sprint4_ar1_assignments.json`,
  `sprint4_ar3_context_windows.json`, and `sprint4_implementation_execution_plan.json`
  in full and cross-checked every score, file:line citation, and FR/GitHub-issue
  reference that carries forward between them: `python-backend-engineer` 0.900,
  `integration-testing-engineer` 0.825, `api-security-auditor` 0.800 are identical in
  AR.0's `candidates[]`/`advisory_review_roster` and AR.1's `assigned_agent`/`score`
  fields for all three sub-tasks; the 7000/5500/5000-token `context_budget_tokens`
  values are identical between AR.3's `context_windows[]` and the IEP prompts' own
  "Context Budget" line; FR-013 and GitHub #23 are cited consistently (never
  paraphrased, never renumbered) across all four files; GitHub #25 is disclosed in all
  three later files as a named, explicitly-not-fixed adjacent defect, never silently
  dropped. **0 contradictions found** across this pass. This is a direct read, not a
  sample — but it is one reviewer's pass, not the paired NLI-detector census Sprint 2/3
  report.
- **b (atomic-fact self-check, in place of C-1 FactScore / C-2 RAGAS):** **1.00** —
  spot-checked atomic claims against their own named sources during the same read: the
  design doc's own status line (`docs/phase-1.5-api/fr013-predicate-schema-design.md:3`,
  "Status: IMPLEMENTATION-READY — pending user review. No implementation files have
  been modified.") matches every file's restatement of it; the HLD Section 3.4/3.8
  citations in AR.0/AR.1 are the same citations Sprint 3's own routing files already
  used for the same underlying mechanism (conflict-detection sweep, routing truth
  table), not a fresh, unverifiable claim; the `dependency_integrity_check.sprint_4_edges`
  in AR.1 (`DEV: []`, `QA: [DEV]`, `REVIEW: [DEV, QA]`) matches the IEP's own
  `depends_on`/`sequencing_note` fields and its `file_collision_analysis.collision_finding`
  of `NONE` exactly. No fabricated or unsupported atomic claim was found. As with `a`,
  this is a single-reviewer check performed inline with this task, not an independent
  second-detector corroboration — Sprint 4 has no C-2-equivalent pass recorded anywhere.
- **c (1-ECE, calibration):** **N/A** — unchanged from every prior Phase 7 RS document:
  no logit/confidence calibration data exists at this routing stage.
- **d (DRE / Coverage):** **N/A** — Sprint 4 is pre-Phase-B. The IEP's own
  `scope_note` states plainly: "Phase B (actual code implementation) is explicitly OUT
  OF SCOPE for this project bundle... This file stores 3 dispatch-ready CoT prompts...
  PREPARED FOR FUTURE USE, NOT EXECUTED NOW." No Sprint 4 code or tests exist to
  compute a defect rate or coverage figure against, the same posture as the Phase 7
  baseline and Sprint 1-3 gates at their own pre-Phase-B stage.

## Scope & maturity context (qualitative, not folded into the RS formula)

The task instruction names three reasons Sprint 4 should read as more ready than a
typical routing-stage snapshot, none of which are `a`/`b`/`c`/`d` inputs but all of
which are worth recording rather than silently assumed:

1. **Design-doc maturity:** `fr013-predicate-schema-design.md` is at v7, scored 9.8/10,
   status IMPLEMENTATION-READY, after seven review rounds (per its own Change Log,
   cited identically across all four bundle files) — a materially higher design-maturity
   floor than Sprint 1-3's routing files worked from, none of which cite a
   seven-round, 9.8/10 design doc as their single source of truth.
2. **Bundle Phase-C-approval (as given, not re-derived here):** the task premise states
   the bundle already cleared Phase C; this document's own `a`/`b` self-check (above)
   found nothing to contradict that, but — per the Scope note — cannot itself stand in
   for the paired-detector artifact that would let a future reader independently audit
   the claim the way `sprint3-phase-c-gate-results.md` lets a reader audit Sprint 3's.
3. **Narrow, well-bounded scope:** one story (`DASH-STORY-026`), three sequential
   sub-tasks, one file cluster (`openapi.yaml`, `schemas.py`, `app.py` owned solely by
   Dev; the new test file owned solely by QA). The IEP's own
   `file_collision_analysis.collision_finding` is `NONE` and its
   `parallelization_opportunity` is `NONE` — not because parallelization was missed,
   but because a strict Dev→QA→Review dependency chain (`sprint_4_edges`) makes
   concurrent dispatch structurally unsafe, and the IEP says so rather than fabricating
   a parallel track. This is a smaller, simpler routing surface than any prior sprint's
   (Sprint 1: 7 stories, Sprint 2: 9, Sprint 3: 6), which lowers the chance of an
   undetected routing/collision defect independent of how well `a`/`b` were checked.

None of these three raise `a` or `b` above what was directly checked — they are
recorded here as the reasons a low-population census (below) is plausible, not as
inputs multiplied into the RS formula.

## Computation

Phase-7-scoped RS (2-component geometric mean of the genuinely measurable components
`a`, `b`), mirroring `sprint2-ar5-rs-computation.md` / `sprint3-ar5-rs-computation.md`'s
method exactly:

```
RS_phase7_sprint4 = sqrt(a x b) = sqrt(1.00 x 1.00) = 1.00
```

**On the confidence interval:** this was a full, non-sampled read of all four bundle
files (not a sample of them), so — as with Sprint 2/3 — a delta-method/binomial
sampling CI is not the correct tool for the checked population itself. For
transparency, the same rule-of-three-style bound Sprint 3 applied is shown here at
Sprint 4's own finer-grained units:

- **9 acceptance-criteria entries** (AC-026-DEV-1/2/3, AC-026-QA-1/2/3,
  AC-026-REVIEW-1/2/3, per `sprint4_ar1_assignments.json`) — the structural analog to
  Sprint 3's 16-AC unit. Rule-of-three at n=9: 3/9 = 0.333, implying a conservative
  floor of `sqrt(0.667 x 0.667) ~= 0.67` on a hypothetical resampled population.
- **26 context-window source-citation entries** (12 for DEV + 7 for QA + 7 for REVIEW,
  summing `files_to_read_or_modify` + `domain_files_cited_across_review_rounds` +
  `github_issues` + `cross_reference` across `sprint4_ar3_context_windows.json`'s three
  `context_windows[]` entries) — the analog to Sprint 3's 71-source unit. Rule-of-three
  at n=26: 3/26 ~= 0.115, implying a floor of `sqrt(0.885 x 0.885) ~= 0.89`.

Both floors are markedly looser than Sprint 3's (0.81 at n=16, 0.96 at n=71) precisely
*because* Sprint 4's population is smaller — a single story with three sub-tasks
produces far fewer atomic units than a six-story sprint, so the same "3 hypothetical
undiscovered defects" assumption costs proportionally more headroom here. This is
disclosed as a genuine consequence of Sprint 4's narrow scope, not a defect in the
method: the narrower the checked population, the less a rule-of-three bound can say,
and the more the reported RS has to rest on the census itself (100% of a and b actually
checked clean) rather than on the statistical bound as reassurance. Both floors are
disclosed for transparency and are **not** the reported RS — the reported RS is the
direct census result, 1.00, since 100% of the checkable population (all four bundle
files, all 9 ACs, all 26 source citations) was verified clean on `a` and `b` by this
session's own single-reviewer pass, with zero contradictions found.

## Verdict

**CONDITIONAL_PASS, with a disclosed evidentiary gap** — RS_phase7_sprint4 = 1.00 >=
0.95 threshold, on the two components measurable at this phase, computed from this
session's own direct read of the four Sprint 4 routing artifacts (not Sprint 4
implementation code, which does not yet exist — the IEP is explicit that Phase B is
out of scope for this bundle).

Unlike Sprint 2/3, this PASS is **not** backed by a paired-detector
`sprintN-phase-c-gate-results.md` artifact — none exists for Sprint 4. The task
premise's claim that the bundle is "Phase-C-approved" is accepted as given here, and
this document's own self-check corroborates it (zero contradictions across `a`/`b`),
but a future reader auditing Sprint 4 the way `sprint3-phase-c-gate-results.md` lets a
reader audit Sprint 3 has no equivalent artifact to read. **Recommended before Phase B
dispatch:** run a dedicated Sprint 4 Phase C gate pass (C-1 hallucination-detector +
C-2 RAGAS Faithfulness, same as Sprint 2/3) against
`sprint4_ar1_assignments.json`/`sprint4_ar3_context_windows.json`/
`sprint4_implementation_execution_plan.json` and record it as
`sprint4-phase-c-gate-results.md`, so Sprint 4 carries the same evidentiary standard as
Sprint 2/3 rather than resting on this document's single-reviewer substitute. This is
not a retry-eligible failure under the SC.1-3 bounded-retry protocol — it is a
disclosed methodology gap, not a gate rejection, and does not block the two open
decisions (`SP4-DEC-201`, `SP4-DEC-202`) the bundle itself already carries as open
before dispatch.

`c` and `d` remain N/A, matching the Phase 7 baseline and Sprint 1-3 gates at the same
pre-Phase-B stage — not approximated or defaulted.

## Source

`a`/`b` in this document are sourced from this session's own direct, full read of
`sprint4_ar0_routing_index.json`, `sprint4_ar1_assignments.json`,
`sprint4_ar3_context_windows.json`, and `sprint4_implementation_execution_plan.json`
(all read in full this session), cross-checked against
`docs/phase-1.5-api/fr013-predicate-schema-design.md` v7's own status line (line 3) and
GitHub issue #23/#25 citations as those four bundle files themselves restate them — not
from a separate `sprint4-phase-c-gate-results.md`, because none exists (see Scope note
above). Computation method (2-component geometric mean of `a` and `b`; `c`, `d`
reported N/A when their underlying artifacts do not yet exist; full-census treatment in
place of a sampling CI; a disclosed rule-of-three bound at the sprint's own
finer-grained units) mirrors `docs/phase-7-routing/sprint2-ar5-rs-computation.md` and
`docs/phase-7-routing/sprint3-ar5-rs-computation.md` exactly, as instructed. No score in
this document was invented or estimated beyond what this session's own read of the
named files supports.
