"""ProvenanceRecord: Zone 7's owned entity (HLD Section 3.8, FR-007).

Every fact written to any of the other seven zones gets a corresponding
ProvenanceRecord here: source attribution, a computed confidence score, and
an append-only edit-history entry. This module is domain-only -- no SQL, no
I/O -- exactly the layering `clean-architecture` requires (see
`database-engineer/agent.md`: "database access is encapsulated in the
repository layer -- domain logic must never contain raw SQL").

PII NOTE: a ProvenanceRecord carries schema-level metadata ONLY -- source
type, confidence, hash chain, an `item_id` pointer -- never the underlying
fact content the provenance record points to (dev_prompt's PII constraint).
`retrieval_context` is therefore always stored pre-hashed by the caller; this
module never sees or stores raw retrieval context text.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from dashanan.domain.zone import ZoneId

_HEX_SHA256_LENGTH = 64


class SourceType(str, Enum):
    """The six fixed source-type values HLD Section 3.8 enumerates.

    A fixed, never-changing value set per database-engineer's own
    schema-design rule ("ENUM types for fixed value sets only when the set
    truly never changes") -- HLD Section 3.8 gives this ENUM literally, with
    no stated extension path.
    """

    USER_STATED = "user_stated"
    TOOL_OUTPUT = "tool_output"
    LLM_INFERRED = "llm_inferred"
    SUMMARIZED_FROM_N = "summarized_from_n"
    IMPORTED = "imported"
    SYSTEM_DERIVED = "system_derived"


class ConflictStatus(str, Enum):
    """The four fixed conflict-status values HLD Section 3.8 enumerates."""

    NONE = "none"
    DISPUTED = "disputed"
    RESOLVED_SUPERSEDED = "resolved_superseded"
    RESOLVED_CONFIRMED = "resolved_confirmed"


_BASE_CONFIDENCE_BY_SOURCE_TYPE: dict[SourceType, float] = {
    SourceType.USER_STATED: 1.0,
    SourceType.TOOL_OUTPUT: 0.9,
    SourceType.IMPORTED: 0.8,
    SourceType.SYSTEM_DERIVED: 0.7,
    SourceType.LLM_INFERRED: 0.6,
    # SUMMARIZED_FROM_N's base is `0.6 x mean(source confidences)` -- computed
    # in `compute_confidence`, not a fixed constant like the other five.
}
"""Base confidence per `source_type`, cited verbatim from HLD Section 3.8:
`user_stated=1.0, tool_output=0.9, imported=0.8, llm_inferred=0.6,
summarized_from_n=0.6 x mean(source confidences), system_derived=0.7`."""

_DISPUTED_MODIFIER: float = -0.3
"""HLD Section 3.8: "-0.3 while conflict_status = disputed"."""

_FAILED_FAITHFULNESS_MODIFIER: float = -0.2
"""HLD Section 3.8: "-0.2 on a failed faithfulness check"."""

_COMPRESSION_GENERATION_FACTOR: float = 0.9
"""HLD Section 3.8: "x 0.9 per compression generation"."""


def _clamp01(value: float) -> float:
    """Clamp to `[0, 1]` (HLD Section 12G's later-added explicit clamp)."""
    return max(0.0, min(1.0, value))


def compute_confidence(
    source_type: SourceType,
    *,
    conflict_status: ConflictStatus = ConflictStatus.NONE,
    failed_faithfulness_check: bool = False,
    compression_generation: int = 0,
    source_confidences: Sequence[float] | None = None,
) -> float:
    """Compute `ProvenanceConfidence` per HLD Section 3.8's formula.

    This is the sole implementation of must-not-deviate item 3
    ("Confidence is COMPUTED from source_type and modifiers, never assigned
    once and left static") -- `ProvenanceRecord.create` is the only path
    that produces a `confidence` value, and it always calls this function.

    Args:
        source_type: Selects the base value (or, for
            `SUMMARIZED_FROM_N`, the base *formula*).
        conflict_status: Applies the `-0.3` disputed modifier when
            `ConflictStatus.DISPUTED`.
        failed_faithfulness_check: Applies the `-0.2` modifier when true.
        compression_generation: Applies `x 0.9` once per generation. Must
            be non-negative; `0` (the default) applies no discount.
        source_confidences: Required, non-empty for
            `SourceType.SUMMARIZED_FROM_N` -- the confidences of the items
            this summary was derived from. Ignored for every other
            `source_type`.

    Returns:
        A value clamped to `[0, 1]` (HLD Section 12G).

    Raises:
        ValueError: If `source_type` is `SUMMARIZED_FROM_N` and
            `source_confidences` is `None` or empty, or if
            `compression_generation` is negative.
    """
    if compression_generation < 0:
        raise ValueError(
            "compute_confidence requires compression_generation >= 0, "
            f"got {compression_generation}"
        )

    if source_type is SourceType.SUMMARIZED_FROM_N:
        if not source_confidences:
            raise ValueError(
                "compute_confidence requires a non-empty source_confidences "
                "sequence for SourceType.SUMMARIZED_FROM_N"
            )
        base = 0.6 * (sum(source_confidences) / len(source_confidences))
    else:
        base = _BASE_CONFIDENCE_BY_SOURCE_TYPE[source_type]

    value = base
    if conflict_status is ConflictStatus.DISPUTED:
        value += _DISPUTED_MODIFIER
    if failed_faithfulness_check:
        value += _FAILED_FAITHFULNESS_MODIFIER
    value *= _COMPRESSION_GENERATION_FACTOR**compression_generation

    return _clamp01(value)


@dataclass(frozen=True, slots=True)
class ProvenanceUpdateEntry:
    """One entry of a `ProvenanceRecord.update_history` array (HLD Section 3.8).

    Attributes:
        ts: When this write event happened.
        actor: Who or what performed it (a caller identity, a service
            name -- never PII; the write-path enforcement story,
            DASH-STORY-006, is expected to bind this to caller identity).
        change: A short, human-readable description of what changed
            (e.g. `"initial write"`, `"correction: conflict resolved"`).
        prev_provenance_id: The `provenance_id` of the record this one
            corrects/supersedes, or `None` for the first record of a
            given `item_id` (HLD Section 3.8: "Corrections are new
            records chained by prev_provenance_id").
    """

    ts: datetime
    actor: str
    change: str
    prev_provenance_id: str | None = None

    def __post_init__(self) -> None:
        if not self.actor.strip():
            raise ValueError("ProvenanceUpdateEntry.actor must not be blank")
        if not self.change.strip():
            raise ValueError("ProvenanceUpdateEntry.change must not be blank")


def compute_record_hash(
    *,
    tenant_id: str,
    provenance_id: str,
    item_id: str,
    source_zone: str,
    source_type: str,
    source_refs: Sequence[str],
    write_timestamp: datetime,
    retrieval_context_hash: str,
    conflict_status: str,
    invalidation_flag: bool,
    confidence: float,
    prev_hash: str | None,
) -> str:
    """Compute this record's SHA-256 `record_hash` (HLD Section 5, tamper evidence).

    Deterministic over every field that identifies THIS record plus
    `prev_hash` -- the same construction `verify_chain` re-derives to detect
    tampering. `update_history` is deliberately excluded from the hash
    input: it is derived from (`prev_provenance_id`, `write_timestamp`,
    `actor`, `change`), all of which are already covered by the record's own
    identity and `prev_hash` linkage, so hashing it again would be
    redundant rather than additionally tamper-evident.

    Returns:
        A lowercase 64-character hex SHA-256 digest.
    """
    canonical = "|".join(
        (
            tenant_id,
            provenance_id,
            item_id,
            source_zone,
            source_type,
            ",".join(source_refs),
            write_timestamp.isoformat(),
            retrieval_context_hash,
            conflict_status,
            "1" if invalidation_flag else "0",
            f"{confidence:.10f}",
            prev_hash or "",
        )
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ProvenanceRecord:
    """Zone 7's owned entity: one immutable, hash-chained provenance record.

    Attributes mirror HLD Section 3.8's owned-entity shape:
    `ProvenanceRecord(tenant_id, provenance_id, item_id, source_zone,
    source_type, source_refs[], write_timestamp, update_history[],
    retrieval_context, conflict_status, invalidation_flag, confidence,
    prev_hash, record_hash)`.

    Immutability is structural here, not just a database grant: this
    dataclass exposes no method that mutates a field, and
    `SqlProvenanceRepository` (infrastructure) exposes no `update`/`delete`
    method -- corrections are always a brand-new `ProvenanceRecord` chained
    via `update_history[0].prev_provenance_id` and `prev_hash` (HLD Section
    3.8: "No UPDATE grant exists on this table for any service role").

    Construct via `ProvenanceRecord.create` (never this dataclass's raw
    `__init__`) so `confidence` is always the computed value from
    `compute_confidence` and `record_hash` is always correctly derived from
    `prev_hash` -- see `create`'s docstring and the DASH-STORY-005 dev
    report's judgment-call list for why the raw constructor cannot itself
    forbid direct, unhashed construction in a plain `dataclass`.

    Attributes:
        tenant_id: Owning tenant. Required on every query (mirrors the
            `ZoneRepository` port's own tenant_id invariant, HLD 3.0
            invariant 2, even though Zone 7 does not implement that
            Protocol -- see this story's judgment-call list).
        provenance_id: This record's own identifier. Unique per tenant.
        item_id: The fact (in any zone) this record is provenance for.
            AC-007's query key.
        source_zone: Which zone owns the fact this record attests to.
        source_type: Selects the confidence base value/formula.
        source_refs: item_ids this was derived from. Empty for
            `user_stated` (HLD Section 3.8).
        write_timestamp: When this record was durably written.
        update_history: Append-only; in practice exactly one entry per
            record, since corrections are new rows rather than in-place
            array growth (see the class docstring and judgment-call list).
        retrieval_context_hash: SHA-256 hex of the query/task under which
            this was written. Pre-hashed by the caller -- this module never
            receives or stores raw retrieval context text (PII note).
        conflict_status: Current dispute state.
        invalidation_flag: Set when this record has been superseded/
            invalidated by a later correction.
        confidence: The computed `ProvenanceConfidence` score term,
            always produced by `compute_confidence` -- never a caller-
            supplied literal (must-not-deviate item 3).
        prev_hash: The previous record's `record_hash` in this item_id's
            chain, or `None` for the first record of that item_id.
        record_hash: This record's own SHA-256 hash, always produced by
            `compute_record_hash` (must-not-deviate item 2).
    """

    tenant_id: str
    provenance_id: str
    item_id: str
    source_zone: ZoneId
    source_type: SourceType
    source_refs: tuple[str, ...]
    write_timestamp: datetime
    update_history: tuple[ProvenanceUpdateEntry, ...]
    retrieval_context_hash: str
    conflict_status: ConflictStatus
    invalidation_flag: bool
    confidence: float
    prev_hash: str | None
    record_hash: str

    def __post_init__(self) -> None:
        """Enforce the invariants the hash chain and PK lookup depend on.

        Raises:
            ValueError: If any identifier is blank, `confidence` is
                outside `[0, 1]`, `record_hash`/`prev_hash`/
                `retrieval_context_hash` is not a 64-character lowercase
                hex string, or `update_history` is empty (every record
                represents exactly one write event).
        """
        if not self.tenant_id.strip():
            raise ValueError("ProvenanceRecord.tenant_id must not be blank")
        if not self.provenance_id.strip():
            raise ValueError("ProvenanceRecord.provenance_id must not be blank")
        if not self.item_id.strip():
            raise ValueError("ProvenanceRecord.item_id must not be blank")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(
                f"ProvenanceRecord.confidence must be in [0, 1], got {self.confidence}"
            )
        if not self.update_history:
            raise ValueError(
                "ProvenanceRecord.update_history must contain at least one entry"
            )
        _require_hex_sha256(self.record_hash, "record_hash")
        if self.prev_hash is not None:
            _require_hex_sha256(self.prev_hash, "prev_hash")
        _require_hex_sha256(self.retrieval_context_hash, "retrieval_context_hash")

    @classmethod
    def create(
        cls,
        *,
        tenant_id: str,
        provenance_id: str,
        item_id: str,
        source_zone: ZoneId,
        source_type: SourceType,
        write_timestamp: datetime,
        actor: str,
        change: str,
        retrieval_context_hash: str,
        source_refs: Sequence[str] = (),
        prev_provenance_id: str | None = None,
        prev_hash: str | None = None,
        conflict_status: ConflictStatus = ConflictStatus.NONE,
        invalidation_flag: bool = False,
        failed_faithfulness_check: bool = False,
        compression_generation: int = 0,
        source_confidences: Sequence[float] | None = None,
    ) -> ProvenanceRecord:
        """Construct a correctly-hashed record with a freshly computed confidence.

        This is the only sanctioned constructor. It: (1) computes
        `confidence` via `compute_confidence` (must-not-deviate item 3);
        (2) builds the single `update_history` entry for this write event;
        (3) computes `record_hash` via `compute_record_hash`, chaining from
        `prev_hash` (must-not-deviate item 2). A caller appending a
        correction passes the previous record's `record_hash` as
        `prev_hash` and its `provenance_id` as `prev_provenance_id`.

        Raises:
            ValueError: Propagated from `compute_confidence` (invalid
                `compression_generation`, or `SUMMARIZED_FROM_N` without
                `source_confidences`) or from `__post_init__` (blank
                identifiers, malformed hash fields).
        """
        confidence = compute_confidence(
            source_type,
            conflict_status=conflict_status,
            failed_faithfulness_check=failed_faithfulness_check,
            compression_generation=compression_generation,
            source_confidences=source_confidences,
        )
        update_entry = ProvenanceUpdateEntry(
            ts=write_timestamp,
            actor=actor,
            change=change,
            prev_provenance_id=prev_provenance_id,
        )
        refs = tuple(source_refs)
        record_hash = compute_record_hash(
            tenant_id=tenant_id,
            provenance_id=provenance_id,
            item_id=item_id,
            source_zone=source_zone.value,
            source_type=source_type.value,
            source_refs=refs,
            write_timestamp=write_timestamp,
            retrieval_context_hash=retrieval_context_hash,
            conflict_status=conflict_status.value,
            invalidation_flag=invalidation_flag,
            confidence=confidence,
            prev_hash=prev_hash,
        )
        return cls(
            tenant_id=tenant_id,
            provenance_id=provenance_id,
            item_id=item_id,
            source_zone=source_zone,
            source_type=source_type,
            source_refs=refs,
            write_timestamp=write_timestamp,
            update_history=(update_entry,),
            retrieval_context_hash=retrieval_context_hash,
            conflict_status=conflict_status,
            invalidation_flag=invalidation_flag,
            confidence=confidence,
            prev_hash=prev_hash,
            record_hash=record_hash,
        )


def _require_hex_sha256(value: str, field_name: str) -> None:
    """Validate `value` is a 64-character lowercase hex string."""
    if len(value) != _HEX_SHA256_LENGTH or any(
        c not in "0123456789abcdef" for c in value
    ):
        raise ValueError(
            f"ProvenanceRecord.{field_name} must be a {_HEX_SHA256_LENGTH}-character "
            f"lowercase hex SHA-256 digest, got {value!r}"
        )


def verify_chain(records: Sequence[ProvenanceRecord]) -> bool:
    """Verify a chronologically-ordered lineage for one `item_id` is untampered.

    Implements HLD Section 10 threat R-1/T-2's mitigation ("the hash chain
    is verifiable"): re-derives each record's `record_hash` from its own
    fields and the previous record's `record_hash`, and confirms the
    `prev_hash` linkage matches. `records` must already be the full,
    chronologically-ordered lineage for a single `item_id` (as
    `SqlProvenanceRepository.find_by_item_id` returns it) -- this function
    does not itself group or sort records.

    Args:
        records: Chronologically-ordered (oldest first) records for one
            `item_id`. An empty sequence is vacuously valid.

    Returns:
        `True` if every record's `record_hash` matches its recomputed
        hash and every `prev_hash` correctly links to the prior record's
        `record_hash` (with the first record's `prev_hash` matching
        whatever it declares, typically `None`); `False` at the first
        mismatch, which pinpoints tampering to that record.
    """
    expected_prev: str | None = records[0].prev_hash if records else None
    for record in records:
        if record.prev_hash != expected_prev:
            return False
        recomputed = compute_record_hash(
            tenant_id=record.tenant_id,
            provenance_id=record.provenance_id,
            item_id=record.item_id,
            source_zone=record.source_zone.value,
            source_type=record.source_type.value,
            source_refs=record.source_refs,
            write_timestamp=record.write_timestamp,
            retrieval_context_hash=record.retrieval_context_hash,
            conflict_status=record.conflict_status.value,
            invalidation_flag=record.invalidation_flag,
            confidence=record.confidence,
            prev_hash=record.prev_hash,
        )
        if recomputed != record.record_hash:
            return False
        expected_prev = record.record_hash
    return True
