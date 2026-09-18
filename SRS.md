# Software Requirements Specification

**Document ID:** SRS-20260917-01
**Version:** 1.0.0
**Status:** DRAFT — pending Phase 2 consensus gate closure
**Created:** 2026-09-17
**Author:** business-analyst-agent
**Source of truth:** `docs/phase-0-output/PRD.md` (PRD-20260917-01 v1.0.0), `docs/phase-1-architecture/HLD.md` (HLD-20260917-01 v1.0.0), `docs/phase-2-validation/ba-traceability-review.md` (BA-TRACE-20260917-01 v1.0.0)

---

## 1. Purpose

Dashanan (दशानन) is a reusable, engine-agnostic dynamic memory orchestration engine for AI/LLM context systems. It gives a host AI system a much larger effective context than its native window by distributing, rotating, compressing, promoting, and retrieving context across 8 specialized, independently governed memory zones, coordinated by a central Memory Orchestrator driven by a locked Memory Score formula and a strict Active -> Compressed -> Archived rotation state machine. This SRS is the project's canonical requirements specification, promoting the PRD's 12 functional requirements and 11 non-functional requirements into a single traceable document, with Acceptance Criteria grounded in the approved HLD.

## 2. Scope

**In scope (this SRS, Dashanan v1):**
- Requirements for all 8 memory zones (Working, Episodic, Semantic, Procedural, Entity, Retrieval-Index, Provenance/Audit, Consolidation) and their rotation/compression/eviction behavior at the requirements level.
- Requirements for the Memory Orchestrator's cross-zone routing, the Memory Score formula, and the Active -> Compressed -> Archived (plus Promoted) rotation state machine.
- Requirements for provenance/audit as a cross-cutting, mandatory capability on every zone write.
- Requirements for a pluggable storage-adapter interface (portability requirement).
- Sprint 1 scope: Orchestrator core (FR-009, FR-012) plus Zones 1 (Working), 2 (Episodic), 6 (Retrieval-Index), 7 (Provenance/Audit), and the cross-cutting provenance obligation (FR-010) as it applies to those four zones.

**Out of scope:** see Section 5.

## 3. Requirements

### 3.1 Functional Requirements

| FR ID | Title | Description | Traces To (HLD) | Priority |
|---|---|---|---|---|
| FR-001 | Working Memory zone | The system SHALL provide a Working Memory zone holding current turn/session scratch context, with highest-frequency read/write access and the smallest TTL of any zone. | HLD §3.2 Zone 1; ADR-005 | High |
| FR-002 | Episodic Memory zone | The system SHALL provide an Episodic Memory zone holding a chronological, time-anchored record of interactions/events, queryable by recency. Each Episodic entry SHALL carry its own recency/decay attribute. | HLD §3.3 Zone 2 | High |
| FR-003 | Semantic Memory zone | The system SHALL provide a Semantic Memory zone holding distilled, deduplicated cross-entity relationships and general facts not owned by any single entity record, per the locked Zone 3/Zone 5 ownership ADR. | HLD §3.4 Zone 3; ADR-006 | Medium |
| FR-004 | Procedural Memory zone | The system SHALL provide a Procedural Memory zone holding learned task procedures, tool-use patterns, and successful action sequences. | HLD §3.5 Zone 4 | Medium |
| FR-005 | Entity Memory zone | The system SHALL provide an Entity Memory zone holding per-entity (person/project/system) profile records, each the canonical sole owner of that entity's own attributes. | HLD §3.6 Zone 5 | High |
| FR-006 | Retrieval-Index Memory zone | The system SHALL provide a Retrieval-Index Memory zone maintaining a vector + lexical index over all other zones for fast recall, and SHALL expose the similarity primitives needed to compute the TaskRelevance and UserAffinity terms of the Memory Score (FR-012). | HLD §3.7 Zone 6; ADR-007; ADR-008 | High |
| FR-007 | Provenance/Audit Memory zone | The system SHALL provide a Provenance/Audit Memory zone recording, for every fact in every other zone, its source attribution, a confidence score, and its edit history. | HLD §3.8 Zone 7; ADR-010 | High |
| FR-008 | Consolidation Memory zone | The system SHALL provide a Consolidation Memory zone as the long-term, cross-session consolidated store that content reaches only via the Archived state of the rotation state machine (FR-012). | HLD §3.9 Zone 8 | High |
| FR-009 | Memory Orchestrator cross-zone routing | The system SHALL provide a central Memory Orchestrator that routes reads and writes across all 8 zones and exposes a single, unified context-assembly API to the host AI system. | HLD §3.1; §2 Container Architecture; Facade pattern | High |
| FR-010 | Cross-cutting provenance/audit obligation | Every write to any of the 8 zones SHALL be required to register a corresponding Provenance/Audit entry (FR-007) at write time; no zone may accept a fact without an attributable source and confidence value. | HLD §3.8; ADR-010 (WAL/outbox); §7.2 API write path; §8.4 failure modes | High |
| FR-011 | Pluggable storage-adapter interface | The system SHALL define a storage-adapter interface that decouples each zone's logical behavior from its physical storage backend, so any zone can be backed by a different concrete adapter without changing the zone's orchestration contract. | HLD §3.0 Layering (`ZoneRepository` port); §2 Container Architecture; ADR-001, ADR-005/006/007/008/009 | High |
| FR-012 | Memory Score & rotation-state-machine algorithm | The system SHALL compute, for every stored item, `MemoryScore = w1*Recency + w2*Frequency + w3*Importance + w4*UserAffinity + w5*TaskRelevance + w6*ProvenanceConfidence` (each term normalized to [0,1], default MVP weights equal at 1/6, configurable per deployment) and SHALL drive every item through an explicit Active -> Compressed -> Archived lifecycle (plus Promoted), gated such that compression can never be skipped (`PromoteThreshold > CompressThreshold > ArchiveThreshold` by construction). | HLD §3.1 (score computation); State Machine / Template Method patterns; §12A-12C (thresholds, half-lives, Big-O) | High |

