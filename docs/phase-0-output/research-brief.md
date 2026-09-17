# Dashanan — Phase 0 Research Brief

**Author:** research-strategist (R&D Intelligence Domain)
**Date:** 2026-09-17
**Method:** Cooldown Research Protocol — 3 WebSearch calls (search -> synthesize -> search), scaffolded with training-knowledge synthesis. Each finding below is labeled `[WEB]` (confirmed via a 2025-2026 web search this session), `[TRAINING]` (synthesized from pre-cutoff training knowledge, not independently re-verified this session), or `[INFERRED]` (this agent's own reasoned extrapolation, not sourced from either).
**Scope discipline:** exactly 4 research questions per dispatch, no follow-on searches beyond the 3 used. Any further depth is explicitly deferred to Phase 1.

---

## 1. Hierarchical / multi-tier memory architectures (MemGPT/Letta virtual context paging)

**Key findings:**
- `[WEB]` MemGPT pioneered an OS-inspired virtual context management model: the LLM performs explicit memory operations moving data between a fixed **main context** ("RAM") and external **archival/recall storage** ("disk"), via function calls it invokes itself (`archival_memory_search`, `core_memory_append`, etc.), triggered by an interrupt mechanism on each user message or timer event.
- `[WEB]` Letta (MemGPT's production successor) refines this into three concrete tiers: **Core Memory** (small, always-resident block — persona/user info, i.e. never evicted), **Recall Storage** (disk-backed, searchable log of all past messages), **Archival Storage** (vector-indexed cold storage for documents/long-term knowledge).
- `[WEB]` This three-tier shape (always-resident / queryable-recent / vector-indexed-cold) recurs across 2025-2026 follow-on work (MEMTIER's tiered-memory retrieval-bottleneck analysis; MOSS's auditable agentic memory architecture), suggesting it is now a settled reference pattern, not a one-off design.
- `[TRAINING]` The paging metaphor's core insight for orchestration engines specifically: memory movement is **self-directed and event-triggered** (an agent explicitly "pages in" what it needs), not purely a background daemon sweep — retrieval and promotion are the same act.

**Recommendation for Dashanan:**
- The MemGPT/Letta 3-tier shape maps cleanly onto a subset of Dashanan's 8 zones: **Working (Zone 1) ~ Core Memory** (always-resident, smallest TTL), **Episodic (Zone 2) ~ Recall Storage** (queryable-by-recency log), **Consolidation (Zone 8) ~ Archival Storage** (vector-indexed cold store). This is independent validation that the Working->Episodic->Consolidation *spine* of Dashanan's rotation is architecturally sound, not just a mythology-driven zone count.
- Adopt the **self-directed paging** principle explicitly for the `Promote` transition already specified in the PRD's state machine ("any state -> higher-retention zone, e.g. Episodic -> Working **on re-access**"): re-access-triggered promotion is exactly MemGPT's pattern of "retrieval IS a memory operation," not a side effect. Recommend Phase 1 formalize this as: every read against Zone 2/3/4/5/8 that resolves a hit re-evaluates that item's `MemoryScore` immediately (not only on the next scheduled sweep) — closing the loop between retrieval and rotation.
- Recommend the per-zone rotation cadence in the PRD (event-driven for Working's fast churn, periodic sweep for Consolidation's slow churn) be extended with this same event-driven trigger for **any zone that receives a read against a Compressed or Archived item** — a read is itself evidence of relevance the score formula should react to immediately, not wait for the next sweep to reflect.

**Confidence / source-mode:** High for the tier-mapping validation (directly web-confirmed against current 2025-2026 literature); Medium for the specific re-access-triggers-immediate-rescoring recommendation (this agent's `[INFERRED]` extension of the pattern, not itself found stated as a rule in the sources).

---

## 2. Promotion/demotion & compression algorithms vs. Dashanan's Active->Compressed->Archived state machine

**Key findings:**
- `[WEB]` 2025-2026 literature (MEMTIER; "Remember the Decision, Not the Description" rate-distortion framework for agent memory) treats **compression as a distinct, lossy-but-recoverable transformation** — a summarization step with its own fidelity/footprint tradeoff — separate from **eviction/archival**, which is a storage-location change. This is the same distinction Dashanan's PRD already encodes: "Compressed is a state, not Zone 8."
- `[WEB]` Time-based/recency decay combined with selective forgetting is the dominant demotion mechanism across surveyed 2025-2026 agent-memory systems; several explicitly implement "soft forgetting" (gradually decaying weight rather than hard deletion) before any hard archival/eviction step — i.e., a graduated, multi-stage decay path, not a single threshold cliff.
- `[TRAINING]` The classic prior-art precedent for this pattern is the Generative Agents (Park et al., 2023) memory stream: a weighted-sum score (recency + importance + relevance) drives what surfaces in the agent's context, with recency itself an exponential decay — the same shape as Dashanan's `Recency = exp(-lambda_zone * delta_t)` term, and the same "score-driven, not queue-driven" rotation philosophy.

**Recommendation for Dashanan — direct evaluation against the locked 3-state model:**
- **The research supports the Active->Compressed->Archived model as specified, with no structural refinement needed.** The literature's compression-as-distinct-from-eviction distinction independently validates the exact bug-fix rationale already recorded in the PRD (state-gated transitions preventing compression skip). No counter-evidence was found for a 2-state or 4-state alternative being preferable at MVP scope.
- One genuine refinement candidate, **flagged as a Phase 1 option, not a required change**: the "soft forgetting" pattern (graduated weight decay before hard demotion) suggests `CompressThreshold` itself could eventually be a *decaying* threshold rather than a fixed constant (an item that has been Active a long time without re-access could compress "faster" than a newly-written item at the same instantaneous score) — this is a tuning-strategy refinement, not a state-machine refinement, and fits squarely inside the PRD's own note that weight-tuning strategy is a Phase 1 solution-architect + mathematics-engineer deliverable.
- Confirm the PRD's own flagged exception — "whether extremely fast score-collapse should ever fast-track Compressed+Archived in one sweep" — is a **reasonable question to leave open**: the rate-distortion framework's treatment of compression as a fidelity/footprint tradeoff implies a fast-collapsing item (e.g. a single-turn scratch note) may have near-zero information loss from being compressed and archived in the same sweep, since there is negligible fidelity to preserve at the intermediate step. Recommend Phase 1 define fast-track eligibility by **content type/size**, not purely by score-collapse velocity.

**Concrete numeric starting points for Phase 1 (heuristic defaults, not derived from a specific empirical study — `[INFERRED]`, explicitly provisional):**
- Threshold *ratios* (scores normalized [0,1], `PromoteThreshold > CompressThreshold > ArchiveThreshold` per the locked ordering): **Promote ~0.75, Compress ~0.35, Archive ~0.15** as MVP defaults — wide enough bands to avoid thrashing between states on small score fluctuations, narrow enough that the middle "stay unchanged" band isn't the dominant outcome for most items.
- Per-zone half-life `lambda_zone` for the Recency term, in rough order-of-magnitude terms consistent with each zone's stated purpose in the PRD table: **Working ~15-30 minutes; Episodic ~1-3 days; Semantic/Entity ~30-90 days (slow decay — these are durable facts/records, not scratch); Procedural ~60-180 days; Consolidation ~180-365 days.** Retrieval-Index (Zone 6) and Provenance (Zone 7) are index/metadata layers that inherit their host item's recency rather than decaying independently.

**Confidence / source-mode:** Medium-High for the state-machine validation (web-confirmed pattern match); Low-Medium for the specific numeric thresholds/half-lives (`[INFERRED]` starting points explicitly requiring empirical tuning — Phase 1's mathematics-engineer deliverable, not a substitute for it).

---

## 3. Hybrid retrieval scoring (vector + lexical) for TaskRelevance / Zone 6

**Key findings:**
- `[WEB]` **Reciprocal Rank Fusion (RRF)** is the dominant 2025-2026 production pattern for combining lexical (BM25) and vector/semantic retrieval: it fuses **ranked lists**, not raw scores, specifically because BM25 and cosine-similarity scores live on incompatible scales and naive linear combination misweights one or the other depending on corpus statistics. RRF is used under the hood by Elasticsearch's `rrf` retriever, OpenSearch hybrid search, Weaviate, Qdrant, and Azure AI Search as of this search.
- `[WEB]` Current production memory systems (e.g. Mem0's 2025-2026 benchmarked approach) score retrieved memories via a **weighted mix of recency (exponential decay) + relevance (embedding similarity) + importance** — explicitly described as "a substantial improvement over pure cosine similarity." This is structurally the same multi-term weighted formula shape as Dashanan's own `MemoryScore`, which is independent validation that Dashanan's overall formula shape (not just its Recency term) matches current production practice, not just academic prior art.

**Recommendation for Dashanan — concrete and actionable for Phase 1's retrieval-layer design:**
- Implement **TaskRelevance** as an RRF-fused score, not a raw cosine similarity: run the current task/query embedding against Zone 6's vector index AND a lexical/BM25 index over the same candidate items, fuse the two ranked candidate lists via RRF (`score = sum over retrievers of 1/(k + rank)`, k typically 60 per common defaults), then min-max normalize the fused RRF score into [0,1] before it enters the `MemoryScore` weighted sum (which already expects each term pre-normalized to [0,1]).
- This directly resolves an open question implicit in the PRD's current TaskRelevance definition ("Cosine similarity between item embedding and task/query embedding") — pure cosine is exactly what the Mem0 finding flags as inferior to a fused approach. Recommend Phase 1 solution-architect adopt hybrid RRF fusion as TaskRelevance's actual implementation, with the PRD's cosine-similarity language treated as the semantic half of the fusion, not the whole mechanism.
- Zone 6 (Retrieval-Index) should therefore maintain **two indexes per applicable zone** — a vector index (existing plan) plus a lexical/inverted index (BM25) — rather than vector-only. This has a direct capacity-estimation consequence Phase 1 should account for: roughly 1.5-2x the index storage/maintenance cost of a vector-only design, in exchange for meaningfully better recall on exact-term/entity-name queries (a known weak spot of pure embedding retrieval, and directly relevant to Zone 5 Entity Memory's per-entity lookup pattern).

**Confidence / source-mode:** High — directly web-confirmed as current (2025-2026) production practice across multiple independent vendors, not a single vendor's marketing claim.

---

## 4. Provenance/attribution tracking for anti-hallucination (Zone 7 / ProvenanceConfidence)

**Key findings:**
- `[WEB]` Current (2025-2026) research explicitly frames the gap most long-term agent memory systems have: they store facts but provide "limited support for tracing where memory items came from, whether they remain valid, whether they conflict with newer evidence, and how they influence later claims or actions" — precisely the gap Dashanan's PRD names Zone 7 to close.
- `[WEB]` The recommended provenance metadata schema converges on: **source attribution, write time, update history, retrieval context, downstream usage, conflict status, and invalidation events.** A "source-grounded fact record" pattern is recommended over raw retrieval hits — forcing any answer-generation step to operate within a provenance-scoped boundary and return "insufficient evidence" rather than unsupported generation.
- `[WEB]` Anti-hallucination research frames grounding as a **trace constraint**: every entity/claim in a final answer must be traceable to a specific prior source or tool output — a single un-traceable entity is flagged as capable of corrupting an entire downstream action chain (entity-level hallucination, not just phrase-level).
- `[WEB]` Memory poisoning (adversarial or accidental corruption of long-term memory) is treated as a first-class security concern in current literature, not an edge case — directly matching the PRD's own "Security Risk: HIGH... prime target for prompt-injection-driven memory poisoning" flag.

**Recommendation for Dashanan:**
- Adopt the converged schema directly for Zone 7's record shape: per fact, store **`source_type`** (user-stated / LLM-inferred / tool-output / summarized-from-N-sources), **`write_timestamp`**, an **append-only `update_history`** log, **`conflict_status`** (flag when a new write contradicts an existing Zone 3/5 record), **`invalidation_flag`**, and the numeric **`ProvenanceConfidence`** score itself. This is a direct, low-ambiguity implementation of the PRD's own worked example ("1.0 for a user-stated fact, lower for an LLM-inferred/summarized fact, lower still for a fact that failed a prior faithfulness check") — the literature confirms this exact taxonomy rather than suggesting a different one.
- Recommend a **conflict-detection sweep** as a Phase 1 candidate mechanism not yet explicit in the PRD: whenever a new write targets Zone 3 (Semantic) or Zone 5 (Entity), check it against existing records for the same entity/relationship; on contradiction, do not silently overwrite — mark both old and new records `conflict_status=disputed` and downgrade both records' `ProvenanceConfidence` until resolved (by re-confirmation, explicit user correction, or majority-source consensus). This operationalizes the "conflicts with newer evidence" gap the research flags, and gives the Memory Score formula's `ProvenanceConfidence` term a concrete, non-static computation path rather than a one-time-assigned constant.
- Recommend the trace-constraint framing be applied at the **retrieval boundary**, not generation time: any item Zone 6 returns for context assembly should carry its Zone 7 provenance record attached, so the host AI system consuming Dashanan's unified context-assembly API can itself enforce "every claim traces to a source" — this is a Zone 6 <-> Zone 7 API-contract implication for Phase 1.5's OpenAPI design, not just a Zone 7-internal concern.

**Confidence / source-mode:** High — directly web-confirmed, and the findings align closely with (rather than contradict) the PRD's own already-stated Zone 7 design intent, reinforcing rather than revising it.

---

## Summary for Phase 1 hand-off

| Open PRD item | This brief's input |
|---|---|
| Validate/refine Active->Compressed->Archived state machine | **Supported as-is.** No structural change recommended; one optional tuning refinement flagged (graduated `CompressThreshold`), one fast-track eligibility rule recommended (by content type/size, not score-velocity alone). |
| `PromoteThreshold` / `CompressThreshold` / `ArchiveThreshold` exact values | Provisional starting ratios given (0.75 / 0.35 / 0.15) — explicitly `[INFERRED]`, for mathematics-engineer to derive/tune, not adopt verbatim. |
| Per-zone `lambda_zone` half-life constants | Order-of-magnitude starting points given per zone (minutes -> months) — same provisional status. |
| TaskRelevance term implementation | **Concrete recommendation:** RRF-fused hybrid (vector + BM25) rank score, normalized to [0,1] — supersedes pure-cosine as originally sketched in the PRD table. |
| ProvenanceConfidence term implementation | **Concrete recommendation:** schema + conflict-detection sweep given; ready for Phase 1 data-model formalization. |
| Zone 6 <-> Zone 7 API contract implication | New: provenance record should travel with every Zone 6 retrieval hit — input to Phase 1.5 OpenAPI design. |

**Sources (from the 3 web searches used):**
- [MemGPT: Hierarchical Memory Management](https://www.emergentmind.com/topics/memgpt-style-memory-management)
- [Hindsight vs Letta (MemGPT): Agent Memory Compared (2026)](https://vectorize.io/articles/hindsight-vs-letta)
- [MEMTIER: Tiered Memory Architecture and Retrieval Bottleneck Analysis for Long-Running Autonomous AI Agents](https://arxiv.org/pdf/2605.03675)
- [Memory-Orchestrated Semantic System (MOSS): An Auditable Agentic Memory Architecture](https://arxiv.org/pdf/2607.04391)
- [Remember the Decision, Not the Description: A Rate-Distortion Framework for Agent Memory](https://arxiv.org/pdf/2605.10870)
- [Hybrid Search for RAG: BM25, Vector Retrieval, and RRF Architecture](https://medium.com/data-science-collective/hybrid-search-for-rag-bm25-vectors-when-each-wins-402f24abaeea)
- [Reciprocal Rank Fusion: the one-line algorithm behind hybrid search](https://blog.serghei.pl/posts/reciprocal-rank-fusion-explained/)
- [State of AI Agent Memory 2026: Benchmarks & Trends Report (Mem0)](https://mem0.ai/blog/state-of-ai-agent-memory-2026)
- [From Agent Traces to Trust: A Survey of Evidence Tracing and Execution Provenance in LLM Agents](https://arxiv.org/pdf/2606.04990)
- [Mitigating Provenance-Role Collapse in Long-Term Agents via Typed Memory Representation](https://arxiv.org/html/2605.25869)
- [Securing LLM-Agent Long-Term Memory Against Poisoning: Non-Malleable, Origin-Bound Authority with Machine-Checked Guarantees](https://arxiv.org/pdf/2606.24322)
