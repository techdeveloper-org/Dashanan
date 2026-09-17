## 0. PROJECT SUMMARY

**Dashasan** -- a reusable, embeddable *dynamic memory orchestration engine* for AI/LLM context systems, named after the ten-headed figure of Hindu epic tradition. The engine maintains **8 specialized, independently-governed memory zones** ("heads") across which context is rotated, promoted/demoted, compressed, and retrieved, so that any AI system plugging it in gets a much larger *effective* working context than its native window, backed by structured long-term memory rather than flat context stuffing.

`[REVISED after user architecture review, 2026-09-17: the original 10-zone taxonomy consolidated Temporal into Episodic (time-anchoring is now an attribute every Episodic entry carries, not a separate zone) and Summary/Compressed into Consolidation (compression is a mechanism Consolidation applies to aged content, not a separate storage zone it competes with). This is a deliberate engineering simplification of the original ten-heads concept -- the "Dashasan" mythology naming stays, the technical zone count is 8. This remains open for Phase 1 solution-architect to further validate or revise.]`

| # | Zone (head) | Purpose |
|---|---|---|
| 1 | Working Memory | Current turn/session scratch context, highest-frequency read/write, smallest TTL |
| 2 | Episodic Memory | Chronological, time-anchored record of interactions/events (absorbs the former "Temporal" zone as a per-entry recency/decay attribute), queryable by recency |
| 3 | Semantic Memory | Distilled facts/entities/relations extracted from episodes, deduplicated |
| 4 | Procedural Memory | Learned task procedures / tool-use patterns / successful action sequences |
| 5 | Entity Memory | Per-entity (person/project/system) profile records, incrementally updated |
| 6 | Retrieval-Index Memory | Vector + lexical index over all other zones for fast recall |
| 7 | Provenance/Audit Memory | Source attribution, confidence, edit history for every fact (anti-hallucination backbone) |
| 8 | Consolidation Memory | Long-term, cross-session consolidated store (sleep-cycle style promotion) -- absorbs the former "Summary/Compressed" zone: compression is the mechanism Consolidation applies to aged-out content, not a separate zone |

Each zone has its own **rotation policy** (promotion/demotion thresholds), **compression policy**, and **eviction policy**; a central **Memory Orchestrator** routes reads/writes across zones and exposes a single unified context-assembly API to the host AI system, driven by the Memory Score formula below.

`[INFERRED: reference implementation language = Python (primary), with a language-agnostic wire protocol (JSON/gRPC) so the engine can be embedded by AI systems written in any language -- inferred because the requirement explicitly says "reusable in any AI".]`
`[INFERRED: deployment shape = embeddable library (pip package) + optional standalone sidecar service (FastAPI/gRPC) for polyglot/microservice consumers -- inferred to satisfy "reusable product in any AI" without forcing in-process coupling.]`
`[INFERRED: storage backend = pluggable adapter interface, default adapter = local + a vector store (e.g. an embedding index) for zones 3/6, with a relational/KV store for zones 1/2/5/7 -- inferred since no storage preference was stated and portability was explicitly requested.]`

---

## CORE ALGORITHM: Memory Scoring & Rotation Policy

**This is the load-bearing algorithm of the entire engine -- without it, the zone taxonomy above is just a storage layout, not an orchestration system.** Flagged during architecture review as the single biggest gap in the first-pass bundle. This section seeds the algorithm's shape; **Phase 1 solution-architect's mandatory deliverable is to finalize exact threshold values, weight-tuning strategy, and a complexity/Big-O analysis, delegating the derivation to `mathematics-engineer` (opus, auto-invoked) per the Mathematical Delegation convention** -- do not treat the numbers below as final, treat the shape as final.

### Memory Score formula

```
MemoryScore(item) = w1*Recency + w2*Frequency + w3*Importance + w4*UserAffinity + w5*TaskRelevance + w6*ProvenanceConfidence
```

Each term, normalized to [0, 1]:

