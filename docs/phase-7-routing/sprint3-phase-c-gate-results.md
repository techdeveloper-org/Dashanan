# Phase 7 Phase C (Hallucination/Faithfulness Gate) Results — Sprint 3 — 2026-09-20

**Scope:** All 6 Sprint 3 stories (DASH-STORY-021, 022, 011, 023, 024, 025), full non-sampled
check of `docs/phase-7-routing/sprint3_ar1_assignments.json` (v1.0.0) and
`sprint3_ar3_context_windows.json` (v1.0.0) against the real source text each file claims to
quote: `docs/phase-1-architecture/HLD.md` (v1.0.0), `SRS.md` (v1.0.11), and
`docs/phase-6-sprint-planning/backlog_draft.json` (v1.1.0), plus direct verification of every
underlying source-code citation the AR.1 file makes on its own authority (`composition_root.py`,
`conflict_detection_sweep.py`, `entity_memory_repository.py`, `sql_semantic_repository.py`,
`procedure.py`, `provenance_schema.sql`/`episodic_schema.sql`,
`tests/integration/test_cross_zone_dpdp_erasure_scope_gap.py`, `docs/phase-1.5-api/openapi.yaml`).
Dispatched `hallucination-detector` and `context-faithfulness-engineer` personas (each loading
its real `agent.md` and mandatory skills), mirroring `sprint2-phase-c-gate-results.md`'s exact
methodology, against the full 6-story set — every story, every citation, non-sampled.

