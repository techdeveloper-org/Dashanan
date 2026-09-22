# Connection Pooling Design — Closing AR1-S3-G3 (Composition Root)

Status: DRAFT — pending user review. No implementation files have been modified.
Related: `src/dashanan/infrastructure/composition_root.py`, `docs/phase-1-architecture/HLD.md`
Section 6 (circuit-breaker precedent), `docs/phase-1.5-api/fr013-predicate-schema-design.md`
Section 6 (the exact wording AR1-S3-G3 is cited by, carried forward here verbatim), FR-015
(Shape B deployment infra).

Persona grounding: written from a `database-engineer` lens (`claude-global-library/agents/
database-engineer/agent.md` — schema/connection-pooling/performance ownership), applying that
agent's `rdbms-core` (pooling, transactions, isolation) and `performance-optimization` (sizing,
contention) skills.

---

## 1. Problem statement

`composition_root.py:build_postgres_connection()` opens exactly **one** `psycopg.Connection`
per process, connected as `app_login_user` (least-privilege, per the module's own docstring).
That single connection object is then threaded into every zone repository builder that needs
Postgres access: `build_provenance_repository`, `build_episodic_repository`,
`build_conflict_aware_semantic_repository` (which itself opens a *second*, independent
`provenance_connection` parameter — but callers today pass the same one connection object for
both `semantic_connection` and `provenance_connection` in practice, since only one connection is
ever constructed), and `build_conflict_aware_entity_memory_repository`.

Confirmed by grep: there is no `psycopg_pool` import anywhere in this codebase, and
`pyproject.toml` declares only `psycopg[binary]>=3.2,<4.0` — the bare driver, no pooling
package. `HLD.md` has no ADR for connection pooling; its only "pool" references are to Postgres's
own internal buffer-cache/connection handling for HNSW indexes (unrelated) and to the vector-store
circuit-breaker's thread-pool-exhaustion rationale (Section 6, a *precedent pattern* for this
design, not a substitute for it).

This is a real, previously undocumented architectural gap, first surfaced as `AR1-S3-G3` in
`sprint3_ar1_assignments.json` (originally a disclosure that Sprint 3's new network-facing
surface + first real production credentials didn't trip any literal P1 security-override
criterion), and re-cited from Sprint 4 onward — including this design doc's own sibling,
`fr013-predicate-schema-design.md` Section 6 — as shorthand for the specific limitation: *"one
shared `psycopg.Connection`, no cross-zone transaction coordination."* This document closes that
specific, narrower limitation.

---

## 2. Confirmed constraint: the API host is synchronous