### 3.2 Non-Functional Requirements

| NFR ID | Title | Description | Priority |
|---|---|---|---|
| NFR-001 | Portability | The reference implementation SHALL be Python 3.12+ with a language-agnostic wire protocol (JSON/gRPC) so the engine can be embedded by AI systems written in any language. | High |
| NFR-002 | Deployment shape | The system SHALL be deployable both as an in-process embeddable library and as an optional standalone sidecar service (FastAPI/gRPC) for polyglot/microservice consumers. | High |
| NFR-003 | Cross-platform support | The system SHALL run on Linux/macOS/Windows, as a library or as a containerized sidecar service. | Medium |
| NFR-004 | Scalability range | The system SHALL be designed to support a range from a single-process, low-QPS, in-memory embedding up to a shared multi-tenant memory service (target: thousands of QPS, external storage). | High |
| NFR-005 | Offline-capable local mode | The system SHALL support an offline-capable local operating mode (no mandatory external network dependency for core zone operations). | Medium |
| NFR-006 | Data protection compliance (India, DPDP Act 2023) | The system SHALL support DPDP Act 2023 (India) compliance for any zone that stores PII extracted from conversations, including purpose-limitation and data-subject rights (erasure) support. | High |
| NFR-007 | Security — memory poisoning / cross-tenant leakage resistance | The system SHALL be designed against prompt-injection-driven memory poisoning and cross-tenant data leakage, per the HLD Section 10 STRIDE threat model. | High |
| NFR-008 | Provenance/confidence as first-class NFR | ProvenanceConfidence (read from Zone 7) SHALL be available for every item as an input to the Memory Score (FR-012); the system SHALL never allow a zone write to bypass provenance recording. | High |
| NFR-009 | Weight/threshold configurability | Memory Score weights (w1..w6) and rotation thresholds SHALL be a per-deployment configuration surface, not hardcoded constants. | Medium |
| NFR-010 | Timeline / delivery model | No fixed deadline is imposed; delivery follows an iterative R&D -> architecture -> consensus-loop model. | Low |
| NFR-011 | Hallucination risk classification | The system's hallucination risk is classified MEDIUM (general AI infrastructure); this SHALL inform the depth of verification gates applied to Dashanan's own generated artifacts in later phases. | Medium |
| NFR-012 | Promotion-before-eviction read-your-own-writes guarantee | Within a single session, a client SHALL be able to read back an item immediately after writing it (Zone 1 synchronous write, ADR-002/ADR-005). When that item's score crosses a promotion threshold, the system SHALL NOT evict it from Zone 1 until its promotion to the target zone is durably committed and published (ADR-016, HLD §7.7). | High |
| NFR-013 | Regulated-identifier detection at the write gate | The system SHALL detect and tokenize a configured set of regulated structured identifiers (e.g. Aadhaar, PAN, payment card numbers) in write-path payloads before persistence or Zone 6 indexing (ADR-017). This is scoped narrowly to regulated structured identifiers and SHALL NOT be construed as general content redaction; it is distinct from and narrower than NFR-006's DPDP purpose-limitation/erasure obligations. | High |

## 4. Acceptance Criteria

