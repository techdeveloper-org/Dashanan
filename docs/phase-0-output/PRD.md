# Product Requirements Document — Dashanan (दशानन)

**Document ID:** PRD-20260917-01
**Version:** 1.0.0
**Status:** DRAFT — pending Phase 1 solution-architect intake
**Created:** 2026-09-17
**Author:** business-analyst-agent (Phase 0 co-lead)
**Source of truth:** `docs/orchestration/01-vision-and-prd.md` (locked, post architecture-review), `README.md`

**Historical note:** this is the Phase 0 snapshot; superseded by the living `SRS.md` (currently v1.0.6, APPROVED) and `HLD.md` for all current requirements/architecture content.

---

## 1. Purpose

Dashanan is a reusable, engine-agnostic **dynamic memory orchestration engine** for AI/LLM context systems. It gives any host AI system a much larger *effective* context than its native window by distributing, rotating, compressing, promoting, and retrieving context across **8 specialized, independently-governed memory zones**, coordinated by a central **Memory Orchestrator** driven by a locked **Memory Score** formula and a strict **Active → Compressed → Archived** rotation state machine. This PRD is the Phase 0 gate artifact: it converts the locked vision/PRD input into numbered, traceable functional and non-functional requirements so that Phase 1 (`solution-architect`) can begin High-Level Design work.

## 2. Scope

**In scope (Phase 0 / this PRD):**
- Requirements for all 8 memory zones and their rotation/compression/eviction behavior at the requirements level (not implementation).
- Requirements for the Memory Orchestrator's cross-zone routing, the Memory Score formula, and the rotation state machine (shape only — exact thresholds/weights are explicitly deferred to Phase 1, per the locked spec).
- Requirements for provenance/audit as a cross-cutting, mandatory capability (Zone 7 plus a cross-cutting audit obligation on every write to any zone).
- Requirements for a pluggable storage-adapter interface (portability requirement — "reusable in any AI").
- Sprint 1 scope framing: Orchestrator core + Zones 1 (Working), 2 (Episodic), 6 (Retrieval-Index), 7 (Provenance/Audit).

**Out of scope (this PRD):**
- Exact numeric thresholds (`PromoteThreshold`, `CompressThreshold`, `ArchiveThreshold`), per-zone half-life constants, and weight-tuning strategy — Phase 1 solution-architect + `mathematics-engineer` deliverable.
- OpenAPI contract detail — Phase 1.5 deliverable.
- Retrieval-layer implementation detail, multi-tenant architecture detail, storage-abstraction schema design — Phase 1 deliverable.
- GitHub repository creation — explicitly deferred to a separate, directly user-confirmed action.
- UI/UX (Dashanan is an embeddable engine/service, not an end-user-facing product) — no Phase 3 UI/UX design pipeline is expected to apply; **[NEEDS INPUT]** confirm no host-facing admin UI is planned for MVP.

## 3. Background

Every AI/LLM application eventually hits the same wall: the model's context window is finite, but real conversations, agents, and long-running tasks generate context that keeps growing. Naive approaches either truncate/forget older context or stuff everything into the window, wasting tokens and degrading attention. Dashanan treats memory as a living, orchestrated system instead — 8 zones, each with its own retention/compression/promotion/retrieval policy, unified behind one context-assembly API.

The zone taxonomy was revised from an original 10-zone concept to 8 during a locked architecture review (2026-09-17): "Temporal" was consolidated into Episodic (time-anchoring is now a per-entry attribute, not a separate zone), and "Summary/Compressed" was consolidated into Consolidation (compression is a mechanism Consolidation applies to aged content, not a competing zone). The "Dashanan" (ten-headed) naming is retained deliberately even though the engineering zone count is 8; this is a documented, intentional simplification, not a naming inconsistency to be re-litigated.

