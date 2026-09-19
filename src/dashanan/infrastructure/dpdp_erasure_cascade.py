"""CrossZoneDpdpErasureCascade: the concrete DpdpErasureCascadePort adapter (DSHN-58 P1 remediation).

REMEDIATION CONTEXT (P1, live-reproduced): a prior adversarial security
review found that `dashanan.application.zone2_capacity_backstop_sweep.
DpdpErasureCascadePort` -- DASH-STORY-004's sole DPDP-erasure mechanism --
was an unimplemented `Protocol` with zero concrete adapters; only a test
fake existed anywhere in the repository. That made AC-002-CAP-DPDP-1
("eviction must satisfy, not bypass, a pending DPDP erasure obligation")
structurally unverifiable -- there was nothing real to run an end-to-end
check against, so the acceptance criterion's claim was actually false.

This module closes that gap the same way every other Sprint-1 port in
this codebase is closed: a Shape A (in-memory), fully real, non-mock
adapter -- exactly mirroring `InMemoryVectorIndex`, `InMemoryLexicalIndex`,
and `WorkingMemoryLRURepository`'s own "Shape A now, Shape B later" split
(`in_memory_vector_index.py`'s docstring: "out of this story's scope...
no Qdrant client dependency is added"). A Shape B (Postgres-backed, with
the narrowly-scoped compliance role `episodic_schema.sql` already
anticipates for a future Zone-2 erasure workflow) is explicitly out of
this remediation's scope -- see the "Zone 2 leg" note below for why that
boundary is a real architectural constraint, not a shortcut.

Zone 2 leg -- why this adapter does NOT delete Zone 2 rows itself:
`episodic_schema.sql`'s `trg_episodic_entries_append_only` trigger
unconditionally rejects every UPDATE/DELETE on `episodic_entries`,
regardless of which role's privileges executed it (owner included) --
a DSHN-55 P1 security control, not an oversight. That file's own comment
states a DPDP erasure workflow is "explicitly OUT OF SCOPE for this
story... expected to run under a separate, narrowly-scoped compliance
role authorized for a PK-only DELETE -- not by widening this application
role's grants, and not by altering or dropping the append-only trigger."
Building that compliance-role SQL path is a separate, substantial piece
of work this bounded remediation does not attempt. Instead, this adapter
depends on `Zone2EvictionPort` (dependency inversion, `clean-architecture`
skill section 22) -- the SAME seam `Zone2CapacityBackstopSweep` already
uses for its own ordinary-eviction leg -- so whatever concrete Zone-2
removal mechanism a future story wires there (the Postgres compliance-role
adapter, or a Shape A in-memory one for dev/test) is reused here without
this module inventing a second, parallel Zone-2 removal path.

Cascade ordering (PII-safety rationale, not arbitrary): Zone 6 (vector +
lexical, which HOLDS ITS OWN COPY of the item's payload per
`HybridRetrievalIndexRepository`'s own docstring) is removed FIRST, Zone 2
LAST. If the Zone 2 leg then fails and the obligation stays pending for a
retry, the searchable PII copy (Zone 6) is already gone -- the worse
failure mode (Zone 6 succeeding after a Zone 2 failure) is avoided by
construction, not just by convention. The pending obligation is cleared
ONLY after every leg succeeds, so a partial failure is always retryable
on the next sweep run rather than silently declared complete.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from dashanan.application.zone2_capacity_backstop_sweep import (
    Zone2BackstopPortError,
    Zone2EvictionPort,
)
from dashanan.domain.retrieval_index_ports import LexicalIndexPort, VectorIndexPort


@runtime_checkable
class ErasureObligationStore(Protocol):
    """Tracks which `(tenant_id, item_id)` pairs have a pending DPDP erasure obligation.

    Kept local to this module (not `dashanan.domain.ports`) per this
    codebase's established convention of keeping a story's own seam
    Protocols local rather than widening the shared ports module (AR1-G2,
    mirrored from `write_gate.ProvenanceJournalPort` and this story's own
    `zone2_capacity_backstop_sweep.Zone2BackstopConfigPort`).
    """

    def has_pending_erasure(self, tenant_id: str, item_id: str) -> bool:
        """Return whether `item_id` currently has an unfulfilled erasure obligation."""
        ...

    def clear_pending_erasure(self, tenant_id: str, item_id: str) -> None:
        """Mark `item_id`'s obligation fulfilled. Idempotent: a no-op if none is pending."""
        ...


class InMemoryErasureObligationStore:
    """Shape A `ErasureObligationStore`: an in-process set of pending obligations.

    `request_erasure` is this adapter's own write path -- the eventual
    `DELETE /tenants/{tenant_id}/subjects/{subject_id}` admin endpoint
    (`docs/phase-7-routing/dpdp-retention-policy.md`'s target-state
    workflow) would call it once `subject_id` has been resolved to the
    `item_id`s it owns; that subject-to-item resolution is a separate,
    not-yet-built concern (the retention policy doc's own "Open decision"
    section) and is deliberately out of this remediation's scope -- this
    store's contract is intentionally `item_id`-keyed, matching
    `DpdpErasureCascadePort.fulfil_pending_erasure`'s own `item_id`-keyed
    signature exactly, so no scope is silently smuggled in here.
    """

    def __init__(self) -> None:
        self._pending: set[tuple[str, str]] = set()

    def request_erasure(self, tenant_id: str, item_id: str) -> None:
        """Record a new pending erasure obligation for `(tenant_id, item_id)`.

        Raises:
            ValueError: If `tenant_id` or `item_id` is blank.
        """
        if not tenant_id.strip():
            raise ValueError(
                "InMemoryErasureObligationStore.request_erasure requires a non-blank tenant_id"
            )
        if not item_id.strip():
            raise ValueError(
                "InMemoryErasureObligationStore.request_erasure requires a non-blank item_id"
            )
        self._pending.add((tenant_id, item_id))

    def has_pending_erasure(self, tenant_id: str, item_id: str) -> bool:
        """Return whether `(tenant_id, item_id)` currently has an unfulfilled obligation."""
        return (tenant_id, item_id) in self._pending

    def clear_pending_erasure(self, tenant_id: str, item_id: str) -> None:
        """Remove `(tenant_id, item_id)`'s obligation, if any. Idempotent no-op otherwise."""
        self._pending.discard((tenant_id, item_id))


