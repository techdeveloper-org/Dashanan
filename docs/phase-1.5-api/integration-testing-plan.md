# Dashanan Phase 1.5 — Integration & Contract Testing Plan

Source of truth: `docs/phase-1.5-api/openapi.yaml` (32 operations, JWT `bearerAuth` with scopes
`context:read` / `memory:write` / `admin:zones` / `admin:tenants`) and
`docs/phase-1-architecture/HLD.md` §3.10 (Data Ownership Map) + §7.6 (Internal event contracts).
Everything below is derived from those two documents — no field, endpoint, or event shape is
invented independently of `openapi.yaml`. Where the spec is ambiguous, that is flagged explicitly
in §6 rather than guessed at, per this task's constraint (this document is one of two inputs to
`api-testing-engineer`'s parallel coverage review).

**2026-09-18 addendum:** The operation count above has been corrected to 32 (was 31 when this
plan was originally authored, before `/items:batchGet` was added to `openapi.yaml` per SRS
Change Log 1.0.5). `/items:batchGet`'s test plan has not yet been derived and is an open item,
not folded into any count below.

---

## 1. Storage adapter → zone mapping (drives the Testcontainers strategy, §2)

Per HLD §3.10 (Data Ownership Map) and §8.2 (Shape B sidecar topology):

| Zone | Owns | Storage adapter (Shape B) |
|---|---|---|
| Zone 1 — Working | `WorkingItem` | Redis (hot store) |
| Zone 2 — Episodic | `Episode` | PostgreSQL |
| Zone 3 — Semantic | `SemanticEdge`, `GeneralFact` | PostgreSQL |
| Zone 4 — Procedural | `Procedure` | PostgreSQL |
| Zone 5 — Entity | `EntityRecord` (sole attribute owner) | PostgreSQL |
| Zone 6 — Retrieval-Index | `VectorEntry` (Qdrant), `LexicalDoc` (OpenSearch) — derived, rebuildable | Qdrant + OpenSearch |
| Zone 7 — Provenance | `ProvenanceRecord`, append-only, SHA-256 `prev_hash` chain | PostgreSQL (no UPDATE grant on any service role) |
| Zone 8 — Consolidation | `ConsolidatedBlob` | Object store (S3-compatible) |
| Event bus | 5 partitioned streams (§7.6/§8.3) | Redis Streams |

**Hard constraint (integration-testing-core Operating Rule 1 / What-Agent-Must-NOT-Do 1):** every
integration test against a zone runs against the real engine in a Testcontainer — PostgreSQL 15.x
Alpine, Redis 7.x, Qdrant, OpenSearch — never H2/SQLite/in-memory fakes. Zones 2–5 share one
PostgreSQL engine but each zone's repository gets its own isolated schema within the container so
cross-zone test pollution cannot occur through a shared table.

---

## 2. Testcontainers isolation strategy (per zone read/write path)

### 2.1 Container lifecycle

Container-per-test-class with `@Container static` reuse across the class (integration-testing-core
§2, M1) — not per-method (too slow: 200-test-suite math shows 27 min vs 8 s), not JVM-static-shared
across classes (breaks cross-class isolation for Zone 7's append-only chain, which must start empty
per test class to make hash-chain assertions deterministic).

```java
// Shared base — one per zone-owning storage engine, not one per zone,
// since Zones 2-5 share PostgreSQL.
@Testcontainers
abstract class PostgresZoneIntegrationTestBase {

    @Container
    static final PostgreSQLContainer<?> POSTGRES = new PostgreSQLContainer<>(
            DockerImageName.parse("postgres:15.4-alpine"))   // must match production version — integration-testing-core §2
        .withDatabaseName("dashanan_test")
        .withReuse(true);

    @DynamicPropertySource
    static void datasourceProps(DynamicPropertyRegistry registry) {
        registry.add("spring.datasource.url", POSTGRES::getJdbcUrl);
        registry.add("spring.datasource.username", POSTGRES::getUsername);
        registry.add("spring.datasource.password", POSTGRES::getPassword);
    }

    @BeforeEach
    void resetSchema() {
        // Flyway baseline re-apply per test method for zones with non-transactional
        // operations (Zone 7 append-only inserts committed outside a rollback-friendly
        // envelope in the real write path). @Transactional rollback alone is
        // insufficient here (integration-testing-core M4) — every method truncates and
        // reapplies migrations rather than relying on rollback semantics.
    }
}
```

