# Phase 7 Reliability Score (RS) Computation — Sprint 2 — 2026-09-20

## Inputs
- a (NLI faithfulness, from C-1 hallucination-detector): **1.00** — "Document faithfulness
  (NLI source-level proxy): 1.00" per `docs/phase-7-routing/sprint2-phase-c-gate-results.md`
  (27/27 prompts' FR-citations, context-source citations, must-not-deviate content, and AC
  text fully entailed by their named sources; 0 contradictions, 0 unsupported/fabricated
  claims; non-sampled, full 9-story/27-prompt coverage).
- b (FactScore, from C-1 hallucination-detector's own atomic-fact check this round): **1.00**
  — "FactScore (RAG atomic-fact check): 1.00" per the same source (every checked claim — 9
  story routings, 27 context-source lists, 27 word counts, 9 must-not-deviate sets, all AC
  text, all quoted HLD passages, all numeric constants — traced to and supported by real
  content in `sprint2_ar1_assignments.json` / `sprint2_ar3_context_windows.json` /
  `backlog_draft.json` / `HLD.md` / `SRS.md`). Cross-checked against C-2's independent RAGAS
  Faithfulness (citation-verbatim dimension) score, also **1.00** — the two-detector paired
  result agrees, so no proxy substitution across detectors was needed this round (unlike the
  Phase 7 baseline computation, where C-2 had not produced a directly comparable FactScore and
  its RAGAS Faithfulness score was used as a conservative proxy for b).
- c (1-ECE, calibration): **N/A** — no logit/confidence calibration data exists pre-Phase-B.
- d (DRE / Coverage): **N/A** — no Sprint 2 code or tests exist yet (Sprint 1's code/tests do
  exist and are unaffected — this component is scoped to Sprint 2, whose Phase B implementation
  is explicitly out of scope for this document).

## Computation
Phase-7-scoped RS (2-component geometric mean of the genuinely measurable components a, b):

```
RS_phase7_sprint2 = sqrt(a x b) = sqrt(1.00 x 1.00) = 1.00
```

**On the confidence interval:** the Phase 7 baseline computation reported a delta-method 95%
CI of [0.97, 1.00] around a 0.99 point estimate, appropriate for a *sampled* estimate of a
larger population. Sprint 2's Phase C gate was explicitly run as a **full non-sampled census**
— all 9 stories, all 27 prompts, checked in full by both detectors (per
`sprint2-phase-c-gate-results.md`: "Disclosed coverage: full 9/9 stories, 27/27 prompts
checked — no sampling gap to close, unlike the Sprint 1 run"). Because this is a census of the
entire checked population rather than a sample drawn from it, a delta-method/binomial
confidence interval is not the correct statistical tool here — there is no unobserved
remainder of this population left to bound.

For transparency, if a conservative sampling-style bound is applied anyway (treating the
27-prompt census as if it were a sample of a hypothetical larger prompt population, using a
rule-of-three bound for zero observed defects at n=27): upper bound on the underlying defect
rate is approximately 3/27 ~= 0.111, implying a conservative floor of sqrt(0.889 x 0.889) ~=
0.89 on a hypothetical resampled population. This floor is disclosed for methodological
honesty but is **not** the reported RS — the reported RS is the direct census result, 1.00,
since 100% of the checkable population (not a sample of it) was verified clean on both a and
b after the one real finding (DASH-STORY-017's 101%-summing story-point split, LOW severity,
non-PII, non-citation) was fixed in place and the file re-validated.

This mirrors the same precedent already established at the Phase 7 baseline gate (`C_api` vs
`C_plan`) and at the Sprint 1 gate — a metric requiring data that does not yet exist (c, d)
is reported N/A, not approximated or defaulted.

## Verdict

**CONDITIONAL_PASS** — RS_phase7_sprint2 = 1.00 >= 0.95 threshold, on the two components
measurable at this phase (routing/prompt-generation artifacts for DASH-STORY-012..020, not
implementation). Full 4-component RS is deferred to a Phase B reliability gate, once code and
tests exist to compute DRE and Coverage against.

This is not a retry-eligible failure under the SC.1-3 bounded-retry protocol — c and d are
not-yet-applicable metrics, not gate rejections. The one real Phase C finding (DASH-STORY-017
story-point arithmetic) was resolved before this RS computation was performed, so RS reflects
the post-resolution, zero-unresolved-findings state per `sprint2-phase-c-gate-results.md`'s
"PASS after resolution" verdict.

## Source
All inputs traced verbatim to `docs/phase-7-routing/sprint2-phase-c-gate-results.md` (Phase C
gate run, 2026-09-20, non-sampled, 9 stories / 27 prompts). No score in this document was
invented or estimated outside that source.