class RetrievalIndexEviction:
    """Real `RetrievalIndexEvictionPort` adapter: composes `VectorIndexPort` + `LexicalIndexPort`.

    Shared by `Zone2CapacityBackstopSweep`'s own ordinary-eviction leg
    (`RetrievalIndexEvictionPort`, DSHN-58 remediation on that module) AND
    by `CrossZoneDpdpErasureCascade` below -- one real Zone-6 removal
    mechanism, composed in both places, rather than two implementations of
    "delete one item from Zone 6" drifting apart over time.

    Deliberately does NOT reuse `HybridRetrievalIndexRepository.delete_item`
    directly: that class is Zone 6's own `ZoneRepository` fetch/index
    adapter and does not translate its own failures into
    `Zone2BackstopPortError`, which both callers of this class require
    (`Zone2CapacityBackstopSweep._execute_one`'s per-item fail-safe
    isolation, and this module's cascade's own retry-safety). This class
    holds the exact same two ports `HybridRetrievalIndexRepository`
    composes and performs the identical two-call delete, with the
    exception-translation both callers here need layered on top.
    """

    def __init__(self, vector_index: VectorIndexPort, lexical_index: LexicalIndexPort) -> None:
        self._vector_index = vector_index
        self._lexical_index = lexical_index

    def delete_item(self, tenant_id: str, item_id: str) -> None:
        """Remove `item_id` from `tenant_id`'s Zone 6 vector + lexical surfaces.

        Raises:
            ValueError: If `tenant_id` is blank.
            Zone2BackstopPortError: If either underlying index delete
                fails; any exception from `vector_index.delete`/
                `lexical_index.delete` is translated into this type so
                callers can rely on a single exception contract.
        """
        if not tenant_id.strip():
            raise ValueError(
                "RetrievalIndexEviction.delete_item requires a non-blank tenant_id"
            )
        try:
            self._vector_index.delete(tenant_id, item_id)
            self._lexical_index.delete(tenant_id, item_id)
        except Zone2BackstopPortError:
            raise
        except Exception as exc:  # noqa: BLE001 -- converted to typed domain error below
            raise Zone2BackstopPortError(
                f"Zone 6 retrieval-index removal failed for tenant={tenant_id!r} "
                f"item={item_id!r}: {exc}"
            ) from exc


class CrossZoneDpdpErasureCascade:
    """Concrete `DpdpErasureCascadePort` adapter: obligation check + real cross-zone cascade.

    Composes an `ErasureObligationStore`, a `Zone2EvictionPort` (the same
    Protocol `Zone2CapacityBackstopSweep` already depends on for its
    ordinary path -- dependency inversion, not a new parallel mechanism),
    and a `RetrievalIndexEviction` (Zone 6). Every leg here is a real
    operation against whatever concrete adapters are injected -- this
    class performs no mocked or simulated work of its own.
    """

    def __init__(
        self,
        obligation_store: ErasureObligationStore,
        zone2_eviction: Zone2EvictionPort,
        retrieval_index_eviction: RetrievalIndexEviction,
    ) -> None:
        self._obligation_store = obligation_store
        self._zone2_eviction = zone2_eviction
        self._retrieval_index_eviction = retrieval_index_eviction

    def fulfil_pending_erasure(self, tenant_id: str, item_id: str) -> bool:
        """Atomically check-and-fulfil a pending erasure obligation for `item_id`.

        See `DpdpErasureCascadePort.fulfil_pending_erasure`'s own contract
        (`zone2_capacity_backstop_sweep.py`) for the full return/raise
        semantics this implementation honours exactly.

        Ordering (module docstring's PII-safety rationale): Zone 6 (both
        surfaces) is removed first, Zone 2 last. The pending obligation is
        cleared only once BOTH legs have succeeded -- a failure partway
        through leaves the obligation pending, so a retried sweep run
        re-attempts every leg (each leg is independently idempotent) with
        no window where the obligation is falsely reported fulfilled.

        Raises:
            ValueError: If `tenant_id` or `item_id` is blank.
            Zone2BackstopPortError: If the obligation check or the cascade
                cannot be completed. Never returns `False` on failure --
                only on a genuine "no obligation exists" outcome.
        """
        if not tenant_id.strip():
            raise ValueError(
                "CrossZoneDpdpErasureCascade.fulfil_pending_erasure requires a non-blank tenant_id"
            )
        if not item_id.strip():
            raise ValueError(
                "CrossZoneDpdpErasureCascade.fulfil_pending_erasure requires a non-blank item_id"
            )

        if not self._obligation_store.has_pending_erasure(tenant_id, item_id):
            return False

        try:
            self._retrieval_index_eviction.delete_item(tenant_id, item_id)
            self._zone2_eviction.evict(tenant_id, item_id)
        except Zone2BackstopPortError:
            raise
        except Exception as exc:  # noqa: BLE001 -- converted to typed domain error below
            raise Zone2BackstopPortError(
                f"DPDP erasure cascade failed for tenant={tenant_id!r} item={item_id!r}: {exc}"
            ) from exc

        self._obligation_store.clear_pending_erasure(tenant_id, item_id)
        return True