Per-zone container subclasses:

| Test class | Container(s) | Isolation mechanism |
|---|---|---|
| `Zone1WorkingIntegrationTest` | `GenericContainer("redis:7.2-alpine")` | `FLUSHDB` in `@BeforeEach`; TTL-bearing keys, no cross-test bleed |
| `Zone2EpisodicIntegrationTest` | `PostgreSQLContainer` (shared base) | `@Transactional` rollback (pure INSERT/SELECT, no DDL) |
| `Zone3SemanticIntegrationTest` | `PostgreSQLContainer` (shared base) | `@Transactional` rollback; asserts entity-reference-only reads into Zone 5 (HLD §3.10 hard rule 2) never copy attribute values |
| `Zone4ProceduralIntegrationTest` | `PostgreSQLContainer` (shared base) | `@Transactional` rollback |
| `Zone5EntityIntegrationTest` | `PostgreSQLContainer` (shared base) | `@Transactional` rollback |
| `Zone6RetrievalIndexIntegrationTest` | `QdrantContainer` + `OpenSearchContainer` (Testcontainers modules) | Fresh collection/index per test class (`@BeforeAll` create, `@AfterAll` drop) — Zone 6 is derived/rebuildable per HLD, so container-per-class re-init is cheap and matches production semantics (a projection rebuild) |
| `Zone7ProvenanceIntegrationTest` | `PostgreSQLContainer` (shared base, separate schema) | Schema truncate + Flyway reapply per test class (append-only + no-UPDATE-grant means `@Transactional` rollback would hide a real defect: a test asserting "no UPDATE possible" needs the actual DB role grants applied by migration, not a rolled-back transaction) |
| `Zone8ConsolidationIntegrationTest` | `MinIOContainer` (S3-compatible object store) | Fresh bucket per test class |
| `EventBusIntegrationTest` | `GenericContainer("redis:7.2-alpine")` (Streams) | Fresh consumer group per test; asserts per-item total ordering via `tenant_id:item_id` partition key (HLD §8.3) |

### 2.2 Container memory budget (integration-testing-core M1)

```
M_parallel(n=8 zone containers) = 8 × M_base_image + M_jvm_heap + 8 × M_overhead
                                 ≈ 8×150MB + 384MB + 8×50MB ≈ 2,384 MB
```
Acceptable for local dev; CI shards run the 4 PostgreSQL-backed zone classes against the SAME
static container (shared base, separate schemas) to hold parallel CI memory near ~1.2 GB rather
than provisioning 4 separate PostgreSQL containers.

### 2.3 N+1 / idempotency assertions (mandatory per zone)

Every write-path integration test asserts:
1. Idempotency: identical `Idempotency-Key` + body replayed → same `write_id`, no duplicate row (HLD `/memory/write` REQUIRED Idempotency-Key, bound to a body hash).
2. Query count: no N+1 across the `/context/assemble` fan-out into Zones 2/3/4/5/6/8 — log query count per test, flag if it scales with result-set size instead of staying O(1) per zone port call.
3. Failure-path: PostgreSQL container paused mid-write → assert `503` + `Retry-After` per HLD §8.4 ("we do not accept a write we cannot durably record"), never a silent partial write.

---

## 3. Pact consumer contracts — sync endpoints

Consumer: `dashanan-host-sdk` (any host AI agent calling the sidecar). Provider: `dashanan-api`.
Contracts derived directly from `openapi.yaml` request/response schemas — Type/Regex matchers on
every generated field (`assembly_id`, `trace_id`, `write_id`, `event_id`) per contract-testing-core
Response Rule 3 (never Exact-match a UUID/timestamp).

### 3.1 `POST /context/assemble` (FR-009, FR-006, FR-007)

