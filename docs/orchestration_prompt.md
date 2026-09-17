# DASHANAN — ORCHESTRATION BUNDLE (INDEX)

**Dashanan** is a reusable, embeddable dynamic memory orchestration engine for AI/LLM context systems — 8 specialized memory zones, a central Memory Score formula driving promotion/compression/archival between them, giving any AI system that embeds it a much larger effective context than its native window. **Enterprise complexity, 24 real agents dispatched** (not the library's full 528), covering Phases 0 → 1 → 1.5 → 2 → 5 → 6 → 7 → 8 (architecture through pre-implementation alignment — Phase B/coding is explicitly *not* included; this bundle stops at Phase 8's `IMPLEMENTATION READY` STOP for user sign-off).

This was originally a single ~2,400-line file. Per architecture review (2026-09-17), it's now split into 3 focused files, each self-contained enough to read alone:

| File | What it covers |
|---|---|
| [`orchestration/01-vision-and-prd.md`](orchestration/01-vision-and-prd.md) | Project summary, the 8-zone taxonomy (revised from the original 10 — consolidation rationale included), the **Memory Score formula + rotation-policy algorithm** (the core engine algorithm — the single biggest gap in the first draft, now fixed here), the task definition, and constraints. |
| [`orchestration/02-architecture-workflow.md`](orchestration/02-architecture-workflow.md) | Routing/complexity classification, the full Phase 0-8 execution plan with gates, the bounded self-correction/escalation protocol (SC.1-3, ADR-9 — 3 iterations then mandatory human escalation, never an infinite loop), Team Alignment resolutions, Interface Contracts, Resilience & QA rules (RS/NLI/FactScore/Coverage thresholds relaxed from an impossible exact `1.0` to a realistic `>= 0.95`), the token/cost estimate (~1.2-1.4M tokens / ~$15-35 per pass, ~$30-70 worst-case with bounded retries), and the execution summary. |
| [`orchestration/03-agent-registry.md`](orchestration/03-agent-registry.md) | The 24-agent roster table, and the full dispatchable multi-agent prompt bundle — one `---persona---`-blocked prompt per agent, ready to pass directly as an `Agent`/`Task` tool call. Phase 5's diagram scope is reduced from 26 diagrams (13 UML + 13 Draw.io) to **7 essential diagrams** (Context, Component, Sequence, Deployment, Data Flow, State, Class). |

## What changed from the first draft

Five fixes applied after user architecture review: (1) 8-zone consolidation (was 10 — Temporal merged into Episodic, Summary/Compressed merged into Consolidation); (2) the Memory Score formula + rotation-policy algorithm, previously missing entirely; (3) diagram scope cut from 26 to 7 essential types; (4) agent-roster noise trimmed — the repeated "528 agents loaded" line in every block replaced with one summary table, and this project's real 24-agent roster made explicit; (5) this 3-file split, so no single file is a 2,400-line wall of text. Separately, the `RS/NLI/FactScore/Coverage = 1.0` mandate was relaxed to `>= 0.95` with bounded (3-iteration, ADR-9) retry-then-human-escalation — fixed both here and in the upstream `claude-global-library/ORCHESTRATION_TEMPLATE.md` (issue #163), since an exact `1.0` bar was never practically reachable and risked endless rejection loops.

## How to use this

Paste all 3 files (in order: 01 → 02 → 03) into a fresh Claude Code conversation as `orchestrator-agent` to begin execution, or continue dispatching phase-by-phase from a live session, as already done for Phase 0 in this project's working session.

Repo creation was handled separately and directly (public, `techdeveloper-org/Dashanan`) — not part of this bundle's own scope, per the same reasoning as before.
