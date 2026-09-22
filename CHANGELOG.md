# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [UNRELEASED]

### Added
- Sprint 5 planning bundle for the two remaining disclosed gaps behind this project's ~25-30%
  shortfall against its SRS v1 target-state — **docs only, no code changed, nothing implemented.**
  Two new stories (DASH-STORY-027 for AC-013's full DPDP crypto-shredding erasure cascade;
  DASH-STORY-028 for AR1-S3-G3's API-host connection pooling), each with a technical design doc
  (`docs/phase-1.5-design/`), an HLD update (new ADR-020 for connection pooling; a DPDP-2/DPDP-3
  implementation-status note — OAQ-10 itself is unchanged), and a full Phase 6/7/8 routing bundle
  (backlog, AR.0-AR.5, IR.1) mirroring Sprints 1-4's own convention. The bundle's own AR.5/IR.1
  files honestly flag that this planning pass was a same-session self-check, not a genuinely
  dispatched multi-agent review — a real Phase C gate and at least one independent design-doc
  review round are recommended before Phase B (implementation) is authorized. **Twelve rounds** of
  genuinely dispatched, independent `solution-architect` review followed the same day, ending in a
  clean **APPROVE** for both stories (round 12) — see the note at the end of this bullet on why an
  exact round count is deliberately not used as this entry's own source of truth going forward.
  Rounds 1-5
  found and fixed progressively smaller issues: an over-scoped compliance-role privilege claim
  (Zones 1/2/3 claimed, only Zone 2 actually needed) repeated across 4 files; a missing
  `subject_item_index` cleanup step; an undisclosed ADR-020/HLD-Section-6 circuit-breaker
  exception; a stale Definition-of-Done table; a `ZoneRepository`-vs-real-`Zone2EvictionPort`
  naming error; an under-sized context budget; a design-doc internal contradiction about Zone 4/5
  scope; and the review process's own audit trail falling behind. **Round 6 found something
  bigger**: the DPDP design doc's entire "what ships today" baseline was false. It claimed
  `DELETE /tenants/{tenant_id}/subjects/{subject_id}` was contract-only with no handler and that
  no mechanism touched Zones 1/3/4/5/8 — both false. The endpoint is already live and wired to a
  real, tested Zone-8-only crypto-shredding service, and a separate, more complete orchestrator
  (`UnifiedSubjectErasureOrchestrator`, already built in a prior sprint) fans out to Zones
  2/3/5/6/8 but isn't wired into that endpoint yet. DASH-STORY-027 was re-scoped from "build the
  full cascade from scratch" to "wire and extend what already exists" — 13 SP → 8 SP, sprint
  total 21 → 16 SP. Rounds 7-8 independently re-verified round 6's core technical claims via fresh
  direct code reads (confirmed correct) and then hunted down — and fixed — every place the v1→v2.0
  version bump, the corrected SP total, and the corrected "what ships today" story had not yet
  propagated: the design doc's own Change Log, `sprint5_sprint_plan.json`'s stale 21-SP total,
  `sprint5_ar0_routing_index.json`'s pre-round-6 framing (flagged superseded rather than rewritten),
  and — most notably — **HLD.md itself**, the project's own top-level architecture document, which
  had repeated the exact same disproven "never calling zone8_crypto_shredding.py" claim in its own
  Section 10 until round 8 caught and corrected it. Rounds 9-11 swept the bundle repeatedly for
  remaining stale "v1 DRAFT"/round-count references in free-text decision fields and this very
  changelog — a self-referential class of drift each fixing round's own write necessarily leaves
  one round further behind, per round 12's own explicit diagnosis — confirming throughout that the
  technical substance (story points 8+8=16, AC count 16, Zone scoping, the DASH-STORY-025
  dependency — independently corroborated against real git history in round 10, and re-confirmed
  clean on 4 separate independent passes: rounds 6, 7, 10, 12) has been stable and unchanged since
  round 6. Round 12 gave a final, clean **APPROVE** for the bundle and explicitly recommended
  against further review rounds chasing the self-referential round-count string — a one-time,
  out-of-loop fix (this note) is the correct remedy, not another dispatched round. DASH-STORY-028
  (connection pooling) received a clean **APPROVE** as early as round 4 and needed no further
  changes through round 12. See `docs/phase-8-alignment/sprint5_ir1_agent_flags.json`'s
  `resolution_note_2026_09_22` for the fullest available round-by-round account (itself subject to
  the same one-round-behind citation lag by nature, not by omission).
- FR-013 predicate/subject_scope/retrieval_context_hash wire schema (GitHub #23) — designed,
  7 independently-reviewed revisions, v7 at 9.8/10, IMPLEMENTATION-READY
  (`docs/phase-1.5-api/fr013-predicate-schema-design.md`) — is now wired into the real write path:
  `POST /memory/write` normalizes `zone_hint` via `_WIRE_TO_DOMAIN_ZONE` and `_do_write` routes
  the new fields into Zone 3/5's conflict-detection sweep. Sprint 4 planning (AR.0-AR.5/IR.1
  bundle + Jira tickets) is complete and this bundle's implementation has landed.
- `jira_list_comments` and `jira_delete_comment` tools added to the `mcp-jira-api` MCP server
  (separate repo) to close a real duplicate-comment incident found during Sprint 3 review.

### Fixed
- `dashanan_zone8_role` and `dashanan_semantic_role` were never granted to the app login role
  (GitHub #22, #24) — every real Zone 8/Zone 3 write failed with `InsufficientPrivilege` against a
  live Postgres deployment. Fixed with a real docker-compose regression test.
- `SqlEpisodicRepository.append()` crashed and never committed against real Postgres — found and
  fixed during the first real API+Postgres end-to-end test round.
- `SqlProvenanceRepository` and `SqlSemanticRepository` crashed and never committed against real
  Postgres — found and fixed alongside the `SqlEpisodicRepository` issue above.
- `zone_hint` comparisons in `_do_write` used non-canonical bare strings that didn't match the
  `ZoneId` wire enum (GitHub #25) — normalized via `_WIRE_TO_DOMAIN_ZONE`.

### Known gaps (disclosed, not silently carried)
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