- **C-1 (hallucination-detector): 2 real findings, LOW severity each, RESOLVED.** All 6
  stories checked in full (non-sampled) via NLI-style source-faithfulness scoring and
  FactScore atomic-fact verification against real file content, not the AR files' own prose.
  `story_id`/`traces_to_fr`/`assigned_agent`, all 6 `acceptance_criteria[]` blocks (16 ACs
  total), all `must_not_deviate` blocks, all `hld_ownership_crosscheck` entries, the
  `p1_security_override.considered_and_not_triggered` reasoning for DASH-STORY-022/023/024,
  and every quoted HLD/SRS passage (Section 2 container table, Section 3.0/3.4/3.6/3.8/3.10/3.11,
  Section 7.1-7.5, Section 10 assets/threats/DPDP-2/DPDP-3, ADR-004/005/006/007/008/009,
  AC-011/013/015, NFR-006, SRS Section 4.1 AC-013 Scope Note) verified verbatim-correct
  against the real HLD.md/SRS.md text. Every source-code line-number citation
  (`composition_root.py` lines 74-119, `conflict_detection_sweep.py` lines 107-176,
  `entity_memory_repository.py` lines 165/200-201, the DPDP gap test's lines 68-116/118-158)
  was independently re-opened and confirmed exact. The "8/8 zone adapters exist" claim in
  DASH-STORY-021's `real_finding` was independently re-verified by a fresh directory glob of
  `src/dashanan/infrastructure/*.py` — all 8 adapter files exist exactly as named. The
  Zone 4 `Procedure` field-set exclusion (DASH-STORY-025) was verified against
  `src/dashanan/domain/procedure.py`'s real dataclass fields — exact match, `procedure_id`
  confirmed genuinely absent per that file's own documented judgment call.
  **Two real defects found, both non-PII, non-security, and both FIXED in place:**
  1. **`openapi.yaml` operationId count.** `sprint3_ar1_assignments.json`'s DASH-STORY-023
     `invest.independent`/`invest.testable`/`AC-023-1`/`AC-023-1.source` and
     `sprint3_ar3_context_windows.json`'s DASH-STORY-023 `pii_note` all stated **"29
     operationIds"** (AC-023-1's own source field additionally said "31 total including
     health/metrics" — a second, different wrong number in the same entry). A direct
     `grep -c "operationId:"` against the real `docs/phase-1.5-api/openapi.yaml` returns
     **32**, and all 32 names were individually enumerated and cross-checked against the
     file's own endpoint definitions (including the three operations/health endpoints
     `getLiveness`, `getReadiness`, `getMetrics`, which the AR.1 file's own name-list already
     included — only the numeric count was wrong, not the enumeration). Fixed: all four
     "29 operationIds"/"29 wired endpoints" occurrences corrected to "32", and the
     self-contradicting "31 total" source note corrected to "32 total including
     health/metrics/readiness".
  2. **Stale Postgres role name.** `sprint3_ar1_assignments.json`'s DASH-STORY-024 entry
     (`p1_security_override.considered_and_not_triggered`, `invest.valuable`,
     `invest.testable`, `AC-024-2`, and the `advisory_review.review_scope`) and
     `sprint3_ar3_context_windows.json`'s DASH-STORY-024 `pii_note` all asserted, as
     directly-verified fact, that `provenance_schema.sql`/`episodic_schema.sql` "define
     `dashanan_app_role`" (this claim was itself carried forward verbatim from
     `backlog_draft.json`'s own 2026-09-20 FR-015 note). A direct read of both schema files
     found **no occurrence of `dashanan_app_role` anywhere** — the real, current role names
     those files define are `dashanan_provenance_role` (Zone 7, added by the DSHN-60 P1
     remediation per the file's own comments) and `dashanan_episodic_role` (Zone 2, same
     remediation), plus `dashanan_schema_owner` (this one name was correct). `SRS.md`'s own
     Change Log (v1.0.7 entry, 2026-09-19) independently confirms the mechanism: "Split the
     single shared `dashanan_app_role` into per-zone least-privilege roles
     (`dashanan_provenance_role`, `dashanan_episodic_role`)" — `dashanan_app_role` is the
     **pre-remediation** name, retired one day before `backlog_draft.json`'s FR-015 note (and
     two days before this AR.1 pass) was written, and the note's own citation of "since
     DSHN-55" for role definition is itself off by one remediation round (DSHN-55 predates the
     DSHN-60 split that actually created the per-zone names). Fixed: all six occurrences
     across both Sprint 3 AR files corrected to name the real, current roles
     (`dashanan_provenance_role`/`dashanan_episodic_role`/`dashanan_schema_owner`), with each
     fix explaining the correction rather than silently swapping the string, so the AR.1 file
     no longer repeats `backlog_draft.json`'s stale citation as independently-verified fact.
     `backlog_draft.json` itself was left unedited — out of this gate's authorized edit scope
     (AR.1/AR.3 only) — and its own stale note is disclosed here rather than silently
     propagated a second time.
  Every other citation across both files — including all HLD section/ADR quotes, all SRS
  AC/NFR text, all `backlog_draft.json` FR-011/013/014/015 notes and DASH-STORY-011 spike-exit
  criteria, the `ConflictDetectingProvenanceRepository`/`build_provenance_repository`
  importer-grep claim (independently re-run: only `composition_root.py` and
  `conflict_detection_sweep.py` itself import either symbol anywhere under `src/`), and the
  `dependency_integrity_check`/`routing_gaps`/`summary` blocks' internal arithmetic (6 stories,
  32 story points, 4/1/1 agent split, `total_context_budget_tokens` 34000 = sum of the 6 listed
  `context_budget_tokens` values) — checked out exact.
