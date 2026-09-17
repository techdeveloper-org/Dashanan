# Dashanan

**Dashanan** (दशानन — "the ten-headed one") is a reusable, engine-agnostic **dynamic memory orchestration engine** for AI / LLM context systems.

Named after the mythological figure with ten heads, Dashanan gives any AI system that plugs it in a form of **unlimited, structured memory**: instead of stuffing everything into one flat context window, it distributes and rotates context across **8 specialized, dynamically managed memory zones**, orchestrating what moves between them, what gets compressed, what gets promoted, and what gets retrieved — at a depth a single context window cannot sustain on its own.

> **"Wait, Dashanan means TEN heads — why only 8 zones?"** Good catch, this is intentional, not a mistake. The project keeps the mythological **10-headed** name, but during architecture review two pairs of the original 10-zone technical design were deliberately consolidated into one zone each — the *name* stayed at 10, the *engineering* went to 8:
> - **Temporal** merged into **Episodic** (time-anchoring became a per-entry attribute, not its own zone)
> - **Summary/Compressed** merged into **Consolidation** (compression is a *mechanism* Consolidation applies to aged data, not a separate storage zone competing with it)
>
> Full rationale: [`docs/orchestration/01-vision-and-prd.md`](docs/orchestration/01-vision-and-prd.md).

> ⚠️ **Status (updated 2026-09-17):** Phases 0 through 8 of the architecture/planning pipeline are complete and approved — PRD, HLD (15 ADRs), OpenAPI 3.1.0 contract, [`SRS.md`](SRS.md), 7 UML/Draw.io diagrams, a live Jira board (`DSHN` project, Sprint 1 planned), agent-task routing, and pre-implementation alignment are all done and reviewed (see `docs/` for the full trail). **STOP 8 reached: IMPLEMENTATION READY.** Phase B — actual code implementation — has **not** started and is intentionally out of scope until the user reviews everything above and gives explicit go-ahead.

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

## Repository layout

```
Dashanan/
├── README.md                    <- this file
├── SRS.md                       <- Software Requirements Specification (12 FRs, 11 NFRs, 17 ACs)
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

TBD.
