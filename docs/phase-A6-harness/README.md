# Phase A.6 / A.6.1 — Harness Gate (activated 2026-09-19)

Per `claude-global-library/ORCHESTRATION_TEMPLATE.md`'s D21 decision node (Enterprise +
AI/LLM-stack projects require a Harness Gate), this directory holds Dashanan's real harness
policy artifacts:

| File | Phase | What it is |
|---|---|---|
| [`harness_control_policy.json`](harness_control_policy.json) | A.6 | Stop conditions, token-budget ceilings, story-level circuit-breaker (Fix→QA→Review→P1 loop), transient agent-call retry/backoff, tool-call mediation, dynamic re-routing |
| [`resource_elastic_policy.json`](resource_elastic_policy.json) | A.6.1 | Multi-objective (Token-Budget × Latency × Accuracy) Sonnet↔Opus switching policy |

Both were produced by real `harness-mathematics-expert` and `harness-engineering-architect`
persona dispatches (their `agent.md` + every mandatory skill read in full), grounded in this
project's actual observed Phase B execution data (Waves 2-5: 38 agents / 12,479,018 tokens / 0
errors; P1 remediation: 26 agents / 6,990,891 tokens / 0 errors, 2 of 4 stories needing a genuine
2nd attempt) — not invented placeholder values. Every field in both files is tagged `DERIVED`
(grounded in real math/data) or `APPROXIMATED` (engineering judgment pending more telemetry) via
a `field_provenance_legend`; nothing derived is presented as approximated or vice versa.

## Retroactive-gap disclosure

This gate is being activated **after** Phase B implementation already substantially happened —
DASH-STORY-001 through DASH-STORY-010, the Waves 2-5 workflow, and the full P1 security
remediation round all ran and merged (`cba559f`, `d888536`) before this policy existed. This is
an honest, disclosed gap, not a silent backdate: those runs are not retroactively claimed to have
been governed by this policy.

## How enforcement actually works in this project

The template's literal design assumes an `orchestrator-agent` runtime that mechanically injects
`harness_control_policy.json` as a mandatory header block into every Phase B agent prompt, with
the orchestrator blocking dispatch if the policy is missing. This project's actual dispatch
mechanism throughout this whole session has been the `Workflow` and `Agent` tools, which have no
such literal prompt-injection layer. Going forward, enforcement is **self-applied**: any future
`Workflow`-based Phase B dispatch in this project (e.g. the still-pending cross-zone integration
test phase, or future-sprint zone stories) is expected to honor this policy's story-level
circuit-breaker (k=2 attempts before escalating to the user — already the working pattern used in
the P1 remediation round), token-budget ceilings, and retry/backoff schedule explicitly, and to
say so at the point of dispatch, rather than claiming a mechanical enforcement that doesn't exist
here.

## Not yet activated

**Phase H** (`harness-evaluation-engineer`'s eval/regression gate, McNemar/Krippendorff-based,
blocking Phase F) is a separate, later-stage gate and is **not** activated by this round — see
`docs/orchestration/02-architecture-workflow.md`'s explicit Phase H line, which remains DEFERRED.