```json
{
  "consumer": { "name": "dashanan-host-sdk" },
  "provider": { "name": "dashanan-api" },
  "interactions": [
    {
      "description": "assemble context for a session with a token budget",
      "providerState": "tenant has at least one item in Zone 1 and Zone 6",
      "request": {
        "method": "POST",
        "path": "/v1/context/assemble",
        "headers": {
          "Authorization": "Bearer <token with context:read scope>",
          "Content-Type": "application/json"
        },
        "body": {
          "session_id": "sess_test_001",
          "task": "string, matcher: type",
          "token_budget": 8192
        },
        "matchingRules": {
          "$.body.token_budget": { "match": "type" }
        }
      },
      "response": {
        "status": 200,
        "headers": { "Content-Type": "application/json" },
        "body": {
          "assembly_id": "regex:^[0-9a-f-]{36}$",
          "trace_id": "regex:^[0-9a-f-]{36}$",
          "degraded": false,
          "items": [
            { "item_id": "type:string", "zone": "type:string", "provenance": { "status": "type:string" } }
          ]
        },
        "matchingRules": {
          "$.body.assembly_id": { "match": "regex" },
          "$.body.trace_id": { "match": "regex" },
          "$.body.degraded": { "match": "type" }
        }
      }
    },
    {
      "description": "assemble context when both retrieval indices are down (degraded)",
      "providerState": "Qdrant and OpenSearch circuit breakers are both open",
      "request": { "method": "POST", "path": "/v1/context/assemble", "body": { "session_id": "sess_degraded", "task": "type:string", "token_budget": 4096 } },
      "response": {
        "status": 200,
        "body": {
          "degraded": true,
          "retrieval_mode": "working_only",
          "zones_unavailable": [2, 3, 4, 5, 6, 8]
        },
        "matchingRules": { "$.body.zones_unavailable": { "match": "type", "min": 0 } }
      }
    },
    {
      "description": "assembleContext rejects an invalid token_budget",
      "providerState": "none",
      "request": { "method": "POST", "path": "/v1/context/assemble", "body": { "session_id": "s", "task": "t", "token_budget": -1 } },
      "response": { "status": 400 }
    }
  ]
}
```

### 3.2 `POST /memory/write` (FR-009, FR-010, FR-013)

```json
{
  "consumer": { "name": "dashanan-host-sdk" },
  "provider": { "name": "dashanan-api" },
  "interactions": [
    {
      "description": "write a memory item with a resolvable source_type",
      "providerState": "tenant exists and is authenticated for memory:write",
      "request": {
        "method": "POST",
        "path": "/v1/memory/write",
        "headers": {
          "Authorization": "Bearer <token with memory:write scope>",
          "Idempotency-Key": "regex:^[0-9a-f-]{36}$",
          "Content-Type": "application/json"
        },
        "body": {
          "content": "type:string",
          "source_type": "user_stated",
          "zone_hint": "type:string"
        }
      },
      "response": {
        "status": 202,
        "body": {
          "write_id": "regex:^[0-9a-f-]{36}$",
          "status": "accepted"
        },
        "matchingRules": { "$.body.write_id": { "match": "regex" } }
      }
    },
    {
      "description": "write is rejected when source_type is not resolvable (FR-010 boundary enforcement)",
      "providerState": "none",
      "request": { "method": "POST", "path": "/v1/memory/write", "body": { "content": "type:string" } },
      "response": { "status": 422 }
    },
    {
      "description": "duplicate Idempotency-Key with a different body is a conflict",
      "providerState": "an accepted write already exists for Idempotency-Key idem-dup-1",
      "request": {
        "method": "POST",
        "path": "/v1/memory/write",
        "headers": { "Idempotency-Key": "idem-dup-1" },
        "body": { "content": "different content", "source_type": "user_stated" }
      },
      "response": { "status": 409 }
    },
    {
      "description": "structured store is down — write is rejected, never silently accepted",
      "providerState": "PostgreSQL primary is unreachable",
      "request": { "method": "POST", "path": "/v1/memory/write", "body": { "content": "type:string", "source_type": "user_stated" } },
      "response": { "status": 503, "headers": { "Retry-After": "type:string" } }
    }
  ]
}
```

### 3.3 Provider state handlers (Spring Boot + Pact JVM, `dashanan-api` side)

```java
@State("tenant has at least one item in Zone 1 and Zone 6")
void seedAssemblyFixture() { /* insert WorkingItem + VectorEntry via test data builders */ }

@State("Qdrant and OpenSearch circuit breakers are both open")
void forceBothBreakersOpen() { /* WireMock fault injection on the adapter ports, not real container kill — keeps the Pact provider-verify step fast */ }

@State("PostgreSQL primary is unreachable")
void simulatePostgresDown() { /* toxiproxy cut on the structured-store connection */ }
```

