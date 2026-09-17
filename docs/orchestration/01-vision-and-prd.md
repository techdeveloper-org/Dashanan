## 0. PROJECT SUMMARY

**Dashanan** -- a reusable, embeddable *dynamic memory orchestration engine* for AI/LLM context systems, named after the ten-headed figure of Hindu epic tradition. The engine maintains **8 specialized, independently-governed memory zones** ("heads") across which context is rotated, promoted/demoted, compressed, and retrieved, so that any AI system plugging it in gets a much larger *effective* working context than its native window, backed by structured long-term memory rather than flat context stuffing.

`[REVISED after user architecture review, 2026-09-17: the original 10-zone taxonomy consolidated Temporal into Episodic (time-anchoring is now an attribute every Episodic entry carries, not a separate zone) and Summary/Compressed into Consolidation (compression is a mechanism Consolidation applies to aged content, not a separate storage zone it competes with). This is a deliberate engineering simplification of the original ten-heads concept -- the "Dashanan" mythology naming stays, the technical zone count is 8. This remains open for Phase 1 solution-architect to further validate or revise.]`

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

## ADR: Zone 3 (Semantic) vs. Zone 5 (Entity) ownership boundary

`[ADDED after user architecture review, 2026-09-17: flagged as the biggest remaining ambiguity -- a fact like "Sachin lives in Mumbai" could plausibly be written to either zone, and without a strict ownership rule two zones would silently duplicate or fragment the same fact.]`

**ADR: Ownership Rule**
```
Chosen:    Entity Memory (Zone 5) = per-entity RECORDS (attributes owned by exactly one entity).
           Semantic Memory (Zone 3) = cross-entity RELATIONSHIPS and general facts not owned by
           any single entity record.

Why:       A record/relationship split is a well-established graph-modeling boundary (node
           properties vs. edges) -- it gives every fact exactly one canonical home, which is
           the actual requirement (avoid duplication/fragmentation), not just a naming choice.
           It also composes cleanly with Zone 6 (Retrieval-Index): an entity lookup resolves to
           one record; a relationship query traverses Semantic edges between records.

Rejected:  Merge Zone 3 and Zone 5 into one zone -- rejected because entity records and general
           semantic facts have different access patterns (point lookup by entity ID vs. similarity/
           graph-traversal search) and different rotation/decay behavior (an entity's own profile
           record should decay far slower than a one-off inferred relationship fact).
           Classify by confidence/source instead of structure -- rejected because it doesn't
           resolve the actual ambiguity (a low-confidence fact can still be entity-owned or
           relational; confidence is already handled separately, by ProvenanceConfidence).
```

**Worked example:** `"Sachin lives in Mumbai"` decomposes as: `Entity Memory` gets/updates the record `Entity(Sachin).current_city = "Mumbai"` (an attribute of the Sachin entity record); `Semantic Memory` optionally gets the general relational fact `located_in(Sachin, Mumbai)` only if something downstream needs to query it as a graph edge independent of loading the full Sachin record (e.g. "who lives in Mumbai?" -- a query Entity Memory's per-entity lookup can't answer efficiently, but a Semantic relationship index can). In the common case, a single-entity attribute fact like this lives ONLY in Entity Memory; Semantic Memory is reserved for facts that genuinely span or relate multiple entities, or general world knowledge not owned by any one entity record (e.g. "Mumbai is a city in Maharashtra" -- a fact about Mumbai as a place, not about any person-entity). This exact rule -- **Entity = record, Semantic = relationship/general-fact, and single-entity attributes default to Entity-only** -- is the mandatory ownership test every write-path decision uses; Phase 1 solution-architect formalizes this into the HLD's data model with exact schema-level enforcement.

---

## CORE ALGORITHM: Memory Scoring & Rotation Policy

`[APPROVED by user review, 2026-09-17: the Memory Score formula SHAPE below (the 6 weighted terms, the per-zone-half-life Recency model, and the Promote/Compressed/Archived STATE MACHINE -- fixed to a strict lifecycle after a second review round caught an ordering bug that could skip compression) is locked in as the Phase 1 starting point. Explicitly still open for Phase 1: the exact numeric threshold values, whether weights should be non-equal (Importance/TaskRelevance/ProvenanceConfidence weighted higher than Recency/Frequency is a flagged future ADR candidate, not decided yet), weight-tuning strategy, and complexity/Big-O analysis -- those remain a mandatory solution-architect + mathematics-engineer deliverable, not fixed here. Also explicitly still open, scoped to Phase 1 (solution-architect) / Phase 1.5 (API contract): Retrieval-layer design detail, Provenance implementation detail, multi-tenant architecture, the OpenAPI contract, and storage-abstraction design -- none of these are gaps in this PRD, they are correctly deferred to their own phases, not yet due at Phase 0.]`

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

