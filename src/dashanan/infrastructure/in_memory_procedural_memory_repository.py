"""InMemoryProceduralMemoryRepository: Shape A Zone 4 storage adapter (HLD 3.5, FR-004).

Shape A only (in-process HashMap, per HLD Section 5's "Zone 4 Procedural"
DSA row: "HashMap keyed by task_signature_hash; ordered array for step
sequences ... Point lookup dominant"). Shape B (a networked adapter behind
the same `ProcedureRepositoryPort`/`ZoneRepository` contracts) is out of
scope for this module, mirroring `WorkingMemoryLRURepository`'s identical
Shape-A-only scope note (DASH-STORY-002).

Zone ownership (HLD Section 3.10, verbatim): "Zone 4 Procedural OWNS:
Procedure / PROJECTS INTO: Zone 6" -- no READS line for Zone 4, which is
must-not-deviate item 2's binding constraint: this module never adds a
dependency on any other zone's adapter (HLD Section 3.11: "Zone N depends
on nothing -- zones are leaves by design").
"""

from __future__ import annotations

import logging
import threading

from dashanan.domain.exceptions import ZoneRepositoryError
from dashanan.domain.memory_item import MemoryItem
from dashanan.domain.ports import Clock, EventBus, ZoneQuery, ZoneRepository
from dashanan.domain.procedural_memory_ports import ProcedureRepositoryPort
from dashanan.domain.procedure import Procedure, hash_task_signature
from dashanan.domain.zone import ZoneId

logger = logging.getLogger(__name__)

PROJECTION_EVENT_TYPE = "memory.projected"
"""HLD 3.10: Zone 4 "PROJECTS INTO: Zone 6" -- the event type this adapter
publishes on every `commit` (AC-004-4) so a Zone 6 index worker (HLD
Section 3.11: "Index worker | EventBus, Zone 6 | Async consume + sync
port" -- out of this story's own scope) can make the item discoverable
via Zone 6. Named to sit in the same dot-separated event-type family this
codebase already uses (`memory.evicted` in
`zone2_capacity_backstop_sweep.py`, `context.assembled` in
`memory_orchestrator.py`).
"""


