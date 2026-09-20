"""Zone-4-scoped port for the Procedural Memory adapter (HLD Section 3.5, FR-004).

Deliberately NOT added to the frozen `dashanan.domain.ports` module (AR1-G2:
the `ZoneRepository` port is frozen for every story after DASH-STORY-001).
This is Zone 4's own seam between its point-lookup/write path and its
storage adapter, mirroring how `retrieval_index_ports.py` keeps Zone 6's
`VectorIndexPort`/`LexicalIndexPort` local rather than widening the shared
ports module -- the same DASH-STORY-003/DASH-STORY-007 convention applied
here for Zone 4.

AC-004-2 (verbatim): "Zone 4's adapter port exposes only point-lookup, no
similarity-search method; any similarity need routes exclusively through
Zone 6. Enforced by a contract test asserting the Zone 4 adapter interface
contains no similarity-search operation." This `ProcedureRepositoryPort`
Protocol IS that adapter interface, and it is that contract test's direct
subject -- see
`tests/test_procedural_memory_zone4_conformance_dash015.py`. It defines
exactly two operations, a point-lookup read and a commit write, and
nothing resembling a `search`/`find_similar`/`query`-by-vector method.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from dashanan.domain.procedure import Procedure


@runtime_checkable
class ProcedureRepositoryPort(Protocol):
    """Zone 4's own point-lookup-only read/write contract (HLD Section 3.5)."""

    def get_by_task_signature_hash(
        self, tenant_id: str, task_signature_hash: str
    ) -> Procedure | None:
        """Return the `Procedure` at this exact key, or `None` (AC-004-1).

        Must be an O(1) point lookup (a HashMap/dict-keyed lookup) --
        never a scan or a similarity comparison against stored vectors.
        """
        ...

    def commit(self, procedure: Procedure) -> None:
        """Upsert `procedure` and publish its Zone 6 projection event (AC-004-4).

        Must not raise for a downstream event-bus delivery failure --
        `EventBus.publish`'s own contract ("must not raise for I/O")
        already guarantees this for every adapter that honors it.
        """
        ...
