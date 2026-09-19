"""ConflictDetectingProvenanceRepository: wires the FR-013 sweep into every append (HLD Threat T-1).

DSHN-60 remediation (HIGH). Orchestration for `domain.conflict_detection`'s
pure decision logic, mirroring this codebase's established "pure domain
decision, application-layer orchestration" split (`domain.write_gate` +
`application.provenance_write_gate`; `domain.zone2_capacity_backstop` +
`application.zone2_capacity_backstop_sweep`).
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable
from uuid import uuid4

from dashanan.domain.conflict_detection import detect_conflict
from dashanan.domain.ports import Clock
from dashanan.domain.provenance_record import ConflictStatus, ProvenanceRecord

logger = logging.getLogger(__name__)

_CONFLICT_SWEEP_ACTOR = "conflict-detection-sweep"


@runtime_checkable
class ProvenanceRepositoryPort(Protocol):
    """The subset of `SqlProvenanceRepository`'s surface this sweep needs.

    Kept local to this module rather than added to the shared `dashanan.
    domain.ports`, mirroring `write_gate.ProvenanceJournalPort` and
    `dpdp_erasure_cascade.ErasureObligationStore`'s identical "kept local"
    choice. Structural typing (`SqlProvenanceRepository` satisfies this
    without either module importing the other -- Dependency Inversion,
    `clean-architecture` skill).
    """

    def find_by_item_id(self, tenant_id: str, item_id: str) -> list[ProvenanceRecord]:
        """Return every record for `item_id`, chronologically ordered oldest-first."""
        ...

    def find_latest_by_item_id(
        self, tenant_id: str, item_id: str
    ) -> ProvenanceRecord | None:
        """Return the current (most recently written) record for `item_id`."""
        ...

    def append(self, record: ProvenanceRecord) -> None:
        """Durably append `record`."""
        ...


class ConflictDetectingProvenanceRepository:
    """Decorator (python-design-patterns-core: Decorator) running FR-013's sweep on every append.

    Wraps any `ProvenanceRepositoryPort` -- in practice `SqlProvenanceRepository`
    -- and intercepts `append`:

      1. Reads `record.item_id`'s existing history from the wrapped
         repository.
      2. Runs `domain.conflict_detection.detect_conflict` against the
         incoming record's own declared `prev_provenance_id` (read off
         `record.update_history[0]`, the single entry every
         `ProvenanceRecord.create`'d record carries).
      3. If a conflict is detected -- the incoming write does not chain
         from the item's current active record -- SRS.md AC-015's own
         text ("a contradicting true fact triggers the conflict-detection
         sweep to downgrade BOTH records rather than silently
         overwriting the true fact") is implemented literally: a new
         `ConflictStatus.DISPUTED` CORRECTION record is appended for the
         previously active one (chained via `prev_provenance_id`/
         `prev_hash`, per the append-only hash-chain contract --
         `ProvenanceRecord` has no in-place mutation, matching
         `provenance_schema.sql`'s "No UPDATE grant exists on this table
         for any service role"), and the incoming record is itself
         re-derived with `conflict_status=DISPUTED` (via `ProvenanceRecord
         .create`, so its `confidence` correctly reflects HLD Section
         3.8's `-0.3` disputed modifier and its `record_hash` is
         recomputed to match -- must-not-deviate items 2 and 3 both
         still hold) before being appended, rather than the caller's
         original, un-downgraded object.
      4. If no conflict is detected, only the incoming `record` is
         appended unchanged -- identical to the wrapped repository's own
         behaviour.

    This runs FR-013's sweep synchronously on the write path itself
    (never a periodic best-effort background job that could let an
    unflagged contradiction be read in the interim) at the cost of one
    extra read per append -- an accepted trade-off given Zone 7 write
    volume is already bounded by `ProvenanceWriteGate`'s own per-tenant
    rate limiter (HLD Threat D-1, this same remediation pass).
    """

    def __init__(self, wrapped: ProvenanceRepositoryPort, clock: Clock) -> None:
        """Compose the sweep from the repository it wraps and an injectable clock.

        Args:
            wrapped: The real storage adapter every read/write ultimately
                reaches.
            clock: Injectable time source for the correction record's
                `write_timestamp` (testing-core: dependency injection
                over patching `datetime.now` directly, matching every
                other clock-consuming class in this codebase).
        """
        self._wrapped = wrapped
        self._clock = clock

    def append(self, record: ProvenanceRecord) -> None:
        """Run the FR-013 sweep for `record.item_id`, then append `record`.

        Raises:
            Whatever `wrapped.append`/`wrapped.find_by_item_id` raises --
            this method adds no new exception type; a wrapped-repository
            failure propagates unchanged (error-handling-patterns: no
            silent swallow).
        """
        incoming_prev_provenance_id = record.update_history[0].prev_provenance_id
        existing = self._wrapped.find_by_item_id(record.tenant_id, record.item_id)
        result = detect_conflict(existing, incoming_prev_provenance_id)

        if result.conflicting:
            disputed = next(
                r
                for r in existing
                if r.provenance_id == result.disputed_provenance_id
            )
            logger.warning(
                "FR-013 conflict-detection sweep flagged a contradiction",
                extra={
                    "tenant_id": record.tenant_id,
                    "item_id": record.item_id,
                    "disputed_provenance_id": disputed.provenance_id,
                    "incoming_provenance_id": record.provenance_id,
                },
            )
            correction = ProvenanceRecord.create(
                tenant_id=disputed.tenant_id,
                provenance_id=str(uuid4()),
                item_id=disputed.item_id,
                source_zone=disputed.source_zone,
                source_type=disputed.source_type,
                write_timestamp=self._clock.now(),
                actor=_CONFLICT_SWEEP_ACTOR,
                change=(
                    f"flagged disputed (FR-013): write {record.provenance_id!r} "
                    f"for the same item_id arrived without chaining from this "
                    "record via prev_provenance_id"
                ),
                retrieval_context_hash=disputed.retrieval_context_hash,
                source_refs=disputed.source_refs,
                prev_provenance_id=disputed.provenance_id,
                prev_hash=disputed.record_hash,
                conflict_status=ConflictStatus.DISPUTED,
                invalidation_flag=False,
            )
            self._wrapped.append(correction)

            incoming_entry = record.update_history[0]
            downgraded_incoming = ProvenanceRecord.create(
                tenant_id=record.tenant_id,
                provenance_id=record.provenance_id,
                item_id=record.item_id,
                source_zone=record.source_zone,
                source_type=record.source_type,
                write_timestamp=record.write_timestamp,
                actor=incoming_entry.actor,
                change=incoming_entry.change,
                retrieval_context_hash=record.retrieval_context_hash,
                source_refs=record.source_refs,
                prev_provenance_id=incoming_entry.prev_provenance_id,
                prev_hash=record.prev_hash,
                conflict_status=ConflictStatus.DISPUTED,
                invalidation_flag=record.invalidation_flag,
            )
            self._wrapped.append(downgraded_incoming)
        else:
            self._wrapped.append(record)

    def find_by_item_id(self, tenant_id: str, item_id: str) -> list[ProvenanceRecord]:
        """Delegate unchanged -- this decorator only intercepts writes."""
        return self._wrapped.find_by_item_id(tenant_id, item_id)

    def find_latest_by_item_id(
        self, tenant_id: str, item_id: str
    ) -> ProvenanceRecord | None:
        """Delegate unchanged -- this decorator only intercepts writes.

        DSHN-60 remediation, attempt 3: added so this decorator is a
        transparent stand-in for `SqlProvenanceRepository` wherever a
        caller reads Zone 7's current (not full-lineage) state for an
        `item_id`, not only the `find_by_item_id`/`append` subset
        `ProvenanceRepositoryPort` itself needs.
        """
        return self._wrapped.find_latest_by_item_id(tenant_id, item_id)
