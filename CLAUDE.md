# CLAUDE.md

## Project Overview

**Dashanan** is a reusable, engine-agnostic dynamic memory orchestration engine for AI/LLM context
systems. It distributes and rotates context across 8 specialized memory zones (Working, Episodic,
Semantic, Procedural, Entity, Retrieval-Index, Provenance/Audit, Consolidation) behind a central
Memory Orchestrator, so a host AI system can sustain a much larger effective context than a single
context window allows. See `README.md` for the full problem statement and the 8-zone model, and
`SRS.md` for the canonical requirements (13 FRs, 15 NFRs, 23 ACs).

Current version: see `VERSION` (source of truth). Full release history: `CHANGELOG.md`.

## Architecture & Structure

Layered/hexagonal architecture under `src/dashanan/`:

```
src/dashanan/
├── domain/          <- zone entities, MemoryScore, rotation state machine — no infra imports
├── application/      <- Memory Orchestrator facade, cross-zone routing, conflict-detection sweep
├── infrastructure/    <- storage adapters, SQL repositories (infrastructure/sql/), migration runner
└── api/               <- FastAPI wire host: JWT bearer auth, 32 endpoints per docs/phase-1.5-api/openapi.yaml
```

An AST-based architecture-fitness test enforces HLD 3.0 invariant 1: no `dashanan/domain/**`
module may import `dashanan/infrastructure/**`.

Local deployment (Shape B) runs Postgres/Redis/Qdrant/OpenSearch/S3 via `docker-compose.yml`, with
per-zone least-privilege DB roles applied by the migration runner.

Design/requirements docs live under `docs/` (`docs/orchestration/` for vision/PRD,
`docs/phase-1-architecture/HLD.md` for the 19 ADRs, `docs/phase-1.5-api/` for the OpenAPI contract
and FR-013's predicate-schema design). Diagrams are under `uml/` (Mermaid) and `drawio/` (editable
XML), both auto-generated from code.

## Development Guidelines

- Install: `pip install -e ".[dev]"` (Python >=3.12; dependencies: `cryptography`, `psycopg[binary]`,
  `fastapi`, `uvicorn`, `pyjwt`).
- Test: `python -m pytest -v` (`tests/` — embedded/in-memory suite; `tests/integration/` requires
  `docker compose up -d` for the real Postgres/Redis/Qdrant/OpenSearch/S3 stack).
- Type-check: `mypy` is configured `strict = true` for `python_version = "3.12"`.
- CI (`.github/workflows/ci.yml`): runs the full test suite on Python 3.12/3.13 on every push/PR,
  plus a `pip-audit` dependency vulnerability scan (SCA gate).
- Issue-first workflow: open a GitHub issue before a fix/feature, reference its number in the
  commit message, close it from the commit. See `README.md`'s Contributing section.
- Requirements changes go into `SRS.md`'s append-only Change Log (§6); notable code changes go
  into `CHANGELOG.md`'s `[UNRELEASED]` section before being rolled into a versioned release.