State coverage requirement (contract-testing-core M3, `StateComplete`): every `providerState` string
above has a matching `@State` handler before CI publishes to the Pact broker — missing handler = hard
verification failure, not a skip.

---

## 4. Async event contracts — the 6 `x-internal-event-contracts` events (FR-013, FR-012, FR-009)

Transport is Redis Streams (HLD §8.3), not HTTP — these are documented as **provider states on the
publishing service's own Pact provider verification**, per contract-testing-core §1 (consumer defines
what it needs; here each **consumer service** — index-worker, provenance-relay, rotation-worker,
metrics, alerting, consolidation — is a Pact consumer of the event schema, and the publishing service
is the provider). Every event's envelope + payload fields below are copied verbatim from
`openapi.yaml`'s `x-internal-event-contracts` block (lines 1455–1499) — not re-derived.

```json
{
  "consumer": { "name": "index-worker" },
  "provider": { "name": "orchestrator-event-publisher" },
  "interactions": [
    {
      "description": "memory.written event carries the required envelope + payload",
      "providerState": "an item was written and durably accepted",
      "request": { "event_type": "memory.written" },
      "response": {
        "body": {
          "event_id": "regex:^[0-9a-f-]{36}$",
          "event_type": "memory.written",
          "occurred_at": "regex:^\\d{4}-\\d{2}-\\d{2}T",
          "correlation_id": "type:string",
          "causation_id": "type:string",
          "tenant_id": "type:string",
          "schema_version": "type:string",
          "item_id": "type:string",
          "zone": "type:string",
          "token_count": "type:integer",
          "provenance_id": "type:string",
          "score_terms": "type:object"
        }
      }
    }
  ]
}
```

The remaining five events follow the identical envelope + their own `payload_fields` list, one Pact
interaction file per event, one per consumer:

| Event | Publisher | Consumers (each a separate Pact consumer contract) | Payload fields |
|---|---|---|---|
| `memory.promoted` | rotation-worker | index-worker, metrics | item_id, from_zone, to_zone, trigger, score |
| `memory.compressed` | rotation-worker | index-worker, provenance-relay | item_id, zone, generation, original_tokens, compressed_tokens |
| `memory.archived` | rotation-worker | index-worker, consolidation | item_id, from_zone, blob_id |
| `memory.evicted` | rotation-worker | index-worker, metrics | item_id, zone, reason |
| `memory.conflict_detected` | Orchestrator | provenance-relay, metrics, alerting | item_ids[], predicate, both_confidences |

**Schema evolution rule enforced by these contracts:** `schema_version` is additive-only (EDA/M6
FULL compatibility, per both `openapi.yaml` and HLD §7.6) — a Pact interaction MUST use a Type
matcher on every payload field so a provider adding a new optional field never breaks these
contracts; a field **removal or retype requires a new `event_type`**, which is itself a new Pact
interaction, not an edit to an existing one (contract-testing-core Breaking Change Taxonomy §2).

**Per-item total ordering (HLD §8.3):** integration tests for the event contracts additionally
assert Redis Streams partition key `tenant_id:item_id` — a `memory.compressed` processed after
`memory.archived` for the same item is asserted as a correctness bug, not just a contract mismatch,
per HLD's explicit "non-negotiable" framing.

---

## 5. E2E smoke-test flow (outline only — acceptance-level, not implemented here)

Per e2e-testing-core §1 (E2E is 10% of the pyramid, ROI-justified only for full-journey defects unit/
integration cannot catch), this is the ONE smoke flow this contract enables — not a full E2E suite.

