# Dashanan

**Dashanan** (दशानन — "the ten-headed one") is a reusable, engine-agnostic **dynamic memory orchestration engine** for AI / LLM context systems.

Named after the mythological figure with ten heads, Dashanan gives any AI system that plugs it in a form of **unlimited, structured memory**: instead of stuffing everything into one flat context window, it distributes and rotates context across **8 specialized, dynamically managed memory zones**, orchestrating what moves between them, what gets compressed, what gets promoted, and what gets retrieved — at a depth a single context window cannot sustain on its own.

> **"Wait, Dashanan means TEN heads — why only 8 zones?"** Good catch, this is intentional, not a mistake. The project keeps the mythological **10-headed** name, but during architecture review two pairs of the original 10-zone technical design were deliberately consolidated into one zone each — the *name* stayed at 10, the *engineering* went to 8:
> - **Temporal** merged into **Episodic** (time-anchoring became a per-entry attribute, not its own zone)
> - **Summary/Compressed** merged into **Consolidation** (compression is a *mechanism* Consolidation applies to aged data, not a separate storage zone competing with it)
>
> Full rationale: [`docs/orchestration/01-vision-and-prd.md`](docs/orchestration/01-vision-and-prd.md).

> ⚠️ **Status (updated 2026-09-17):** Phases 0 through 8 of the architecture/planning pipeline are complete and approved — PRD, HLD (19 ADRs), OpenAPI 3.1.0 contract, [`SRS.md`](SRS.md), 7 UML/Draw.io diagrams, a live Jira board (`DSHN` project, Sprint 1 planned), agent-task routing, and pre-implementation alignment are all done and reviewed for all 11 Sprint 1 stories — the original 10 (`ir5_alignment_verdict.json`) plus DASH-STORY-011's own genuine supplemental review, added 2026-09-19 (`ir5_alignment_verdict.json`'s `story_011_supplemental_verdict`); see `docs/` for the full trail). **STOP 8 reached: IMPLEMENTATION READY.** Phase B — actual code implementation — has **not** started and is intentionally out of scope until the user reviews everything above and gives explicit go-ahead.

> **Implementation Ready — with these documented pre-implementation decisions pending** (added 2026-09-18, per a repo-wide consistency audit). "Implementation Ready" above means the planning pipeline's own gates all passed; it does not mean every underlying architecture question is closed. The following items in [`docs/phase-1-architecture/HLD.md`](docs/phase-1-architecture/HLD.md)'s Open Architecture Questions table are genuinely still open and should be resolved (or explicitly accepted as-is) before or early in Phase B:
> - **OAQ-10** — DPDP erasure via crypto-shredding of an append-only audit store. Status: Proposed. Whether key destruction constitutes erasure under DPDP Act 2023 requires legal confirmation, not an architect's judgment.
> - **OAQ-13** — All NFR-004 capacity/latency numbers (2,000 read QPS / 5,000 write QPS / 99.9% availability, Profile C) are `[ASSUMED]`, not measured. Must be confirmed before further scaling decisions build on them.
> - **OAQ-18** — Regulated-identifier detection at the write gate (ADR-017). Status: Partially resolved — the identifier set (Aadhaar/PAN/payment card) has a citation-backed recommendation, but counsel ratification and the formal fail-open/fail-closed choice remain open.
> - **OAQ-20** — Per-zone Frequency `f_cap` defaults (§12G). Status: Partially resolved — `[ASSUMED]` starting defaults only (Zone 1: 20, Zone 2: 100), not validated against real access-count telemetry.
> - **OAQ-22** — `context.assemble` RPC transport shape at large token budgets (ADR-019). Status: Partially resolved — unary transport is adopted, but the specific streaming-mandatory token-count threshold stays open pending a benchmarking pass.

## What problem does this solve?

Every AI/LLM application eventually hits the same wall: the model's context window is finite, but real conversations, agents, and long-running tasks generate context that keeps growing — conversation history, retrieved documents, tool outputs, intermediate reasoning, user preferences, long-term facts. Naive approaches either:
- truncate/forget older context (losing important information), or
- stuff everything into the window (wasting tokens, degrading attention, hitting hard limits).

