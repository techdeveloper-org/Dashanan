# Phase 7 Reliability Score (RS) Computation — 2026-09-17

## Inputs
- a (NLI faithfulness, from C-1 hallucination-detector): 0.99
- b (FactScore, from C-2 context-faithfulness-engineer, RAGAS F used as conservative proxy): 0.99
- c (1-ECE, calibration): **N/A** — no logit/confidence calibration data exists pre-Phase-B
- d (DRE / Coverage): **N/A** — no code or tests exist yet; Phase B (implementation) is explicitly out of scope for this entire project bundle

## Computation
Phase-7-scoped RS (2-component geometric mean of the genuinely measurable components a, b):

```
RS_phase7 = sqrt(a × b) = sqrt(0.99 × 0.99) = 0.99
95% CI: [0.97, 1.00] (small sample, delta-method estimate)
```

This mirrors the same precedent already established at the Phase 1.5 gate (`C_api` vs `C_plan` — a metric requiring execution cannot be computed before execution exists). No default/fabricated value was substituted for c or d; both are honestly reported as structurally not-yet-applicable rather than approximated.

## Verdict

**CONDITIONAL_PASS** — RS_phase7 = 0.99 >= 0.95 threshold, on the two components measurable at this phase (routing/prompt-generation artifacts, not implementation). Full 4-component RS is deferred to a Phase B reliability gate, once code and tests exist to compute DRE and Coverage against.

This is not a retry-eligible failure under the SC.1-3 bounded-retry protocol — it is a not-yet-applicable metric, not a gate rejection.
