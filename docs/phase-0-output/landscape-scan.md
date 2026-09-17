# Dashanan -- Technology Landscape Scan: LLM/Agent Memory & Context Engineering

**Agent:** technology-scout-analyst | **Domain:** rnd-intelligence (D45)
**Sources:** training knowledge only (no live web search used this pass -- see note at end)
**Purpose:** feed PRD competitive-context section + solution-architect ADR "Alternatives Rejected" columns

---

## Note on library reference mismatch (reported per orchestration rule)

The dispatch header named `skills/technology-landscape-scanning-core` and
`skills/competitive-analysis-core` as mandatory skills. Neither directory
exists under `claude-global-library/skills/`. The actual `agent.md` for
`technology-scout-analyst` lists different mandatory skills --
`technology-horizon-scanning-core` and `information-retrieval-mastery-core`
-- both of which DO exist and were read in full before producing this scan.
This scan proceeds on the existing, correct mandatory skills; the two named
but nonexistent skills should be treated as a dispatch-header/agent.md drift
to fix at the routing layer, not invented.

---

## Area 1: Vector-DB-backed RAG memory (generic embedding-store recall)

**What exists:** The dominant pattern across agent frameworks (LangChain,
LlamaIndex, most custom agent stacks) -- conversation turns or documents are
embedded, written to a vector store (Pinecone/Chroma/Qdrant/pgvector/FAISS),
and top-k similarity search re-injects "relevant" chunks into the prompt at
generation time.

**What it does well:** Simple to bolt onto any LLM app; scales storage
independent of context window; works reasonably for document QA and
loosely-coupled fact recall; mature tooling and low integration cost.