| Term | Definition | Shape |
|---|---|---|
| **Recency** | Exponential time-decay since last access | `Recency = exp(-lambda_zone * delta_t)`, where `delta_t` = time since last access/write and `lambda_zone` is a **per-zone half-life constant** (Working Memory: short half-life, minutes; Consolidation: long half-life, weeks-months) -- this is what makes rotation zone-aware rather than one global clock |
| **Frequency** | Access-count, log-scaled to avoid a few hot items dominating | `Frequency = log(1 + access_count) / log(1 + access_count_max_in_zone)` |
| **Importance** | Explicit signal (user-marked, or system-inferred from task-completion outcomes) | `Importance in [0,1]`, source: explicit user/agent tag, or an inferred salience model (Phase 1 to specify) |
| **UserAffinity** | Relevance to the current user/session identity | Cosine similarity between the item's owning-session embedding and the current session's embedding, or 1.0/0.0 for a strict per-tenant partition if cross-user sharing is disallowed (a Phase 1 ADR decision) |
| **TaskRelevance** | Relevance to the current task/query context | Cosine similarity between the item's embedding (Zone 6 Retrieval-Index) and the current task/query embedding |
| **ProvenanceConfidence** | How reliable this memory's source is | Read directly from Zone 7 (Provenance/Audit) -- e.g. `1.0` for a user-stated fact, lower for an LLM-inferred/summarized fact, lower still for a fact that failed a prior faithfulness check |

**Default weights (MVP, equal-weighted -- tunable per deployment):** `w1=w2=w3=w4=w5=w6=1/6`, sum-to-1 normalized. Phase 1 should treat these as a starting baseline, not a final answer -- different host AI systems (a coding agent vs. a customer-support agent) will plausibly want different weight profiles, so the weights should be a configuration surface, not a hardcoded constant.

### Rotation policy (promotion / compression / archive)

```
if MemoryScore(item) > PromoteThreshold:
    promote(item)      # move to a higher-retention / lower-latency zone (e.g. Episodic -> Working on re-access)
elif MemoryScore(item) < ArchiveThreshold:
    archive(item)       # move to Consolidation (Zone 8), compressed
elif MemoryScore(item) < CompressThreshold:
    compress(item)      # summarize in place (still in its current zone, footprint reduced)
# else: item stays in its current zone, unchanged
```

`PromoteThreshold > CompressThreshold > ArchiveThreshold` by construction; exact numeric values are a **Phase 1 solution-architect + mathematics-engineer deliverable** (informed by the deep-research brief on promotion/demotion algorithms from Phase 0 -- see `phase-0-rnd/research-brief.md`), not fixed here. The rotation policy runs on a per-zone schedule (event-driven on write for Working Memory's fast churn; periodic sweep for Consolidation's slow churn) -- the exact trigger cadence per zone is also a Phase 1 deliverable.

---

## YOUR TASK

Take Dashasan from a named idea through: (1) deep R&D into the state of the art for LLM memory/context-engineering systems, agentic long-context architectures, and rotation/compression/retrieval algorithms; (2) a Solution Architecture blueprint for the 8-zone Memory Orchestrator, validated to `consensus-agent` BINARY `APPROVED`; (3) a full OpenAPI 3.1.0 contract for the engine's embed/service API; (4) a joint BA+PM+SA validation pass; (5) formal SRS + 7 UML + 7 Draw.io documentation; (6) Jira sprint planning under a new `DASHASAN` project; (7) agent-task routing with CoT prompts for the first sprint; (8) pre-implementation alignment — and stop there for explicit user sign-off before any code is written.

Do **not** create the GitHub repository as part of this bundle — repo creation is a separate, explicitly-confirmed action the user must approve directly (organization-visible, public, irreversible-ish); it is out of scope for a prompt-generation deliverable and is called out again in the closing note below.

## CONSTRAINTS

```
Tech Stack:      Python 3.12+ reference implementation [INFERRED] + language-agnostic gRPC/JSON wire protocol; pluggable storage adapters
Platform:        Cross-platform library (Linux/macOS/Windows) + optional containerized sidecar service
Scale:           [INFERRED — no numbers given] design for: embeddable in a single-process agent (low QPS, in-memory) UP TO a shared multi-tenant memory service (thousands of QPS, external storage) — capacity estimation is a Phase 1 deliverable, not assumed here
Timeline:        Production-ready, iterative (R&D → architecture → consensus loop explicitly requested — no fixed deadline)
Compliance:      DPDP Act 2023 (India) if any zone stores PII from conversations — flagged for Phase 1 threat model; no other regulation indicated
Special Needs:   Pluggable/portable (any AI system), long-context support, offline-capable local mode, provenance/anti-hallucination tracking (Zone 7) is a first-class requirement, not an afterthought
Hallucination Risk: MEDIUM (general AI infrastructure — a bug here degrades every downstream AI system's factual grounding, but Dashasan itself is not medical/legal/finance-regulated)
Security Risk:   HIGH (a memory engine is a prime target for prompt-injection-driven memory poisoning and cross-tenant data leakage — full Phase F depth required once implementation starts)
Thinking Budget: AUTO — assigned per agent via STEP 4.5 below
```

---

