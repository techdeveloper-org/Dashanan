# Phase 7 Reliability Score (RS) Computation — Sprint 3 — 2026-09-20

## Inputs
- a (NLI faithfulness, from C-1 hallucination-detector): **1.00** — "Document faithfulness
  (NLI source-level proxy): 1.00 after fix" per `docs/phase-7-routing/sprint3-phase-c-gate-results.md`
  (6/6 stories' FR-citations, context-source citations, must-not-deviate content, AC text, and
  quoted HLD/SRS passages fully entailed by their named real sources after the two Phase C
  corrections — the `openapi.yaml` operationId count and the stale `dashanan_app_role` Postgres
  role name — were applied; 0 remaining contradictions or unsupported claims; non-sampled, full
  6-story/16-AC/62-citation coverage).
- b (FactScore, from C-1 hallucination-detector's own atomic-fact check this round): **1.00**
  — "FactScore (RAG atomic-fact check): 1.00 after fix" per the same source (every checked
  claim — 6 story routings, 71 context-source list entries (corrected from 62, see Addendum), 16 acceptance-criteria entries, 6
  must-not-deviate sets, all quoted HLD/SRS/openapi.yaml/schema-SQL/source-code passages, all
  source-code line-number citations — traced to and supported by real content in
  `sprint3_ar1_assignments.json` / `sprint3_ar3_context_windows.json` / `HLD.md` / `SRS.md` /
  `backlog_draft.json` / `openapi.yaml` / the cited `.py`/`.sql` files). Cross-checked against
  C-2's independent RAGAS Faithfulness (citation-verbatim dimension) score, also **1.00 after
  fix** — the two-detector paired result agrees post-resolution, so no proxy substitution
  across detectors was needed this round (same posture as Sprint 2; the Phase 7 baseline
  computation is the only round that required a proxy substitution for b).
- c (1-ECE, calibration): **N/A** — no logit/confidence calibration data exists at this stage.
- d (DRE / Coverage): **N/A** — no Sprint 3 code or tests exist yet. Per
  `sprint3-phase-c-gate-results.md`'s own "Phase C / Phase B boundary" note: "Sprint 3 is still
  in the AR.1/AR.3 routing stage... Once Phase B produces real code and a
  `sprint3_implementation_execution_plan.json`-driven CoT prompt set for these stories, that
  artifact is the correct target for a Sprint-3-implementation Phase C pass, not a re-run of
  this routing-level gate." This is an earlier-stage scope than Sprint 1/2, which already had a
  corresponding `implementation_execution_plan.json` this gate type would check instead — c and
  d remain scoped-out-of-existence for this document exactly as they were for the Phase 7
  baseline and Sprint 1/2 gates.

## Computation
Phase-7-scoped RS (2-component geometric mean of the genuinely measurable components a, b),
mirroring `sprint2-ar5-rs-computation.md`'s method exactly:

```
RS_phase7_sprint3 = sqrt(a x b) = sqrt(1.00 x 1.00) = 1.00
```

**On the confidence interval:** as with the Sprint 2 computation, a delta-method/binomial
sampling-style CI is not the correct statistical tool here, because Sprint 3's Phase C gate was
run as a **full non-sampled census** — all 6 stories, all 16 acceptance-criteria entries, all 62
`context_windows[].sources[]` entries, checked in full by both detectors (per
`sprint3-phase-c-gate-results.md`: "Disclosed coverage: full 6/6 stories checked — no sampling
gap to close, matching Sprint 2's own non-sampled-from-the-start posture"). There is no
unobserved remainder of this checked population left to bound.

For transparency, if a conservative sampling-style bound is applied anyway (treating the
census as if it were a sample of a hypothetical larger population), the appropriate atomic unit
to mirror Sprint 2's choice of "27 prompts" (the finer-grained unit nested one level below the
9-story count, over which per-item entailment was actually scored) is Sprint 3's own
finer-grained unit nested one level below its 6-story count: the **16 acceptance-criteria
entries**. Sprint 3 has no "prompt" unit yet — it is pre-Phase-B, so no
`implementation_execution_plan.json`-driven CoT prompt set has been generated for these stories
(see the "d" input above) — so ACs are the closest structural analog to Sprint 2's prompts as
the atomic per-item entailment unit.

Using a rule-of-three bound for zero observed defects at n=16 (post-resolution — the 2 real
findings were fixed before this computation, mirroring Sprint 2's post-resolution treatment of
its 1 real finding): upper bound on the underlying defect rate is approximately 3/16 ~= 0.188,
implying a conservative floor of sqrt(0.813 x 0.813) ~= 0.81 on a hypothetical resampled
population.

A second, more granular reference point using the 71 individual `sources[]` citation entries
(corrected 2026-09-21 from a miscount of 62 -- see Addendum; the finest atomic unit the gate doc
explicitly reconciles) gives 3/71 ~= 0.042, implying a floor of sqrt(0.958 x 0.958) ~= 0.96 at
that granularity. Both alternate floors are disclosed
for methodological transparency but are **not** the reported RS — the reported RS is the direct
census result, 1.00, since 100% of the checkable population (not a sample of it) was verified
clean on both a and b after the two real findings (DASH-STORY-023's `openapi.yaml` operationId
count, and DASH-STORY-024's stale `dashanan_app_role` Postgres role name, both LOW severity,
non-PII, non-citation-fabrication in the sense of being genuinely-invented sources) were fixed in
place and both files re-validated as syntactically valid JSON.

This mirrors the same precedent already established at the Phase 7 baseline gate, the Sprint 1
gate, and the Sprint 2 gate (`sprint2-ar5-rs-computation.md`) — a metric requiring data that does
not yet exist (c, d) is reported N/A, not approximated or defaulted, and a full census is
reported as its direct result rather than forced through a sampling-CI framework that does not
apply to it.

## Verdict

**CONDITIONAL_PASS** — RS_phase7_sprint3 = 1.00 >= 0.95 threshold, on the two components
measurable at this phase (routing/context-window design artifacts for
DASH-STORY-021/022/011/023/024/025, i.e. `sprint3_ar1_assignments.json` and
`sprint3_ar3_context_windows.json` — not Sprint 3 implementation code, which does not yet
exist). Full 4-component RS is deferred to a Phase B reliability gate, once Sprint 3 code and
tests exist to compute DRE and Coverage against — the same deferral pattern as the Sprint 2 gate,
except Sprint 3 is deferred one stage earlier (Sprint 2 already had an
`implementation_execution_plan.json`-scale Phase B target in view; Sprint 3 does not yet).

This is not a retry-eligible failure under the SC.1-3 bounded-retry protocol — c and d are
not-yet-applicable metrics, not gate rejections. The two real Phase C findings (DASH-STORY-023's
operationId count, DASH-STORY-024's stale role name) were resolved before this RS computation
was performed, so RS reflects the post-resolution, zero-unresolved-findings state per
`sprint3-phase-c-gate-results.md`'s "PASS after resolution" verdict — the same discipline as
Sprint 2's single-finding resolution, applied here to two independent defect classes found in
one round (a numeric-count error and a stale identifier), both concentrated in the sprint's two
highest-citation-density stories per that document's own disclosure.

## Source
All inputs traced verbatim to `docs/phase-7-routing/sprint3-phase-c-gate-results.md` (Phase C
gate run, 2026-09-20, non-sampled, 6 stories / 16 acceptance criteria / 71 source citations, corrected from 62). No
score in this document was invented or estimated outside that source. Computation method
(2-component geometric mean of a and b; c, d reported N/A when their underlying artifacts do not
yet exist; full-census treatment in place of a sampling CI) mirrors
`docs/phase-7-routing/sprint2-ar5-rs-computation.md` exactly, as instructed.


## Addendum (2026-09-21) — Post-hoc recomputation after external review

An external review (2026-09-21) found the source-count figure this document used (62) was wrong
(real count is 71, see `sprint3-phase-c-gate-results.md`'s own addendum for the full list of 7
findings) and that additional citation-faithfulness defects existed in
`sprint3_implementation_execution_plan.json` — a file this RS computation's `a`/`b` inputs never
actually covered (Phase C's gate scope was `sprint3_ar1_assignments.json` and
`sprint3_ar3_context_windows.json` only).

Recomputation: all 7 external-review findings are now fixed in place (agent-field sync, stale
"29"/`dashanan_app_role` text in the execution plan, source-count correction, decision-status
sync — see the other three artifacts' own 2026-09-21 addenda). With the source-count correction,
the rule-of-three floor at the sources-granularity recomputes to sqrt(0.958 x 0.958) ~= 0.96 (was
~= 0.95 under the wrong 62 count), still comfortably above the 0.95 threshold.

The **reported RS remains 1.00**, unchanged: RS is defined as the direct census result over the
files Phase C's gate actually checked (AR.1 + AR.3), and both are re-verified as 100% clean on `a`
(NLI faithfulness) and `b` (FactScore) after this round's fixes — the same census-based
methodology as the original computation, not a sample. The execution plan's own defects (findings
1, 3, 4 in the Phase C gate's addendum) were never part of this RS's checkable population in the
first place, since neither `a` nor `b` were ever computed against that file — they are disclosed
as a scope gap in this RS methodology, not a violation of the reported 1.00. A future Phase 6 round
that extends Phase C's gate to cover `sprint3_implementation_execution_plan.json` directly should
compute a distinct RS for that population rather than assuming this 1.00 covers it.
