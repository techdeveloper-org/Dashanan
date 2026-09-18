# RBAC Matrix — Dashanan API

**Source of truth:** `docs/phase-1.5-api/openapi.yaml` — this file consolidates the `bearerAuth` scope
requirement that is already declared inline on every operation in the spec. It does not introduce any
new scope or endpoint; it is a read-only summary for reviewers who need the full picture without
reading every operation block.

**On the error-response format:** this API intentionally does **not** use RFC 7807
(`application/problem+json`) for error bodies. `~/.claude/rules/01-common-standards.md` §6 mandates a
single consistent `{success, data, error, timestamp}` envelope for every endpoint in every project on
this machine, and `openapi.yaml`'s `ErrorResponse` schema already follows it. Adopting RFC 7807 would
directly violate that global standard, so it is out of scope here by design, not by oversight.

## Scopes

| Scope | Grants |
|---|---|
| `context:read` | Read-only retrieval: assemble context, hybrid search, item/provenance/score reads |
| `memory:write` | Mutating writes to memory items (create, patch, soft-delete, batch write) |
| `admin:zones` | Zone/config/job administration, scoring config, metrics |
| `admin:tenants` | Tenant lifecycle, tenant config, DPDP subject erasure/export |
| *(none)* | `/healthz`, `/readyz` — unauthenticated liveness/readiness probes |

## Endpoint × Method × Scope

| Endpoint | Method | Scope Required | Notes |
|---|---|---|---|
| `/context/assemble` | POST | `context:read` | Budget-fitted, provenance-carrying context slice |
| `/context/assemble` | GET | `context:read` | Plain-text query variant (debug/simple hosts) |
| `/memory/search` | POST | `context:read` | Raw hybrid retrieval, no budget-fitting |
| `/items/{item_id}` | GET | `context:read` | Fetch one item with current score terms |
| `/items/{item_id}` | PATCH | `memory:write` | Partial update — appends provenance, never in-place |
| `/items/{item_id}` | DELETE | `memory:write` | Soft delete + tombstone propagation to Zone 6 |
| `/items/{item_id}/provenance` | GET | `context:read` | Full provenance chain |
| `/items/{item_id}/score:explain` | POST | `context:read` | Explain Memory Score computation |
| `/memory/write` | POST | `memory:write` | Single item write (async-accepted) |
| `/memory/write:batch` | POST | `memory:write` | Batch write, up to 100 items |
| `/writes/{write_id}` | GET | `memory:write` | Poll accepted-write status |
| `/zones` | GET | `admin:zones` | All 8 zones — health/count/capacity/adapter/sweep |
| `/zones/{zone}` | GET | `admin:zones` | Zone detail + rotation statistics |
| `/zones/{zone}/config` | GET | `admin:zones` | Read zone config (lambda, thresholds, TTL, adapter) |
| `/zones/{zone}/config` | PUT | `admin:zones` | Update zone config (Strategy pattern) |
| `/zones/{zone}/sweep` | POST | `admin:zones` | Force a rotation sweep |
| `/zones/{zone}/items` | GET | `admin:zones` | Cursor-paginated admin browse |
| `/zones/{zone}/reindex` | POST | `admin:zones` | Rebuild Zone 6 vector+lexical projection |
| `/config/scoring` | GET | `admin:zones` | Read Memory Score weights/thresholds |
| `/config/scoring` | PUT | `admin:zones` | Update Memory Score weights/thresholds |
| `/jobs/{job_id}` | GET | `admin:zones` | Async admin job status |
| `/tenants` | POST | `admin:tenants` | Provision tenant + physical partitions |
| `/tenants` | GET | `admin:tenants` | List provisioned tenants |
| `/tenants/{tenant_id}` | GET | `admin:tenants` | Tenant config, incl. `embedding_residency` (ADR-015) |
| `/tenants/{tenant_id}` | PATCH | `admin:tenants` | Update tenant config |
| `/tenants/{tenant_id}` | DELETE | `admin:tenants` | Deprovision tenant |
| `/tenants/{tenant_id}/subjects/{subject_id}` | DELETE | `admin:tenants` | DPDP erasure cascade for one data subject |
| `/tenants/{tenant_id}/subjects/{subject_id}/export` | GET | `admin:tenants` | DPDP data-portability export |
| `/healthz` | GET | — | Liveness probe, no auth |
| `/readyz` | GET | — | Readiness probe, no auth |
| `/metrics` | GET | `admin:zones` | Prometheus metrics exposition |

## Error-state boundary (401 vs 403 vs 404)

Per `openapi.yaml`'s `ErrorResponse` and the `Forbidden`/`TenantIdMismatch` examples already in the spec
(`/tenants/{tenant_id}` GET, line ~801): a request with no valid bearer token returns `401`; a request
with a valid token lacking the required scope, or whose JWT `tenant_id` claim does not match the path's
`tenant_id`, returns `403` (never a `404`, to avoid leaking whether the resource exists to a caller who
isn't authorized to know); a request for a genuinely nonexistent `item_id`/`zone`/`tenant_id`/`job_id`
from a caller who *does* hold the correct scope and tenant match returns `404`.