An ADR locked during the same review resolves the Zone 3 (Semantic) vs. Zone 5 (Entity) ownership boundary: **Entity Memory = per-entity records (attributes owned by exactly one entity); Semantic Memory = cross-entity relationships and general facts not owned by any single entity record.** A single-entity attribute fact defaults to Entity-only; Semantic Memory is reserved for facts that genuinely span or relate multiple entities, or general world knowledge not owned by any one entity. This rule is binding on every FR below that touches Zone 3 or Zone 5.

## 4. Users / Personas

| Persona | Description | Primary Need |
|---|---|---|
| Host AI system / agent framework integrator | A developer embedding Dashanan as a library (pip package) or calling it as a sidecar service (FastAPI/gRPC) from a polyglot stack | Simple, stable context-assembly API; predictable latency; portability across storage backends |
| Long-running coding agent | An AI agent maintaining state across a long multi-session engineering task | High-fidelity Procedural + Entity + Provenance memory; low tolerance for hallucinated/unattributed facts |
| Short-lived customer-support session | A conversational agent with a short session lifespan | Fast Working/Episodic access; less need for deep Consolidation |
| Deployment operator | The person configuring zone weights, thresholds, and storage adapters per deployment | Configuration surface for weights/thresholds (not hardcoded constants); observability into rotation decisions |

**[NEEDS INPUT]** No specific named customer, vertical, or first integration target is stated in the locked spec. Persona table above is derived from the locked spec's explicit "different host AI systems ... will plausibly want different weight profiles" language and the reference-implementation description; it is not sourced from a stated customer list.

## 5. Goals

1. **Engine-agnostic**: usable as an embeddable library/SDK by any AI system or agent framework, not tied to one model provider.
2. **Deep, not flat**: structured, multi-zone memory instead of one undifferentiated context blob.
3. **Scales to long-running context**: supports much longer effective sessions/tasks than a single context window allows.
4. **Reusable as a product**: a general-purpose memory engine other AI products can adopt (pip package + optional standalone sidecar service).
5. **Anti-hallucination backbone**: provenance/confidence tracking is a first-class requirement from Sprint 1, not an afterthought.
6. **Portable storage**: pluggable storage-adapter interface so no single backend is a hard dependency.

## 6. Non-Goals

1. Dashanan does not itself select, host, or fine-tune the underlying LLM — it is a memory layer the host AI system calls into.
2. Dashanan does not define or enforce the host system's prompt-engineering strategy — it supplies retrieved/assembled context; how the host composes its final prompt is out of scope.
3. Dashanan's MVP does not ship adaptive/learned weight-tuning for the Memory Score — this is an explicitly deferred future ADR candidate (static, equal-weighted config-surface weights only for MVP).
4. Dashanan's MVP does not commit to a specific numeric scale/capacity target — capacity estimation is an explicit Phase 1 deliverable, not a Phase 0 commitment.
5. This PRD does not cover GitHub repository creation, CI/CD setup, or any implementation-level specification — BA artifacts only, per `business-analyst-agent`'s operating rules.

## 7. Functional Requirements

Every FR below is traceable to exactly one of: a memory zone, the orchestrator, provenance/audit, or the storage-adapter interface, per the routing prompt's constraint.

