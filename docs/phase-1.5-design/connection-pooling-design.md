# Connection Pooling Design — Closing AR1-S3-G3 (Composition Root)

Status: DRAFT v2.6 — pending user review. No implementation files have been modified.
Related: `src/dashanan/infrastructure/composition_root.py`, `src/dashanan/api/composition.py`,
`src/dashanan/api/app.py`, `docs/phase-1-architecture/HLD.md` Section 6 (circuit-breaker
precedent), `docs/phase-1.5-api/fr013-predicate-schema-design.md` Section 6 (the exact wording
AR1-S3-G3 is cited by, carried forward here verbatim), FR-015 (Shape B deployment infra).

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

**Scope correction (v2): the shared-connection surface is larger than Zone 2/3/5/7.**
`src/dashanan/api/composition.py`'s own module docstring (lines 38-45) states plainly that the
single `psycopg.Connection` `build_postgres_connection()` opens "is reused across Zone
2/3/7/8-manifest" for this deployment shape. Verified against `composition.py:145-253`
(`build_app_context`): that one connection is threaded not only into
`build_provenance_repository`, `build_episodic_repository`,
`build_conflict_aware_semantic_repository`, and `build_conflict_aware_entity_memory_repository`,
but also into `SqlManifestRepository` (`composition.py:223`), `Zone8ConsolidationStore`
(`composition.py:224`), `Zone8SubjectKeyedArchiver` (`composition.py:227`), and
`SubjectErasureCascadeService` (`composition.py:232`) — the Zone 8 manifest/consolidation/
crypto-shredding surface. Prior drafts of this document scoped the problem (and Section 7's
migration table) to the Zone 2/3/5/7 repository builders only; that omitted a real quarter of the
connection-sharing surface this design is meant to close. This revision brings all four Zone 8
components into Section 1's problem statement and Section 7's migration table.

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
contended-shared-connection problem AR1-S3-G3 names.

**Correction (v2): this is a larger refactor than "swap the connection source," because the
repository/service objects are built once at startup, not once per request.** Verified against
`src/dashanan/api/composition.py:145-253` (`build_app_context`) and
`src/dashanan/api/app.py:127-140` (`create_app`): `build_app_context()` runs exactly **once**, at
application startup, and calls every one of `build_provenance_repository`,
`build_episodic_repository`, `build_conflict_aware_semantic_repository`,
`build_conflict_aware_entity_memory_repository`, `SqlManifestRepository`,
`Zone8ConsolidationStore`, `Zone8SubjectKeyedArchiver`, and `SubjectErasureCascadeService`
exactly once, passing each the one process-lifetime connection. The resulting objects are stored
as fixed fields on `AppContext` (`ctx.provenance_repository`, `ctx.semantic_repository`,
`ctx.episodic_repository`, `ctx.conflict_aware_entity_repository`, `ctx.subject_erasure_service`,
and the Zone 8 manifest/consolidation/archiver equivalents), and every route handler in
`app.py` references them directly as **closure variables** — confirmed at `app.py:302,305` (`ctx.
provenance_repository`), `:420,449,482,498` (`ctx.semantic_repository` /
`ctx.conflict_aware_entity_repository`), `:572,588` (`ctx.episodic_repository`), and `:878,885`
(`ctx.subject_erasure_service`). There is no per-request call site anywhere in this codebase that
re-invokes these builders — the earlier claim in this document (v1) that "call sites change their
connection source, not their call shape" was **false**: today there is no per-request call site
to change the connection source *of*.

Correctly implementing acquire-per-request pooling therefore requires two changes together, not
one:

- **Move construction, not just the connection.** `build_provenance_repository`,
  `build_episodic_repository`, `build_conflict_aware_semantic_repository`,
  `build_conflict_aware_entity_memory_repository`, `SqlManifestRepository`,
  `Zone8ConsolidationStore`, `Zone8SubjectKeyedArchiver`, and `SubjectErasureCascadeService` must
  be called on the **per-request path**, not inside startup-time `build_app_context`. Each keeps
  its existing constructor signature (still takes a `connection: <Zone>SqlConnection` typed
  against the minimal DB-API 2.0 `SqlConnection` Protocol) — what moves is *when and where* that
  constructor call happens, from once at startup to once per request.
- **Replace closure-variable access with FastAPI dependency injection.** A new
  `get_pooled_connection` FastAPI dependency wraps `pool.connection(timeout=...)` as a
  generator-style dependency (`with pool.connection() as conn: yield conn`); FastAPI's dependency
  system already runs generator dependencies' teardown after the response is sent, giving
  checkout/checkin semantics for free. On top of it, one FastAPI dependency per repository/service
  (e.g. `get_provenance_repository(conn: SqlConnection = Depends(get_pooled_connection)) ->
  ProvenanceRepository: return build_provenance_repository(connection=conn, ...)`) constructs the
  repository fresh, once per request, from the per-request connection. Every route handler that
  currently reads `ctx.<repository>` as a closure variable (the ~10 handlers at the `app.py`
  line numbers listed above) is rewritten to instead take the repository as a parameter via
  `Depends(get_<repository>)`. `AppContext` retains only what is genuinely process-lifetime
  (settings, the pool itself, feature flags) — it stops holding the repository/service instances.
- `AppContext.postgres_available`-style guard checks (e.g. `app.py:302`'s
  `if not ctx.postgres_available or ctx.provenance_repository is None`) are re-expressed as
  `Depends(...)` dependencies that raise the equivalent `503`/`not-configured` response when the
  pool was never constructed, preserving today's degrade-gracefully-without-Postgres behavior —
  this design does not remove that guard, only relocates it to the dependency layer.