```
1. WRITE   POST /memory/write  (source_type: user_stated, zone_hint: episodic)
           → 202, write_id captured
           assert: memory.written event observed on the event bus within 2s (HLD write-visibility p99)

2. ASSEMBLE POST /context/assemble (same session_id, task referencing the written content)
           → 200, item present in `items[]`, provenance.status != "pending" once relay drains
           assert: read-your-own-write holds within-session (ADR-002) even though cross-zone
                   visibility is eventual — this is the one invariant only an E2E test can prove,
                   because it spans the sync write-accept path AND the async indexing path together

3. ROTATE   Force a promotion trigger (age the item past PromoteThreshold via test clock, or call
            POST /zones/{zone}/sweep with admin:zones scope)
           → memory.promoted event observed; item's zone in a subsequent GET /items/{item_id}
             reflects to_zone

4. VERIFY   GET /items/{item_id}/score:explain
           → score_terms reflect the post-promotion state; provenance chain
             (GET /items/{item_id}/provenance) shows the write + the promotion as separate,
             hash-chained records (HLD §3.8 append-only, SHA-256 prev_hash)
```

Selector/tooling note: this flow is API-only (no browser), so it is implemented with the same
REST-Assured / requests client used for integration tests, run against the full Shape B docker-compose
stack (all 8 zone adapters + event bus, not per-zone Testcontainers) — data-testid/POM concerns from
e2e-testing-core §4/§10 do not apply here since there is no rendered UI in this contract's scope.

---

## 6. Test suite directory structure (test-automation-architecture-core §1.2 Architecture Levels)

```
dashanan-api/
  src/test/java/com/dashanan/
    unit/                                  # Level 1 — not this agent's scope; placeholder for Phase B
    integration/
      base/
        PostgresZoneIntegrationTestBase.java
      zone1working/Zone1WorkingIntegrationTest.java
      zone2episodic/Zone2EpisodicIntegrationTest.java
      zone3semantic/Zone3SemanticIntegrationTest.java
      zone4procedural/Zone4ProceduralIntegrationTest.java
      zone5entity/Zone5EntityIntegrationTest.java
      zone6retrievalindex/Zone6RetrievalIndexIntegrationTest.java
      zone7provenance/Zone7ProvenanceIntegrationTest.java
      zone8consolidation/Zone8ConsolidationIntegrationTest.java
      eventbus/EventBusIntegrationTest.java
      wiremock/                            # stubs for EmbeddingProvider, Summarization LLM — generated from their own upstream OpenAPI specs, never hand-rolled (Operating Rule 5)
    contract/
      consumer/
        ContextAssemblyPactConsumerTest.java
        MemoryWritePactConsumerTest.java
      provider/
        DashananApiPactProviderVerificationTest.java   # runs VERIFY(C,P) against every consumer pact pulled from the broker
      events/
        MemoryWrittenEventPactTest.java
        MemoryPromotedEventPactTest.java
        MemoryCompressedEventPactTest.java
        MemoryArchivedEventPactTest.java
        MemoryEvictedEventPactTest.java
        ConflictDetectedEventPactTest.java
      pacts/                                # generated .json artifacts, published to Pact Broker, not hand-edited
    e2e/
      smoke/
        MemoryWriteAssembleRotateSmokeTest.java         # the one flow in §5
  docker-compose.test.yml                   # full-stack compose for the E2E smoke test only
```

**Architecture level mapping (test-automation-architecture-core §1.2):** Level 1 test cases live in
`integration/{zone}/` and `contract/`; Level 3 (business-action layer) is the `WriteMemoryTestDataBuilder`
/ `ContextAssemblyRequestBuilder` classes each zone package owns — not shown above for brevity but
mandatory per integration-testing-core Operating Rule 7 (no raw SQL/JSON literals in test setup).

---

## 7. CI pipeline snippet — Pact publish/verify + can-i-deploy gate (mandatory, not optional)