| FR ID | Title | Description | Traces To | Priority |
|---|---|---|---|---|
| FR-001 | Working Memory zone | The system SHALL provide a Working Memory zone holding current turn/session scratch context, with highest-frequency read/write access and the smallest TTL of any zone. | Zone 1 (Working) | High |
| FR-002 | Episodic Memory zone | The system SHALL provide an Episodic Memory zone holding a chronological, time-anchored record of interactions/events, queryable by recency. Each Episodic entry SHALL carry its own recency/decay attribute (absorbing the former separate "Temporal" zone's function). | Zone 2 (Episodic) | High |
| FR-003 | Semantic Memory zone | The system SHALL provide a Semantic Memory zone holding distilled, deduplicated cross-entity relationships and general facts not owned by any single entity record, per the locked Zone 3/Zone 5 ownership ADR. | Zone 3 (Semantic) | Medium |
| FR-004 | Procedural Memory zone | The system SHALL provide a Procedural Memory zone holding learned task procedures, tool-use patterns, and successful action sequences. | Zone 4 (Procedural) | Medium |
| FR-005 | Entity Memory zone | The system SHALL provide an Entity Memory zone holding per-entity (person/project/system) profile records that are incrementally updated, where each record is the canonical, sole owner of that entity's own attributes, per the locked Zone 3/Zone 5 ownership ADR. | Zone 5 (Entity) | High |
| FR-006 | Retrieval-Index Memory zone | The system SHALL provide a Retrieval-Index Memory zone maintaining a vector + lexical index over all other zones for fast recall, and SHALL expose the similarity primitives needed to compute the TaskRelevance and UserAffinity terms of the Memory Score (FR-012). | Zone 6 (Retrieval-Index) | High |
| FR-007 | Provenance/Audit Memory zone | The system SHALL provide a Provenance/Audit Memory zone recording, for every fact in every other zone, its source attribution, a confidence score, and its edit history, serving as the engine's anti-hallucination backbone. | Zone 7 (Provenance/Audit) | High |
| FR-008 | Consolidation Memory zone | The system SHALL provide a Consolidation Memory zone as the long-term, cross-session consolidated store that content reaches only via the Archived state of the rotation state machine (FR-012); compression SHALL be treated as a mechanism Consolidation applies to aged-out content, not as a separate zone. | Zone 8 (Consolidation) | High |
| FR-009 | Memory Orchestrator cross-zone routing | The system SHALL provide a central Memory Orchestrator that routes reads and writes across all 8 zones and exposes a single, unified context-assembly API to the host AI system, so the host never addresses individual zones directly. | Orchestrator | High |
| FR-010 | Cross-cutting provenance/audit obligation | Every write to any of the 8 zones SHALL be required to register a corresponding Provenance/Audit entry (FR-007) at write time — no zone may accept a fact without an attributable source and confidence value. This is distinct from FR-007 (which defines Zone 7's own storage/query behavior); FR-010 is the enforcement obligation on the other 7 zones' write paths. | Provenance/Audit (cross-cutting) | High |
| FR-011 | Pluggable storage-adapter interface | The system SHALL define a storage-adapter interface that decouples each zone's logical behavior from its physical storage backend, so that any zone can be backed by a different concrete adapter (e.g., local, vector store, relational/KV store) without changing the zone's orchestration contract, per the "reusable in any AI" / portability goal. | Storage-adapter interface | High |
| FR-012 | Memory Score & rotation-state-machine algorithm | The system SHALL compute, for every stored item, a `MemoryScore = w1·Recency + w2·Frequency + w3·Importance + w4·UserAffinity + w5·TaskRelevance + w6·ProvenanceConfidence` (each term normalized to [0,1], default MVP weights equal at 1/6, configurable per deployment) and SHALL drive every item through an explicit `Active → Compressed → Archived` lifecycle state (plus `Promoted`, a re-entry to a higher-retention zone from any state), gated by both current state and score such that compression can never be skipped (`PromoteThreshold > CompressThreshold > ArchiveThreshold` by construction; an item cannot reach Archived without first passing through Compressed). This FR references the locked formula/state-machine shape from `docs/orchestration/01-vision-and-prd.md` and does **not** redesign it — exact threshold values, weight-tuning strategy, and per-zone rotation cadence are explicitly out of scope for Phase 0 and are a mandatory Phase 1 solution-architect + `mathematics-engineer` deliverable. | Orchestrator (algorithm) | High |

**Sprint 1 scope note:** per the locked spec, Sprint 1 implements the Orchestrator core (FR-009, FR-012) plus Zones 1/2/6/7 (FR-001, FR-002, FR-006, FR-007) and the cross-cutting provenance obligation (FR-010) as it applies to those four zones. FR-003, FR-004, FR-005, FR-008, and the remainder of FR-010/FR-011 are backlog for subsequent sprints — **[NEEDS INPUT]** exact sprint sequencing beyond Sprint 1 is not stated in the locked spec and is a Phase 6 (Sprint Planning) deliverable, not this PRD's.

## 8. Non-Functional Requirements

| NFR ID | Title | Description | Priority |
|---|---|---|---|
| NFR-001 | Portability | The reference implementation SHALL be Python 3.12+ with a language-agnostic wire protocol (JSON/gRPC) so the engine can be embedded by AI systems written in any language. **[INFERRED in the locked spec, not a stated hard requirement]** | High |
| NFR-002 | Deployment shape | The system SHALL be deployable both as an in-process embeddable library and as an optional standalone sidecar service (FastAPI/gRPC) for polyglot/microservice consumers. **[INFERRED in the locked spec]** | High |
| NFR-003 | Cross-platform support | The system SHALL run on Linux/macOS/Windows, as a library or as a containerized sidecar service. | Medium |
| NFR-004 | Scalability range | The system SHALL be designed to support a range from a single-process, low-QPS, in-memory embedding up to a shared multi-tenant memory service (target: thousands of QPS, external storage). **[NEEDS INPUT — no concrete numeric SLA/throughput target is stated in the locked spec; capacity estimation is explicitly deferred to Phase 1.]** | High |
| NFR-005 | Offline-capable local mode | The system SHALL support an offline-capable local operating mode (no mandatory external network dependency for core zone operations). | Medium |
| NFR-006 | Data protection compliance (India) | The system SHALL support DPDP Act 2023 (India) compliance for any zone that stores PII extracted from conversations, including purpose-limitation and data-subject rights support. **[Flagged in the locked spec for a Phase 1 threat model — exact controls NEEDS INPUT from Phase 1.]** | High |
| NFR-007 | Security — memory poisoning / cross-tenant leakage resistance | The system SHALL be designed against prompt-injection-driven memory poisoning and cross-tenant data leakage; Security Risk is flagged HIGH in the locked spec, requiring full Phase F security-audit depth once implementation starts. **[Exact controls NEEDS INPUT from Phase 1 threat model.]** | High |
| NFR-008 | Provenance/confidence as first-class NFR | ProvenanceConfidence (read from Zone 7) SHALL be available for every item as an input to the Memory Score (FR-012); the system SHALL never allow a zone write to bypass provenance recording. | High |
| NFR-009 | Weight/threshold configurability | Memory Score weights (`w1..w6`) and rotation thresholds SHALL be a per-deployment configuration surface, not hardcoded constants, so different host AI system profiles (e.g., long-running coding agent vs. short-lived support session) can be tuned independently. | Medium |
| NFR-010 | Timeline / delivery model | No fixed deadline is imposed; delivery follows an iterative R&D → architecture → consensus-loop model per the locked spec. **[NEEDS INPUT if a hard delivery date exists outside the locked spec.]** | Low |
| NFR-011 | Hallucination risk classification | The locked spec classifies Dashanan's hallucination risk as MEDIUM (general AI infrastructure — a bug degrades every downstream AI system's factual grounding, but Dashanan itself is not medical/legal/finance-regulated). This classification SHALL inform the depth of NLI/FactScore verification gates applied to Dashanan's own generated artifacts in later phases. | Medium |

## 9. User Stories

> Written at Phase 0 depth (epic-level); full INVEST-format story decomposition with WSJF priority is a Phase 6 (Sprint Planning) deliverable, not this PRD's.

1. As a host AI system integrator, I want a single unified context-assembly API, so that I do not need to know which of the 8 zones holds a given piece of context. *(FR-009)*
2. As a long-running coding agent, I want my Working Memory to be fast and my Provenance/Audit trail to be complete, so that I can trust facts surfaced back to me across a long session. *(FR-001, FR-007, FR-010)*
3. As a deployment operator, I want to configure Memory Score weights per deployment, so that a customer-support session and a coding agent do not share an ill-fitting default profile. *(FR-012, NFR-009)*
4. As a host AI system integrator, I want to swap the storage backend without changing my integration code, so that Dashanan is portable across my infrastructure choices. *(FR-011)*
5. As an item in any zone, when my Memory Score crosses `CompressThreshold`, I want to be compressed in place before I can ever be archived, so that no content silently disappears without a recoverable intermediate form. *(FR-012)*

## 10. Success Metrics

**[NEEDS INPUT]** The locked spec (`docs/orchestration/01-vision-and-prd.md`) does not state numeric success metrics, KPI targets, or a measurement cadence for Dashanan — it is infrastructure, not a metered end-user product, and no OKR/KPI section exists in the source. The following are proposed candidate metrics for Phase 1/Phase 6 to formalize, not locked commitments:

| Candidate Metric | Rationale | Status |
|---|---|---|
| Effective context multiplier (effective context size / host's native window size) | Directly measures the core value proposition | [NEEDS INPUT — no baseline/target stated] |
| Provenance coverage (% of stored facts with a non-null Provenance/Audit entry) | Directly measures FR-010 compliance / anti-hallucination backbone | [NEEDS INPUT — target should be 100% per FR-010, but not explicitly stated as a KPI in the locked spec] |
| Rotation-sweep latency per zone | Operational health of FR-012 | [NEEDS INPUT] |
| Retrieval-Index recall/precision (Zone 6) | Quality of context reassembly | [NEEDS INPUT] |

## 11. Risks

| Risk | Description | Severity | Source |
|---|---|---|---|
| R-001 | Security Risk flagged HIGH in the locked spec: a memory engine is a prime target for prompt-injection-driven memory poisoning and cross-tenant data leakage. | High | Locked spec constraints |
| R-002 | Hallucination Risk flagged MEDIUM: a defect in Dashanan degrades every downstream AI system's factual grounding, even though Dashanan itself is not a regulated domain. | Medium | Locked spec constraints |
| R-003 | Exact numeric thresholds/weights for the Memory Score and rotation state machine are undefined at Phase 0; an ill-chosen default could cause premature archival (information loss) or memory bloat (unbounded growth) before Phase 1 finalizes them. | Medium | FR-012 scope gap |
| R-004 | No stated scale/capacity target (NFR-004) creates risk of over- or under-engineering the storage-adapter and Retrieval-Index design in Phase 1 without a capacity estimation baseline. | Medium | NFR-004 gap |
| R-005 | Zone 3 (Semantic) / Zone 5 (Entity) ownership ambiguity was the single biggest flagged gap in the first architecture-review round; although now resolved by ADR, the schema-level enforcement of that rule is still a Phase 1 deliverable and a regression here would silently duplicate/fragment facts. | Medium | ADR (Section 3) |

## 12. Dependencies

1. Phase 1 (`solution-architect`, `mathematics-engineer`) — finalizes exact Memory Score weights, rotation thresholds, per-zone rotation cadence, and a complexity/Big-O analysis for FR-012; this PRD's FR-012 explicitly does not fix those values.
2. Phase 1.5 (API Contract) — the OpenAPI 3.1.0 contract for the engine's embed/service API depends on FR-009's unified context-assembly API being finalized in Phase 1.
3. Phase 1 threat model — NFR-006 (DPDP) and NFR-007 (security) depend on a dedicated threat-modeling pass explicitly deferred by the locked spec to implementation-start Phase F depth.
4. Storage backend selection — FR-011's concrete default adapters (vector store for Zones 3/6; relational/KV store for Zones 1/2/5/7) are **[INFERRED]** in the locked spec, not confirmed; Phase 1 must validate or revise.
5. `docs/orchestration/01-vision-and-prd.md` itself remains explicitly open for Phase 1 solution-architect to further validate or revise the 8-zone taxonomy — this PRD's FR-001–FR-008 inherit that same open status.

## 13. Open Questions

All items below are also flagged inline as **[NEEDS INPUT]** in their originating section; consolidated here per PRD format:

1. Is there a first named integration target / host AI system for Dashanan's MVP, or is it purely general-purpose from day one? (Section 4)
2. Is any host-facing admin UI planned for MVP, or is Dashanan API/library-only with no UI surface? (Section 2)
3. What is the concrete numeric scale/capacity target (QPS, item count, storage volume) for MVP vs. the eventual multi-tenant service? (NFR-004)
4. Is there a hard delivery deadline outside the "iterative, no fixed deadline" framing in the locked spec? (NFR-010)
5. What are the target values for the candidate success metrics in Section 10 (effective context multiplier, provenance coverage target, latency budgets)?
6. Beyond Sprint 1 (Orchestrator + Zones 1/2/6/7), what is the sprint sequencing for the remaining zones (3, 4, 5, 8)? (Section 7, Sprint 1 scope note)

## 14. Appendix

### 14.1 Zone Reference Table (from locked spec)

| # | Zone (head) | Purpose | FR |
|---|---|---|---|
| 1 | Working Memory | Current turn/session scratch context, highest-frequency read/write, smallest TTL | FR-001 |
| 2 | Episodic Memory | Chronological, time-anchored record of interactions/events, queryable by recency | FR-002 |
| 3 | Semantic Memory | Distilled facts/entities/relations extracted from episodes, deduplicated | FR-003 |
| 4 | Procedural Memory | Learned task procedures / tool-use patterns / successful action sequences | FR-004 |
| 5 | Entity Memory | Per-entity (person/project/system) profile records, incrementally updated | FR-005 |
| 6 | Retrieval-Index Memory | Vector + lexical index over all other zones for fast recall | FR-006 |
| 7 | Provenance/Audit Memory | Source attribution, confidence, edit history for every fact | FR-007 |
| 8 | Consolidation Memory | Long-term, cross-session consolidated store (sleep-cycle style promotion) | FR-008 |

### 14.2 Memory Score Formula (locked shape, referenced not redesigned)

```
MemoryScore(item) = w1*Recency + w2*Frequency + w3*Importance + w4*UserAffinity + w5*TaskRelevance + w6*ProvenanceConfidence
```

Default MVP weights: `w1=w2=w3=w4=w5=w6=1/6` (sum-to-1, configurable per deployment — NFR-009).

### 14.3 Rotation State Machine (locked shape, referenced not redesigned)

```
Active -> Compressed -> Archived   (plus Promoted, re-entry to a higher-retention zone from any state)
PromoteThreshold > CompressThreshold > ArchiveThreshold  (by construction)
```

### 14.4 Source Documents

- `docs/orchestration/01-vision-and-prd.md` — locked Phase 0 input, 2 rounds of architecture review
- `README.md` — public-facing project summary

### 14.5 Skill/Process Note

This PRD was authored following `business-analyst-agent`'s operating rules and the `business-requirements-analysis-core` skill's BRD/PRD structuring guidance (IEEE 29148:2018-aligned requirement identifier scheme, FR/NFR numbering). The routing prompt for this task named three skills (`requirements-elicitation-core`, `prd-authoring-core`, `functional-requirements-core`) that do not exist as files anywhere under `claude-global-library/skills/` — verified via directory search. This PRD instead applied the agent's actual mandatory skill (`business-requirements-analysis-core`, confirmed present) and the 14-section outline given explicitly in the routing prompt's TASK/OUTPUT instructions. Flagged here per the library's own No-Go Rule ("never reference skills that don't exist in the repo") for whoever maintains the routing/orchestration layer that generated this task.

### 14.6 Change Log

| Date | Version | Change |
|---|---|---|
| 2026-09-17 | 1.0.0 | Initial Phase 0 PRD authored from locked vision/PRD spec |