This means the migration is confined to `composition_root.py` (pool factory), `api/composition.py`
(dependency provider functions replacing `build_app_context`'s repository-construction calls),
and `api/app.py` (route handler signatures gaining `Depends(...)` parameters in place of `ctx.*`
closure reads) — **zero changes to the repository/service classes themselves**
(`SqlProvenanceRepository`, `SqlEpisodicRepository`, `SqlSemanticRepository`,
`ConflictAware*Repository`, `SqlManifestRepository`, `Zone8ConsolidationStore`,
`Zone8SubjectKeyedArchiver`, `SubjectErasureCascadeService` never see a pool, only a connection
conforming to the same Protocol they already depend on) — but it is materially more than a
connection-source swap at the call sites: roughly ten route handlers in `app.py` change from
closure-variable reads to dependency-injected parameters, and the corresponding builder calls
move out of startup-time `build_app_context` into new per-request dependency-provider functions.
See Section 6's migration note for the repository-class boundary that *does* stay unchanged, and
Section 7's migration table for the corrected per-file breakdown.

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
range of 20-32 for a typical 4-16 core deployment host — formula-derived `pool_min=2, pool_max=20`
as the Shape B default (env-overridable, matching every other Shape B setting's
`load_postgres_settings_from_env` convention).

**PROVISIONAL, NOT FINAL (updated v2.5, user-directed correction).** The `pool_min=2, pool_max=20`
values above are a reasoned estimate derived from the formula, not measured against real
concurrent load. Prior drafts of this section framed that as an acceptable "documented starting
default" a reviewer could simply accept. The user overrode that framing 2026-09-22: these numbers
MUST NOT be treated as final for implementation until a real load test has been run against the
actual `docker-compose.yml` Postgres container and produced real connection-acquisition-latency
and pool-exhaustion-behavior numbers under realistic concurrency. That load test is now a tracked,
blocking prerequisite — `DASH-STORY-028-LOADTEST` (see Section 8 item 1 and this codebase's
`docs/phase-6-sprint-planning/backlog_draft.json`) — which must complete and produce real
min/max pool-size recommendations **before** `DASH-STORY-028-DEV` treats this section's numbers as
final. No load test has been run to produce this revision; none of this section's numbers change
until that real run happens.

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
  short-lived under bursty load, not a sustained outage). **Error code (added v2.4, see Section
  5.1's disambiguation subsection below): `POOL_EXHAUSTED`.**
- This ties into HLD Section 6's existing circuit-breaker precedent conceptually (both are
  "fail fast rather than let one saturated dependency exhaust a shared resource and take the
  whole request path down with it," `error-handling-patterns` M3): a bounded pool with a bounded
  acquire timeout already provides the fail-fast property Section 6's breaker provides for the
  vector store, for the specific case where Postgres is healthy but momentarily saturated.
  **RESOLVED 2026-09-22 (user decision, see Section 8 item 2 and the Change Log): pooled Postgres
  access also gets a full circuit breaker**, matching HLD Section 6's mandatory "Every adapter
  call -> Circuit Breaker" row with no exception, rather than relying on pool-exhaustion
  backpressure alone to cover the distinct "Postgres itself is down or refusing connections" case.
  Section 5.1 below specifies the breaker.

### 5.1 Circuit breaker for pooled Postgres access

This subsection closes HLD.md ADR-020's previously-disclosed "Documented exception" (Section 6's
design-patterns table mandates Circuit Breaker for "Every adapter call"; pooled Postgres access is
such an adapter call and had none). It mirrors HLD Section 8's existing vector-store (Qdrant)
breaker pattern exactly, reusing the same count-based ring-buffer implementation HLD Section 5
already mandates for "Circuit breakers" generically (`Count-based ring buffer (sliding window,
N=20) | Failure-rate and slow-call-rate over the window`) — this is not a second, bespoke breaker
design; it is the existing breaker utility given a Postgres-specific failure predicate and wired
in front of `pool.connection(...)`.

**What counts as a "failure" for this breaker (and what does not).** The breaker's ring buffer
records only *connection-establishment* outcomes — the pool's own attempt to open or re-open a
physical TCP/TLS connection to Postgres (`psycopg.OperationalError` on connect, connection
refused, DNS/network unreachable, authentication failure against `app_login_user`) — never
query-level outcomes on an already-open, healthy connection:

- **Counts as a breaker failure:** a connect attempt the pool makes (at pool construction, on
  `pool_min` top-up, or replacing a connection the pool itself discarded as broken) raising a
  connection-level error, and a connect attempt that succeeds but exceeds a slow-call threshold
  (mirroring HLD's vector-store definition below).
- **Does NOT count as a breaker failure:** any error raised by a query running on a connection the
  pool already successfully handed out — `IntegrityError`, constraint violations, syntax errors,
  application-level validation failures, or any other normal query outcome. These are business
  logic, not a dependency-health signal, and must not be allowed to trip the breaker; conflating
  them would let a single malformed request (e.g. a duplicate-key insert) fast-fail unrelated
  concurrent requests.
- **Does NOT count as a breaker failure:** `PoolTimeout` (Section 5's existing pool-exhaustion
  signal). Pool exhaustion means every pooled connection is healthy but currently checked out —
  a saturation condition already handled by Section 5's bounded `timeout` + 503/Retry-After. The
  breaker exists for the distinct case where Postgres itself cannot be reached at all; mixing the
  two signals would trip the breaker on ordinary bursty load with no actual backend outage.

**Thresholds (identical to HLD Section 8's vector-store breaker, for one consistent operator
mental model across every adapter in this system):**

- Sliding window: the last **20** connect attempts (ring buffer, per HLD Section 5's `Circuit
  breakers` DSA row).
- Trip to **OPEN** when failure rate >= **50%** over the window, OR slow-connect rate >= **30%**
  at >= **2x** the measured p99 connect latency.
- **OPEN state fallback behavior:** the breaker is checked *before* `pool.connection(...)` is
  called at all — when OPEN, the request fails fast with `503` + `Retry-After` immediately,
  without attempting pool acquisition and without waiting out Section 5's own acquire `timeout`.
  This is deliberately cheaper than Section 5's PoolTimeout path: a known-down backend should not
  cost every caller a multi-second acquire-timeout wait before failing.
- **HALF_OPEN probe behavior:** the breaker half-opens after `30s * 2^(trips - 1)`, capped at
  `300s` (identical backoff formula to HLD Section 8's vector-store breaker). While HALF_OPEN, a
  bounded number of **5** probe connection attempts are allowed through to the real pool; **all 5
  must succeed** to transition to CLOSED. Any probe failure re-opens the breaker and restarts the
  backoff clock (trip count increments).
- **CLOSED state:** normal operation — every request proceeds to `pool.connection(...)` as
  Section 3/5 already describe, with connect outcomes continuing to feed the same ring buffer.

**Where this sits relative to Section 5's PoolTimeout handling.** The breaker wraps the *outer*
acquisition path; Section 5's `timeout` + `PoolTimeout` -> 503 mapping is unchanged and still
applies whenever the breaker is CLOSED or HALF_OPEN and a request reaches `pool.connection(...)`
but the pool itself (not Postgres) is momentarily saturated. The two mechanisms answer different
questions — "is Postgres reachable at all" (breaker) vs. "are all pooled connections currently
checked out" (PoolTimeout) — and both map to the same `503` + `Retry-After` response shape for
response-contract consistency, but are tripped by different, non-overlapping signals per the
failure-definition above.

**Migration note (added to Section 7's table by this revision):** the breaker wraps
`get_pooled_connection`'s `with pool.connection(timeout=...) as conn: yield conn` body — the
breaker's OPEN check happens as the first line of that generator dependency, before entering the
`with` block, so an OPEN breaker never invokes `pool.connection()` at all. No repository or
service class is touched; this is confined to the same FastAPI dependency-provider layer Section
3/7 already scope this design to.

### 5.1.1 Retry-After and error-code disambiguation from PoolTimeout (added v2.4, consensus-agent review remediation, BLOCKER fix)

Prior to this revision, Section 5.1's OPEN-state 503 reused the exact same "503 + Retry-After"
response shape as Section 5's `PoolTimeout` 503, with no distinguishing value or error code. This
was a real defect: `PoolTimeout` is a short-lived saturation signal (Section 5's `Retry-After: 1`
second is sized for that), while breaker-OPEN can persist for up to 300 seconds (`30s *
2^(trips-1)`, capped). A client reusing the 1-second `Retry-After` value against a breaker-OPEN
response would hammer a known-down Postgres roughly once per second for up to five minutes,
defeating the breaker's entire purpose — and an operator reading only the response body could not
tell "Postgres is unreachable" from "the pool is momentarily saturated but Postgres itself is
healthy" without cross-referencing server-side breaker state.

**Decision: breaker-OPEN and PoolTimeout now return distinguishable `Retry-After` values AND
distinguishable error codes.**

- **`Retry-After` value for breaker-OPEN:** computed dynamically from the breaker's own state, not
  a second fixed constant. The breaker already tracks `opened_at` (the timestamp it last
  transitioned to OPEN) and the current backoff duration `30s * 2^(trips - 1)` (capped at `300s`,
  per Section 5.1's existing HALF_OPEN formula). `Retry-After` is set to
  `max(1, ceil(opened_at + backoff_duration - now))` — the actual remaining time until the breaker
  next transitions to HALF_OPEN and allows a probe through. This is more useful to a well-behaved
  client than a fixed value: a client retrying exactly when the breaker is about to probe wastes no
  requests, and a client retrying during the early part of a long backoff window is told the real
  wait, not an artificially short one that invites exactly the hammering behavior this fix exists
  to prevent. Recomputed per-response (not cached), since `now` advances between requests.
- **Error code for breaker-OPEN: `POSTGRES_UNAVAILABLE`** (distinct from Section 5's
  `POOL_EXHAUSTED` for `PoolTimeout`). Returned in the same error-body shape `_do_write`'s existing
  contention-503 responses already use (code + message), so this is a new *value* of an existing
  field, not a new response schema. The message text should name the distinction explicitly (e.g.
  `"Postgres is currently unreachable (circuit breaker OPEN); retry after the indicated interval"`)
  so an operator reading logs or a client error handler branching on `error.code` can tell the two
  conditions apart without needing to inspect the `Retry-After` value's magnitude.
- **Why not reuse `PoolTimeout`'s fixed 1-second value for breaker-OPEN, simplified:** a fixed
  larger constant (e.g. always 30 seconds) was considered and rejected — it does not shrink as the
  breaker approaches its next HALF_OPEN probe, so a client polling on a fixed cadence would either
  poll too aggressively early in a long backoff window or wait needlessly long right before the
  breaker is about to recover. The dynamically-computed remaining-backoff value strictly dominates
  a fixed constant for both correctness and client-experience reasons, at negligible implementation
  cost (the breaker already stores the two timestamps this computation needs).
- **This disambiguation is part of Section 5.1's existing OPEN-state fallback behavior, not a
  separate code path:** the same first-line-of-the-generator-dependency check that fails fast
  without calling `pool.connection()` now also computes and attaches this `Retry-After` value and
  `error.code` before returning.

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
| `api/composition.py` | **Corrected (v2).** `build_app_context()` (`composition.py:145-253`) stops calling `build_provenance_repository`, `build_episodic_repository`, `build_conflict_aware_semantic_repository`, `build_conflict_aware_entity_memory_repository`, `SqlManifestRepository`, `Zone8ConsolidationStore`, `Zone8SubjectKeyedArchiver`, and `SubjectErasureCascadeService` at startup. `AppContext` is constructed once at startup holding only the pool (via `build_postgres_connection_pool`) and settings/flags — no repository/service fields. New per-request FastAPI dependency-provider functions are added (e.g. `get_provenance_repository`, `get_semantic_repository`, `get_episodic_repository`, `get_conflict_aware_entity_repository`, `get_manifest_repository`, `get_zone8_consolidation_store`, `get_zone8_archiver`, `get_subject_erasure_service`), each depending on `get_pooled_connection` (which itself does `with pool.connection(timeout=...) as conn: yield conn`) and calling the corresponding existing builder function per request with that connection. |
| `api/app.py` route handlers | **Corrected (v2).** This is not a connection-source swap at unchanged call sites — the call sites themselves move. The ~10 handlers currently reading `ctx.provenance_repository` (`app.py:302,305`), `ctx.semantic_repository`/`ctx.conflict_aware_entity_repository` (`app.py:420,449,482,498`), `ctx.episodic_repository` (`app.py:572,588`), and `ctx.subject_erasure_service` (`app.py:878,885`) as closure variables are rewritten to receive the repository/service as a parameter via `Depends(get_<repository>)` from `api/composition.py`'s new dependency providers. The existing `ctx.postgres_available`-guarded not-configured branches are re-expressed as the equivalent condition inside the dependency (raising the same `503`/not-configured response) rather than an `if ctx.x is None` check inside the handler body. |
| `SqlProvenanceRepository`, `SqlEpisodicRepository`, `SqlSemanticRepository`, `ConflictAware*Repository`, `SqlManifestRepository`, `Zone8ConsolidationStore`, `Zone8SubjectKeyedArchiver`, `SubjectErasureCascadeService` | **No changes.** Each already depends only on the minimal DB-API 2.0 `SqlConnection` Protocol (`.cursor()`) or on repository instances built from one, which a pool-checked-out `psycopg.Connection` satisfies identically to the single long-lived connection it satisfied before. This is the whole point of the existing Protocol-based typing — it already decouples "what a repository needs from a connection" from "how that connection was obtained," so this migration is confined to the composition root, the new per-request dependency providers, and the route-handler signatures that consume them. This row now explicitly includes the Zone 8 manifest/consolidation/crypto-shredding/erasure-cascade classes identified in Section 1's v2 scope correction — they were omitted from this table in v1. |
| `docker-compose.yml` | No required changes (Postgres's own `max_connections=100` default already covers the recommended `pool_max=20` starting point with headroom); optionally document the recommended pool env vars alongside the other Shape B settings in `.env.example`. |
| `HLD.md` | Add a new ADR (Section 8 below drafts its content) documenting the pooling model, sizing rationale, and failure/backpressure behavior — closing the "no ADR for connection pooling" gap this document's Section 1 identified. **Also closes ADR-020's own "Documented exception" (see this document's Section 5.1 and HLD.md ADR-020's updated Consequences block).** |
| `api/composition.py` (breaker) | `get_pooled_connection` gains a Postgres-specific circuit breaker (Section 5.1) wrapping `pool.connection(timeout=...)`, reusing the same count-based ring-buffer breaker utility HLD Section 5 already mandates — not a new breaker implementation. |

No database schema changes are required. No changes to per-zone Postgres roles/privileges are
required — the pool connects as the same `app_login_user` `build_postgres_connection` already
uses.

### 7.1 Rollback procedure

This table's own row for `composition_root.py` already notes that
`build_postgres_connection()` — the pre-existing single-connection factory — "is not deleted,
only no longer used as the API host's per-request connection source." That fact was previously
implicit; this subsection names it explicitly as the deliberate rollback path for this change,
so the migration is not a one-way door with no disclosed exit if per-request pooling introduces
a production regression discovered only post-rollout (e.g. a connection leak under sustained
load, or pool-exhaustion behavior that manifests differently against real traffic than against
the DASH-STORY-028-QA test suite's synthetic load).

**Recommended mechanism: a `POOLING_ENABLED` environment-variable gate, following this
codebase's existing `load_postgres_settings_from_env` convention (Section 4).**

- `composition_root.py` reads `POOLING_ENABLED` (default `true`) alongside its other
  env-sourced `PostgresSettings`. When `true` (the default, post-rollout), `build_app_context`
  wires the per-request `Depends(...)` providers described in Section 3/7. When explicitly set
  to `false`, `build_app_context` falls back to calling `build_postgres_connection()` once at
  startup and constructing every repository/service (`build_provenance_repository`,
  `build_episodic_repository`, `build_conflict_aware_semantic_repository`,
  `build_conflict_aware_entity_memory_repository`, `SqlManifestRepository`,
  `Zone8ConsolidationStore`, `Zone8SubjectKeyedArchiver`, `SubjectErasureCascadeService`) exactly
  as it did before this change — the pre-pooling code path this document is replacing, not a new
  one written for the flag.
- This requires `build_app_context` to keep both code paths (pooled and single-connection)
  live rather than deleting the single-connection wiring outright, until the flag itself is
  removed in a later cleanup once pooling has been running in production without incident for
  an agreed period (an explicit follow-up item, not scoped by this document).
- The flag flips behavior with an env-var change and a process restart — **no code deploy, no
  rollback commit, no redeploy pipeline run** — which is the property that makes this a genuine
  emergency rollback path rather than a same-speed-as-forward-fix revert.

**Concrete revert procedure, if the flag is judged out of scope for the initial implementation
(fallback to disclose regardless of the flag decision):**

1. Revert `api/app.py`'s route-handler signatures from `Depends(get_<repository>)` parameters
   back to `ctx.<repository>` closure reads (the ~10 handlers Section 7's migration table
   names).
2. Revert `api/composition.py`'s `build_app_context` to construct
   `build_provenance_repository`, `build_episodic_repository`,
   `build_conflict_aware_semantic_repository`, `build_conflict_aware_entity_memory_repository`,
   `SqlManifestRepository`, `Zone8ConsolidationStore`, `Zone8SubjectKeyedArchiver`, and
   `SubjectErasureCascadeService` once at startup from a single connection, and delete the
   per-request `Depends(...)` provider functions added by this change.
3. Revert `composition_root.py` to call `build_postgres_connection()` (already present,
   unmodified) instead of `build_postgres_connection_pool()`, and remove the pool construction
   call from startup.
4. `psycopg[pool]` may remain in `pyproject.toml` (harmless if unused) or be removed in the
   same revert commit — either is acceptable, since it is an additive dependency with no schema
   or data impact.
5. No database migration, schema, or data revert is required at any step — this entire rollback
   is confined to the composition root and the FastAPI dependency-injection layer, matching
   Section 7's migration table's own scope for the forward change.

This procedure requires a code revert and redeploy (unlike the flag-based path above), but it
is bounded to exactly the three files Section 7's migration table already names, in a fixed
order, with no schema involvement — it is not an open-ended investigation.

---

## 8. Open items (why this is DRAFT, not IMPLEMENTATION-READY)

Unlike `fr013-predicate-schema-design.md` (which reached IMPLEMENTATION-READY after seven
review rounds resolving every open question against this codebase's own existing precedents),
this document has now been through one dedicated adversarial review round (2026-09-22, see the
Change Log) plus a user-decision resolution round (2026-09-22, v2.3), which together resolved
items 2 and 3 below in place. One genuinely open item remains that a further review pass should
resolve before implementation:

1. **BLOCKED on load-test prerequisite (updated v2.5, user-directed correction) — see backlog for
   the tracking task.** Section 4's `pool_min=2, pool_max=20` is derived from a formula, not from
   load-test data — no load test exists for this codebase yet. This item is **no longer** "accept
   as documented default, adjustable later" — the user explicitly overrode that framing 2026-09-22
   and required a REAL load test (k6/locust/pgbench-style concurrent-connection simulation against
   the actual Postgres container defined in `docker-compose.yml`, measuring connection-acquisition
   latency and exhaustion behavior under realistic concurrency) to produce real min/max pool-size
   numbers before these defaults are treated as final for implementation. Tracked as
   `DASH-STORY-028-LOADTEST` in `docs/phase-6-sprint-planning/backlog_draft.json`'s
   `sprint_5_dependency_graph` and `sprint5_ar1_assignments.json`'s
   `dependency_integrity_check.sprint_5_edges`, as a blocking prerequisite for
   `DASH-STORY-028-DEV`. This item stays OPEN/BLOCKED — not closed — until that sub-task actually
   runs and produces real numbers; no load test has been run and no load-test numbers exist as of
   this revision.
2. ~~**Whether Postgres access should also get a full circuit breaker**~~ **RESOLVED in v2.3.**
   The user decided pooled Postgres access REQUIRES a full circuit breaker now, matching HLD
   Section 6's existing mandatory "Every adapter call -> Circuit Breaker" pattern with no
   exception, rather than deferring the question. Section 5.1 specifies the breaker (thresholds,
   states, fallback behavior, and the Postgres-specific failure definition), reusing HLD Section
   5's existing count-based ring-buffer breaker utility rather than inventing a new mechanism.
   This item is closed — it is not carried forward as still-open.
3. ~~**The exact FastAPI dependency-injection wiring point** (Section 3/7) is described at the
   component level, not verified line-by-line against `api/composition.py`'s and `api/app.py`'s
   real current structure...~~ **Resolved in v2.** A dedicated adversarial review round (2026-09-22)
   verified Section 3/7 against `composition.py:145-253` (`build_app_context`) and
   `app.py:127-140` (`create_app`) line-by-line, found the v1 description factually wrong
   (repository builders run once at startup, not per request — there was no per-request call site
   to "change the connection source of"), and Section 3/7 have been rewritten in place to describe
   the real required refactor: repository/service construction moves from startup-time
   `build_app_context` into per-request FastAPI `Depends(...)` providers, and every route handler
   currently closing over `ctx.<repository>` is rewritten to receive it via dependency injection.
   The same review found four Zone 8 components (`SqlManifestRepository`,
   `Zone8ConsolidationStore`, `Zone8SubjectKeyedArchiver`, `SubjectErasureCascadeService`) sharing
   the same connection per `composition.py`'s own docstring but previously out of this document's
   scope; Section 1 and Section 7 now include them. This item is closed — it is not carried
   forward as still-open.

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
- [ ] The per-request repository/service construction path (Section 3/7) is verified wired
      correctly for **all** of Zone 2 (episodic), Zone 3 (semantic), Zone 5 (conflict-aware entity),
      Zone 7 (provenance), and Zone 8-manifest (`SqlManifestRepository`,
      `Zone8ConsolidationStore`, `Zone8SubjectKeyedArchiver`, `SubjectErasureCascadeService`) —
      not only the Zone 2/3/5/7 subset this document named before its v2 scope correction. Verified
      by confirming `api/composition.py` has a per-request `Depends(...)` provider for each of the
      eight builders/classes listed in Section 7's migration table, and that `api/app.py` has zero
      remaining `ctx.<repository>`/`ctx.<service>` closure-variable reads for any of them.
- [ ] Section 7.1's rollback path is real, not aspirational: either the `POOLING_ENABLED`
      env-var gate is implemented and its single-connection fallback code path is exercised by
      at least one test, or the flag is explicitly deferred and Section 7.1's concrete
      code-revert procedure is confirmed accurate against the as-shipped file layout.
- [ ] Section 5.1's circuit breaker is implemented in front of `get_pooled_connection`, reusing
      HLD Section 5's existing count-based ring-buffer breaker utility: connect-attempt failures
      (not query-level errors, not `PoolTimeout`) trip it per the documented thresholds, OPEN
      fails fast with `503` + `Retry-After` without attempting pool acquisition, and HALF_OPEN's
      5-probe-must-all-succeed recovery path is exercised by at least one test proving all three
      state transitions (CLOSED -> OPEN, OPEN -> HALF_OPEN, HALF_OPEN -> CLOSED/OPEN).
- [ ] Section 5.1.1's Retry-After/error-code disambiguation is implemented and independently
      verified (not only self-certified by Dev/QA): breaker-OPEN responses carry
      `error.code = POSTGRES_UNAVAILABLE` and a `Retry-After` computed from the breaker's actual
      remaining backoff (`max(1, ceil(opened_at + backoff_duration - now))`), while `PoolTimeout`
      responses carry `error.code = POOL_EXHAUSTED` and the existing fixed ~1s `Retry-After` —
      confirmed distinguishable in at least one test asserting both response bodies side by side.

---

## Change Log

| Date | Change |
|---|---|
| 2026-09-22 | v1: Initial design. Confirmed the API host is synchronous (plain `def` route handlers) from `api/app.py`, settling the `ConnectionPool` vs `AsyncConnectionPool` choice from evidence rather than preference. Recommended acquire-per-request via a FastAPI generator dependency over app-lifetime connection holding, since only acquire-per-request actually resolves the shared-connection contention AR1-S3-G3 names. Derived a pool-sizing formula from Postgres's default `max_connections=100` and FastAPI's default threadpool ceiling. Mapped pool exhaustion to the existing NFR-014/AC-021 503+Retry-After convention rather than inventing a new one. Explicitly restated (not contradicted) Sprint 4's existing partial-reporting contract and the still-separate cross-zone-atomicity gap. Marked DRAFT, not IMPLEMENTATION-READY, pending a review round per Section 8's three open items — no review round has run yet. |
| 2026-09-22 | v2 (`solution-architect`): First dedicated adversarial deep-dive review of this document (prior review rounds only touched it as a side-effect of reviewing other stories) found and fixed a real defect verified against source: v1's Section 3/7 claimed call sites "change their connection source, not their call shape," implying the zone-repository builders are called per-request today. Verified against `api/composition.py:145-253` (`build_app_context`) and `api/app.py:127-140` (`create_app`) that this is **false** — every builder runs exactly once at startup, results are stored as fixed `AppContext` fields, and ~10 route handlers reference them as closure variables (`app.py:302,305,420,449,482,498,572,588,878,885`); there is no per-request call site to swap a connection source at. Rewrote Section 3 and Section 7's `api/app.py`/`api/composition.py` rows to describe the real required refactor: move repository/service construction out of startup-time `build_app_context` into per-request FastAPI `Depends(...)` providers, and rewrite the affected route handlers to receive their repository via dependency injection instead of a closure read. Extended Section 1's problem statement and Section 7's migration table to include Zone 8's connection-sharing surface (`SqlManifestRepository`, `Zone8ConsolidationStore`, `Zone8SubjectKeyedArchiver`, `SubjectErasureCascadeService`), confirmed via `composition.py`'s own docstring (lines 38-45, "reused across Zone 2/3/7/8-manifest") and never previously in scope. Added a new Definition-of-Done item (Section 9) requiring the per-request wiring to be verified for all of Zone 2/3/5/7/8-manifest, not only the previously-named subset. Resolved Section 8's former open item 3 (DI wiring point not line-verified) in place — closed, not carried forward as open; Section 8's two remaining open items (pool sizing unmeasured, circuit-breaker-vs-Postgres decision) are unaffected by this revision. Companion fixes in the same review, tracked outside this file: `sprint5_ar1_assignments.json` AC-028-REVIEW-1 was scoped too narrowly to catch this class of defect (checked only zone repository code, not the `api/app.py`+`api/composition.py` wiring layer) — added AC-028-REVIEW-3 requiring the reviewer to confirm the per-request DI refactor covers both files correctly; propagated into `sprint5_implementation_execution_plan.json`'s DASH-STORY-028-REVIEW dispatch prompt. Still DRAFT, not IMPLEMENTATION-READY, pending the two remaining Section 8 open items. |
| 2026-09-22 | v2.1 (`solution-architect`, round-2 adversarial re-review): Fixed a zone-number/repository-name mispairing in Section 9's DoD item introduced by the v2 fix pass — it read "Zone 2 (provenance), Zone 3 (conflict-aware entity/semantic), Zone 5 (episodic), Zone 7 (conflict-aware semantic)," which scrambled the mapping. Verified the real mapping against `src/dashanan/domain/zone.py`'s `ZoneId` enum and `src/dashanan/api/app.py:80-89`'s `_WIRE_TO_DOMAIN_ZONE` table: Zone 2 = episodic, Zone 3 = semantic, Zone 5 = entity, Zone 7 = provenance. Section 9's DoD item now reads "Zone 2 (episodic), Zone 3 (semantic), Zone 5 (conflict-aware entity), Zone 7 (provenance)," correctly pairing `episodic_repository` with Zone 2, `semantic_repository` with Zone 3, `conflict_aware_entity_repository` with Zone 5, and `provenance_repository` with Zone 7. No other section of this document repeats the scrambled pairing. Also, DASH-STORY-028's story-point estimate (8 SP) was found not to reflect this doc's own v2-corrected, wider refactor scope -- re-estimated to 13 SP in the routing bundle (tracked outside this file). |
| 2026-09-22 | v2.2 (`solution-architect`, consensus-agent APPROVED_WITH_CONDITIONS remediation): consensus-agent's review of DASH-STORY-028's 13 SP scope gave APPROVED_WITH_CONDITIONS (2 WARNING, 2 INFO), not a clean APPROVE. WARNING 1 fixed here: Section 7's migration table already noted `build_postgres_connection()` "is not deleted, only no longer used as the API host's per-request connection source," but that fact was never named as a disclosed rollback mechanism — added new Section 7.1 ("Rollback procedure") proposing a `POOLING_ENABLED` env-var gate (letting `composition_root.py`/`api/composition.py` fall back to the pre-existing single-connection startup path without a code deploy) as the primary mechanism, plus a concrete, ordered, three-file code-revert procedure as a fallback if the flag is deferred. Added a matching Definition of Done item (Section 9) requiring the rollback path to be real, not aspirational, before this story closes. WARNING 2 (retry granularity for DASH-STORY-028-DEV's larger scope) and INFO 1 (AC-028-QA-3's escalating-gate cross-reference) and INFO 2 (QA/REVIEW context-budget sizing) are fixed in `sprint5_implementation_execution_plan.json`, `sprint5_ar1_assignments.json`, and `sprint5_ar3_context_windows.json` respectively — tracked outside this file, per this document's own established companion-fix convention (see the v2 entry above). |
| 2026-09-22 | v2.3 (`solution-architect`, user-decision resolution): the user resolved Section 8's former open item 2 ("whether Postgres access should also get a full circuit breaker") by REQUIRING a full circuit breaker now, matching HLD Section 6's existing "Every adapter call -> Circuit Breaker" mandatory pattern (previously carrying a documented, time-bound exception for pooled Postgres access, per ADR-020's Consequences block) rather than deferring the question further. Added new Section 5.1 ("Circuit breaker for pooled Postgres access") specifying: the Postgres-specific failure definition (connect/acquire-establishment failures only — never normal query-level errors, and never `PoolTimeout`, which remains Section 5's separate pool-exhaustion signal); thresholds identical to HLD Section 8's existing vector-store breaker (failure rate >= 50% or slow-connect rate >= 30% at 2x p99 over a 20-call ring-buffer window, reusing HLD Section 5's existing count-based ring-buffer breaker utility rather than inventing a new one); OPEN-state fallback behavior (fail-fast 503+Retry-After without even attempting `pool.connection()`); and HALF_OPEN probe behavior (backoff `30s * 2^(trips-1)` capped at 300s, 5 probes all must succeed to close) — identical shape to HLD's vector-store breaker for one consistent operator mental model. Updated Section 5's own paragraph to point to 5.1 instead of deferring. Updated Section 7's migration table with a new `api/composition.py` (breaker) row and a cross-reference on the `HLD.md` row. Resolved Section 8's former open item 2 in place — closed, not carried forward as open; Section 8's one remaining open item (pool sizing unmeasured) is unaffected by this revision. Added a new Definition of Done item (Section 9) requiring the breaker's three state transitions to be tested. Companion fixes in the same pass, tracked outside this file: `HLD.md` ADR-020's "Documented exception" Consequences text is updated to record the exception as closed (see HLD.md's own Revision History); `sprint5_ar1_assignments.json` gains AC-028-DEV-4/AC-028-QA-4 and DASH-STORY-028's story points are re-estimated from 13 SP to 21 SP (Dev 13/QA 5/Review 3) to account for this real, nontrivial added scope; `sprint5_sprint_plan.json`, `sprint5_implementation_execution_plan.json`, and `sprint5_ar3_context_windows.json` are updated to match. |
| 2026-09-22 | v2.4 (`solution-architect`, consensus-agent review remediation — 4 BLOCKER findings on v2.3's Section 5.1): a consensus-agent review of DASH-STORY-028's new circuit-breaker design found and required fixes for 4 issues, all BLOCKER. (1) **Retry-After/error-code disambiguation gap**: v2.3's Section 5.1 OPEN-state 503 reused the exact same response shape as Section 5's `PoolTimeout` 503 with no distinguishing value or error code — a client or operator could not tell "Postgres is down" (breaker-OPEN, can persist up to 300s) from "pool momentarily saturated" (`PoolTimeout`, ~1s) from the response alone, and reusing the 1s value against breaker-OPEN would hammer a known-down dependency roughly once per second for up to five minutes, defeating the breaker's purpose. Fixed by adding new Section 5.1.1 ("Retry-After and error-code disambiguation from PoolTimeout"): breaker-OPEN now returns `error.code = POSTGRES_UNAVAILABLE` and a dynamically-computed `Retry-After` (`max(1, ceil(opened_at + backoff_duration - now))`, the actual remaining time until the next HALF_OPEN probe), while `PoolTimeout` (Section 5) is given the corresponding `error.code = POOL_EXHAUSTED` for symmetry. Added a matching Definition of Done item (Section 9). (2) **Missing independent REVIEW-side verification of the breaker**: DASH-STORY-028-REVIEW's 4 ACs verified DI-wiring, atomicity-overclaim absence, and rollback, but none verified the breaker itself was implemented per spec, despite this bundle's own established precedent (AC-028-REVIEW-3) that self-certified Dev/QA claims about correctness-critical behavior get independent review — the breaker is 8 of the story's 21 SP, its own largest scope addition, with zero prior REVIEW coverage. Fixed outside this file: `sprint5_ar1_assignments.json` gains AC-028-REVIEW-5 (failure taxonomy, thresholds/backoff, and this revision's Retry-After/error-code disambiguation, all independently verified against source); propagated into `sprint5_implementation_execution_plan.json`'s review_prompt. (3) **Stale v2.2 citation**: `sprint5_implementation_execution_plan.json`'s DASH-STORY-028-REVIEW `review_prompt` CONTEXT SOURCES still cited `design_doc_full_connection-pooling-design.md_v2.2` after this doc advanced to v2.3 — fixed to cite v2.4 (this revision). (4) **Internal v2.2/v2.3 inconsistency**: `sprint5_ar3_context_windows.json`'s DASH-STORY-028-REVIEW entry had `sources.design_doc_full` correctly at v2.3 but its own `files_to_read_or_modify` two lines later still cited "connection-pooling-design.md v2.2 Section 7.1" — fixed within the same object to v2.4. Section 8's one remaining open item (pool sizing unmeasured) is unaffected by this revision; no new open item was introduced — the disambiguation gap was a defect in already-"resolved" v2.3 content, not a newly discovered design question. |
| 2026-09-22 | v2.5 (`solution-architect`, user-directed correction — REAL load test now required before Section 4's defaults ship): the user overrode this document's own prior framing of Section 8 item 1 ("accept `pool_min=2, pool_max=20` as a documented starting default, adjustable via env var without a code change") and required a REAL load test — k6/locust/pgbench-style concurrent-connection simulation against the actual Postgres container in `docker-compose.yml`, measuring connection-acquisition latency and exhaustion behavior under realistic concurrency — to produce real min/max pool-size numbers before Section 4's formula-derived defaults are treated as final for implementation. Section 4 updated to mark `pool_min=2, pool_max=20` PROVISIONAL, NOT FINAL. Section 8 item 1 updated from "accept as documented default" to BLOCKED, citing the new tracking task. This is a **docs-only planning correction**: no load test was run, no load-test numbers were fabricated or added anywhere in this document, and no implementation files were touched. Companion fix, tracked outside this file: a new lightweight sub-task, `DASH-STORY-028-LOADTEST`, was added to `docs/phase-6-sprint-planning/backlog_draft.json` (blocking `DASH-STORY-028-DEV`, not folded into DASH-STORY-028's existing 21 SP), and propagated into `sprint5_sprint_plan.json`, `sprint5_ar1_assignments.json`'s `dependency_integrity_check`, and `sprint5_implementation_execution_plan.json`'s `sequencing_note` and DASH-STORY-028-DEV prompt (now stated as blocked pending this prerequisite), plus its own dispatch-ready (PREPARED FOR FUTURE USE, NOT EXECUTED) prompt and its own `sprint5_ar3_context_windows.json` entry. Section 8's other former item (circuit breaker) remains RESOLVED per v2.3/v2.4 and is unaffected by this revision. |
| 2026-09-22 | v2.6 (`solution-architect`, consensus-agent review remediation on the DASH-STORY-028-LOADTEST sub-task — INFO fix 4 of 5): this table's v2.5 row was listed BEFORE the v2.4 row, reversing chronological order (v2.4 precedes v2.5 in this document's own version history). Reordered so v2.4 appears before v2.5, matching every other row's chronological sequence. No design content changed by this fix — table ordering only. Companion fixes in the same consensus-agent review pass, tracked outside this file: `sprint5_ar1_assignments.json` gains a `failure_and_escalation_policy` field on DASH-STORY-028-LOADTEST (fix 1, BLOCKER) and a clarified DASH-STORY-028 `logging_note` (fix 5, INFO); `sprint5_ar3_context_windows.json`'s `summary.sub_tasks_with_pii_exclusion_applied` corrected from 6 to 7 (fix 2, BLOCKER); `sprint5_implementation_execution_plan.json` and `sprint5_ar3_context_windows.json` had their stale v2.4 citations of this document bumped to v2.5, and `sprint5_implementation_execution_plan.json`'s DASH-STORY-028-DEV dev_prompt's self-contradictory "pool sizing unmeasured" vs. "BLOCKED PENDING LOAD-TEST PREREQUISITE" phrasing was aligned to BLOCKED throughout (fix 3, non-BLOCKER). |