| AC ID | For FR/NFR | Acceptance Criterion |
|---|---|---|
| AC-001 | FR-001 | Given a host session writes to Working Memory, When the item's TTL expires without promotion, Then the item is evicted from Zone 1 without requiring an explicit delete call, and no other zone is mutated as a side effect. |
| AC-002 | FR-002 | Given multiple Episodic entries with distinct timestamps, When the host queries Zone 2 by recency, Then results are returned in strict chronological order and each entry exposes its own decay attribute. |
| AC-003 | FR-003 | Given a candidate fact that spans two or more entities, When it is classified per the Zone 3/Zone 5 ownership ADR (HLD §3.4, ADR-006), Then it is stored in Semantic Memory, not duplicated into any single Entity record. |
| AC-004 | FR-004 | Given a host records a successful tool-use sequence, When Procedural Memory is queried for that task type, Then the stored procedure is retrievable and distinguishable from one-off episodic events. |
| AC-005 | FR-005 | Given an attribute fact owned by exactly one entity, When it is written, Then it is stored only in that entity's Entity Memory record (never duplicated into Semantic Memory), per the locked ownership ADR. |
| AC-006 | FR-006 | Given an item exists in any of Zones 1-5, 7, 8, When the Retrieval-Index is queried with a task-relevant query, Then the vector + lexical fused result includes that item within the top-k candidates used to compute TaskRelevance for the Memory Score. |
| AC-007 | FR-007 | Given any fact is written to any zone, When the Provenance/Audit zone is queried for that fact's `item_id`, Then a source attribution, confidence score, and edit history entry exists and is retrievable. |
| AC-008 | FR-008 | Given an item's state transitions to Archived per the rotation state machine (FR-012), When it lands in Consolidation Memory, Then it is queryable as long-term, cross-session content and cannot have reached Zone 8 without first passing through Compressed. |
| AC-009 | FR-009 | Given a host calls the unified context-assembly API, When the request does not name any individual zone, Then the Orchestrator resolves and returns assembled context sourced from the correct zones without the host needing zone-level knowledge. |
| AC-010 | FR-010 | Given a write request to any zone omits a resolvable `source_type`, When the Orchestrator's write path processes it (HLD §7.2), Then the write is rejected (HTTP 422) and no fact is persisted without a corresponding Provenance/Audit entry, consistent with the WAL/outbox durability barrier (ADR-010). |
| AC-011 | FR-011 | Given a zone is configured with a different concrete storage adapter (e.g., swapping a relational adapter for a vector-store adapter), When the zone's orchestration contract (`ZoneRepository` port) is exercised, Then zone behavior is unchanged from the caller's perspective. |
| AC-012 | FR-012 | Given an item's MemoryScore crosses `CompressThreshold` but not `ArchiveThreshold`, When the rotation sweep runs, Then the item transitions to Compressed and remains retrievable in compressed form; it SHALL NOT be able to reach Archived without having passed through Compressed, per `PromoteThreshold > CompressThreshold > ArchiveThreshold`. |
| AC-013 | NFR-006 (DPDP) | Given a data subject requests erasure, When the erasure cascade executes across all 8 zones plus indices and archives, Then the subject's payload content becomes permanently unrecoverable via crypto-shredding while the Zone 7 provenance chain structure (hashes, timestamps) remains intact and verifiable (HLD §10 DPDP-2, DPDP-3). |
| AC-014 | NFR-007 (Tenant isolation, STRIDE I-1/I-2/I-3) | Given two tenants A and B each store semantically similar content, When tenant A performs a retrieval query, Then the Retrieval-Index returns results only from tenant A's physically partitioned vector/lexical collection (HLD threat I-1, ADR-007), `UserAffinity` never acts as a tenant-isolation term (threat I-2, ADR-013), and no shared cache key leaks a hit/miss signal about tenant B's content (threat I-3). |
| AC-015 | NFR-007 (Memory poisoning, STRIDE T-1) | Given adversarial content is written to any zone via prompt injection, When that content is later surfaced, Then it is never interpolated into any prompt Dashanan itself issues as an instruction, and a contradicting true fact triggers the conflict-detection sweep to downgrade both records rather than silently overwriting the true fact (HLD threat T-1). |
| AC-016 | NFR-008 | Given a zone write bypasses provenance recording, When the write path is exercised, Then the write is rejected before persistence (same enforcement path as AC-010). |
| AC-017 | NFR-009 | Given a deployment operator sets a non-default weight profile (w1..w6) or rotation thresholds, When the Orchestrator computes MemoryScore for that deployment, Then the configured values are used instead of the MVP defaults, without a code change. |
| AC-018 | NFR-012 | Given an item's score crosses `PromoteThreshold` (or is read-triggered per ADR-012) while it is also eligible for Zone 1 TTL/capacity eviction, When the rotation worker's sweep processes it, Then the target-zone write is durably committed and its `memory.promoted` event is published before the item is evicted from Zone 1, so no `assemble` call in that window returns a result missing the item (HLD §7.7, ADR-016). |
| AC-019 | NFR-013 | Given a write payload contains a regulated structured identifier from the configured set (e.g. Aadhaar, PAN, card number), When the write is processed by `POST /v1/memory/write`, Then the identifier is tokenized before the ADR-010 journal fsync and before any zone persistence or Zone 6 indexing, and the rest of the payload's user-stated content is persisted unredacted (ADR-017). |
| AC-020 | FR-012 | Given raw inputs for each MemoryScore term (an access count, an Importance tag, a session-embedding pair, an item/query embedding pair, and a provenance base value with its modifiers), When each is normalized per HLD §12G, Then every resulting term value lies in `[0,1]`, the configured weights `w1..w6` sum to 1, and both are visible in the `score:explain` response (HLD §7.1). |

