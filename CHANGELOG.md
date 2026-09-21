# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [UNRELEASED]

### Added
- FR-013 predicate/subject_scope/retrieval_context_hash wire schema design for `POST /memory/write`
  Zone 3/5 routing (GitHub #23), 7 independently-reviewed revisions, v7 at 9.8/10, marked
  IMPLEMENTATION-READY (`docs/phase-1.5-api/fr013-predicate-schema-design.md`). No implementation
  performed yet — Sprint 4 planning (AR.0-AR.5/IR.1 bundle + Jira tickets) in progress.
- `jira_list_comments` and `jira_delete_comment` tools added to the `mcp-jira-api` MCP server
  (separate repo) to close a real duplicate-comment incident found during Sprint 3 review.

### Fixed
- `dashanan_zone8_role` and `dashanan_semantic_role` were never granted to the app login role
  (GitHub #22, #24) — every real Zone 8/Zone 3 write failed with `InsufficientPrivilege` against a
  live Postgres deployment. Fixed with a real docker-compose regression test.
- `SqlEpisodicRepository.append()` crashed and never committed against real Postgres — found and
  fixed during the first real API+Postgres end-to-end test round.

### Known gaps (disclosed, not silently carried)
- `zone_hint` comparisons in `_do_write` use non-canonical bare strings that don't match the
  `ZoneId` wire enum (GitHub #25) — pre-existing, unrelated to FR-013, disclosed not fixed.
- Zone 3/5 writes remain unreachable via the live API until FR-013's design (above) is implemented
  (GitHub #23).
- `AR1-S3-G3`: the API host shares one `psycopg.Connection` across zones; not safe for concurrent
  writes without real connection pooling (FR-015 follow-up, not started).

## [0.3.0] - 2026-09-21

### Added
- Real FastAPI/HTTP wire host (`src/dashanan/api/`) implementing `openapi.yaml`'s contract: JWT
  bearer auth, tenant-scope enforcement, 32 endpoints (FR-014, closes the DSHN-60 composition-root
  gap).
- Shape B deployment infrastructure: Postgres/Redis/Qdrant/OpenSearch/S3 via `docker-compose.yml`,
  env-sourced settings, a real migration runner with per-zone least-privilege DB roles (FR-015).
- FR-013 Zone 3/5 conflict-detection sweep wired into the real write path
  (`conflict_aware_zone_writes.py`), closing DSHN-53's disclosed gap for the application layer
  (API-layer reachability remained open — see GitHub #23 above).
- FR-011 cross-zone storage-adapter substitutability verification across all 8 shipped zone
  adapters.
- First real end-to-end integration suite wiring the live API host to a real, live Postgres
  connection (`tests/integration/test_api_real_postgres_e2e_dash3.py`) — proved real concurrency
  behavior (documented AR1-S3-G3), real Zone 8 archival, real erasure cascade reach.
- 4 advisory security reviews (crypto, API, cloud) on top of the already-approved Sprint 3 stories,
  finding and fixing 9 real, independently-confirmed defects.

### Fixed
- HMAC attestation forgery (Critical, F.6 security audit) closed by the new JWT bearer auth layer
  and the wired FR-013 conflict-detection sweep — both re-verified as genuinely mitigated.
- A real DSHN-55 privilege-bypass regression, a hallucinated fact-check failure, and multiple
  crypto/API/infra findings from the advisory review round.

## [0.2.0] - 2026-09-20

### Added
- Semantic Memory (Zone 3), Procedural Memory (Zone 4), and Entity Memory (Zone 5) core stores
  with ownership/classification gates (`EntityOwnershipSpecification`, FR-003/FR-004/FR-005).
- Consolidation Memory (Zone 8): object-store archival, hot-manifest index, Compressed-to-Archived
  rotation closure (FR-008).
- DPDP erasure-cascade reach extended toward Zone 8 (crypto-shredding, India residency).
- Cross-zone adversarial test round; found and fixed 2 real defects (subject-index registration
  gap, unprovisioned-tenant gap in Zone 8 stores).

## [0.1.0] - 2026-09-19

### Added
- Initial Dashanan dynamic memory orchestration engine: Memory Orchestrator core facade, Working
  Memory (Zone 1), Episodic Memory (Zone 2), Retrieval-Index (Zone 6), Provenance/Audit (Zone 7).
- Memory Score six-term computation engine and Active-to-Compressed rotation state machine
  (FR-012).
- Cross-cutting provenance write-path enforcement (WAL/outbox gate, FR-009/FR-010).
- Full Phase 0-8 planning artifact set (HLD, SRS — 12 FRs/11 NFRs/17 ACs, Sprint 1 Jira backlog,
  AR.0-AR.5/IR.1-IR.5 routing/alignment pipeline) preceding all implementation.
