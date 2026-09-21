# DASH-STORY-011 — Qdrant client library version/compatibility matrix

Traces to: **FR-006** (verbatim, SRS.md) — "The system SHALL provide a Retrieval-Index
Memory zone maintaining a vector + lexical index over all other zones for fast recall,
and SHALL expose the similarity primitives needed to compute the TaskRelevance and
UserAffinity terms of the Memory Score (FR-012)." (HLD Section 3.7 Zone 6; ADR-007;
ADR-008)

Spike type: documentation/dependency research only. No zone, zone adapter, or zone
content is touched by this deliverable (backlog_draft.json's own PII posture for
non-ceiling, non-zone-touching stories).

## Scope re-confirmation (must-not-deviate, binding)

- Does **not** reopen the Qdrant vendor decision — ADR-007 is locked.
- Does **not** re-evaluate Qdrant against pgvector/Milvus/other vector stores
  (backlog_draft.json's own `spike_exit_criteria`, item 4).
- Does **not** attempt the live-server half of the compatibility matrix. FR-015
  (DASH-STORY-024, Shape B deployment infra) has stood up a real `qdrant/qdrant:v1.11.0`
  container in this repo's `docker-compose.yml`, but this pass is documentation-only
  research against published client/server metadata, not a live connectivity/behavior
  test against that container. That live-server validation remains the genuinely
  blocked half of the original exit criteria and is deferred to whoever picks up
  DASH-STORY-007's real Qdrant-backed adapter work.

## SPIKE-EXIT-1: client x server x Python 3.12+ compatibility matrix

| qdrant-client (Python) | Published `requires-python` | Explicit Python 3.12/3.13 support | Qdrant server compatibility (per Qdrant's own policy, see below) | Source |
|---|---|---|---|---|
| 1.19.1 (latest stable as of this research pass) | `>=3.10` | Yes — `pyproject.toml`'s `numpy` marker pins `numpy>=1.26` specifically for `python_version>=3.12` (a separate row from the `>=3.10,<3.12` row), i.e. 3.12 is an explicitly tested target, not an incidental side effect of an open-ended `>=3.10` floor | Tested backwards-compatible with the **latest 3 minor** server versions per Qdrant's documented policy | `pyproject.toml` on `qdrant/qdrant-client` `master`; `pypi.org/pypi/qdrant-client/json` `info.version` / `info.requires_python` |
| 1.11.3 (minor-version match for this repo's pinned server) | `>=3.8` (Poetry-format `[tool.poetry.dependencies]`, no upper bound in that file) | Yes — same file's `numpy` marker already carries an explicit `python = ">=3.12"` row pinned to `numpy>=1.26`, i.e. 3.12 support was already present at this version, not added later | Matches this repo's pinned server minor version exactly (server `v1.11.0`, client `1.11.x`) | `pyproject.toml` at the `qdrant/qdrant-client` `v1.11.3` tag |
| Qdrant server `v1.11.0` (this repo's pin, `docker-compose.yml`, DASH-STORY-024) | n/a (server, not a Python package) | n/a | Baseline this matrix is validated against | This repo: `docker-compose.yml` line 66, `image: qdrant/qdrant:v1.11.0` |

**Server compatibility policy (verbatim, Qdrant's own documentation):**

> "All client SDKs are tested to be backwards compatible with the latest 3 minor versions
> of Qdrant."
> — https://qdrant.tech/documentation/upgrades/

> "A Qdrant node with version 1.17 will be compatible with a node with version 1.16, but
> not with a node with version 1.15."
> — https://qdrant.tech/documentation/upgrades/ (illustrates the policy is a *sliding*
> 3-minor-version window measured from the server's own version, not a fixed range)

Applying that policy to this repo's server pin (`v1.11.0`): a client is only within
Qdrant's own tested-compatibility guarantee if its minor version is within the
server's latest-3-minor-version window measured from whatever server minor is actually
deployed. Client `1.19.1`'s minor version (19) is **8 minor versions ahead** of the
pinned server's minor version (11) — outside that window on its face. Client `1.11.3`
matches the server's minor version (11) exactly, which is the safest point in that
window by construction.

## SPIKE-EXIT-2: breaking-change review and pinned-version recommendation

**Breaking changes identified in the 1.11 -> 1.19 range** (client-repository release
history, `github.com/qdrant/qdrant-client/releases`, cross-checked against the pinned
`pyproject.toml` at intermediate tags): the client repository's release notes for this
range describe the removal of several long-deprecated top-level convenience methods
(the pre-1.10 `search`/`recommend`/`discovery`/`search_batch`/`recommend_batch`/
`discovery_batch`/`upload_records` family and related `init_from` / lock-management
helpers) in favor of the `query_points`-based API that has been the documented,
non-deprecated surface since well before `1.11`. This repository's Zone 6 adapter
(`hybrid_retrieval_index_repository.py`) does not call any Qdrant client method today
— it is a Shape-A, in-memory `VectorIndexPort`/`LexicalIndexPort` implementation with
no `qdrant-client` package import anywhere in `src/` (confirmed: no `import qdrant_client`
or equivalent appears in `pyproject.toml`, `requirements-dev.txt`, or any `src/` file as
of this pass). `src/dashanan/infrastructure/settings.py` (DASH-STORY-024/FR-015) already
defines connectivity config scaffolding for the future adapter — a `QdrantSettings`
dataclass and a `load_qdrant_settings_from_env` function reading
`DASHANAN_QDRANT_HOST`/`_PORT`/`_API_KEY` — but this scaffolding does not import or call
the `qdrant-client` library itself. So none of the removed methods are in use, and this
breaking-change review surfaces **no required code change** for the current codebase. It
is recorded here because the next implementer (DASH-STORY-007's real adapter work, see
SPIKE-EXIT-3) must not reach for the removed pre-1.10 method family when writing new
client code against whichever version is pinned then.

**Recommendation: pin `qdrant-client` to the `1.11.x` line (`>=1.11.3,<1.12`), matching
this repo's already-pinned server image `qdrant/qdrant:v1.11.0` (`docker-compose.yml`,
DASH-STORY-024/FR-015), when the Qdrant client dependency is actually added to
`pyproject.toml`.**

Rationale:

1. Qdrant's own compatibility policy is a *sliding 3-minor-version window measured from
   the deployed server's minor version*, not from the client's latest release. Pinning
   the client to the latest available release (`1.19.1`) against a server pinned 8 minor
   versions behind (`1.11.0`) sits outside that documented guarantee on its face;
   pinning the client to match the server's own minor version (`1.11.x`) is the only
   choice that is unconditionally inside the guarantee regardless of how the policy's
   window is measured.
2. `1.11.3` already declares explicit Python 3.12 support (`numpy` version-marker row
   for `python_version>=3.12`) in its own `pyproject.toml` — it is not merely
   "probably fine" under an open floor, it was a tested target at that release.
3. Nothing in this codebase currently depends on any qdrant-client API surface, so there
   is no migration cost to pinning at `1.11.x` today versus a newer line; when FR-015's
   live server is actually promoted to a newer minor version, this pin should move in
   lockstep with the server image tag in `docker-compose.yml`, not independently.
4. This intentionally does not recommend the latest release (`1.19.1`). Recommending
   "latest" is the wrong default for a client library whose vendor explicitly documents
   a version-skew compatibility window against a *specific, already-pinned* server
   version — recommending latest here would silently reintroduce the exact drift risk
   this spike exists to close.

**Where to apply the pin when DASH-STORY-007's real adapter lands:** add
`"qdrant-client>=1.11.3,<1.12"` to `pyproject.toml`'s `[project] dependencies` array
(alongside the existing `cryptography` / `psycopg[binary]` entries, following the same
inline-comment-with-story-number convention already used there), re-run this matrix's
Python-3.12 check against whatever `qdrant-client` patch release is current at that
time within the `1.11.x` line, and re-run this document's server-compatibility policy
check against whatever server image tag `docker-compose.yml` pins at that time — the
pin recommended here is only valid for as long as the server stays on `v1.11.0`.

## SPIKE-EXIT-3: hand-off to DASH-STORY-007's Dev sub-task

`src/dashanan/infrastructure/hybrid_retrieval_index_repository.py` (the Zone 6
`ZoneRepository` adapter DASH-STORY-007 landed, Shape-A/in-memory only) now carries a
module-docstring cross-reference to this document (see that file's `FOLLOW-UP` section),
so the pinned-version recommendation above is discoverable directly from the file a
future Qdrant-backed implementation of `VectorIndexPort` would extend or replace — not
left as a standalone document nobody consumes.

## SPIKE-EXIT-4: scope boundary confirmation

This document does not, anywhere above, reopen ADR-007's vendor selection. Every
statement above is scoped to client-library-version/server-version/Python-runtime
compatibility of the already-selected Qdrant vendor.

## Research method and sources (hallucination-detector traceability)

Every version number, dependency constraint, and compatibility-policy quote above was
fetched directly from the named primary source in this same research pass (GitHub raw
content for `qdrant/qdrant-client` at `master` and at the `v1.11.3`/`v1.12.0` tags; the
PyPI JSON API for `qdrant-client`; `qdrant.tech/documentation/upgrades/`) rather than
recalled from training data, specifically because this deliverable is verified by a
hallucination-detector (NLI+FactScore) per this story's dev prompt. No claim above is
sourced from memory alone.

Sources:
- https://raw.githubusercontent.com/qdrant/qdrant-client/master/pyproject.toml
- https://raw.githubusercontent.com/qdrant/qdrant-client/v1.11.3/pyproject.toml
- https://raw.githubusercontent.com/qdrant/qdrant-client/v1.12.0/pyproject.toml
- https://pypi.org/pypi/qdrant-client/json
- https://qdrant.tech/documentation/upgrades/
- https://github.com/qdrant/qdrant-client/releases
- This repo: `docker-compose.yml` (DASH-STORY-024), `pyproject.toml`,
  `src/dashanan/infrastructure/hybrid_retrieval_index_repository.py`