## 5. Out of Scope

The following are explicitly out of scope for Dashanan v1 and are excluded to prevent scope creep:

1. **Phase B / actual code implementation** — this SRS specifies requirements only; implementation is a downstream SDLC phase.
2. **Exact numeric thresholds, weights, and half-life constants for the Memory Score and rotation state machine** beyond the HLD-finalized defaults (`PromoteThreshold=0.70`, `CompressThreshold`, `ArchiveThreshold=0.25`) — any further tuning strategy (adaptive/learned weights) is an explicitly deferred future ADR candidate, not MVP scope.
3. **UI/UX / host-facing admin dashboard** — Dashanan is an embeddable engine/service, not an end-user-facing product; no Phase 3 UI/UX design pipeline applies.
4. **Multi-region deployment beyond what is architected** — only the per-tenant `embedding_residency` policy (ADR-015) and in-region storage for Indian personal data are in scope; broader multi-region active-active topology is out of scope.
5. **OpenAPI contract detail** — a Phase 1.5 deliverable, not part of this SRS.
6. **Adaptive/learned weight-tuning for the Memory Score** — MVP ships static, equal-weighted (1/6) configurable weights only; learned tuning is a non-goal for v1.
7. **LLM selection, hosting, or fine-tuning** — Dashanan is a memory layer the host AI system calls into; it does not select or host the underlying model.
8. **Host system's prompt-engineering strategy** — Dashanan supplies assembled context; how the host composes its final prompt is the host's responsibility.
9. **GitHub repository creation, CI/CD setup** — deferred to a separate, directly user-confirmed action.
10. **Zones 3, 4, 5, 8 and the remainder of FR-010/FR-011 beyond the four Sprint-1 zones** — backlog for subsequent sprints per the PRD's Sprint 1 scope note; exact sprint sequencing is a Phase 6 (Sprint Planning) deliverable.
11. **Legal confirmation of crypto-shredding as DPDP-compliant erasure** — flagged as OAQ-10 in the HLD; outside this SRS's authority to resolve.
12. **Concrete numeric success-metric targets** (effective context multiplier, provenance-coverage target, latency budgets) — candidate metrics only per PRD Section 10; formal targets are a Phase 1/Phase 6 deliverable.

## 6. Change Log

| Date | Version | Task | Change Summary | Status |
|---|---|---|---|---|
| 2026-09-17 | 1.0.0 | SRS authoring (Phase 0-2 promotion) | Initial canonical SRS created, promoting all 12 PRD FRs and 11 PRD NFRs with HLD component traceability, 17 Acceptance Criteria grounded in HLD Section 10 STRIDE/DPDP controls, and 12 explicit Out of Scope items for Dashanan v1. | Done |
| 2026-09-17 | 1.0.1 | Close doc gaps from external architecture review (#1) | Added NFR-012/NFR-013 and AC-018/AC-019 for the two real gaps an external review surfaced: promotion-before-eviction read-your-own-writes ordering (HLD ADR-016, §7.7) and regulated-identifier detection at the write gate (HLD ADR-017), narrowly scoped and reconciled with existing ADR-002/ADR-005/ADR-010/ADR-014/DPDP-1/DPDP-2. The other 3 claims in the same review (zone count, MemoryScore normalization, circuit-breaker degradation) were verified already correct; no SRS change needed for those. | Done |
| 2026-09-18 | 1.0.2 | Formalize MemoryScore term normalization (#2) | Added AC-020 for HLD's new §12G, which supplies the [0,1] bounding formulas for the 5 MemoryScore terms (Frequency, Importance, UserAffinity, TaskRelevance, ProvenanceConfidence) that previously had none — only Recency had an explicit formula before this. FR-012's existing text was already correct and needed no wording change; §12G fills in the implementation detail behind it. | Done |
