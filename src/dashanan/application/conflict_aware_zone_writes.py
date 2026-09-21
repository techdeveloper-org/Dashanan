"""Wires the FR-013 conflict-detection sweep into the real Zone 3/Zone 5 write paths.

DASH-STORY-022, traced to FR-013 (SRS.md, verbatim): "On every write
targeting Zone 3 (Semantic) or Zone 5 (Entity), the system SHALL run a
conflict-detection sweep matching on (entity, predicate) or (subject,
predicate, object): a contradicting write SHALL cause both the disputed
prior record and the incoming record to be marked conflict_status=disputed
with reduced confidence, rather than silently overwriting the prior fact."

The FR-013 mechanism itself -- `domain.conflict_detection.detect_conflict`
and `ConflictDetectingProvenanceRepository.append`'s orchestration of it --
already exists, is already tested, and is explicitly locked (must-not-
deviate item 1, sprint3_ar1_assignments.json AR1-S3-022): this module adds
no new conflict-detection algorithm and does not touch either of those
two collaborators' own logic. What was missing, confirmed by this story's
own grep-based gap analysis, is a CALL SITE: `SqlSemanticRepository`
(Zone 3) and `EntityMemoryRepository.write_attribute` (Zone 5) never
imported or invoked `ConflictDetectingProvenanceRepository`/
`composition_root.build_provenance_repository` at all, so every Zone 3/5
write silently bypassed the sweep despite it being real and reachable.

This module closes that gap the same way `application.
zone2_capacity_backstop_sweep` closes its own gap over `domain.
zone2_capacity_backstop`: a thin application-layer orchestration class per
zone, composed from the zone's own real repository plus the
already-wrapped `ProvenanceRepositoryPort` (per must-not-deviate item 3,
this MUST be the exact `ConflictDetectingProvenanceRepository` instance
`infrastructure.composition_root.build_provenance_repository` builds --
never a second, parallel `ConflictDetectingProvenanceRepository`
constructed here, which would reproduce the "unwired primitive" failure
mode `composition_root.py`'s own module docstring documents for a prior
DSHN-60 remediation attempt).

Item_id keying (the "(entity, predicate) or (subject, predicate, object)"
match FR-013's text and AC-022-1 both name): `ConflictDetectingProvenanceRepository.
append` detects a conflict purely from Zone 7's own `item_id` +
`prev_provenance_id` chain (see `conflict_detection_sweep.py`'s own module
docstring: "a second claim about item_id that never acknowledges the
first"). Making that mechanism key on "the same (entity, predicate)" or
"the same (subject, predicate, object)" is therefore a matter of which
`item_id` string this wiring constructs for each write, not a change to
the sweep itself:

  - Zone 5 (`write_attribute`): `item_id = "{entity_id}:{attribute_name}"`
    -- the (entity, predicate) pair, reusing VERBATIM the same
    colon-joined convention `EntityMemoryRepository.write_attribute`
    already builds for its own AC-005-6 projection event's `item_id`
    field (`entity_memory_repository.py`, `PROJECTION_EVENT_TYPE` payload:
    `"item_id": f"{entity_id}:{attribute_name}"`), not a new scheme
    invented here.
  - Zone 3 `SemanticEdge` (`insert_edge`): `item_id =
    "{subject_ref}:{predicate}:{object_ref}"` -- the full (subject,
    predicate, object) triple, mirroring Zone 5's own convention above
    for internal consistency across both zones this story wires.
  - Zone 3 `GeneralFact` (`insert_general_fact`): `GeneralFact` has no
    subject/predicate/object shape at all -- `domain.semantic_memory`'s
    own module docstring is explicit that a single-subject/no-object fact
    is Entity-owned, never a `SemanticEdge`, and `GeneralFact.
    __post_init__` enforces no subject/object fields exist to key on.
    Stated honestly as a judgment call (mirrors `domain.
    conflict_detection.py`'s own "scope, stated honestly" convention):
    this wiring keys a `GeneralFact` write on its own `fact_id` --
    `item_id = fact_id` -- so it still participates in the same sweep
    mechanism and satisfies AC-022-1's "SemanticEdge or GeneralFact"
    text structurally, while acknowledging that two `GeneralFact` writes
    asserting the same real-world claim under two different `fact_id`s
    are not detected as contradicting one another (no shape exists for
    this module to key on that a caller has not already collapsed into
    one `fact_id`). This does not reopen `domain.conflict_detection.py`'s
    algorithm (must-not-deviate item 1) -- it only chooses this wiring's
    own `item_id` input to that unchanged algorithm.

Caller-supplied provenance metadata: `ProvenanceRecord.create` requires a
`source_type`, `actor`, `change`, and `retrieval_context_hash` this module
has no authority to invent on a caller's behalf (HLD Section 3.8; HLD
Threat S-2's caller-binding requirement, `write_gate.py`). Every method
below therefore accepts these as explicit, caller-supplied keyword
arguments -- exactly as `ProvenanceRecord.create` itself already requires
them -- rather than defaulting them to a fixed literal, which would
misattribute every write's source.

Ordering (durability-first, mirroring `ProvenanceWriteGate.submit_write`'s
own "journal, then persist_fact" ordering, ADR-010): each method below
runs the FR-013 sweep (the Zone 7 provenance append) BEFORE the
corresponding Zone 3/5 content write. A sweep failure therefore leaves the
Zone 3/5 content entirely unwritten -- error-handling-patterns section 2
(fail-fast): this module raises whatever the provenance append raises,
unchanged, rather than swallowing it and risking an un-provenanced Zone
3/5 write (which is exactly the FR-010/AC-010 violation
`provenance_write_gate.py` exists to prevent for every OTHER zone).

PII NOTE: mirrors `conflict_detection_sweep.py`'s and `provenance_record.
py`'s identical posture -- this module handles only schema-level
provenance metadata (`item_id`, `provenance_id`, `source_type`,
`retrieval_context_hash`) and forwards each zone's own opaque content
object (`SemanticEdge`, `GeneralFact`, an attribute `value`) to its real
repository unread; nothing here inspects, logs, or interprets zone
content, per the dev_prompt's PII constraint.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from dashanan.domain.entity_record import EntityRecord
from dashanan.domain.ports import Clock
from dashanan.domain.provenance_record import ProvenanceRecord, SourceType
from dashanan.domain.semantic_memory import GeneralFact, SemanticEdge
from dashanan.domain.write_gate import WriteGateResult
from dashanan.domain.zone import ZoneId
from dashanan.infrastructure.entity_memory_repository import EntityMemoryRepository
from dashanan.infrastructure.sql_semantic_repository import SqlSemanticRepository

logger = logging.getLogger(__name__)

_ZONE3_ACTOR_LABEL = "zone3-conflict-aware-write"
_ZONE5_ACTOR_LABEL = "zone5-conflict-aware-write"


@runtime_checkable
class ProvenanceRepositoryPort(Protocol):
    """The subset of `ConflictDetectingProvenanceRepository`'s surface this wiring needs.

    Kept local to this module rather than added to the shared
    `dashanan.domain.ports` (AR1-G2, ZoneRepository frozen) -- mirrors
    `conflict_detection_sweep.ProvenanceRepositoryPort`'s and
    `write_gate.ProvenanceJournalPort`'s identical "kept local" choice.
    Structural typing: the real `ConflictDetectingProvenanceRepository`
    (or, with `detect_conflicts=False`, the bare `SqlProvenanceRepository`)
    satisfies this without either module importing the other.
    """

    def append(self, record: ProvenanceRecord) -> None:
        """Durably append `record`, running the FR-013 sweep when wrapped."""
        ...


def _zone5_attribute_item_id(entity_id: str, attribute_name: str) -> str:
    """The (entity, predicate) key `write_attribute` already uses for its own projection event.

    Length-prefixes each component (`f"{len(component)}:{component}"`,
    concatenated) so no component's own content -- including a literal
    colon -- can ever be misread as a delimiter between components. Two
    distinct (entity_id, attribute_name) pairs therefore always produce
    distinct item_ids, unlike a bare colon join where e.g.
    entity_id="A:B", attribute_name="C" would collide with
    entity_id="A", attribute_name="B:C".
    """
    return f"{len(entity_id)}:{entity_id}:{len(attribute_name)}:{attribute_name}"


def _zone3_edge_item_id(subject_ref: str, predicate: str, object_ref: str) -> str:
    """The (subject, predicate, object) key a `SemanticEdge` write is detected against.

    Length-prefixes each component the same way `_zone5_attribute_item_id`
    does, so no component's own content can be misread as the delimiter
    between components -- see that function's docstring for the collision
    a bare colon join would otherwise allow.
    """
    return (
        f"{len(subject_ref)}:{subject_ref}:"
        f"{len(predicate)}:{predicate}:"
        f"{len(object_ref)}:{object_ref}"
    )


def _zone3_fact_item_id(fact_id: str) -> str:
    """A `GeneralFact`'s own `fact_id`, its only available conflict-detection key.

    See this module's docstring, "Zone 3 GeneralFact" paragraph, for why
    no (entity, predicate)/(subject, predicate, object) shape exists for
    a `GeneralFact` to key on instead.
    """
    return fact_id


class ConflictAwareSemanticRepository:
    """Zone 3's FR-013-swept write path: wraps `SqlSemanticRepository` + the FR-013 sweep.

    Composed from the real `SqlSemanticRepository` (Zone 3's own storage
    adapter, unmodified -- must-not-deviate item 1) and a
    `ProvenanceRepositoryPort` that MUST already be the wrapped
    `ConflictDetectingProvenanceRepository` `infrastructure.
    composition_root.build_provenance_repository` builds (must-not-deviate
    item 3) -- this class never constructs its own conflict-detecting
    wrapper.

    `find_edges_by_subject`/`find_edges_by_object`/`find_fact_by_id`
    delegate unchanged to the wrapped `SqlSemanticRepository`: this
    class only intercepts the two WRITE methods FR-013 applies to,
    mirroring `ConflictDetectingProvenanceRepository`'s own
    "this decorator only intercepts writes" read-delegation pattern.
    """

    def __init__(
        self,
        semantic_repository: SqlSemanticRepository,
        provenance_repository: ProvenanceRepositoryPort,
        clock: Clock,
    ) -> None:
        """Compose this writer from Zone 3's real adapter, the swept Zone 7 port, and a clock.

        Args:
            semantic_repository: The real Zone 3 storage adapter every
                write ultimately reaches.
            provenance_repository: The already FR-013-wrapped Zone 7 port
                (must-not-deviate item 3) -- in production, whatever
                `composition_root.build_provenance_repository` returned.
            clock: Injectable time source for each write's provenance
                `write_timestamp` (testing-core: dependency injection over
                `datetime.now()`, matching every other clock-consuming
                class in this codebase).
        """
        self._semantic_repository = semantic_repository
        self._provenance_repository = provenance_repository
        self._clock = clock

    def find_edges_by_subject(self, tenant_id: str, subject_ref: str) -> list[SemanticEdge]:
        """Delegate unchanged -- this wiring only intercepts writes."""
        return self._semantic_repository.find_edges_by_subject(tenant_id, subject_ref)

    def find_edges_by_object(self, tenant_id: str, object_ref: str) -> list[SemanticEdge]:
        """Delegate unchanged -- this wiring only intercepts writes."""
        return self._semantic_repository.find_edges_by_object(tenant_id, object_ref)

    def find_fact_by_id(self, tenant_id: str, fact_id: str) -> GeneralFact | None:
        """Delegate unchanged -- this wiring only intercepts writes."""
        return self._semantic_repository.find_fact_by_id(tenant_id, fact_id)

    def insert_edge(
        self,
        edge: SemanticEdge,
        *,
        provenance_id: str,
        source_type: SourceType,
        retrieval_context_hash: str,
        actor: str = _ZONE3_ACTOR_LABEL,
        change: str = "zone3 semantic edge write",
        source_refs: Sequence[str] = (),
        prev_provenance_id: str | None = None,
        prev_hash: str | None = None,
    ) -> None:
        """Run the FR-013 sweep for `edge`'s (subject, predicate, object), then insert it.

        AC-022-1: the sweep runs against `item_id = "{subject_ref}:
        {predicate}:{object_ref}"` BEFORE `edge` is persisted (this
        module's docstring, "Ordering"). AC-022-2: when no conflict is
        detected, this adds exactly the sweep's own documented one extra
        read, then inserts `edge` unchanged.

        Args:
            edge: The candidate `SemanticEdge` to write. Untouched by
                this method -- Zone 3 content itself carries no
                conflict_status field; only its Zone 7 provenance record
                does (HLD Section 3.8).
            provenance_id: This write's own Zone 7 provenance_id. Unique
                per tenant.
            source_type: HLD Section 3.8's confidence-base selector.
            retrieval_context_hash: SHA-256 hex of the retrieval context
                this write was issued under (HLD Threat S-2 caller
                binding) -- pre-hashed by the caller (PII note).
            actor: Who/what performed this write.
            change: A short, human-readable description of this write
                event.
            source_refs: item_ids this write was derived from.
            prev_provenance_id: The provenance_id this write claims to
                correct/confirm, or `None` for a fresh, unlinked claim
                about this (subject, predicate, object) -- `None` against
                an already-active record for the same key is exactly the
                AC-022-1 contradiction case (`domain.conflict_detection.
                detect_conflict`'s own contract, unchanged here).
            prev_hash: The previous record's `record_hash` in this
                item_id's chain, required when `prev_provenance_id` is
                supplied (mirrors `ProvenanceRecord.create`'s own
                contract).

        Raises:
            Whatever `provenance_repository.append`/`semantic_repository.
            insert_edge` raises -- this method adds no new exception type
            and does not swallow a sweep failure (this module's
            docstring, "Ordering").
        """
        item_id = _zone3_edge_item_id(edge.subject_ref, edge.predicate, edge.object_ref)
        record = ProvenanceRecord.create(
            tenant_id=edge.tenant_id,
            provenance_id=provenance_id,
            item_id=item_id,
            source_zone=ZoneId.SEMANTIC,
            source_type=source_type,
            write_timestamp=self._clock.now(),
            actor=actor,
            change=change,
            retrieval_context_hash=retrieval_context_hash,
            source_refs=source_refs,
            prev_provenance_id=prev_provenance_id,
            prev_hash=prev_hash,
        )
        self._provenance_repository.append(record)
        self._semantic_repository.insert_edge(edge)

    def insert_general_fact(
        self,
        fact: GeneralFact,
        *,
        provenance_id: str,
        source_type: SourceType,
        retrieval_context_hash: str,
        actor: str = _ZONE3_ACTOR_LABEL,
        change: str = "zone3 general fact write",
        source_refs: Sequence[str] = (),
        prev_provenance_id: str | None = None,
        prev_hash: str | None = None,
    ) -> None:
        """Run the FR-013 sweep for `fact`'s own `fact_id`, then insert it.

        See this module's docstring, "Zone 3 GeneralFact" paragraph, for
        why `fact_id` (rather than a subject/predicate/object shape
        `GeneralFact` does not have) is this write's conflict-detection
        key. Args/Raises mirror `insert_edge` exactly, substituting
        `fact` for `edge`.
        """
        item_id = _zone3_fact_item_id(fact.fact_id)
        record = ProvenanceRecord.create(
            tenant_id=fact.tenant_id,
            provenance_id=provenance_id,
            item_id=item_id,
            source_zone=ZoneId.SEMANTIC,
            source_type=source_type,
            write_timestamp=self._clock.now(),
            actor=actor,
            change=change,
            retrieval_context_hash=retrieval_context_hash,
            source_refs=source_refs,
            prev_provenance_id=prev_provenance_id,
            prev_hash=prev_hash,
        )
        self._provenance_repository.append(record)
        self._semantic_repository.insert_general_fact(fact)


class ConflictAwareEntityMemoryRepository:
    """Zone 5's FR-013-swept write path: wraps `EntityMemoryRepository` + the FR-013 sweep.

    Composed from the real `EntityMemoryRepository` (Zone 5's own storage
    adapter, unmodified -- must-not-deviate item 1) and a
    `ProvenanceRepositoryPort` that MUST already be the wrapped
    `ConflictDetectingProvenanceRepository` (must-not-deviate item 3,
    mirrors `ConflictAwareSemanticRepository`'s identical composition
    contract).

    Every OTHER `EntityMemoryRepository` method (`register_alias`,
    `get_entity`, `resolve_alias_prefix`, `resolve_exact_term`, `fetch`)
    delegates unchanged -- FR-013 applies only to `write_attribute`, the
    sole method that writes a new attribute sub-record.
    """

    def __init__(
        self,
        entity_memory_repository: EntityMemoryRepository,
        provenance_repository: ProvenanceRepositoryPort,
        clock: Clock,
    ) -> None:
        """Compose this writer from Zone 5's real adapter, the swept Zone 7 port, and a clock.

        Args mirror `ConflictAwareSemanticRepository.__init__` exactly,
        substituting Zone 5's `EntityMemoryRepository` for Zone 3's
        `SqlSemanticRepository`.
        """
        self._entity_memory_repository = entity_memory_repository
        self._provenance_repository = provenance_repository
        self._clock = clock

    def register_alias(self, tenant_id: str, entity_id: str, alias: str) -> None:
        """Delegate unchanged -- FR-013 does not apply to alias indexing."""
        self._entity_memory_repository.register_alias(tenant_id, entity_id, alias)

    def get_entity(self, tenant_id: str, entity_id: str) -> EntityRecord | None:
        """Delegate unchanged -- this wiring only intercepts writes."""
        return self._entity_memory_repository.get_entity(tenant_id, entity_id)

    def resolve_alias_prefix(self, tenant_id: str, prefix: str) -> frozenset[str]:
        """Delegate unchanged -- this wiring only intercepts writes."""
        return self._entity_memory_repository.resolve_alias_prefix(tenant_id, prefix)

    def resolve_exact_term(self, tenant_id: str, term: str) -> frozenset[str]:
        """Delegate unchanged -- this wiring only intercepts writes."""
        return self._entity_memory_repository.resolve_exact_term(tenant_id, term)

    def write_attribute(
        self,
        tenant_id: str,
        entity_id: str,
        attribute_name: str,
        value: str,
        provenance_id: str,
        *,
        source_type: SourceType,
        retrieval_context_hash: str,
        actor: str = _ZONE5_ACTOR_LABEL,
        change: str = "zone5 attribute write",
        source_refs: Sequence[str] = (),
        prev_provenance_id: str | None = None,
        prev_hash: str | None = None,
        updated_at: datetime | None = None,
    ) -> WriteGateResult:
        """Run the FR-013 sweep for `(entity_id, attribute_name)`, then write the attribute.

        AC-022-1: the sweep runs against `item_id = "{entity_id}:
        {attribute_name}"` -- the (entity, predicate) pair -- BEFORE the
        attribute itself is persisted. AC-022-2: no conflict adds exactly
        the sweep's own documented one extra read.

        A blank `provenance_id` is delegated straight to
        `EntityMemoryRepository.write_attribute` without first building a
        `ProvenanceRecord` (which would raise, since `ProvenanceRecord.
        provenance_id` must be non-blank): AC-005-5's existing rejection
        ("nothing is stored and no event is published") is `write_attribute`'s
        own owned validation, reused here unchanged rather than duplicated
        (DRY) -- this method never re-implements that check.

        Args:
            tenant_id, entity_id, attribute_name, value, provenance_id:
                Forwarded unchanged to `EntityMemoryRepository.write_attribute`.
            source_type: HLD Section 3.8's confidence-base selector.
            retrieval_context_hash: SHA-256 hex of the retrieval context
                this write was issued under (pre-hashed by the caller).
            actor: Who/what performed this write.
            change: A short, human-readable description of this write
                event.
            source_refs: item_ids this write was derived from.
            prev_provenance_id: The provenance_id this write claims to
                correct/confirm, or `None` for a fresh, unlinked claim
                about this (entity, predicate) pair.
            prev_hash: The previous record's `record_hash` in this
                item_id's chain.
            updated_at: Forwarded to `EntityMemoryRepository.write_attribute`
                and reused as this write's own provenance `write_timestamp`
                (kept identical between the Zone 5 attribute row and its
                Zone 7 provenance record); defaults to `self._clock.now()`
                when omitted, matching `write_attribute`'s own default.

        Returns:
            Whatever `EntityMemoryRepository.write_attribute` returns --
            `WriteAccepted` once both the sweep and the attribute write
            have completed, or `WriteRejected` (AC-005-5) when
            `provenance_id` is blank, in which case the sweep never runs.

        Raises:
            Whatever `provenance_repository.append` raises on a
            non-blank `provenance_id` -- this method adds no new
            exception type and does not swallow a sweep failure.
        """
        if not provenance_id or not provenance_id.strip():
            return self._entity_memory_repository.write_attribute(
                tenant_id,
                entity_id,
                attribute_name,
                value,
                provenance_id,
                updated_at=updated_at,
            )

        when = updated_at if updated_at is not None else self._clock.now()
        item_id = _zone5_attribute_item_id(entity_id, attribute_name)
        record = ProvenanceRecord.create(
            tenant_id=tenant_id,
            provenance_id=provenance_id,
            item_id=item_id,
            source_zone=ZoneId.ENTITY,
            source_type=source_type,
            write_timestamp=when,
            actor=actor,
            change=change,
            retrieval_context_hash=retrieval_context_hash,
            source_refs=source_refs,
            prev_provenance_id=prev_provenance_id,
            prev_hash=prev_hash,
        )
        self._provenance_repository.append(record)
        return self._entity_memory_repository.write_attribute(
            tenant_id,
            entity_id,
            attribute_name,
            value,
            provenance_id,
            updated_at=when,
        )