`src/dashanan/api/app.py`'s route handlers are declared `def`, not `async def` (verified: every
top-level handler in the file — `_do_write`, `assemble_context`, and the rest — uses plain `def`).
FastAPI runs synchronous route handlers in a bounded threadpool
(`anyio`'s default worker-thread executor), not on the event loop. This settles the
sync-vs-async pool question directly from the existing code, not by preference:

**Decision: use `psycopg_pool.ConnectionPool` (the synchronous pool), not
`AsyncConnectionPool`.** An async pool would require every route handler to become `async def`
and every repository call to be awaited — a much larger, unrelated refactor this gap does not
require and this design does not propose. `ConnectionPool.connection()` is a context manager
that blocks the calling thread (one of FastAPI's threadpool workers) until a connection is
available, which is the correct match for this host's existing synchronous execution model.

---

## 3. Where pooled connections thread through: acquire-per-request, not app-lifetime

Two options were weighed:

- **(a) Hold pool-provided connections for the app lifetime** — call `pool.getconn()` once at
  startup per "slot" and never release. This is simpler to wire but does **not** fix the actual
  problem: it just replaces one long-lived shared connection with N long-lived shared
  connections, still contended across concurrent requests once N < concurrent request count, and
  still leaves a connection checked out indefinitely even when idle.
- **(b) Acquire-per-request (checkout/checkin around each API request)** — the composition root
  hands each zone-repository builder a *pool*, not a *connection*; the actual `psycopg.Connection`
  is checked out from the pool at the start of request handling and returned at the end (or on
  exception), scoped to a FastAPI dependency.

**Decision: (b), acquire-per-request.** This is the only option that actually resolves the
contended-shared-connection problem AR1-S3-G3 names. Concretely:

- The composition root's `build_postgres_connection()` factory is replaced by a new
  `build_postgres_connection_pool(settings, env) -> psycopg_pool.ConnectionPool` that constructs
  the pool once at application startup (long-lived, matching the pool's own intended lifecycle —
  pools are meant to be created once and reused, not per-request).
- A new FastAPI dependency (e.g. `get_pooled_connection`) wraps `pool.connection()` as a
  generator-style dependency: `with pool.connection() as conn: yield conn`. FastAPI's dependency
  system already runs generator dependencies' teardown (the code after `yield`) after the response
  is sent, which is exactly checkout/checkin semantics — no new lifecycle mechanism is needed
  beyond what FastAPI already provides.
- `build_provenance_repository`, `build_episodic_repository`,
  `build_conflict_aware_semantic_repository`, and `build_conflict_aware_entity_memory_repository`
  keep their existing signatures — each still takes a `connection: <Zone>SqlConnection` parameter
  typed against the minimal DB-API 2.0 `SqlConnection` Protocol. What changes is *only* how the
  request-handling layer (`api/app.py` / `api/composition.py`) obtains the connection object it
  passes to these builders: today it's the one process-lifetime connection; after this change,
  it's the per-request connection the pooled dependency yields.
- This means **zero downstream repository code changes** (`SqlProvenanceRepository`,
  `SqlEpisodicRepository`, `SqlSemanticRepository` etc. never see a pool, only a connection
  conforming to the same Protocol they already depend on) — see Section 6's migration note for
  the precise boundary.

---

## 4. Pool sizing

`docker-compose.yml`'s Shape B stack runs a single Postgres instance with no documented
`max_connections` override, so it retains Postgres's own default (100). Sizing must leave
headroom for: the migration runner's own connections (short-lived, at startup only), any
operator/admin connections, and the other services in the stack that also connect to Postgres
(none currently do — Redis/Qdrant/OpenSearch/S3 are separate stores).

**Sizing formula** (`performance-optimization` skill guidance: pool size should track expected
*concurrent* in-flight requests that touch Postgres, not total request rate — a request that
completes in single-digit milliseconds doesn't need a dedicated connection per request/second):

```
pool_max = min(
    uvicorn_worker_thread_pool_size,   # a pooled connection is only useful up to
                                         # the number of threads that can concurrently
                                         # hold one — sizing the pool larger than the
                                         # host's own concurrency ceiling wastes
                                         # Postgres-side connection slots for no benefit
    postgres_max_connections * 0.5      # leave >=50% of Postgres's own max_connections
                                         # headroom for the migration runner, operator
                                         # access, and other roles (dashanan_provenance_role,
                                         # dashanan_semantic_role, etc. all multiplex through
                                         # this one pool's connections at the DB layer,
                                         # since they're GRANted roles on one login user,
                                         # not separate connections)
)
```

Concretely, with Postgres's default `max_connections=100` and FastAPI's default threadpool size
(`min(32, os.cpu_count() + 4)` per `anyio`'s default), this yields a starting `pool_max` in the
range of 20-32 for a typical 4-16 core deployment host — **recommend `pool_min=2, pool_max=20`**
as the Shape B default (env-overridable, matching every other Shape B setting's
`load_postgres_settings_from_env` convention), reviewed and adjusted once real load-test numbers
exist (no load-test data exists yet for this codebase — this is an explicit open item, Section 8).

`pool_min=2` (not 0): `psycopg_pool.ConnectionPool` eagerly opens `pool_min` connections at
construction and keeps them warm, avoiding a cold-connect penalty on the first requests after
startup — appropriate for a service expected to receive traffic immediately after health-check
readiness, without over-provisioning idle connections the way a larger `pool_min` would.

---

## 5. Failure / backpressure behavior when the pool is exhausted

`psycopg_pool.ConnectionPool.connection()` accepts a `timeout` parameter and raises
`psycopg_pool.PoolTimeout` if no connection becomes available within it, rather than blocking
indefinitely. This is the pool's own built-in backpressure signal.

**Decision: map `PoolTimeout` to the same `503` + `Retry-After` pattern already established in
this codebase** (NFR-014/AC-021's read-modify-write contention 503: *"if contention exceeds the
configured retry budget the write is rejected with 503 + Retry-After rather than corrupting
state"*), for response-contract consistency rather than inventing a second convention for a
structurally similar "the system is temporarily saturated" condition:

- The pooled-connection FastAPI dependency sets an explicit `timeout` (recommend a short value,
  e.g. 2 seconds, well under typical client HTTP timeouts, so a saturated pool fails fast rather
  than making callers wait near their own timeout budget) on `pool.connection(timeout=...)`.
- On `PoolTimeout`, return `503` with the same error-body shape `_do_write`'s existing
  `WRITE_REJECTED_NOT_DURABLE`/contention-503 responses already use (code + message), plus a
  `Retry-After` header (a fixed small value, e.g. 1 second, since pool exhaustion is typically
  short-lived under bursty load, not a sustained outage).
- This ties into HLD Section 6's existing circuit-breaker precedent conceptually (both are
  "fail fast rather than let one saturated dependency exhaust a shared resource and take the
  whole request path down with it," `error-handling-patterns` M3) without requiring a full
  circuit-breaker state machine for this gap alone — a bounded pool with a bounded acquire timeout
  already provides the fail-fast property Section 6's breaker provides for the vector store.
  Whether Postgres access should *also* get a full circuit breaker (e.g. to fast-fail overload
  from repeated connect failures, not just pool exhaustion) is a separate, larger decision left
  explicitly open (Section 8) — this design solves the pooling gap, not "does Postgres access need
  a breaker," which HLD.md has never scoped for this codebase's relational store.

---

## 6. What this change does NOT solve: cross-zone atomicity

**Explicitly out of scope, restated from `fr013-predicate-schema-design.md` Section 6 so this
document does not contradict it:** pooling connections does not, by itself, give the composition
root a way to run a Zone 5 write and a Zone 3 write (or any two zone writes) inside one shared
Postgres transaction. Each acquired pooled connection is still a single, independent
`psycopg.Connection` — pooling changes *how many* connections exist and *how they're
distributed across concurrent requests*, not whether two zone-repository calls within the same
request share one transaction. True cross-zone atomicity (2-phase-commit or a saga/compensating-
transaction pattern) remains a separate, larger architectural decision this gap alone does not
resolve.

Sprint 4's existing fail-fast-plus-honest-partial-reporting contract for Zone 3/5 writes (`202`
full success / `207 Multi-Status` partial / `503` on the first, blocking failure — see
`fr013-predicate-schema-design.md` Section 4.2 step 6) remains the correct interim design even
after this pooling change lands. Nothing in this document proposes changing that contract.

*(Side note for a future atomicity design, not part of this document's scope: acquiring a single
pooled connection **per request** rather than per zone-repository call, as Section 3 already
proposes, is a necessary precondition for a future same-connection multi-zone transaction — but
having one connection available is not the same as having transaction-coordination logic that
uses it that way. That logic does not exist today and this document does not add it.)*

---

## 7. Migration / rollout note

| Layer | Changes |
|---|---|
| `pyproject.toml` | Add `psycopg[pool]>=3.2,<4.0` (the `psycopg_pool` extra of the same psycopg 3.x package already depended on — not a new unrelated package — matching the existing `psycopg[binary]` dependency-comment convention explaining *why* each dependency was added). |
| `composition_root.py` | Add `build_postgres_connection_pool(settings, env) -> psycopg_pool.ConnectionPool`, constructed once at app startup. The existing `build_postgres_connection()` single-connection factory can remain (e.g. for the migration runner's own short-lived startup connection, or test fixtures that want a bare connection with no pool) — it is not deleted, only no longer used as the API host's per-request connection source. |
| `api/composition.py` (or equivalent app-startup wiring) | Construct the pool once (`AppContext`-lifetime), expose a FastAPI dependency that checks out/in a connection per request via `pool.connection(timeout=...)`. |
| `api/app.py` route handlers | Replace direct references to a single shared connection object with the new pooled-connection dependency, threaded into the same `build_provenance_repository`/etc. calls that already exist — call sites change their connection *source*, not their call shape. |
| `SqlProvenanceRepository`, `SqlEpisodicRepository`, `SqlSemanticRepository`, `ConflictAware*Repository` | **No changes.** Each already depends only on the minimal DB-API 2.0 `SqlConnection` Protocol (`.cursor()`), which a pool-checked-out `psycopg.Connection` satisfies identically to the single long-lived connection it satisfied before. This is the whole point of the existing Protocol-based typing — it already decouples "what a repository needs from a connection" from "how that connection was obtained," so this migration is confined to the composition root and the request-handling boundary. |
| `docker-compose.yml` | No required changes (Postgres's own `max_connections=100` default already covers the recommended `pool_max=20` starting point with headroom); optionally document the recommended pool env vars alongside the other Shape B settings in `.env.example`. |
| `HLD.md` | Add a new ADR (Section 8 below drafts its content) documenting the pooling model, sizing rationale, and failure/backpressure behavior — closing the "no ADR for connection pooling" gap this document's Section 1 identified. |

No database schema changes are required. No changes to per-zone Postgres roles/privileges are
required — the pool connects as the same `app_login_user` `build_postgres_connection` already
uses.

---

## 8. Open items (why this is DRAFT, not IMPLEMENTATION-READY)

Unlike `fr013-predicate-schema-design.md` (which reached IMPLEMENTATION-READY after seven
review rounds resolving every open question against this codebase's own existing precedents),
this document has not yet been through a review round, and carries three genuinely open items
that a review pass should resolve before implementation:

1. **Pool sizing is a reasoned estimate, not measured.** Section 4's `pool_min=2, pool_max=20`
   is derived from a formula, not from load-test data — no load test exists for this codebase
   yet. A review round should either accept this as a documented starting default (adjustable via
   env var, no code change needed to retune later) or require a load test first.
2. **Whether Postgres access should also get a full circuit breaker** (beyond the pool's own
   bounded-timeout fail-fast behavior) is flagged in Section 5 as explicitly separate and
   unresolved — needs an explicit accept/defer decision.
3. **The exact FastAPI dependency-injection wiring point** (Section 3/7) is described at the
   component level, not verified line-by-line against `api/composition.py`'s and `api/app.py`'s
   real current structure the way `fr013-predicate-schema-design.md` verified every one of its
   claims against real line numbers — a review round should re-verify Section 3 and Section 7
   against the actual current file contents before this is marked IMPLEMENTATION-READY, matching
   this project's own established rigor for design docs that gate real implementation.

---

## 9. Definition of Done

- [ ] `psycopg[pool]` added to `pyproject.toml` with a rationale comment matching this
      codebase's existing dependency-comment convention.
- [ ] `composition_root.py` exposes `build_postgres_connection_pool`, constructed once at
      startup, `pool_min`/`pool_max` configurable via the same env-sourced settings pattern as
      `PostgresSettings`.
- [ ] The API host acquires one pooled connection per request (not per zone-repository call,
      not app-lifetime), released after the response completes or on exception.
- [ ] `PoolTimeout` maps to `503` + `Retry-After`, matching the existing NFR-014/AC-021 503
      response shape.
- [ ] No changes required to any `Sql*Repository`/`ConflictAware*Repository` class — verified by
      the fact that none of them import or reference `psycopg_pool` directly, only the
      `SqlConnection` Protocol.
- [ ] `HLD.md` carries a new ADR documenting the pooling model (see Section 7's HLD.md row).
- [ ] Sprint 4's existing 202/207/503 partial-reporting contract for Zone 3/5 writes is
      unchanged and unaffected — verified by a re-run of `fr013-predicate-schema-design.md`
      Section 5's 14-case test matrix after pooling lands, confirming no behavior regression.
- [ ] Real load-test numbers replace Section 4's formula-derived defaults, or the defaults are
      explicitly accepted as-is by the user/reviewer.

---

## Change Log

| Date | Change |
|---|---|
| 2026-09-22 | v1 (this revision): Initial design. Confirmed the API host is synchronous (plain `def` route handlers) from `api/app.py`, settling the `ConnectionPool` vs `AsyncConnectionPool` choice from evidence rather than preference. Recommended acquire-per-request via a FastAPI generator dependency over app-lifetime connection holding, since only acquire-per-request actually resolves the shared-connection contention AR1-S3-G3 names. Derived a pool-sizing formula from Postgres's default `max_connections=100` and FastAPI's default threadpool ceiling. Mapped pool exhaustion to the existing NFR-014/AC-021 503+Retry-After convention rather than inventing a new one. Explicitly restated (not contradicted) Sprint 4's existing partial-reporting contract and the still-separate cross-zone-atomicity gap. Marked DRAFT, not IMPLEMENTATION-READY, pending a review round per Section 8's three open items — no review round has run yet. |
