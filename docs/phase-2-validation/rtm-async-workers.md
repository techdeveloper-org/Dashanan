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

| Case | Assertion ID | Fault injected | Expected behavior | Ties to |
|---|---|---|---|---|
| Rotation-worker OOM mid-sweep | CHAOS-001 | Kill `rotation-worker` process during an active zone sweep (`POST /zones/{zone}/sweep`) | Sweep is resumable/idempotent — no partial state transition is left without its paired event; a re-run sweep does not double-emit `memory.promoted`/`memory.compressed`/`memory.archived` for an item already transitioned | HLD §8.3 per-item total ordering; FR-012 |
| Redis Streams timeout on publish | CHAOS-002 | Toxiproxy-inject a timeout on the Redis Streams connection used for event publication | Publisher retries per its own backoff policy; publish is never silently dropped — either it eventually succeeds or it surfaces as a `WriteRejectedNotDurable` (`openapi.yaml`) rather than a falsely-acknowledged write | `openapi.yaml` `WriteRejectedNotDurable` response (Section 8.4 accepted single point of failure, by design) |
| Consumer-side Redis Streams read timeout | CHAOS-003 | Toxiproxy-inject a timeout on a consumer's (`index-worker`) stream read | Consumer's consumer-group offset is not advanced past the unread event; no event loss on reconnect | Redis Streams consumer-group semantics (at-least-once delivery, per HLD §8.3) |

### CHAOS-001 — recovery mechanism and integration-test assertions

**Recovery mechanism (how in-flight state is actually tracked and resumed):** the rotation worker does not maintain a separate in-flight-job ledger. Recovery relies on two things already specified elsewhere in this doc set, made explicit here: (1) the persisted timer wheel that schedules sweeps is rebuilt from durable deadlines on worker restart (`HLD.md` §8.4, Rotation-worker failure-mode row: "Restart; timer wheel rebuilt from persisted deadlines"), so a killed worker's pending work is never lost, only delayed until the next sweep tick; (2) each zone-transition write is a conditional `UPDATE ... WHERE state = ? AND version = ?` (`HLD.md` §8.3, Idempotency), so a re-run sweep that re-processes an item already transitioned by the killed worker's last (possibly uncommitted) attempt finds the `WHERE` clause no longer matches and skips it — it does not re-apply the transition or re-emit its event.

**Integration-test assertions:**
- **CHAOS-001a** — Given a sweep has transitioned item A to `Compressed` and committed its `memory.compressed` event, When `rotation-worker` is killed (SIGKILL) immediately after that commit but before advancing to item B, Then a fresh `rotation-worker` process, on restart, resumes the sweep from the persisted timer-wheel deadline and does **not** re-emit `memory.compressed` for item A (assert: exactly one `memory.compressed` event for item A's `item_id` across both worker lifetimes).
- **CHAOS-001b** — Given the same kill point, When the resumed sweep reaches item B (never transitioned by the killed worker), Then item B is transitioned normally and its event is emitted exactly once (assert: no missed items — sweep coverage after restart equals the original sweep's item set minus items already transitioned before the kill).
- **CHAOS-001c** — Given a kill occurs mid-write (worker killed between the conditional `UPDATE` attempt and event publication, before either is durably confirmed), When the resumed sweep re-evaluates that item, Then the conditional `UPDATE`'s `WHERE state = ? AND version = ?` clause deterministically resolves to either "apply once" (if the original write never committed) or "skip" (if it did) — never a partial or duplicate application (assert: item's final state is reachable via exactly one valid transition path, per HLD §8.3 per-item total ordering).
