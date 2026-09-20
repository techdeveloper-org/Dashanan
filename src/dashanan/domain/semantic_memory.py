"""SemanticEdge / GeneralFact: Zone 3's owned entities (HLD Section 3.4, FR-003).

FR-003 (SRS.md, verbatim): "The system SHALL provide a Semantic Memory
zone holding distilled, deduplicated cross-entity relationships and
general facts not owned by any single entity record, per the locked
Zone 3/Zone 5 ownership ADR."

Zone 3 owns exactly these two entity types (must-not-deviate item 1,
sprint2_ar1_assignments.json AR1-S2-012) -- no other owned entity type is
defined in this module or anywhere else in this story's scope. This module
is domain-only -- no SQL, no I/O -- per `clean-architecture` and this
codebase's established convention (`provenance_record.py`, `episode.py`):
database access is encapsulated in `infrastructure.sql_semantic_repository`,
never here.

Hard Rule 2 (HLD Section 3.10): `subject_ref` and `object_ref` are Zone 5
`entity_id` REFERENCES ONLY -- this module never copies an attribute value
from Zone 5, and neither field's type nor validation here reaches into
`dashanan.domain` beyond what DASH-STORY-001 already established (`zone.py`,
`ports.py`, `exceptions.py`, `memory_item.py`), per must-not-deviate item 3
("zones are leaves by design," HLD Section 3.11).

PII NOTE: `GeneralFact.statement` and `SemanticEdge.qualifiers` are Zone
3's own opaque payload content (mirrors `Episode.payload`'s identical
"zone-owned content, opaque to the Orchestrator" contract) -- this module
never inspects or interprets that content itself. Per the dev_prompt's PII
constraint, any illustration of a `SemanticEdge`/`GeneralFact` in a
docstring or test uses pseudonymized `<ENTITY_A>`/`<ENTITY_B>` placeholders
or the literal `<PII_EXAMPLE_REDACTED>` marker, never HLD's own worked
example's realistic name ("Sachin", "Mumbai").
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum


class SemanticState(str, Enum):
    """The three states of the locked `Active -> Compressed -> Archived` lifecycle.

    Names and values mirror `dashanan.domain.episode.EpisodeState` and
    `dashanan.domain.rotation_state.RotationState` exactly (HLD Section 6,
    "MemoryItem lifecycle" row: a single, zone-agnostic state machine
    definition; each zone's own entity type carries its own state field
    with the same values) -- an ENUM is the correct choice here per
    database-engineer's schema-design rule ("ENUM types for fixed value
    sets only when the set truly never changes").
    """

    ACTIVE = "active"
    COMPRESSED = "compressed"
    ARCHIVED = "archived"


@dataclass(frozen=True, slots=True)
class SemanticEdge:
    """Zone 3's first owned entity: a directed, predicated edge between two entities.

    Attributes mirror HLD Section 3.4's owned-entity shape exactly:
    `SemanticEdge(tenant_id, edge_id, subject_ref, predicate, object_ref,
    qualifiers{}, state, score_terms)`. Constructed only when the Zone 3/5
    ownership routing decision (HLD Section 3.4's `EntityOwnershipSpecification`
    pseudocode -- the Orchestrator's write-path concern, out of this
    schema-focused story's scope, see the dev report's judgment-call list)
    has already selected the `|subjects| >= 1 and |objects| >= 1` branch.

    Attributes:
        tenant_id: Owning tenant. Required on every query (mirrors the
            `ZoneRepository` port's own tenant_id invariant, HLD 3.0
            invariant 2).
        edge_id: This edge's own identifier. Unique per tenant.
        subject_ref: The Zone 5 `entity_id` this fact predicates ABOUT.
            A reference only -- never a copied attribute value (Hard
            Rule 2).
        predicate: The relationship name (e.g. `"located_in"`).
        object_ref: The Zone 5 `entity_id` this fact predicates TOWARD.
            Structurally required -- see `__post_init__` and
            `semantic_schema.sql`'s `chk_semantic_edges_object_ref_not_null`
            for why a `SemanticEdge` can never carry a missing object_ref
            (must-not-deviate item 2, AC-003-SCHEMA-1): a single-subject
            no-object fact is by definition Entity-owned (HLD Section
            3.4's `|objects|==0` branch), never a `SemanticEdge`.
        qualifiers: Zone-owned, opaque key-value metadata about this edge
            (e.g. a validity window) -- never interpreted by this module.
        state: Current position in the locked rotation state machine.
        score_terms: Zone-computed partial MemoryScore contributions,
            keyed by term name.
    """

    tenant_id: str
    edge_id: str
    subject_ref: str
    predicate: str
    object_ref: str
    qualifiers: Mapping[str, str] = field(default_factory=dict)
    state: SemanticState = SemanticState.ACTIVE
    score_terms: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Enforce the invariants the non-duplication guarantee depends on.

        This is domain-level DEFENSE-IN-DEPTH alongside, never a
        replacement for, `semantic_schema.sql`'s
        `chk_semantic_edges_object_ref_not_null` CHECK constraint --
        must-not-deviate item 2 is explicit that the DB constraint, not an
        application-level convention, is the binding enforcement
        (AC-003-SCHEMA-1). This check exists so an in-process caller gets
        an immediate, typed `ValueError` before ever reaching the database,
        exactly mirroring `ProvenanceRecord.__post_init__`'s identical
        "domain check backs the DB constraint, does not substitute for it"
        posture.

        Raises:
            ValueError: If any identifier, `predicate`, or `object_ref` is
                blank.
        """
        if not self.tenant_id.strip():
            raise ValueError("SemanticEdge.tenant_id must not be blank")
        if not self.edge_id.strip():
            raise ValueError("SemanticEdge.edge_id must not be blank")
        if not self.subject_ref.strip():
            raise ValueError("SemanticEdge.subject_ref must not be blank")
        if not self.predicate.strip():
            raise ValueError("SemanticEdge.predicate must not be blank")
        if not self.object_ref or not self.object_ref.strip():
            raise ValueError(
                "SemanticEdge.object_ref must not be blank -- a "
                "single-subject-no-object fact is Entity-owned (HLD "
                "Section 3.4), never a SemanticEdge (must-not-deviate "
                "item 2, AC-003-SCHEMA-1)"
            )


@dataclass(frozen=True, slots=True)
class GeneralFact:
    """Zone 3's second owned entity: world knowledge owned by no single entity.

    Attributes mirror HLD Section 3.4's owned-entity shape exactly:
    `GeneralFact(tenant_id, fact_id, statement, subject_scope, state,
    score_terms)`. Constructed only when the ownership routing decision
    selects the `|subjects| == 0` branch (AC-003-GF-1) -- e.g. HLD's own
    worked example, pseudonymized here per the dev_prompt's PII
    constraint: `"<ENTITY_A> is a city in <ENTITY_B>"` has no person-level
    subject, so it is world knowledge, never an `Entity` attribute.

    Attributes:
        tenant_id: Owning tenant. Required on every query.
        fact_id: This fact's own identifier. Unique per tenant.
        statement: The fact's own text content. Zone-owned, opaque
            payload -- mirrors `Episode.payload`'s identical contract.
        subject_scope: A caller-supplied descriptor of the fact's scope
            (e.g. a locale or domain tag) -- HLD Section 3.4 names this
            field in `GeneralFact`'s literal shape without further
            elaborating its structure; this module treats it as an
            opaque, required label (see the dev report's judgment-call
            list for the full rationale).
        state: Current position in the locked rotation state machine.
        score_terms: Zone-computed partial MemoryScore contributions,
            keyed by term name.
    """

    tenant_id: str
    fact_id: str
    statement: str
    subject_scope: str
    state: SemanticState = SemanticState.ACTIVE
    score_terms: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Enforce the invariants the PK lookup and routing contract depend on.

        Raises:
            ValueError: If any identifier, `statement`, or `subject_scope`
                is blank.
        """
        if not self.tenant_id.strip():
            raise ValueError("GeneralFact.tenant_id must not be blank")
        if not self.fact_id.strip():
            raise ValueError("GeneralFact.fact_id must not be blank")
        if not self.statement.strip():
            raise ValueError("GeneralFact.statement must not be blank")
        if not self.subject_scope.strip():
            raise ValueError("GeneralFact.subject_scope must not be blank")
