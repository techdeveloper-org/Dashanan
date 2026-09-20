"""EntityRecord: Zone 5's owned canonical per-entity attribute store (HLD Section 3.6, FR-005).

FR-005 (verbatim, SRS.md): "The system SHALL provide an Entity Memory
zone holding per-entity (person/project/system) profile records, each
the canonical sole owner of that entity's own attributes." Traced by
DASH-STORY-017.

Zone 5 is the classification model's sole canonical owner of an entity's
attribute values (HLD Section 3.10 Data Ownership Map: "Zone5 OWNS
EntityRecord and all attributes", Hard Rule 2: no other zone may copy an
attribute value directly -- cross-zone references use `entity_id` only).
This module holds pure domain logic only: no SQL, no I/O, mirroring
`dashanan.domain.provenance_record`'s identical "domain-only" layering
(clean-architecture: database/index access is encapsulated in the
repository layer -- domain logic must never contain raw storage code).

Sub-record provenance granularity (HLD Section 3.6): the storage unit is
`EntityAttributeRecord`, ONE per (entity_id, attribute_name) pair, each
carrying its own `provenance_id` -- never a single provenance_id shared
across a whole entity. `EntityRecord` is the read-side aggregate view
`EntityMemoryRepository.get_entity` assembles from an entity's own
attribute sub-records (AC-005-1); it is not itself the storage unit, and
constructing one does not write anything (see
`infrastructure.entity_memory_repository` for the write path).

PII NOTE: `EntityAttributeRecord.value` is the canonical store for a real
entity's real attribute value -- this module's own docstrings and any
worked example use only structural shape (attribute names, provenance
ids) and pseudonymized placeholders, never a real or HLD-sourced example
value, per this story's PII redaction discipline. The module's runtime
behavior is unrestricted (it stores whatever value a caller legitimately
writes); the redaction applies to documentation and test fixtures, not
to what the class is capable of storing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class EntityAttributeRecord:
    """One attribute sub-record for a single entity (HLD Section 3.6 sub-record granularity).

    Zone 5's storage unit is THIS record, not the whole `EntityRecord` --
    a single-attribute update (AC-005-2) writes exactly one new
    `EntityAttributeRecord`, touching no sibling attribute's `value`,
    `provenance_id`, or `updated_at`, and never triggers a whole-entity
    rewrite. Every attribute carries its own `provenance_id`,
    independent of every sibling attribute's provenance.

    Attributes:
        tenant_id: Owning tenant (HLD 3.0 invariant 2). Mandatory on
            every Zone 5 storage operation.
        entity_id: The entity this attribute belongs to.
        attribute_name: The attribute's key within its entity. Unique
            per `(tenant_id, entity_id)` -- `EntityRecord.__post_init__`
            enforces this at the aggregate level.
        value: The attribute's current value. Opaque to this module --
            never inspected, transformed, or copied to another zone by
            this class (HLD Section 3.10 Hard Rule 2).
        provenance_id: This attribute's own Zone 7 provenance record
            identifier. `AC-005-5` (implemented in
            `infrastructure.entity_memory_repository`) rejects a write
            attempting to persist an attribute without one.
        updated_at: When this attribute value was last written.
    """

    tenant_id: str
    entity_id: str
    attribute_name: str
    value: str
    provenance_id: str
    updated_at: datetime

    def __post_init__(self) -> None:
        """Enforce the identifiers AC-005-1/AC-005-2/AC-005-5 depend on.

        Raises:
            ValueError: If `tenant_id`, `entity_id`, `attribute_name`, or
                `provenance_id` is blank.
        """
        if not self.tenant_id.strip():
            raise ValueError("EntityAttributeRecord.tenant_id must not be blank")
        if not self.entity_id.strip():
            raise ValueError("EntityAttributeRecord.entity_id must not be blank")
        if not self.attribute_name.strip():
            raise ValueError("EntityAttributeRecord.attribute_name must not be blank")
        if not self.provenance_id.strip():
            raise ValueError("EntityAttributeRecord.provenance_id must not be blank")


@dataclass(frozen=True, slots=True)
class EntityRecord:
    """The read-side aggregate view of one entity's attributes (AC-005-1).

    Assembled by `EntityMemoryRepository.get_entity` from that entity's
    own stored `EntityAttributeRecord`s -- this class performs no
    storage itself; it is the point-lookup's RETURN shape, not the
    storage unit (see `EntityAttributeRecord`'s own docstring for that
    distinction).

    Attributes:
        tenant_id: Owning tenant.
        entity_id: This entity's identifier.
        attributes: Every attribute currently stored for this entity, in
            no particular order. An empty tuple is structurally valid
            (e.g. for a caller building one directly in a test); the
            repository itself returns `None` from `get_entity` rather
            than an empty `EntityRecord` when no attribute has ever been
            written for an entity_id.
    """

    tenant_id: str
    entity_id: str
    attributes: tuple[EntityAttributeRecord, ...] = ()

    def __post_init__(self) -> None:
        """Enforce identifiers and that every attribute belongs to this entity.

        Raises:
            ValueError: If `tenant_id`/`entity_id` is blank, any
                attribute's own `(tenant_id, entity_id)` does not match
                this record's, or two attributes share the same
                `attribute_name` (AC-005-2's "exactly one" invariant
                would otherwise be unenforceable at the aggregate
                level).
        """
        if not self.tenant_id.strip():
            raise ValueError("EntityRecord.tenant_id must not be blank")
        if not self.entity_id.strip():
            raise ValueError("EntityRecord.entity_id must not be blank")
        seen_names: set[str] = set()
        for attribute in self.attributes:
            if (
                attribute.tenant_id != self.tenant_id
                or attribute.entity_id != self.entity_id
            ):
                raise ValueError(
                    "EntityRecord.attributes must all share this record's "
                    f"(tenant_id={self.tenant_id!r}, entity_id={self.entity_id!r}); "
                    f"got (tenant_id={attribute.tenant_id!r}, "
                    f"entity_id={attribute.entity_id!r})"
                )
            if attribute.attribute_name in seen_names:
                raise ValueError(
                    "EntityRecord.attributes must not contain a duplicate "
                    f"attribute_name {attribute.attribute_name!r}"
                )
            seen_names.add(attribute.attribute_name)

    def get_attribute(self, attribute_name: str) -> EntityAttributeRecord | None:
        """Return the sub-record for `attribute_name`, or `None` if unset."""
        for attribute in self.attributes:
            if attribute.attribute_name == attribute_name:
                return attribute
        return None