```yaml
# .github/workflows/dashanan-api-ci.yml (excerpt)
jobs:
  pact-consumer:
    runs-on: ubuntu-latest
    steps:
      - run: ./gradlew :dashanan-host-sdk:pactTest
      - run: |
          pact-broker publish build/pacts \
            --broker-base-url "$PACT_BROKER_URL" \
            --consumer-app-version "$GITHUB_SHA" \
            --branch "$GITHUB_REF_NAME"

  pact-provider-verify:
    needs: pact-consumer
    runs-on: ubuntu-latest
    steps:
      - run: ./gradlew :dashanan-api:pactVerify
        env:
          PACT_BROKER_URL: ${{ secrets.PACT_BROKER_URL }}
          PACT_PROVIDER_VERSION: ${{ github.sha }}
      - run: |
          pact-broker publish-verification-results \
            --provider dashanan-api \
            --provider-app-version "$GITHUB_SHA"

  integration-tests:
    needs: pact-provider-verify
    runs-on: ubuntu-latest
    steps:
      - run: ./gradlew :dashanan-api:integrationTest
        # Testcontainers spins PostgreSQL/Redis/Qdrant/OpenSearch/MinIO per §2 — no external infra needed in CI

  can-i-deploy-gate:
    needs: [pact-consumer, pact-provider-verify]
    runs-on: ubuntu-latest
    steps:
      - name: can-i-deploy (host-sdk -> api)
        run: |
          pact-broker can-i-deploy \
            --pacticipant dashanan-host-sdk \
            --version "$GITHUB_SHA" \
            --to-environment staging
      - name: can-i-deploy (api -> host-sdk)
        run: |
          pact-broker can-i-deploy \
            --pacticipant dashanan-api \
            --version "$GITHUB_SHA" \
            --to-environment staging
    # HARD GATE: this job's exit code blocks the deploy job below. Never skipped
    # (integration-testing-engineer Operating Rule 4 / What-Agent-Must-NOT-Do 5).

  deploy:
    needs: [can-i-deploy-gate, integration-tests]
    runs-on: ubuntu-latest
    steps:
      - run: ./deploy.sh staging
```

---

## 8. Flagged ambiguities in `openapi.yaml` for `api-testing-engineer`'s parallel review

Per this task's constraint ("flag any endpoint shape you find ambiguous rather than silently
guessing"), the following were NOT resolved unilaterally when building the contracts above:

1. **Colon-suffixed path segments** — `/memory/write:batch` (line 330) and
   `/items/{item_id}/score:explain` (line 261) use a Google-API-style custom-method colon suffix
   (`:verb`) rather than a sub-resource path segment (`/memory/writes/batch`,
   `/items/{item_id}/score-explanation`). This is valid OpenAPI 3.1 syntax, but several API
   gateways and some HTTP client libraries mis-handle a literal `:` inside a path segment (it can be
   parsed as a port-separator or a matrix-parameter delimiter depending on the client). The Pact
   contracts in §3 use the literal paths as written; recommend `api-testing-engineer` confirm the
   chosen API gateway (mentioned as Shape B sidecar, gateway unnamed in the spec excerpt read) round-trips
   these paths correctly before this pattern is relied on elsewhere.
2. **`WriteMemoryRequest` / `AssembleContextRequest` full schemas were not fully enumerated in this
   document** — §3's Pact bodies show the fields referenced by the endpoint descriptions and FR
   traceability comments (`content`, `source_type`, `zone_hint`, `session_id`, `task`,
   `token_budget`) but the full `components/schemas` block (beyond what was read for this plan) may
   contain additional required fields. **Do not treat §3's Pact bodies as the complete schema** —
   `api-testing-engineer`'s OpenAPI-schema-validation coverage pass is the authoritative source for
   full required-field enumeration; this document's contracts should be regenerated from the schema
   objects directly (not hand-typed) before they are committed as real `.json` pact files, per
   integration-testing-engineer's own Operating Rule 5 / What-Agent-Must-NOT-Do 2 (no hand-rolled
   contracts that duplicate/drift from the OpenAPI source).
3. **`x-consensus-gate-status: PENDING`** (line 18) — the OpenAPI document's own metadata marks the
   contract as not yet consensus-approved. The Pact contracts and CI gate above are written against
   the DRAFT spec; the `can-i-deploy` gate in §7 will need re-verification once the spec exits
   PENDING status, since Pact interactions generated against a pre-consensus contract are exactly
   the kind of drift `can-i-deploy` is meant to catch.
4. **NFR-004 latency targets (`docs/phase-1-architecture/HLD.md` §9) are explicitly `[ASSUMED]`
   pending OAQ-13** — this plan does not encode timing-contract assertions (integration-testing-core
   M5's fourth contract dimension) against these numbers, since asserting against an unconfirmed
   target would itself be a fabricated contract. Recommend performance-testing-core scope, not this
   plan, once OAQ-13 resolves.

---

## Version

1.0.0 — 2026-09-17 | Phase 1.5 API Contract pipeline | Author: integration-testing-engineer
