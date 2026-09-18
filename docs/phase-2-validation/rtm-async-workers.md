# Requirements Traceability Matrix — Async Workers & Event Contracts

**Source of truth:** `docs/phase-1.5-api/integration-testing-plan.md` §4 ("Async event contracts — the
6 `x-internal-event-contracts` events") and `docs/phase-1.5-api/openapi.yaml`'s
`x-internal-event-contracts` block. This file cross-references and summarizes those for RTM purposes —
it does **not** duplicate the Pact consumer-contract JSON, the payload-field lists, or the
event-envelope schema defined there. Consult the source file for the authoritative contract detail.

This closes the gap left by `docs/phase-2-validation/ba-traceability-review.md`, which traces
`dashanan-rotation-worker` to FR-012 only as an architecture-ownership mapping (component-level), not
as event-level test coverage.

## FR traceability note

`integration-testing-plan.md` §4 cites "FR-013, FR-012, FR-009" as the FRs governing this whole event
section collectively. **FR-013 does not exist anywhere in `docs/phase-0-output/PRD.md`'s FR list
(FR-001 through FR-012 only)** — this is a docs-drift defect in the source file, not something this RTM
can resolve by inventing a meaning for FR-013. It is recorded as `[NEEDS INPUT]` below rather than
silently dropped or guessed at.

## Event → Consumer → FR → Test Matrix

| Event | Publisher | Consumer(s) | FR | Test type | Pass criteria |
|---|---|---|---|---|---|
| `memory.written` | orchestrator-event-publisher | index-worker | FR-009 (orchestrator write routing); FR-010 (provenance obligation — payload carries `provenance_id`) | Unit (schema validator) + Integration (Pact) | Envelope + payload match schema; consumer observes event within HLD write-visibility p99 (2s, per §5 smoke flow) |
| `memory.promoted` | rotation-worker | index-worker, metrics | FR-012 (rotation state machine — `Promoted` state) | Integration (Pact) | `from_zone`→`to_zone` transition matches `PromoteThreshold` gate; both consumers verify independently |
| `memory.compressed` | rotation-worker | index-worker, provenance-relay | FR-012 (rotation state machine — `Compressed` state) | Integration (Pact) + ordering assertion | Compressed strictly precedes any later `memory.archived` for the same `item_id` (HLD §8.3 per-item total ordering, non-negotiable) |
| `memory.archived` | rotation-worker | index-worker, consolidation | FR-012 (`Archived` state); FR-008 (Consolidation zone reached only via Archived) | Integration (Pact) + ordering assertion | Only reachable after a prior `memory.compressed` for the same `item_id` — never observed first |
| `memory.evicted` | rotation-worker | index-worker, metrics | `[NEEDS INPUT]` — FR-012's state list (`Active → Compressed → Archived`, plus `Promoted`) does not name an `Evicted` state; this event's FR mapping needs solution-architect clarification, not an invented one | Integration (Pact) | Deferred until FR mapping is resolved |
| `memory.conflict_detected` | Orchestrator | provenance-relay, metrics, alerting | FR-007 (Provenance/Audit — confidence scoring across conflicting sources) | Integration (Pact), fan-out to 3 consumers | All 3 consumers independently verify against the same interaction; `both_confidences` payload populated |

## Chaos / failure-injection test cases

These extend `integration-testing-plan.md` §4's ordering/schema-evolution assertions with the
infrastructure-failure scenarios named in the original documentation audit, scoped to the two workers
already named in the source plan (`rotation-worker`, `index-worker`).

| Case | Fault injected | Expected behavior | Ties to |
|---|---|---|---|
| Rotation-worker OOM mid-sweep | Kill `rotation-worker` process during an active zone sweep (`POST /zones/{zone}/sweep`) | Sweep is resumable/idempotent — no partial state transition is left without its paired event; a re-run sweep does not double-emit `memory.promoted`/`memory.compressed`/`memory.archived` for an item already transitioned | HLD §8.3 per-item total ordering; FR-012 |
| Redis Streams timeout on publish | Toxiproxy-inject a timeout on the Redis Streams connection used for event publication | Publisher retries per its own backoff policy; publish is never silently dropped — either it eventually succeeds or it surfaces as a `WriteRejectedNotDurable` (`openapi.yaml`) rather than a falsely-acknowledged write | `openapi.yaml` `WriteRejectedNotDurable` response (Section 8.4 accepted single point of failure, by design) |
| Consumer-side Redis Streams read timeout | Toxiproxy-inject a timeout on a consumer's (`index-worker`) stream read | Consumer's consumer-group offset is not advanced past the unread event; no event loss on reconnect | Redis Streams consumer-group semantics (at-least-once delivery, per HLD §8.3) |