class InMemoryProceduralMemoryRepository(ZoneRepository, ProcedureRepositoryPort):
    """Shape A adapter for Zone 4 (HLD 3.5): point lookup, Importance, projection.

    Implements both the frozen `ZoneRepository` Protocol (so this adapter
    registers with `MemoryOrchestrator.__init__`'s `zone_repositories`
    exactly like every other zone's Shape A adapter, AR1-G2) and this
    zone's own `ProcedureRepositoryPort` (AC-004-2's "adapter port" --
    point lookup and commit only, no similarity-search method).

    Storage shape (HLD Section 5, "Zone 4 Procedural" row): one `dict`
    keyed by `(tenant_id, task_signature_hash)`, giving O(1)
    `get_by_task_signature_hash`/`commit` -- the exact HashMap the DSA
    choice mandates. There is deliberately no secondary index, sorted
    structure, or embedding column on this class: "similarity fallback via
    Zone 6" (HLD Section 5) is a separate zone's job this adapter never
    reaches into (must-not-deviate item 1) and is never called back into
    from here (must-not-deviate item 2 -- see `fetch`'s docstring).

    Concurrency: one `threading.Lock` guards the whole `_procedures` dict
    for the read-modify-write span of `commit`. Zone 4's write volume
    (task-completion events, not per-token streaming) does not need
    `WorkingMemoryLRURepository`'s finer per-session locking scheme; a
    single coarse lock keeps this first Zone 4 adapter simple while still
    being safe under the concurrent-caller assumption ADR-018 established
    for every zone adapter, at coarser granularity since there is no
    per-session partition to lock on for this zone.
    """

    def __init__(self, event_bus: EventBus, clock: Clock) -> None:
        """Compose the adapter from its EventBus and Clock ports.

        Args:
            event_bus: Where `commit` publishes `PROJECTION_EVENT_TYPE`
                (AC-004-4). `NoOpEventBus` satisfies this for Shape A /
                offline deployments with no broker bound.
            clock: Injectable time source for the projection event's
                `committed_at` and for `record_execution`'s
                `last_used_at` (testing-core DI, matching every other
                adapter's identical `Clock` convention).
        """
        self._event_bus = event_bus
        self._clock = clock
        self._procedures: dict[tuple[str, str], Procedure] = {}
        self._guard = threading.Lock()

    def get_by_task_signature_hash(
        self, tenant_id: str, task_signature_hash: str
    ) -> Procedure | None:
        """Return the `Procedure` at this exact key, or `None` (AC-004-1).

        O(1): a single `dict.get` against the `(tenant_id,
        task_signature_hash)` key -- no scan, no similarity scoring.

        Args:
            tenant_id: Mandatory; enforced non-blank (HLD 3.0 invariant 2).
            task_signature_hash: The exact point-lookup key.

        Raises:
            ValueError: If `tenant_id` or `task_signature_hash` is blank.
        """
        self._require_non_blank("tenant_id", tenant_id)
        self._require_non_blank("task_signature_hash", task_signature_hash)
        with self._guard:
            return self._procedures.get((tenant_id, task_signature_hash))

    def commit(self, procedure: Procedure) -> None:
        """Upsert `procedure` and publish its Zone 6 projection event (AC-004-4).

        This is the "write commit" AC-004-4 names: the only write path
        this adapter exposes. The in-memory upsert lands before the event
        is published, so a caller reacting to the published event can
        immediately `get_by_task_signature_hash` the committed value.

        Args:
            procedure: The full `Procedure` to store, keyed by its own
                `(tenant_id, task_signature_hash)`. This method is a pure
                upsert, not an increment-in-place -- callers (or
                `record_execution` below) compute the next
                `success_count`/`failure_count` themselves via
                `dataclasses.replace`, matching this codebase's immutable
                Value-Object write convention (`WorkingItem`, `Episode`).
        """
        with self._guard:
            self._procedures[
                (procedure.tenant_id, procedure.task_signature_hash)
            ] = procedure

        self._event_bus.publish(
            PROJECTION_EVENT_TYPE,
            {
                "tenant_id": procedure.tenant_id,
                "item_id": procedure.task_signature_hash,
                "zone": ZoneId.PROCEDURAL.value,
                "target_zone": ZoneId.RETRIEVAL_INDEX.value,
                "committed_at": self._clock.now().isoformat(),
            },
        )
        logger.info(
            "zone4 procedural memory committed and projected",
            extra={
                "tenant_id": procedure.tenant_id,
                "item_id": procedure.task_signature_hash,
            },
        )

    def record_execution(
        self,
        tenant_id: str,
        task: str,
        steps: tuple[str, ...],
        *,
        succeeded: bool,
    ) -> Procedure:
        """Record one execution outcome for `task` and commit the result.

        Convenience wrapper around `get_by_task_signature_hash` +
        `commit` so a caller records an outcome (AC-004's own scenario:
        "a host records a successful tool-use sequence") without hand-
        rolling the read-increment-write sequence itself. Creates a new
        `Procedure` at `success_count=failure_count=0` on first use for a
        given task, then increments exactly one of the two counters.

        Args:
            tenant_id: Mandatory; enforced non-blank.
            task: Raw task description; hashed via `hash_task_signature`
                into the point-lookup key.
            steps: The action sequence to store (or overwrite) for this
                task.
            succeeded: Whether this execution succeeded (increments
                `success_count`) or failed (increments `failure_count`).

        Returns:
            The committed `Procedure`, with its updated counters and
            `importance()` reflecting the new outcome.

        Raises:
            ValueError: If `tenant_id` or `task` is blank, or `steps` is
                empty (propagated from `Procedure.__post_init__`).
        """
        self._require_non_blank("tenant_id", tenant_id)
        task_signature_hash = hash_task_signature(task)
        existing = self.get_by_task_signature_hash(tenant_id, task_signature_hash)

        success_count = existing.success_count if existing else 0
        failure_count = existing.failure_count if existing else 0
        if succeeded:
            success_count += 1
        else:
            failure_count += 1

        procedure = Procedure(
            tenant_id=tenant_id,
            task_signature_hash=task_signature_hash,
            steps=steps,
            success_count=success_count,
            failure_count=failure_count,
            last_used_at=self._clock.now(),
        )
        self.commit(procedure)
        return procedure

    def fetch(self, query: ZoneQuery) -> list[MemoryItem]:
        """Serve the `ZoneRepository` read contract for Zone 4 (HLD Section 7.1).

        Point lookup only (AC-004-2, must-not-deviate item 1): hashes
        `query.task` into its `task_signature_hash` via
        `hash_task_signature` and looks that single key up -- never a
        similarity scan across every stored `Procedure`. Returns a
        zero-or-one-item list, unlike a zone whose `fetch` performs a
        genuine top-k search.

        Args:
            query: The stripped-down per-zone query from the Orchestrator.
                `query.task` is required for a Zone 4 hit; `None` (no task
                text supplied) yields no candidates rather than raising,
                matching `WorkingMemoryLRURepository.fetch`'s convention
                of a query shape it cannot serve degrading to an empty
                result rather than an error.

        Returns:
            A list containing the single matching `MemoryItem`, or an
            empty list if `query.task` is absent or no `Procedure` is
            stored at that exact key for `query.tenant_id`.

        Raises:
            dashanan.domain.exceptions.ZoneRepositoryError: If
                `query.tenant_id` is blank.
        """
        if not query.tenant_id.strip():
            raise ZoneRepositoryError(
                zone=ZoneId.PROCEDURAL.value, reason="tenant_id must not be blank"
            )
        if query.task is None:
            return []

        task_signature_hash = hash_task_signature(query.task)
        procedure = self.get_by_task_signature_hash(
            query.tenant_id, task_signature_hash
        )
        if procedure is None:
            return []

        payload = " -> ".join(procedure.steps)
        return [
            MemoryItem(
                item_id=procedure.task_signature_hash,
                source_zone=ZoneId.PROCEDURAL,
                payload=payload,
                token_count=len(procedure.steps),
                score=procedure.importance(),
            )
        ]

    @staticmethod
    def _require_non_blank(name: str, value: str) -> None:
        """Enforce HLD 3.0 invariant 2 (mandatory tenant identifier, extended here).

        Raises:
            ValueError: If `value` is blank.
        """
        if not value.strip():
            raise ValueError(f"{name} must not be blank")
