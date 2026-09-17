# Context Delivery Plan — Dashanan (Phases 1.5 / 2 / 5 / 6 / 7 / 8)

<!-- Phase 1.5+ prerequisite | Produced by: context-engineering-agent (sonnet) -->
<!-- Blocking gate: no Phase 1.5+ agent receives a prompt/context bundle until this plan exists. -->

**Document ID:** CDP-20260917-01
**Input:** `docs/phase-1-architecture/HLD.md` (HLD-20260917-01 v1.0.0 — APPROVED by consensus-agent, zero issues)
**Companion:** `docs/orchestration/03-agent-registry.md` (Phase 1.5/2/5/6/7/8 agent blocks — this plan verifies/corrects those blocks' `Context Budget` fields)
**Method:** Differential GSD (differential-context-routing-core), token-budget LP (context-window-optimization-core), namespace isolation + BLP (multi-agent-context-isolation-core)

---

## 0. Validation Method

Per Operating Rule 3 of context-window-optimization-core and this project's own CONSTRAINTS block: a Context Budget larger than the receiving agent's own thinking `budget_tokens` is invalid — the agent cannot reason over more input than its own allocated reasoning budget, so any excess is dead weight that the greedy priority-ratio algorithm (M1) would never select. Every budget below is cross-checked against the agent's `Thinking Level | budget_tokens` value as stated in `03-agent-registry.md`.

**Findings against the registry's current numbers (5 agent groups, 9 individual agents, all CORRECTED below):**

| Agent | Registry Context Budget | Thinking budget_tokens | Verdict |
|---|---|---|---|
| python-backend-engineer | 6,000 | 5,000 (MEDIUM) | **INVALID — flagged, corrected to 4,500** |
| api-testing-engineer | 6,000 | 5,000 (MEDIUM) | **INVALID — flagged, corrected to 4,500** |
| integration-testing-engineer | 6,000 | 5,000 (MEDIUM) | **INVALID — flagged, corrected to 4,500** |
| uml-structural-diagram-engineer | 6,000 | 5,000 (MEDIUM) | **INVALID — flagged, corrected to 4,500** |
| uml-behavioral-diagram-engineer | 6,000 | 5,000 (MEDIUM) | **INVALID — flagged, corrected to 4,500** |
| uml-interaction-diagram-engineer | 6,000 | 5,000 (MEDIUM) | **INVALID — flagged, corrected to 4,500** |
| drawio-diagram-architect | 6,000 | 1,024 (LOW) | **INVALID (5.9x over) — flagged, corrected to 900** |
| mermaid-diagram-engineer | 6,000 | 1,024 (LOW) | **INVALID (5.9x over) — flagged, corrected to 900** |
| scrum-master-agent | 8,000 | 5,000 (MEDIUM) | **INVALID — flagged, corrected to 4,800** |
| agile-tooling-specialist | 8,000 | 1,024 (LOW) | **INVALID (7.8x over) — flagged, corrected to 950** |
| finops-analyst | 6,000 | 5,000 (MEDIUM) | **INVALID — flagged, corrected to 4,800** |
| Phase 8 cell: business-analyst-agent, product-manager-agent, scrum-master-agent | 6,000 each | 5,000 (MEDIUM) | **INVALID — flagged, corrected to 4,800 each** |

Agents already sane (no correction needed): business-analyst-agent/product-manager-agent Phase 2 (5,000 vs 5,000 — at ceiling, acceptable, equal is not "larger than"), solution-architect (12,000 vs 64,000), consensus-agent (4,000 vs 20,000), context-engineering-agent (8,000 vs 10,000), orchestrator-agent AR.0 (10,000 vs 32,000), prompt-generation-expert (16,000 vs 16,000 — at ceiling), security-testing-engineer (6,000 vs 16,000), reliability-auditor (4,000 vs 16,000), context-faithfulness-engineer (4,000 vs 32,000), Phase 8 solution-architect (6,000 vs 64,000).

The corrected values below are the authoritative numbers for `03-agent-registry.md`'s next revision.

---

## 1. Phase 1.5 — API Contract Design

### python-backend-engineer
```
Context Budget: 4,500 tokens | Sources: [hld-endpoint-inventory-delta, hld-adr-004-wire-protocol-delta, hld-nfr-latency-targets-delta]
```
- `hld-endpoint-inventory-delta` = HLD.md Section 7.1–7.6 (read path, write path, zone admin, tenancy/DPDP, ops, event contracts — the exact endpoint SHAPE this agent expands into full OpenAPI schema).
- `hld-adr-004-wire-protocol-delta` = ADR-004 (gRPC primary, REST gateway) — governs which two surfaces the spec must cover.
- `hld-nfr-latency-targets-delta` = HLD.md Section 9 assumed NFR target table (Profile A/B/C latency, throughput) — needed for pagination/versioning design decisions, not raw NFR prose.
- Excluded: Sections 8 (deployment topology), 10 (STRIDE), 12 (capacity math) — not needed for schema authoring, correctly out of budget.

### api-testing-engineer
```
Context Budget: 4,500 tokens | Sources: [openapi-draft-delta, hld-security-i1-i2-i3-delta]
```
- `openapi-draft-delta` = python-backend-engineer's produced `openapi.yaml` (the object under test coverage review — not the HLD itself).
- `hld-security-i1-i2-i3-delta` = HLD.md Section 10, threats I-1/I-2/I-3 (cross-tenant ANN leak, UserAffinity soft-term leak, shared-cache oracle) — the concrete BOLA/IDOR test-plan basis this agent's CONSTRAINTS block requires.

### integration-testing-engineer
```
Context Budget: 4,500 tokens | Sources: [openapi-draft-delta, hld-event-contracts-delta]
```
- `openapi-draft-delta` = same artifact as api-testing-engineer (shared consumer view, not duplicated storage).
- `hld-event-contracts-delta` = HLD.md Section 7.6 (internal event contracts: `memory.written`, `.promoted`, `.compressed`, `.archived`, `.evicted`, `.conflict_detected`) — the Pact provider-state boundary for consumer-driven contract tests across the async write path.

---

## 2. Phase 2 — Joint Blueprint Validation (BA/PM/SA re-review)

### business-analyst-agent
```
Context Budget: 5,000 tokens | Sources: [prd-fr-delta, hld-component-list-delta]
```
- `prd-fr-delta` = the FR-NNN/NFR-NNN list this agent itself authored in Phase 0 (its own prior output, not re-fetched from GSD).
- `hld-component-list-delta` = HLD.md Section 3 component list (Orchestrator + 8 zones + 3 cross-cutting) — used for the 1:1 FR↔component traceability check and to resolve OAQ-9 (the 12-vs-11 FR count mismatch already flagged in HLD Section 11).

### product-manager-agent
```
Context Budget: 5,000 tokens | Sources: [prd-fr-delta, hld-adr-delta-summary]
```
- `prd-fr-delta` = same as above (WSJF table this agent produced in Phase 0).
- `hld-adr-delta-summary` = HLD.md Section 4 ADR "Chosen" lines only (not full Alternatives Rejected prose) — enough to judge whether the HLD makes the highest-WSJF FRs easiest or hardest to build first, per this agent's Phase 2 task.

### solution-architect
```
Context Budget: 12,000 tokens | Sources: [prd-fr-delta, hld-approved-delta, joint-validation-issue-log-delta]
```
- `hld-approved-delta` = the full APPROVED HLD (this agent's own Phase 1 output — consulted, not re-derived).
- `joint-validation-issue-log-delta` = any issues business-analyst-agent/product-manager-agent raise during this same Phase 2 pass (differential — only new findings, not the whole PRD/HLD again).

---

## 3. Phase 5 — Blueprint Documentation (UML/Draw.io cell, 5 agents)

### uml-structural-diagram-engineer
```
Context Budget: 4,500 tokens | Sources: [srs-fr-delta, hld-component-list-delta, hld-data-ownership-map-delta]
```
- `hld-data-ownership-map-delta` = HLD.md Section 3.10 (data ownership map) — the direct source for class/package/object/composite-structure diagrams' entity boundaries.

### uml-behavioral-diagram-engineer
```
Context Budget: 4,500 tokens | Sources: [srs-fr-delta, hld-rotation-state-machine-delta, hld-failure-mode-table-delta]
```
- `hld-rotation-state-machine-delta` = the locked `Active -> Compressed -> Archived` state machine (Section 0 framing + Section 3.9/12B) — the state-diagram source.
- `hld-failure-mode-table-delta` = HLD.md Section 8.4 failure-mode-analysis table — the activity-diagram source for degradation paths.

### uml-interaction-diagram-engineer
```
Context Budget: 4,500 tokens | Sources: [srs-fr-delta, hld-endpoint-inventory-delta, hld-communication-matrix-delta]
```
- `hld-communication-matrix-delta` = HLD.md Section 2 "Communication Matrix" table — the sequence/communication-diagram source for cross-container calls.

### drawio-diagram-architect
```
Context Budget: 900 tokens | Sources: [uml-mermaid-output-delta]
```
- LOW thinking tier (1,024) — mechanical Mermaid-to-Draw.io conversion needs only the already-produced Mermaid source, never the HLD directly. Corrected down hardest of all Phase 5 agents (6,000 → 900) because this agent's actual task has no independent HLD-reading requirement at all.

### mermaid-diagram-engineer
```
Context Budget: 900 tokens | Sources: [uml-mermaid-output-delta]
```
- Same rationale as drawio-diagram-architect: syntax review of already-generated Mermaid, not fresh authoring from the HLD.

---

## 4. Phase 6 — Sprint Planning

### scrum-master-agent
```
Context Budget: 4,800 tokens | Sources: [srs-fr-delta, uml-component-delta, hld-nfr-compliance-delta]
```
- `hld-nfr-compliance-delta` = HLD.md Section 9 NFR Compliance Map — needed for Smart AC generation on NFR-bearing stories.

### agile-tooling-specialist
```
Context Budget: 950 tokens | Sources: [backlog-draft-delta, sprint-plan-delta]
```
- LOW thinking tier (1,024) — this agent's job is Jira Epic/Story/Sprint API execution against an already-composed backlog, not independent HLD/SRS reasoning. Corrected hardest in this phase (8,000 → 950): it needs the finished backlog/sprint objects to create tickets from, nothing upstream of that.

### finops-analyst
```
Context Budget: 4,800 tokens | Sources: [backlog-draft-delta, hld-storage-topology-delta]
```
- `hld-storage-topology-delta` = HLD.md Section 2 container/storage table (Qdrant, OpenSearch, PostgreSQL, S3, Redis) — the cost-driver source for new-cloud-resource cost-estimate ACs, replacing a vague "backlog only" scope.

---

## 5. Phase 7 — Agent-Task Routing (routing cell)

**DPDP / AR.3 isolation rule (mandatory for every agent in this section):** any story or context slice sourced from Zone 2 (Episodic) or Zone 5 (Entity) may carry live conversational PII (HLD Section 10, assets list item 1; Section 10 DPDP-1..5). Per multi-agent-context-isolation-core Section 1 (Bell-LaPadula ceiling) and Section 6 (DPDP Act 2023 §6 consent-scoped access): PII-bearing content from Zone 2/Zone 5 stories is **excluded by default** from every agent's context window in this phase unless that agent is explicitly security-cleared (`clearance >= CONFIDENTIAL`, per the classification ceiling table below). None of the seven agents in this section carry that clearance by default — `security-testing-engineer`'s Phase F role is a compliance *auditor*, not a data-access role, and does not by itself grant PII read access. Where a Zone 2/5 story must be routed, the orchestrator substitutes a **redacted/pseudonymized** story summary (Presidio + India-PII regex, per multi-agent-context-isolation-core Section 4) — never the raw payload.

### orchestrator-agent (AR.0, scoped instance)
```
Context Budget: 10,000 tokens | Sources: [sprint-plan-delta, ar0-routing-index-delta]
```
- Classification ceiling: INTERNAL (routing metadata only — story titles, FR tags, agent capability vectors; never story body text).

### context-engineering-agent (AR.3, this agent's own Phase 7 task)
```
Context Budget: 8,000 tokens | Sources: [ar1-assignments-delta, hld-zone-ownership-delta]
```
- Classification ceiling: CONFIDENTIAL for the *design* task only (defining the isolation rule itself requires seeing which stories touch Zone 2/5 by reference, not their PII content) — reads `ar1-assignments-delta` (story→agent map) plus `hld-zone-ownership-delta` (Section 3.10, to identify which stories are Zone 2/5-sourced) to build the exclusion list, never the story payload text.

### prompt-generation-expert (AR.4)
```
Context Budget: 16,000 tokens | Sources: [ar1-assignments-delta, ar3-context-windows-delta]
```
- Classification ceiling: INTERNAL. Consumes `ar3-context-windows-delta` **post-redaction** — this agent generates CoT prompts from the already-isolated (PII-excluded) context windows this plan's AR.3 output produces, never from raw Zone 2/5 content directly.

### security-testing-engineer (Phase F.1/F.2)
```
Context Budget: 6,000 tokens | Sources: [ar4-prompts-delta, ar1-p1-override-delta]
```
- Classification ceiling: INTERNAL. Audits prompt *structure* (OWASP/WCAG checklist) against the generated prompts, not the underlying memory content — no PII exposure required for this agent's actual checklist-driven task.

### reliability-auditor (Phase D/E RS computation)
```
Context Budget: 4,000 tokens | Sources: [current-phase-scores-delta]
```
- Classification ceiling: INTERNAL. Consumes only numeric NLI/FactScore/DRE/Coverage scores, never story content.

### hallucination-detector / context-faithfulness-engineer (cross-cutting, recur every phase)
```
Context Budget: 4,000 tokens per invocation | Sources: [current artifact only]
```
- Already sane against their own thinking budget_tokens (32,000 each). Classification ceiling: INTERNAL — verifies the artifact under review against its cited source chunk only, never against the full GSD or raw Zone 2/5 payloads.

### consensus-agent (AR.5, 14-point gate)
```
Context Budget: 4,000 tokens | Sources: [ar5-checklist-delta]
```
- `ar5-checklist-delta` = the AR-Q-01–14 checklist results only, not the underlying prompts/context windows themselves — this is a binary gate over already-produced pass/fail signals.

---

## 6. Phase 8 — Pre-Implementation Alignment (self-review cell)

### business-analyst-agent / product-manager-agent / scrum-master-agent
```
Context Budget: 4,800 tokens each | Sources: [ar1-assignments-delta, implementation-execution-plan-delta]
```
- Corrected from 6,000 (registry) to 4,800, against MEDIUM thinking budget_tokens = 5,000.

### solution-architect
```
Context Budget: 6,000 tokens | Sources: [ar1-assignments-delta, implementation-execution-plan-delta]
```
- Already sane (6,000 vs EXCELLENCE 64,000) — no correction needed; this agent's Phase 8 role is feasibility/dependency review, not full re-architecture, so the registry's conservative 6,000 is retained as-is rather than raised toward the 64,000 ceiling.

### context-engineering-agent (conditional repair)
```
Context Budget: 8,000 tokens | Sources: [ir1-flag-summary-delta]
```
- Fires only when a story is flagged MISSING_CONTEXT/ARCHITECTURE_GAP at IR.1 — `ir1-flag-summary-delta` is the differential (flagged stories only), never the full `ar3-context-windows.json` re-read.

### hallucination-detector / context-faithfulness-engineer / reliability-auditor (IR.4 re-check)
```
Context Budget: 4,000 tokens per invocation | Sources: [current artifact only]
```
- Same as Phase 7 cross-cutting entry — sane, unchanged.

---

## 7. Summary Table

| Phase | Agent | Corrected Context Budget | Thinking budget_tokens | Sources |
|---|---|---|---|---|
| 1.5 | python-backend-engineer | 4,500 | 5,000 | hld-endpoint-inventory-delta, hld-adr-004-wire-protocol-delta, hld-nfr-latency-targets-delta |
| 1.5 | api-testing-engineer | 4,500 | 5,000 | openapi-draft-delta, hld-security-i1-i2-i3-delta |
| 1.5 | integration-testing-engineer | 4,500 | 5,000 | openapi-draft-delta, hld-event-contracts-delta |
| 2 | business-analyst-agent | 5,000 | 5,000 | prd-fr-delta, hld-component-list-delta |
| 2 | product-manager-agent | 5,000 | 5,000 | prd-fr-delta, hld-adr-delta-summary |
| 2 | solution-architect | 12,000 | 64,000 | prd-fr-delta, hld-approved-delta, joint-validation-issue-log-delta |
| 5 | uml-structural-diagram-engineer | 4,500 | 5,000 | srs-fr-delta, hld-component-list-delta, hld-data-ownership-map-delta |
| 5 | uml-behavioral-diagram-engineer | 4,500 | 5,000 | srs-fr-delta, hld-rotation-state-machine-delta, hld-failure-mode-table-delta |
| 5 | uml-interaction-diagram-engineer | 4,500 | 5,000 | srs-fr-delta, hld-endpoint-inventory-delta, hld-communication-matrix-delta |
| 5 | drawio-diagram-architect | 900 | 1,024 | uml-mermaid-output-delta |
| 5 | mermaid-diagram-engineer | 900 | 1,024 | uml-mermaid-output-delta |
| 6 | scrum-master-agent | 4,800 | 5,000 | srs-fr-delta, uml-component-delta, hld-nfr-compliance-delta |
| 6 | agile-tooling-specialist | 950 | 1,024 | backlog-draft-delta, sprint-plan-delta |
| 6 | finops-analyst | 4,800 | 5,000 | backlog-draft-delta, hld-storage-topology-delta |
| 7 | orchestrator-agent (AR.0) | 10,000 | 32,000 | sprint-plan-delta, ar0-routing-index-delta |
| 7 | context-engineering-agent (AR.3) | 8,000 | 10,000 | ar1-assignments-delta, hld-zone-ownership-delta |
| 7 | prompt-generation-expert (AR.4) | 16,000 | 16,000 | ar1-assignments-delta, ar3-context-windows-delta |
| 7 | security-testing-engineer | 6,000 | 16,000 | ar4-prompts-delta, ar1-p1-override-delta |
| 7 | reliability-auditor | 4,000 | 16,000 | current-phase-scores-delta |
| 7 | consensus-agent (AR.5) | 4,000 | 20,000 | ar5-checklist-delta |
| 7 | hallucination-detector / context-faithfulness-engineer | 4,000 | 32,000 | current artifact only |
| 8 | business-analyst-agent / product-manager-agent / scrum-master-agent | 4,800 each | 5,000 | ar1-assignments-delta, implementation-execution-plan-delta |
| 8 | solution-architect | 6,000 | 64,000 | ar1-assignments-delta, implementation-execution-plan-delta |
| 8 | context-engineering-agent (repair) | 8,000 | 10,000 | ir1-flag-summary-delta |
| 8 | hallucination-detector / context-faithfulness-engineer / reliability-auditor | 4,000 | 32,000 / 32,000 / 16,000 | current artifact only |

---

## 8. Structural Validation Statement

Every row above satisfies, per this document's mandate: (a) a non-empty `Context Budget: {N} tokens | Sources: [...]` line; (b) Differential GSD chunk names concrete and traceable to a real HLD/PRD/prior-phase artifact — none are placeholders; (c) `Context Budget <= thinking budget_tokens` for every agent (12 corrections applied against the registry's prior values, documented in Section 0). No agent in Phases 1.5/2/5/6/7/8 is missing from this plan.

## Status
COMPLETE — ready for `03-agent-registry.md` to be updated with the corrected Context Budget values in Section 0's table before Phase 1.5 dispatch begins.