Dashanan instead treats memory as a **living, orchestrated system** with multiple specialized zones, each with its own retention, compression, promotion/demotion, and retrieval policy — so an AI system using it can hold a much larger *effective* context, without needing a larger context window.

## The Eight Heads — memory zones (working model, subject to the architecture phase)

1. **Working** — the active, in-flight context for the current task
2. **Episodic** — recent turn-by-turn interaction history, time-anchored (absorbs the former separate "Temporal" zone as a per-entry attribute)
3. **Semantic** — durable facts and knowledge extracted from interactions
4. **Procedural** — learned task/workflow patterns and how-to knowledge
5. **Entity** — structured knowledge about people, objects, and concepts referenced over time
6. **Retrieval-Index** — the searchable index layer used to pull relevant memory back in
7. **Provenance / Audit** — where each piece of memory came from and how it was derived
8. **Consolidation** — long-term, cross-session consolidated store (absorbs the former separate "Summary/Compressed" zone: compression is a mechanism Consolidation applies, not its own zone)

A central **Memory Score** formula (`MemoryScore = w1·Recency + w2·Frequency + w3·Importance + w4·UserAffinity + w5·TaskRelevance + w6·ProvenanceConfidence`) drives rotation between these zones — promoted, compressed, or archived — under an orchestration policy, rather than context living statically in one place. See `docs/orchestration/01-vision-and-prd.md` for the full formula and rotation-policy definition.

## Goals

- **Engine-agnostic**: usable as an embeddable library/SDK by any AI system or agent framework, not tied to one model provider.
- **Deep, not flat**: structured, multi-zone memory instead of one undifferentiated context blob.
- **Scales to long-running context**: designed to support much longer effective sessions/tasks than a single context window allows.
- **Reusable as a product**: a general-purpose memory engine other AI products can adopt.

## Getting started

This repository is currently **pre-implementation** — Phases 0 through 8 (architecture and planning) are complete, but Phase B (actual code) has not started yet (see the Status callout above). There is no package to install or server to run yet; getting started today means getting oriented in the design, not running code.

Recommended reading order for someone new to the project:
1. **This README** — problem statement, the 8-zone model, goals.
2. [`SRS.md`](SRS.md) — the canonical requirements specification (13 FRs, 15 NFRs, 23 ACs): what the system is required to do.
3. [`docs/phase-1-architecture/HLD.md`](docs/phase-1-architecture/HLD.md) — the High-Level Design: how it's built, with 19 ADRs covering every major technology and mechanism choice.
4. [`docs/phase-1.5-api/openapi.yaml`](docs/phase-1.5-api/openapi.yaml) — the concrete API contract.
5. The remaining `docs/phase-*` directories (see Repository layout below) for validation, sprint planning, and pre-implementation routing, in phase order.

Local development / build / run instructions will be added here once Phase B implementation begins and there is actual code to set up.

## Repository layout

```
Dashanan/
├── README.md                    <- this file
├── SRS.md                       <- Software Requirements Specification (13 FRs, 15 NFRs, 23 ACs)
├── docs/
│   ├── orchestration_prompt.md  <- index into the 3-file orchestration bundle
│   ├── orchestration/           <- 01-vision-and-prd.md, 02-architecture-workflow.md, 03-agent-registry.md
│   ├── phase-1-architecture/    <- HLD.md, context-delivery-plan.md
│   ├── phase-1.5-api/           <- openapi.yaml + test/review artifacts
│   ├── phase-2-validation/      <- joint BA/PM/SA blueprint validation
│   ├── phase-6-sprint-planning/ <- backlog_draft.json, sprint_plan.json, jira_setup_report.json
│   ├── phase-7-routing/         <- AR.0-AR.5 agent-task routing + 30 CoT implementation prompts
│   └── phase-8-alignment/       <- pre-implementation self-review + consensus gate records
├── uml/                          <- 7 Mermaid diagrams (context, component, deployment, class, state, data-flow, sequence)
├── drawio/                       <- same 7 diagrams as editable .drawio XML
└── (source code)                 <- NOT YET CREATED — Phase B implementation awaits explicit user go-ahead
```

## License

Licensed under the [MIT License](LICENSE).
