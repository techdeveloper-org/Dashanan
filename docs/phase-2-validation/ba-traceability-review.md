# BA Traceability Review — PRD (12 FRs) vs. Approved HLD

**Document ID:** BA-TRACE-20260917-01
**Phase:** 2 (Joint Blueprint Validation)
**Author:** business-analyst-agent
**Skill applied:** requirements-traceability-core (bipartite RTM, §1–§2; orphan detection, §2.2/M1)
**Inputs:** `docs/phase-0-output/PRD.md` (PRD-20260917-01 v1.0.0), `docs/phase-1-architecture/HLD.md` (HLD-20260917-01 v1.0.0), `docs/phase-0-output/product-strategy-and-prioritization.md` (PM WSJF table, cross-checked per this task)
**Status:** COMPLETE — feeds Phase 2 consensus-agent gate

---

## 1. RTM Model

Modeled per requirements-traceability-core §2.1 as a bipartite graph G = (R ∪ C, E), R = the 12 PRD FRs, C = HLD components/sections, edge (r,c) ∈ E iff c implements or directly enforces r. deg(r) = 0 → orphan FR (§2.2). A component with no inbound edge from any r ∈ R → orphan component.

## 2. Traceability Matrix

| FR ID | Title | HLD Component / Section | deg(r) | Status |
|---|---|---|---|---|
| FR-001 | Working Memory zone | §3.2 Zone 1 — Working Memory; ADR-005 (Zone 1 storage adapter) | 2 | Covered |
| FR-002 | Episodic Memory zone | §3.3 Zone 2 — Episodic Memory | 1 | Covered |
| FR-003 | Semantic Memory zone | §3.4 Zone 3 — Semantic Memory (incl. `EntityOwnershipSpecification` executable spec); ADR-006 | 2 | Covered |
| FR-004 | Procedural Memory zone | §3.5 Zone 4 — Procedural Memory | 1 | Covered |
| FR-005 | Entity Memory zone | §3.6 Zone 5 — Entity Memory | 1 | Covered |
| FR-006 | Retrieval-Index Memory zone | §3.7 Zone 6 — Retrieval-Index Memory; ADR-007 (vector index); ADR-008 (lexical index + RRF fusion) | 3 | Covered |
| FR-007 | Provenance/Audit Memory zone | §3.8 Zone 7 — Provenance/Audit Memory; ADR-010 (provenance write path) | 2 | Covered |
| FR-008 | Consolidation Memory zone | §3.9 Zone 8 — Consolidation Memory | 1 | Covered |
| FR-009 | Memory Orchestrator cross-zone routing | §3.1 Memory Orchestrator; §2 Container Architecture (`dashanan-orchestrator`); design-pattern table (Facade) | 3 | Covered |
| FR-010 | Cross-cutting provenance/audit obligation | §3.8 (FR-007, FR-010 heading); ADR-010 (WAL/outbox durability barrier); §7.2 API write path (422 rejection without resolvable `source_type`); §8.4 failure-mode row "Provenance store (Zone 7) Down" | 4 | Covered |
| FR-011 | Pluggable storage-adapter interface | §3.0 Layering (Infrastructure layer, `ZoneRepository` port, dependency inversion); §2 Container Architecture (per-zone adapter table); ADR-001 (dual deployment shape); ADR-005/006/007/008/009 (per-zone adapter ADRs) | 6 | Covered |
| FR-012 | Memory Score & rotation-state-machine algorithm | §3.1 Memory Orchestrator (score computation); design-pattern table (State Machine, Template Method); §12A–12C (thresholds, half-lives, Big-O — referenced in Section 0 deliverables table) | 3 | Covered |

**Requirement coverage ratio CR = 12/12 = 1.0.** No FR has deg(r) = 0.

## 3. Orphan Findings

**Orphan FRs (deg = 0): NONE.** All 12 PRD FRs trace to at least one HLD component/section.

**Orphan HLD components (an implemented component with no traceable FR): NONE found.** Every named component in HLD §3 (Orchestrator, Zones 1–8) and every supporting container in §2 (`dashanan-api`, `dashanan-rotation-worker`, `dashanan-index-worker`, `dashanan-provenance-relay`) resolves to FR-009, FR-006, or FR-010 respectively — none is unaccounted-for infrastructure. The Rotation Engine (§3.11, owns `RotationDeadline`) is the execution mechanism of FR-012 and is not a standalone orphan.

**Zero orphan FRs, zero orphan components — the Phase 2 consensus-gate precondition is met on traceability grounds.**

## 4. FR-010 WSJF Gap — CONFIRMED

The solution-architect's flag is **confirmed, not refuted**.

`docs/phase-0-output/product-strategy-and-prioritization.md` (PM co-author, product-manager-agent) states explicitly (line 5) that it was authored **before** `PRD.md` existed, scoring instead against the locked 8-zone taxonomy plus "3 inferred cross-cutting FRs" (line 27: `FR-CC-01..03`, 11 FRs total). Cross-referencing the WSJF table's three cross-cutting rows against the PRD's later-assigned FR IDs:

| WSJF row | Description | Maps to PRD FR |
|---|---|---|
| FR-CC-01 | Memory Orchestrator: unified context-assembly API | FR-009 |
| FR-CC-02 | Memory Score & Rotation Policy Engine | FR-012 |
| FR-CC-03 | Pluggable storage adapter layer | FR-011 |

All three scored cross-cutting rows map cleanly to FR-009, FR-011, and FR-012. **FR-010 (the cross-cutting provenance/audit write-time enforcement obligation) has no corresponding WSJF row at all** — it was never scored as its own prioritization item. The WSJF table's own zone row for FR-ZONE-07 (Provenance/Audit zone, = PRD FR-007) exists and is scored, but FR-010 is a distinct requirement in the PRD ("every write to any of the 8 zones SHALL be required to register a corresponding Provenance/Audit entry... distinct from FR-007") and that distinction was not carried into the WSJF pass, because the WSJF table predates the PRD's FR numbering entirely.

**Conclusion:** FR-010 is fully traced and implemented in the HLD (§3.8, ADR-010, §7.2, §8.4 — see matrix row above), so there is no architectural gap. The gap is confined to Phase 0 prioritization: FR-010 needs its own WSJF/Kano scoring pass (delegated to `ba-pm-mathematics-expert` per this agent's Mathematical Delegation contract) before Phase 6 Sprint Planning, since Sprint 1 scope notes in both the PRD and HLD (§12F) already assume FR-010 ships in Sprint 1 without it ever having been independently prioritized against the other Sprint-1 candidates.

## 5. Recommendation to Phase 2 Consensus Gate

1. Traceability precondition (zero orphan FRs, zero orphan components): **PASS**.
2. FR-010 WSJF gap: **real, but non-blocking for this gate** — it is a Phase 0/Phase 6 prioritization completeness issue, not an HLD architectural gap. Recommend a follow-up action item: PM co-author adds an explicit FR-010 WSJF/Kano row before Phase 6 Sprint Planning intake, rather than blocking Phase 2 JOINT APPROVED on it.

---

## Change Log

| Date | Version | Change |
|---|---|---|
| 2026-09-17 | 1.0.0 | Initial Phase 2 traceability review — 12/12 FRs covered, 0 orphans, FR-010 WSJF gap confirmed |
