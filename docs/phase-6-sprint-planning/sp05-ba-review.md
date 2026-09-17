# SP.0.5 Business Analyst Review — Sprint 1 Backlog FR Coverage

**Document ID:** SP05-BA-REVIEW-20260917-01
**Version:** 1.0.0
**Author:** business-analyst-agent
**Date:** 2026-09-17
**Reviewed against:** `docs/phase-6-sprint-planning/backlog_draft.json` v1.0.0, `SRS.md` v1.0.0

---

## 1. Purpose

Verify FR coverage of the 9 Sprint 1 stories against SRS.md, confirm no story references a non-existent FR, and flag any DPDP-relevant story missing an explicit DPDP acceptance criterion.

## 2. FR Coverage Confirmation

Sprint-1-scoped FRs per SRS Section 2: FR-001, FR-002, FR-006, FR-007, FR-009, FR-010, FR-012.

| FR ID | SRS Title | Exists in SRS 3.1? | Covered by Story |
|---|---|---|---|
| FR-001 | Working Memory zone | Yes | DASH-STORY-002 |
| FR-002 | Episodic Memory zone | Yes | DASH-STORY-003, DASH-STORY-004 |
| FR-006 | Retrieval-Index Memory zone | Yes | DASH-STORY-007 |
| FR-007 | Provenance/Audit Memory zone | Yes | DASH-STORY-005 |
| FR-009 | Memory Orchestrator cross-zone routing | Yes | DASH-STORY-001 |
| FR-010 | Cross-cutting provenance/audit obligation | Yes | DASH-STORY-006 |
| FR-012 | Memory Score & rotation-state-machine | Yes | DASH-STORY-008, DASH-STORY-009 |

**Result: All 7 Sprint-1-scoped FRs are covered.** No gaps.

**Non-existent-FR check:** every `traces_to_fr` value in backlog_draft.json (FR-009, FR-001, FR-002 x2, FR-007, FR-010, FR-006, FR-012 x2) resolves to a valid row in SRS Section 3.1. No story references an FR absent from the SRS.

## 3. DPDP Flag

NFR-006 (DPDP Act 2023 compliance) applies to "any zone that stores PII extracted from conversations," with its erasure obligation grounded in AC-013.

- **DASH-STORY-003 (Episodic Memory, Zone 2) — FLAGGED.** This zone stores a chronological record of interactions/events, i.e., conversational content that may carry PII. Its current acceptance criteria (AC-002 only) cover recency ordering and decay exposure but contain no erasure/purpose-limitation criterion. **Recommend adding an explicit DPDP AC** to DASH-STORY-003 (or noting the erasure cascade as an accepted Sprint 1 deferral if Zone 7's crypto-shredding mechanism from AC-013 is not yet wired for Zone 2), since Zone 2 is the persistence surface, not Zone 7.
- DASH-STORY-004 (Zone 2 capacity/MaxAge backstop) — secondary flag; forced eviction touches the same PII-bearing Zone 2 content. No DPDP AC currently present either; the erasure gap in DASH-STORY-003 is the primary item to resolve.
- Zone 5 (Entity) is out of Sprint 1 scope (FR-005 is in `future_sprints_backlog`), so no Sprint 1 story exists for it — no action needed this sprint, but flag for the sprint that delivers FR-005.

## 4. Summary

All Sprint-1-scoped FRs (FR-001, FR-002, FR-006, FR-007, FR-009, FR-010, FR-012) are covered with no orphan or non-existent FR references. One DPDP gap identified: DASH-STORY-003 (Episodic, Zone 2) lacks an explicit DPDP/erasure acceptance criterion despite persisting conversational content potentially containing PII.