- **C-2 (context-faithfulness-engineer): 0 additional findings beyond C-1's two (both already
  fixed above); PASS.** Citation fidelity re-checked independently: Context Precision 1.00 and
  Context Recall 1.00 (6/6 stories, every `context_windows[].sources[]` entry in
  `sprint3_ar3_context_windows.json` traces to a real HLD section/ADR/SRS-AC/`sprint3_ar1_assignments.json`
  field/named source file this session independently re-opened — none invented), RAGAS
  Faithfulness (citation-verbatim dimension) 1.00 after the two operationId-count and
  role-name fixes above (both were citation-accuracy defects squarely in C-2's own remit;
  re-checked jointly with C-1 rather than duplicated as a second independent finding).
  Context-budget arithmetic reconciled: `context_budget_tokens` per story (4500 + 5500 + 3500 +
  7000 + 7000 + 6500) sums to exactly 34000, matching `summary.total_context_budget_tokens`.
  **PII/classification-model check: PASS.** The Bell-LaPadula ceiling discipline is applied
  correctly to all 5 PII-adjacent/new-risk-class stories (021 Zone 3/5 structural-only, 022
  Zone 3/5 abstract-placeholder-only, 023 network/credential-surface exclusion, 024
  network/credential-surface exclusion, 025 strictest Zone 3/5 DPDP redaction); DASH-STORY-011
  is correctly the sole story with `pii_exclusion_applied: false` (a documentation-only spike
  touching no zone). No context window admits a real or realistic PII value, secret, or
  credential anywhere across all 6 stories — confirmed by direct read of every `pii_note`, not
  a sampled subset. `sprint_3_new_risk_class_note`'s own claim that DASH-STORY-023/024 are
  "the first two stories in any sprint" to trigger this exclusion class for a non-PII reason
  was cross-checked against Sprint 1/2's own AR.3 files' `context_windows[]` entries (neither
  Sprint 1 nor Sprint 2 has a `pii_note` describing a network-facing-host or
  production-credential exclusion) — verified true, not merely asserted.
- Context-source citation accuracy: exact match after the two fixes above (both reviewers
  independently confirmed, 6/6 stories, 62 individual `sources[]` entries).

**Verdict: PASS after resolution.** Same discipline as Sprint 1's and Sprint 2's rounds: two
real findings found by genuine, non-sampled, source-level review (a fabricated/inconsistent
numeric count and a stale role name inherited from an upstream doc), both non-PII,
non-security-in-the-exploit sense, fixed directly in the two files this gate has edit
authority over, and re-validated as syntactically valid JSON after each edit — not silently
disclosed-and-deferred. Unlike Sprint 2's single arithmetic defect, this round found two
independent defect classes (a count and a stale identifier), both concentrated in the two
stories (DASH-STORY-023, DASH-STORY-024) that carry the most third-party-file citations of
any Sprint 3 story — consistent with those stories' own `routing_gaps.AR1-S3-G3` disclosure
that they are the sprint's highest-novelty, highest-citation-density entries.

## C-1: hallucination-detector

- **Document faithfulness (NLI source-level proxy): 1.00 after fix** (6/6 stories' FR-citations,
  context-source citations, must-not-deviate content, AC text, and quoted HLD/SRS passages
  fully entailed by their named real sources after the operationId-count and role-name
  corrections; 0 remaining contradictions or unsupported claims)
- **FactScore (RAG atomic-fact check): 1.00 after fix** (every checked claim — 6 story
  routings, 62 context-source list entries, 16 acceptance-criteria entries, 6 must-not-deviate
  sets, all quoted HLD/SRS/openapi.yaml/schema-SQL/source-code passages, all source-code
  line-number citations — traced to and supported by real content in
  `sprint3_ar1_assignments.json` / `sprint3_ar3_context_windows.json` / `HLD.md` / `SRS.md` /
  `backlog_draft.json` / `openapi.yaml` / the cited `.py`/`.sql` files)
- SelfCheckGPT score / semantic entropy: N/A — same rationale as Sprint 1/2: this is a static,
  already-authored artifact under source-faithfulness audit, not a sampled LLM output stream.
- Severity: LOW for both findings. No blocking finding; PASS after fix. FR/AC-citation accuracy
  16/16 exact match after fix; context-source citation accuracy verified word-for-word for all
  6 stories (non-sampled, full check); source-code line-number citations 4/4 exact
  (`composition_root.py` L74-119, `conflict_detection_sweep.py` L107-176,
  `entity_memory_repository.py` L165/L200-201, DPDP gap test L68-116/L118-158).
