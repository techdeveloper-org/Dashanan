# Dashanan

**Dashanan** (दशानन — "the ten-headed one") is a reusable, engine-agnostic **dynamic memory orchestration engine** for AI / LLM context systems.

Named after the mythological figure with ten heads, Dashanan gives any AI system that plugs it in a form of **unlimited, structured memory**: instead of stuffing everything into one flat context window, it distributes and rotates context across **8 specialized, dynamically managed memory zones**, orchestrating what moves between them, what gets compressed, what gets promoted, and what gets retrieved — at a depth a single context window cannot sustain on its own.

> **"Wait, Dashanan means TEN heads — why only 8 zones?"** Good catch, this is intentional, not a mistake. The project keeps the mythological **10-headed** name, but during architecture review two pairs of the original 10-zone technical design were deliberately consolidated into one zone each — the *name* stayed at 10, the *engineering* went to 8:
> - **Temporal** merged into **Episodic** (time-anchoring became a per-entry attribute, not its own zone)
> - **Summary/Compressed** merged into **Consolidation** (compression is a *mechanism* Consolidation applies to aged data, not a separate storage zone competing with it)
>
> Full rationale: [`docs/orchestration/01-vision-and-prd.md`](docs/orchestration/01-vision-and-prd.md).

> ⚠️ **Status (updated 2026-09-21, v0.3.0 — see [`CHANGELOG.md`](CHANGELOG.md) for the full version
> history):** Three sprints are implemented and merged. **Sprint 1** (Working/Episodic/
> Retrieval-Index/Provenance zones, Memory Score, rotation state machine, WAL/outbox gate) landed
> 2026-09-19. **Sprint 2** (Semantic/Procedural/Entity/Consolidation zones — Zones 3/4/5/8) landed
> 2026-09-20. **Sprint 3** landed 2026-09-21: a real FastAPI wire/API host
> (`src/dashanan/api/`, 32 endpoints, JWT bearer auth, closes the security-wiring gap this README
> used to flag as open — see `DSHN-60`/[GitHub #20](https://github.com/techdeveloper-org/Dashanan/issues/20),
> now closed), Shape B deployment infrastructure (`docker-compose.yml` — Postgres/Redis/Qdrant/
> OpenSearch/S3, a real migration runner with per-zone least-privilege DB roles), the FR-013
> conflict-detection sweep wired into the application-layer write path, and the first real
> end-to-end integration suite wiring the live API host to a real, live Postgres connection.
>
> **What's next — FR-013 API-layer reachability (GitHub [#23](https://github.com/techdeveloper-org/Dashanan/issues/23)):**
> Zone 3/5 writes are wired at the application layer (above) but still unreachable through the
> public API — `POST /memory/write` has no wire field for the `predicate`/`subject_scope`/
> `retrieval_context_hash` a Zone 3/5 write needs. The schema fix is fully designed and
> **IMPLEMENTATION-READY** (`docs/phase-1.5-api/fr013-predicate-schema-design.md`, v7, 9.8/10,
> 7 independent review rounds), and a real Sprint 4 planning pass (AR.0→AR.1→AR.3→execution-plan→
> Phase C gate→AR.5→IR.1, mirroring Sprint 1-3's own pipeline) is in progress before implementation
> starts — see `docs/phase-7-routing/sprint4_*` once complete.
>
> **Known, disclosed gaps** (see [`CHANGELOG.md`](CHANGELOG.md)'s `[UNRELEASED]` section for the
> current, authoritative list — do not treat this README bullet as more current than that file):
> `_do_write`'s `zone_hint` comparisons don't match their own OpenAPI wire enum
> ([GitHub #25](https://github.com/techdeveloper-org/Dashanan/issues/25), pre-existing, disclosed
> not silently fixed); `AR1-S3-G3` — the API host shares one `psycopg.Connection` across zones,
> not safe for concurrent writes without real pooling (FR-015 follow-up, not started).

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

Sprints 1-3 are implemented (see the Status callout above and Local development below) — this is
a real, installable, tested Python library with a real FastAPI wire host and Shape B deployment
infrastructure, not a pre-implementation planning repo. FR-013's remaining API-layer wiring
(GitHub #23) is designed and implementation-ready, not yet implemented.

Recommended reading order for someone new to the project:
1. **This README** — problem statement, the 8-zone model, goals.
2. [`SRS.md`](SRS.md) — the canonical requirements specification (15 FRs, 15 NFRs, 23 ACs): what the system is required to do.
3. [`docs/phase-1-architecture/HLD.md`](docs/phase-1-architecture/HLD.md) — the High-Level Design: how it's built, with 19 ADRs covering every major technology and mechanism choice.
4. [`docs/phase-1.5-api/openapi.yaml`](docs/phase-1.5-api/openapi.yaml) — the concrete API contract.
5. The remaining `docs/phase-*` directories (see Repository layout below) for validation, sprint planning, and pre-implementation routing, in phase order.

## Local development

**Sprint 1** (2026-09-19): 11 stories under `src/dashanan/`, Shape A (embedded library, in-process,
no network) per HLD Section 2 — Memory Orchestrator facade, Zones 1/2/6/7, MemoryScore engine,
rotation state machine, WAL/outbox write-path gate. DASH-STORY-004/005/006/007 initially failed a
real adversarial P1 security review (a live-reproduced cross-tenant data leak, a forgeable
write-path attestation, an unenforced append-only DB guarantee, a non-functional DPDP erasure
claim) and were remediated and re-verified before merging — `docs/phase-A6-harness/` for the
harness policy, Jira `DSHN-55`..`DSHN-58` for the trail.

**Sprint 2** (2026-09-20): Zones 3/4/5/8 (Semantic/Procedural/Entity/Consolidation) with
`EntityOwnershipSpecification` ownership/classification gates, object-store archival, and DPDP
erasure-cascade extension.

**Sprint 3** (2026-09-21): a real FastAPI wire host (`src/dashanan/api/` — JWT bearer auth,
tenant-scope enforcement, 32 endpoints), Shape B deployment infra (`docker-compose.yml`, a real
migration runner binding per-zone least-privilege DB roles to real env-sourced LOGIN credentials),
the FR-013 conflict-detection sweep wired into the application-layer write path, and the repo's
first real end-to-end suite (`tests/integration/test_api_real_postgres_e2e_dash3.py`) wiring the
live API host to a real, live Postgres connection via `docker compose`.

```bash
# Requires Python 3.12+ (NFR-001)
pip install -e ".[dev]"
python -m pytest -v

# Real Postgres/Redis/Qdrant/OpenSearch/S3 stack (Shape B) for the full integration suite,
# including tests/integration/test_api_real_postgres_e2e_dash3.py:
docker compose up -d
```

1719 tests pass as of Sprint 3 (2026-09-21), including an AST-based architecture-fitness test
enforcing HLD 3.0 invariant 1 (no `dashanan/domain/**` module may import
`dashanan/infrastructure/**`) and real Docker-Postgres regression tests for the DB-role-wiring
fixes (GitHub #22/#24). Run `python -m pytest -v` yourself to reconfirm the current count — this
number is updated manually and can drift; do not treat it as more authoritative than a fresh test
run.

## Repository layout

```
Dashanan/
├── README.md                    <- this file
├── SRS.md                       <- Software Requirements Specification (15 FRs, 15 NFRs, 23 ACs)
├── VERSION                      <- single-line semver, source of truth for the current release
├── CHANGELOG.md                 <- Keep a Changelog format, full version history
├── docker-compose.yml            <- Shape B stack: Postgres/Redis/Qdrant/OpenSearch/S3
├── docs/
│   ├── orchestration_prompt.md  <- index into the 3-file orchestration bundle
│   ├── orchestration/           <- 01-vision-and-prd.md, 02-architecture-workflow.md, 03-agent-registry.md
│   ├── phase-1-architecture/    <- HLD.md, context-delivery-plan.md
│   ├── phase-1.5-api/           <- openapi.yaml, FR-013 predicate schema design, + test/review artifacts
│   ├── phase-2-validation/      <- joint BA/PM/SA blueprint validation
│   ├── phase-6-sprint-planning/ <- backlog_draft.json, sprint_plan.json, jira_setup_report.json
│   ├── phase-7-routing/         <- AR.0-AR.5 agent-task routing (Sprints 1-4) + implementation prompts
│   └── phase-8-alignment/       <- pre-implementation self-review + consensus gate records
├── uml/                          <- Mermaid diagrams (context, component, deployment, class, state, data-flow, sequence)
├── drawio/                       <- same diagrams as editable .drawio XML
├── src/dashanan/
│   ├── api/                      <- Sprint 3: real FastAPI wire host (JWT auth, 32 endpoints)
│   ├── domain/, application/, infrastructure/  <- Sprints 1-3: all 8 zones, orchestrator, DB layer
├── tests/                        <- 1719 tests, including tests/integration/ (real Postgres e2e, cross-zone, adversarial suites)
└── pyproject.toml                <- pip install -e ".[dev]" — see Local development below
```

## License

Licensed under the [MIT License](LICENSE).