**What gap remains:** Flat, single-tier -- no distinction between a
throwaway scratch fact and a durable user preference; no explicit
promotion/demotion lifecycle (everything just sits in the index until
manually pruned or TTL'd); recency and importance are usually bolted on as
ad-hoc metadata filters, not a first-class scoring function; no provenance
or confidence tracking on stored facts, so hallucinated or stale content
re-enters context with the same weight as verified content; retrieval
quality degrades as the index grows without active consolidation.

**How Dashanan differentiates:** Zone 6 (Retrieval-Index) is one of eight
governed zones, not the whole memory system -- it indexes content that has
already been triaged by zone-appropriate policy (Working/Episodic/Semantic/
Procedural/Entity), so retrieval quality benefits from upstream
structure rather than doing all the work alone. The Memory Score formula
(`w1*Recency + w2*Frequency + w3*Importance + w4*UserAffinity +
w5*TaskRelevance + w6*ProvenanceConfidence`) makes rotation an explicit,
tunable function instead of ad-hoc metadata filtering, and Zone 7
(Provenance/Audit) gives every retrieved item a confidence term the generic
RAG pattern has no equivalent for.

---

## Area 2: MemGPT-style hierarchical paging / virtual context management

**What exists:** MemGPT (and its productized descendant, Letta) treats the
LLM context window like OS virtual memory -- a small "core" context plus
paged-out "archival"/"recall" storage, with the model itself issuing
function calls to move data between tiers ("self-editing memory"). This was
an influential 2023-2024 framing that popularized the idea of an LLM
actively managing its own memory rather than a fixed pipeline doing it
externally.

**What it does well:** Elegant conceptual framing (paging is well
understood); gives the agent agency over what to keep "hot"; demonstrated
long-running conversational agents that don't just truncate.

**What gap remains:** Typically two-tier (core vs. archival), not multiple
independently-governed zones with different semantics (episodic vs.
semantic vs. procedural vs. entity are usually collapsed into one
undifferentiated archival store); relies heavily on the LLM's own judgment
calls for what to page in/out, which is expensive (extra tool calls) and
inconsistent across models; no first-class provenance/audit trail
independent of the paged content itself; compression, when present, is
generally an LLM-summarization pass with no explicit state machine
guaranteeing an item is never silently skipped from compression before
archival.

**How Dashanan differentiates:** Eight independently-governed zones instead
of a core/archival binary, each with its own rotation, compression, and
eviction policy. Rotation is driven by an explicit, auditable scoring
formula plus a strict `Active -> Compressed -> Archived` state machine
(with the ordering bug MemGPT-style ad-hoc score checks are prone to --
skipping straight from Active to Archived -- explicitly designed out, per
the PRD's ADR). Memory management is an orchestration-layer responsibility,
not something the host LLM must be prompted/tooled to do itself every turn.

---

## Area 3: Agentic long-context / "just use a bigger window" approaches

**What exists:** As commercial context windows have grown (100K-1M+
tokens), a competing strategy is to lean on raw window size plus prompt
caching rather than external memory architecture -- stuff recent history
and retrieved documents directly into the window each call.

**What it does well:** Zero additional infrastructure; no retrieval-miss
risk since everything relevant is (in theory) already present; prompt
caching mitigates the cost penalty for repeated large contexts.

**What gap remains:** Attention/recall quality degrades over very long
contexts even when the tokens are technically "in window" (well-documented
lost-in-the-middle and needle-in-haystack degradation effects); cost and
latency scale with context size regardless of whether most of it is
relevant to the current turn; no cross-session persistence -- once the
session ends or the window is exceeded, information is gone unless
externally saved; no structural distinction between working state,
long-term facts, and provenance, so context bloat and irrelevant-content
dilution are the two dominant failure modes at scale.

**How Dashanan differentiates:** Explicitly positioned as giving a much
larger *effective* context without requiring a larger native window --
the whole point of the 8-zone design is to keep the assembled prompt small
and high-signal (only what Working Memory + a Memory-Score-selected slice
of the other zones actually need this turn), rather than trying to win by
brute-force window size.

---

## Area 4: Managed memory products (Zep, Mem0, and similar "memory-as-a-service" layers)

**What exists:** A newer wave of dedicated memory services (Zep, Mem0, and
comparable offerings) that sit between an agent framework and storage,
typically providing automatic fact extraction, temporal knowledge graphs,
and a memory-retrieval API consumed via SDK.

**What it does well:** Purpose-built for memory rather than repurposed
document RAG; several incorporate temporal/graph structure (facts with
validity windows) which is a meaningful step past flat vector recall;
managed/hosted options reduce integration burden.

**What gap remains:** Typically single-provider, SaaS-coupled (vendor
lock-in tension with an "engine-agnostic, embeddable" requirement);
memory model is usually fact-graph-centric, without Dashanan's explicit
separation of procedural knowledge (how-to patterns), entity records, and
cross-entity semantic relationships as distinctly governed zones;
provenance/confidence tracking, where present, is generally implicit in
extraction confidence rather than a first-class, independently queryable
zone; promotion/demotion policy is generally not exposed as a tunable,
per-deployment configuration surface.

**How Dashanan differentiates:** Designed from the outset as an
engine-agnostic embeddable library with a pluggable storage adapter
interface (not a hosted SaaS dependency), explicit per-zone Entity vs.
Semantic ownership boundary (see the PRD's ADR: Entity = per-entity
records, Semantic = cross-entity relationships/general facts) as a formal,
enforced rule rather than an implicit graph-extraction side effect, and a
first-class Provenance/Audit zone (Zone 7) feeding directly into the
Memory Score's `ProvenanceConfidence` term -- provenance is a governed
input to rotation decisions, not just descriptive metadata.

---

## Area 5: LangChain / LlamaIndex memory modules (framework-native memory)

**What exists:** Built-in memory abstractions inside general-purpose agent
frameworks -- e.g. buffer memory, summary memory, entity memory, and
vector-store-backed memory classes that snap into a chain/agent's prompt
construction step.

**What it does well:** Zero extra infrastructure if already using the
framework; low-friction default for simple chat-history retention;
entity-memory variants show the field already recognizes entity tracking
as a distinct need (a partial precedent for Dashanan's Zone 5).

**What gap remains:** These are framework conveniences, not a
general-purpose reusable engine -- tightly coupled to the framework's own
abstractions, not portable across agent stacks or model providers; each
memory type (buffer/summary/entity) is usually chosen and configured
independently with no unifying orchestration policy connecting them;
no unified cross-zone scoring or rotation between memory types --
switching from buffer to summary memory is a manual configuration choice,
not a dynamic, score-driven promotion/demotion the system does
automatically.

**How Dashanan differentiates:** Framework-agnostic by design requirement
(pip-installable library + language-agnostic wire protocol), and the eight
zones are not independently-configured alternatives a developer picks one
of -- they are a single orchestrated system where the Memory Orchestrator
actively routes and rotates content across all of them via one shared
scoring function, exposed through one unified context-assembly API to the
host system.

---

## Summary for PRD / ADR consumption

Across all four families surveyed (generic vector-RAG, MemGPT-style
paging, brute-force long-context, and managed memory services / framework
modules), three gaps recur consistently and map directly to Dashanan's
stated differentiators:

1. **Flat or shallow-tiered storage** (1-2 tiers) vs. Dashanan's 8
   independently-governed zones with distinct semantics.
2. **No explicit, auditable rotation state machine** -- most competitors
   either hand judgment to the LLM at runtime or apply ad-hoc score
   thresholds without a guaranteed lifecycle -- vs. Dashanan's locked
   `Active -> Compressed -> Archived` state machine with ordering
   guarantees.
3. **No first-class provenance/confidence as a scoring input** -- vs.
   Dashanan's Zone 7 (Provenance/Audit) feeding `ProvenanceConfidence`
   directly into the Memory Score formula.

These three points are the recommended "Alternatives Rejected" anchors for
solution-architect's ADRs on the zone taxonomy and the rotation policy.

---

## Methodology note

This scan was produced from training knowledge (no web search invoked this
pass) per the Cooldown Research Protocol's allowance to work from training
knowledge when live search is not used, with findings labeled accordingly
rather than presented as sourced citations. Product/paper names
(MemGPT/Letta, Zep, Mem0, LangChain, LlamaIndex) reflect the state of the
field as of this agent's training cutoff and should be spot-checked by a
downstream agent with live web access if the PRD requires dated citations
or confirmation of current feature sets.

**Version:** v1.0.0 -- Phase 0 R&D output