- Recommended follow-up (non-blocking, out of this gate's edit scope): `backlog_draft.json`'s
  own FR-015 note still names `dashanan_app_role` instead of the real
  `dashanan_provenance_role`/`dashanan_episodic_role` pair — a future Phase 6 documentation
  pass should correct it at the source so no future AR file inherits the same stale citation a
  third time.

## C-2: context-faithfulness-engineer

- **RAGAS Context Precision: 1.00** (6/6 stories, 0 extraneous citations)
- **RAGAS Context Recall: 1.00** (62/62 `sources[]` entries reproduced against real, traceable
  content)
- **RAGAS Faithfulness (citation-verbatim dimension): 1.00 after fix** (no story's `sources[]`
  or `pii_note` contains an invented/paraphrased source label after the two corrections above)
- Context-budget cross-check: `context_windows[].context_budget_tokens` sums to 34000,
  matching `summary.total_context_budget_tokens` exactly (4500+5500+3500+7000+7000+6500=34000).
- **PII hard-fail check: PASS** — all 5 PII-adjacent/new-risk-class stories and the 1 non-PII
  spike inspected directly; 0/6 context windows leak PII, a real subject/entity identifier, a
  real secret/credential/connection-string value, or example payload content.
- Real findings (severity LOW each, non-PII, non-exploit-security): the two citation-accuracy
  defects reported under C-1 above (operationId count, stale role name) are the same defects
  C-2's RAGAS-faithfulness check independently surfaces — reported once, jointly, per this
  project's established convention of not double-counting one underlying defect across two
  reviewer sections. **Both fixed directly** in `sprint3_ar1_assignments.json` (5
  role-name occurrences plus 3 operationId-count occurrences) and
  `sprint3_ar3_context_windows.json` (2 role-name occurrences plus 1 operationId-count
  occurrence); both files re-validated as syntactically valid JSON after the edits.
- Disclosed coverage: full 6/6 stories checked — no sampling gap to close (matching Sprint 2's
  own non-sampled-from-the-start posture).

## Overall verdict

Both Phase C gates **PASS after resolution** for DASH-STORY-021/022/011/023/024/025 (2026-09-20
run, non-sampled, full 6-story coverage of `sprint3_ar1_assignments.json` and
`sprint3_ar3_context_windows.json` against the real `HLD.md`/`SRS.md`/`backlog_draft.json`/
`openapi.yaml`/source-code text each file cites). C-1: document faithfulness 1.00 after fix,
FactScore 1.00 after fix, two real LOW-severity findings (both fixed). C-2: Context
Precision/Recall/Faithfulness 1.00 after fix, PII hard-fail PASS, the same two findings
(reported jointly with C-1, not duplicated) fixed in place. No unresolved findings remain.

**Files modified:**
- `docs/phase-7-routing/sprint3_ar1_assignments.json` — 8 string edits (3 operationId-count
  fixes + 5 role-name fixes), JSON-valid after each change.
- `docs/phase-7-routing/sprint3_ar3_context_windows.json` — 3 string edits (1 operationId-count
  fix + 2 role-name fixes), JSON-valid after each change.

**Phase C / Phase B boundary (same scope note as the Sprint 1/2 gates):** This PASS verdict
covers **AR.1/AR.3-to-source faithfulness only** — whether Sprint 3's routing assignments and
context-window design accurately cite their real HLD/SRS/backlog/openapi/source-code sources.
It does not cover, and was not run against, any Phase B implementation code for Sprint 3's
stories (none has been written yet — Sprint 3 is still in the AR.1/AR.3 routing stage, unlike
Sprint 1/2 which already had a corresponding `implementation_execution_plan.json` this gate
type would check instead). Once Phase B produces real code and a
`sprint3_implementation_execution_plan.json`-driven CoT prompt set for these stories, that
artifact is the correct target for a Sprint-3-implementation Phase C pass, not a re-run of this
routing-level gate.
