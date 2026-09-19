"""Cross-write conflict-detection sweep: pure decision logic (HLD Threat T-1, FR-013).

DSHN-60 remediation (HIGH): the HLD names the conflict-detection sweep,
formally adopted as FR-013, as its "strongest control" for HLD Threat T-1 (a
write silently contradicts an existing fact for the same item). Before this
module, `ProvenanceRecord.conflict_status` was a passive `ConflictStatus`
enum field that nothing in `application/` or `domain/` ever computed or set
to `DISPUTED` -- FR-013 existed only as an unimplemented field.

Scope, stated honestly (mirrors this codebase's own convention of stating
residual limitations rather than overclaiming -- see `append_only_
privilege_guard.py`'s module docstring): a `ProvenanceRecord` carries
schema-level metadata only, never the underlying fact payload
(`provenance_record.py`'s own PII note). A semantic "does this fact's
CONTENT contradict that fact's content" comparison is therefore structurally
out of reach at this layer -- it would require the zone that actually holds
the payload (Zone 2/3/4/5, none of which this codebase has landed an
implementation for yet) to participate, which is a materially larger
feature this security remediation does not attempt to invent from nothing.

What this module DOES detect, purely from Zone 7's own already-durable
metadata, is the concrete, mechanically checkable contradiction FR-013's
own text also names: two INDEPENDENT write events for the SAME `item_id`
that do not chain as a correction of one another via `prev_provenance_id`
-- i.e. a second claim about `item_id` that never acknowledges the first.
Two unlinked claims about the same item are a real, structural contradiction
signal a provenance ledger can raise entirely from its own evidence, with
no need to inspect fact content.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from dashanan.domain.provenance_record import ProvenanceRecord


@dataclass(frozen=True, slots=True)
class ConflictDetectionResult:
    """One sweep's decision for one incoming write against one item_id's history.

    Attributes:
        conflicting: True when the incoming write does not chain from the
            current active (non-invalidated) record for this `item_id`.
        disputed_provenance_id: The `provenance_id` of the existing active
            record the incoming write contradicts, or `None` when
            `conflicting` is `False`.
    """

    conflicting: bool
    disputed_provenance_id: str | None


def latest_active_record(
    records_for_item: Sequence[ProvenanceRecord],
) -> ProvenanceRecord | None:
    """Return the most recent non-invalidated record in `records_for_item`, if any.

    Args:
        records_for_item: Every record for one `item_id`, in the
            chronological (oldest-first) order
            `SqlProvenanceRepository.find_by_item_id` returns them.

    Returns:
        The chronologically latest record whose `invalidation_flag` is
        `False`, or `None` if `records_for_item` is empty or every record
        in it has already been invalidated (there is then nothing active
        left for an incoming write to contradict).
    """
    for record in reversed(records_for_item):
        if not record.invalidation_flag:
            return record
    return None


def detect_conflict(
    records_for_item: Sequence[ProvenanceRecord],
    incoming_prev_provenance_id: str | None,
) -> ConflictDetectionResult:
    """FR-013's decision: does the incoming write acknowledge the current active record?

    Args:
        records_for_item: Every existing record for the incoming write's
            `item_id`, chronologically ordered (see `latest_active_record`).
        incoming_prev_provenance_id: The `prev_provenance_id` the incoming
            write declares (`ProvenanceRecord.update_history[0].
            prev_provenance_id`) -- `None` when it claims to be a
            first-ever write for this `item_id`, otherwise the
            `provenance_id` of whichever record it claims to
            correct/confirm.

    Returns:
        `ConflictDetectionResult(conflicting=False, ...)` when there is no
        active existing record (a genuine first write, or every prior
        record has already been invalidated), or when
        `incoming_prev_provenance_id` matches the active record's
        `provenance_id` (a proper, acknowledged correction/confirmation).
        `ConflictDetectionResult(conflicting=True, ...)` when an active
        record exists and the incoming write does not chain from it --
        two independent, unlinked claims about the same `item_id`.
    """
    active = latest_active_record(records_for_item)
    if active is None:
        return ConflictDetectionResult(conflicting=False, disputed_provenance_id=None)
    if incoming_prev_provenance_id == active.provenance_id:
        return ConflictDetectionResult(conflicting=False, disputed_provenance_id=None)
    return ConflictDetectionResult(
        conflicting=True, disputed_provenance_id=active.provenance_id
    )