### Rotation policy (promotion / compression / archive) -- explicit state machine

`[FIXED after user architecture review, 2026-09-17: the original single if/elif score check had an ordering bug -- a fast-decaying item could score below ArchiveThreshold before it was ever compressed, skipping compression entirely and losing the summarized-but-recoverable intermediate form. Fixed by making compression and archival separate STATE transitions, not just separate score bands -- an item cannot reach Archived without having passed through Compressed first.]`

Every item carries an explicit **lifecycle state**, not just a score: `Active -> Compressed -> Archived` (plus `Promoted`, a re-entry to a higher-retention zone from any state). State transitions are gated by BOTH the current state AND the score, so compression can never be skipped:

```
# item.state in {Active, Compressed, Archived}; evaluated on each zone's rotation sweep

if MemoryScore(item) > PromoteThreshold:
    promote(item)              # any state -> higher-retention/lower-latency zone (e.g. Episodic -> Working on re-access)
                                # a promoted item's state resets to Active in its new zone

elif item.state == Active and MemoryScore(item) < CompressThreshold:
    compress(item)              # Active -> Compressed: summarize in place, footprint reduced, still recoverable
                                # NOTE: item stays in its current zone at this step -- Compressed is a state, not Zone 8

elif item.state == Compressed and MemoryScore(item) < ArchiveThreshold:
    archive(item)                # Compressed -> Archived: move to Consolidation (Zone 8)
                                  # only reachable from Compressed -- an Active item can never jump straight to Archived

# else: item stays in its current state/zone, unchanged
```

`PromoteThreshold > CompressThreshold > ArchiveThreshold` by construction. This guarantees a strict **Active -> Compressed -> Archived** lifecycle even for an item whose score drops fast (e.g. across multiple rotation sweeps) -- it is compressed on the sweep where it first crosses `CompressThreshold`, and only archived on a later sweep once it is already `Compressed` and has further crossed `ArchiveThreshold`. Exact numeric threshold values, the per-sweep cadence, and whether extremely fast score-collapse should ever fast-track Compressed+Archived in one sweep (an explicit exception, not the default) are a **Phase 1 solution-architect + mathematics-engineer deliverable** (informed by the deep-research brief on promotion/demotion algorithms from Phase 0 -- see the Phase 0 research brief once produced), not fixed here. The rotation policy runs on a per-zone schedule (event-driven on write for Working Memory's fast churn; periodic sweep for Consolidation's slow churn) -- the exact trigger cadence per zone is also a Phase 1 deliverable.

---

## YOUR TASK

Take Dashanan from a named idea through: (1) deep R&D into the state of the art for LLM memory/context-engineering systems, agentic long-context architectures, and rotation/compression/retrieval algorithms; (2) a Solution Architecture blueprint for the 8-zone Memory Orchestrator, validated to `consensus-agent` BINARY `APPROVED`; (3) a full OpenAPI 3.1.0 contract for the engine's embed/service API; (4) a joint BA+PM+SA validation pass; (5) formal SRS + 7 UML + 7 Draw.io documentation; (6) Jira sprint planning under a new `DASHANAN` project; (7) agent-task routing with CoT prompts for the first sprint; (8) pre-implementation alignment — and stop there for explicit user sign-off before any code is written.

Do **not** create the GitHub repository as part of this bundle — repo creation is a separate, explicitly-confirmed action the user must approve directly (organization-visible, public, irreversible-ish); it is out of scope for a prompt-generation deliverable and is called out again in the closing note below.

## CONSTRAINTS

```
Tech Stack:      Python 3.12+ reference implementation [INFERRED] + language-agnostic gRPC/JSON wire protocol; pluggable storage adapters
Platform:        Cross-platform library (Linux/macOS/Windows) + optional containerized sidecar service
Scale:           [INFERRED — no numbers given] design for: embeddable in a single-process agent (low QPS, in-memory) UP TO a shared multi-tenant memory service (thousands of QPS, external storage) — capacity estimation is a Phase 1 deliverable, not assumed here
Timeline:        Production-ready, iterative (R&D → architecture → consensus loop explicitly requested — no fixed deadline)
Compliance:      DPDP Act 2023 (India) if any zone stores PII from conversations — flagged for Phase 1 threat model; no other regulation indicated
Special Needs:   Pluggable/portable (any AI system), long-context support, offline-capable local mode, provenance/anti-hallucination tracking (Zone 7) is a first-class requirement, not an afterthought
Hallucination Risk: MEDIUM (general AI infrastructure — a bug here degrades every downstream AI system's factual grounding, but Dashanan itself is not medical/legal/finance-regulated)
Security Risk:   HIGH (a memory engine is a prime target for prompt-injection-driven memory poisoning and cross-tenant data leakage — full Phase F depth required once implementation starts)
Thinking Budget: AUTO — assigned per agent via STEP 4.5 below
```

---

