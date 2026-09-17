# SP.0.5 FinOps Analyst Review — DASH-STORY-007 Infrastructure Cost Gate

**Document ID:** SP05-FINOPS-REVIEW-20260917-01
**Version:** 1.0.0
**Author:** finops-analyst
**Date:** 2026-09-17
**Reviewed against:** `docs/phase-6-sprint-planning/backlog_draft.json` v1.0.0 (DASH-STORY-007), `docs/phase-1-architecture/HLD.md` Section 12F / ADR-007 / ADR-008 / Section 12D
**Trigger:** `infra_cost_relevant = true` — DASH-STORY-007 (Zone 6 Retrieval-Index) provisions new vector-store (Qdrant, ADR-007) and lexical-index (OpenSearch, ADR-008) infrastructure.

---

## 1. Purpose

Define the minimal tagging taxonomy for DASH-STORY-007's new infrastructure, add a cost-estimate acceptance criterion that distinguishes average from marginal per-query retrieval cost, and flag any multi-cloud/multi-vendor cost-governance gap in the current Zone 6 storage topology, per `cloud-cost-allocation-tagging-core` and `finops-unit-economics-core`.

## 2. Tagging Taxonomy for DASH-STORY-007 Infrastructure

DASH-STORY-007 provisions two new, independently billed resource classes: the Qdrant vector collections and the OpenSearch lexical indices, both per-tenant partitioned (ADR-007/ADR-013). Applying the minimal viable taxonomy (`cloud-cost-allocation-tagging-core` Section 1):

| Tag | Value for DASH-STORY-007 | Rationale |
|---|---|---|
| `cost-center` / `team` | `dashanan-memory-platform` | Single owning team per HLD Section 12F; no cross-team shared-cost split needed yet (Shapley/M1 not applicable until a second team consumes Zone 6). |
| `environment` | `prod` \| `staging` \| `dev` | Standard three-tier split; Profile A/B/C (Section 12D) scale bands should map to environment, not a separate tag, to avoid taxonomy sprawl. |
| `application` / `service` | `zone6-retrieval-index` | Distinguishes Zone 6 spend from the other 7 zones and the Orchestrator facade for roll-up (M5). |
| `owner` | `dashanan-index-worker` (service account) | Points cost-at-risk investigation (M3) at the actual write-path component (HLD line 98). |
| **`resource-class`** (Zone-6-specific addition) | `vector-index` \| `lexical-index` | **Mandatory 5th tag for this story.** Without it, the two vendor cost surfaces (Qdrant vs. OpenSearch) cannot be separated in a consolidated bill, which blocks both the average/marginal decomposition (Section 3) and the multi-vendor governance flag (Section 4). |
| `tenant_id` | per-tenant partition key | Already structurally mandatory per ADR-007/ADR-013 (physical per-tenant collections/indices) and the `ZoneRepository` port contract — reuse it as a cost tag, not a separate concept, so tenant-level showback (a likely future ask given per-tenant physical partitioning) requires no new instrumentation. |

**Enforcement tier recommendation:** **preventive**, not advisory, from day one for `resource-class` and `tenant_id` specifically — not the default per M3's "measure cost-at-risk under advisory first" rule. Justification: both tags are already structurally required by ADR-007/ADR-013 for tenant isolation (a security control, not just a FinOps one), so policy-as-code enforcement at provisioning time costs nothing additional beyond what tenant-isolation correctness already demands, and skipping it would let untagged Zone 6 spend accumulate with zero measured cost-at-risk trend to justify catching up later. `cost-center`/`environment`/`application`/`owner` follow the standard **advisory-first** tier since they are new for this story and have no such pre-existing structural driver.

## 3. Cost-Estimate Acceptance Criterion (Add to DASH-STORY-007)

Current DASH-STORY-007 ACs (AC-006, AC-014) cover retrieval correctness and tenant isolation only — neither addresses cost. Per `finops-unit-economics-core` M1, add the following AC distinguishing average from marginal cost per unit (a per-query retrieval, not total monthly spend):

> **AC-006-COST-1:** Given Zone 6's vector-index (Qdrant) and lexical-index (OpenSearch) infrastructure is provisioned for a target scale profile (Section 12D Profile A/B/C), When the sprint's cost estimate is produced, Then the estimate SHALL report (a) **average cost per retrieval query** = `(C_fixed_vector + C_fixed_lexical + c_var·V) / V` for the target query volume `V`, inclusive of both vendor surfaces, and (b) **marginal cost per additional retrieval query** = `c_var` (the incremental Qdrant + OpenSearch cost of one more query, holding provisioned capacity fixed), reported separately — average cost SHALL NOT be presented alone as an efficiency indicator, since at low Sprint-1 query volume it is dominated by fixed-capacity amortization (HNSW index build, per-tenant collection/index baseline) rather than genuine per-query efficiency.

This AC is scoped to require the *reporting distinction*, not a specific numeric target — the actual `c_var` figure depends on the chosen scale profile and embedding-provider pricing (HLD line 59, OAQ-15, not yet resolved), so a hard numeric AC would be premature at Sprint 1.

## 4. Multi-Cloud / Multi-Vendor Cost-Governance Gap

**Flagged.** Zone 6's storage topology is two separately billed vendor surfaces — Qdrant (vector, ADR-007) and OpenSearch (lexical, ADR-008) — fused only at the application layer via Reciprocal Rank Fusion (HLD line 1026), with no shared billing, no shared commitment/reservation vehicle, and no unified cost dashboard implied anywhere in HLD Section 12F or the ADRs. This is a `multi-cloud-cost-governance-core` concern layered on top of the tagging/unit-economics work above:

- **No cross-surface roll-up defined yet.** HLD Section 12D gives combined storage figures (74.7 GB vectors + ~300 GB lexical at Profile C, line 700) but the *cost* roll-up consistency check (M5: `C(root) = Σ children`) has no defined parent node spanning both vendors — DASH-STORY-007's cost estimate (Section 3 AC) must sum Qdrant + OpenSearch explicitly rather than treating either vendor's bill as the whole of Zone 6 spend.
- **Independent scaling, independent commitment cycles.** Both are called out in HLD's degraded-mode table (lines 1193–1194) as independently failable — the FinOps corollary is they are also independently *priced and independently committed* (separate Reserved/Savings-Plan-equivalent negotiation cycles, separate rate-optimization surfaces owned by `cloud-rate-optimization-engineer`, out of this agent's scope per the agent's own boundary). Treating "Zone 6 cost" as one number without the `resource-class` tag (Section 2) would hide which vendor is driving a spend change, defeating M6-style anomaly attribution before it can even run.
- **Recommendation:** require the `resource-class` tag (Section 2) as a hard prerequisite before any Zone 6 budget-variance or anomaly-attribution work in a later sprint, and route the actual RI/commitment-mix decision for each vendor separately to `cloud-rate-optimization-engineer` once Profile B/C scale is reached — this review does not set commitment strategy, only flags that a unified "Zone 6 spend" figure without the vendor split will be governance-blind by construction.

## 5. Summary

Minimal taxonomy for DASH-STORY-007 is the standard 4 tags plus a Zone-6-specific `resource-class` (vector-index/lexical-index) tag, both `resource-class` and `tenant_id` enforced preventively from provisioning given their pre-existing ADR-007/ADR-013 structural basis. One cost-estimate AC (AC-006-COST-1) is recommended for addition, requiring average and marginal per-query cost to be reported separately rather than a single total-spend figure. One governance gap flagged: Qdrant and OpenSearch are two independent vendor cost surfaces with no defined cross-surface roll-up — the `resource-class` tag is the prerequisite fix, and vendor-specific rate optimization is explicitly out of this agent's scope (`cloud-rate-optimization-engineer`).

**Next agent should know:** if `consensus-agent`'s SP.0.5 gate requires all reviewer flags resolved before `DRAFT APPROVED`, DASH-STORY-007 needs AC-006-COST-1 added and the `resource-class`/`tenant_id` preventive-enforcement note carried into its Dev sub-task before Sprint 1 lock.
