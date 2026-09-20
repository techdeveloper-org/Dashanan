"""EntityOwnershipSpecification: the Zone 3/Zone 5 write-path classification gate.

HLD Section 3.4 (verbatim pseudocode, lines 235-249), traced to FR-003 in
SRS.md:

    given a candidate fact F:
      subjects := entity references in F that the fact predicates ABOUT
      objects  := entity references in F that the fact predicates TOWARD

      if |subjects| == 1 and |objects| == 0:
          -> Zone 5 (Entity). Write as an attribute of that entity record. ONLY Zone 5.
      elif |subjects| == 1 and |objects| >= 1:
          -> Zone 5 attribute write, AND a Zone 3 SemanticEdge IFF
             reverse-traversal is declared needed (index_reverse=true on the write request).
             Default index_reverse=false.
      elif |subjects| == 0:
          -> Zone 3 GeneralFact (world knowledge owned by no entity).
      else:  # |subjects| > 1
          -> Zone 3 SemanticEdge(s), one per (subject, object) pair.

FR-003 (SRS.md, verbatim): "The system SHALL provide a Semantic Memory
zone holding distilled, deduplicated cross-entity relationships and
general facts not owned by any single entity record, per the locked
Zone 3/Zone 5 ownership ADR."

Scope (must-not-deviate, sprint2_ar1_assignments.json AR1-S2-013): this
module implements ONLY the classification branches that produce a Zone 3
outcome (`SemanticEdgeRouting`, `GeneralFactRouting`) or explicitly signal
"no Zone 3 write" (`ZoneFiveOnlyRouting`). It never constructs, writes, or
stubs a Zone 5 attribute write for the `|subjects|==1` branches -- that
targets FR-005, unscheduled at draft time. `ZoneFiveOnlyRouting` is a pure
marker value the caller reads to know a Zone 5 write is the caller's own
separate responsibility; it performs no I/O and holds no Zone 5 domain
type.

This module is domain-only -- no SQL, no I/O, no ID generation -- per
`clean-architecture` and this codebase's established convention
(`domain/conflict_detection.py`, `domain/write_gate.py`): classification
is a pure function of its input, and identifier minting (`uuid4()`, mirroring
`application/conflict_detection_sweep.py` and `application/
provenance_write_gate.py`) is deferred to whichever caller wires this gate
into the write path -- passed in here as `edge_id_factory`/`fact_id_factory`
callables (Strategy/first-class-function pattern, `python-design-patterns-
core` section 18) so this module stays a pure, synchronous, easily-tested
classifier with no hidden randomness.

Hard Rule 2 (HLD Section 3.10): every `SemanticEdge` this module
constructs carries only the `subject_ref`/`object_ref` entity_id strings a
caller supplies on `CandidateFact` -- this module never reads, copies, or
even has access to an entity's attribute values. `index_reverse` defaults
to `False` on `CandidateFact` (HLD Section 3.4's own stated default),
enforced structurally by the dataclass default rather than by a runtime
check the caller could omit.

PII NOTE: per the dev_prompt's PII constraint, `CandidateFact.subjects`/
`objects` are opaque Zone 5 `entity_id` reference strings and `statement`/
`predicate` are Zone-owned opaque payload content (mirrors `SemanticEdge`/
`GeneralFact`'s own identical contract in `domain/semantic_memory.py`) --
this module never inspects or interprets that content. Every illustration
in this module's docstrings uses pseudonymized `<ENTITY_A>`/`<PLACE_A>`
placeholders, never HLD's own worked example's realistic names ("Sachin",
"Mumbai").

Cross-FR sequencing risk (carried forward per the dev_prompt, mirroring
DASH-STORY-004/009's Sprint 1 scope-stop flags): until FR-005 (the Zone 5
attribute write) is scheduled, a caller that routes a `ZoneFiveOnlyRouting`
result to nothing at all has performed only a partial write -- the fact's
Zone 5 half is simply not yet implementable anywhere in this codebase.
This module's own contract is unaffected: it correctly reports "no Zone 3
write for this branch," which is all FR-003 assigns to Zone 3.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from dashanan.domain.semantic_memory import GeneralFact, SemanticEdge


@dataclass(frozen=True, slots=True)
class CandidateFact:
    """One candidate fact `F`, as HLD Section 3.4's pseudocode names it.

    This is the classification gate's own input shape -- distinct from
    `SemanticEdge`/`GeneralFact` (the possible OUTPUTS of classifying one)
    -- so a caller can present a fact for routing before any Zone 3
    identifier exists for it.

    Attributes:
        tenant_id: Owning tenant. Required on every query (mirrors the
            `ZoneRepository` port's own tenant_id invariant, HLD 3.0
            invariant 2).
        subjects: Entity references (Zone 5 `entity_id` strings) the fact
            predicates ABOUT. `len(subjects)` is the pseudocode's
            `|subjects|` -- the primary branch selector.
        objects: Entity references the fact predicates TOWARD.
            `len(objects)` is the pseudocode's `|objects|`.
        predicate: The relationship name (e.g. `"located_in"`) used to
            construct any `SemanticEdge`. Irrelevant, and safely left
            blank, for the `|subjects|==0` (`GeneralFact`) branch --
            `GeneralFact` has no predicate field.
        qualifiers: Zone-owned, opaque key-value metadata forwarded
            verbatim onto every `SemanticEdge` this fact produces. Never
            interpreted by this module.
        statement: The fact's own text content, used to construct a
            `GeneralFact` when `|subjects|==0`. Irrelevant, and safely
            left blank, for every other branch.
        subject_scope: A caller-supplied scope descriptor, used to
            construct a `GeneralFact` when `|subjects|==0`. Irrelevant,
            and safely left blank, for every other branch.
        index_reverse: HLD Section 3.4's own named field: "reverse-
            traversal is declared needed." Defaults to `False` per HLD's
            explicit "Default index_reverse=false" -- this default is
            must-not-deviate item 2 (AR1-S2-013), enforced structurally
            here rather than by a runtime check a caller could bypass.
            Read only by the `|subjects|==1 and |objects|>=1` branch;
            every other branch ignores it.
    """

    tenant_id: str
    subjects: tuple[str, ...]
    objects: tuple[str, ...]
    predicate: str = ""
    qualifiers: Mapping[str, str] = field(default_factory=dict)
    statement: str = ""
    subject_scope: str = ""
    index_reverse: bool = False

    def __post_init__(self) -> None:
        """Reject blank identifiers -- defense-in-depth ahead of `classify`.

        This is a structural check on `CandidateFact` itself, distinct
        from the branch-specific invariants (`statement` non-blank for a
        `GeneralFact`, `predicate` non-blank for a `SemanticEdge`) that
        `SemanticEdge.__post_init__`/`GeneralFact.__post_init__` already
        enforce when `classify` constructs one -- this module never
        duplicates those checks (DRY), it relies on the domain object's
        own invariant to fire with a clear, field-specific `ValueError`.

        Raises:
            ValueError: If `tenant_id` is blank, or any entry of
                `subjects`/`objects` is blank.
        """
        if not self.tenant_id.strip():
            raise ValueError("CandidateFact.tenant_id must not be blank")
        for ref in self.subjects:
            if not ref.strip():
                raise ValueError("CandidateFact.subjects must not contain a blank entity_id")
        for ref in self.objects:
            if not ref.strip():
                raise ValueError("CandidateFact.objects must not contain a blank entity_id")


@dataclass(frozen=True, slots=True)
class SemanticEdgeRouting:
    """Zone 3 write outcome: one or more `SemanticEdge`s to create.

    Produced by the `|subjects| > 1` branch (one edge per `(subject,
    object)` pair, AC-003-CLASS-1) and by the `|subjects|==1 and
    |objects|>=1` branch's conditional sub-case when `index_reverse=True`
    (AC-003-CLASS-4).
    """

    edges: tuple[SemanticEdge, ...]


@dataclass(frozen=True, slots=True)
class GeneralFactRouting:
    """Zone 3 write outcome: exactly one `GeneralFact` to create.

    Produced by the `|subjects| == 0` branch (AC-003-GF-1).
    """

    fact: GeneralFact


@dataclass(frozen=True, slots=True)
class ZoneFiveOnlyRouting:
    """No Zone 3 write for this fact -- routes to Zone 5 only.

    A pure marker: this module performs no Zone 5 write, stub, or fake of
    one (must-not-deviate item 1, AR1-S2-013) -- a caller that receives
    this value is being told "Zone 3 has nothing to do with this fact,"
    and any Zone 5 attribute write is that caller's own separate FR-005
    concern, entirely outside this module.

    Attributes:
        reason: Which branch produced this outcome -- distinguishes the
            unconditional `|subjects|==1 and |objects|==0` case
            (AC-003-CLASS-2) from the `index_reverse=False` sub-case of
            `|subjects|==1 and |objects|>=1` (AC-003-CLASS-3). Purely
            informational (e.g. for logging); no caller behavior is
            expected to branch on its exact value beyond "no Zone 3
            write occurred."
    """

    reason: str


EntityOwnershipRouting = SemanticEdgeRouting | GeneralFactRouting | ZoneFiveOnlyRouting
"""The gate's Result type (mirrors `domain.write_gate.WriteGateResult`):
exactly one of the three possible Zone 3 routing outcomes HLD Section
3.4's pseudocode can produce."""


REASON_SINGLE_SUBJECT_NO_OBJECT = "single_subject_no_object"
"""`ZoneFiveOnlyRouting.reason` for the unconditional `|subjects|==1 and
|objects|==0` branch (AC-003-CLASS-2, HLD line 239-240: "ONLY Zone 5")."""

REASON_SINGLE_SUBJECT_INDEX_REVERSE_FALSE = "single_subject_index_reverse_false"
"""`ZoneFiveOnlyRouting.reason` for the `|subjects|==1 and |objects|>=1`
branch when `index_reverse` is `False` (AC-003-CLASS-3, the default)."""


def classify(
    fact: CandidateFact,
    *,
    edge_id_factory: Callable[[], str],
    fact_id_factory: Callable[[], str],
) -> EntityOwnershipRouting:
    """Apply HLD Section 3.4's `EntityOwnershipSpecification` routing table.

    Branch order mirrors the pseudocode's own `if`/`elif`/`elif`/`else`
    chain exactly, evaluated in the same order, so a fact that could
    structurally match more than one description (there is none in HLD's
    own chain, but this keeps the mapping literal and auditable) resolves
    identically to the locked spec.

    Args:
        fact: The candidate fact to classify.
        edge_id_factory: Invoked once per `SemanticEdge` this call needs
            to construct (zero, one, or `len(subjects) * len(objects)`
            times depending on the branch taken) -- never invoked for a
            branch that produces no edge. A caller wires this to its own
            ID minting strategy (mirrors `application.
            provenance_write_gate`'s `uuid4()` usage); this module never
            generates an identifier itself.
        fact_id_factory: Invoked exactly once, and only when the
            `|subjects|==0` branch is taken (AC-003-GF-1). Never invoked
            for any other branch.

    Returns:
        `GeneralFactRouting` for `|subjects|==0` (AC-003-CLASS-1,
        AC-003-GF-1).

        `ZoneFiveOnlyRouting` for `|subjects|==1 and |objects|==0`
        (AC-003-CLASS-2), and for `|subjects|==1 and |objects|>=1` when
        `fact.index_reverse` is `False` (AC-003-CLASS-3, the default).

        `SemanticEdgeRouting` for `|subjects|==1 and |objects|>=1` when
        `fact.index_reverse` is `True` -- one edge per object in
        `fact.objects` (AC-003-CLASS-4 covers the worked example's
        `len(objects) == 1` case, producing exactly one edge) -- and for
        `|subjects| > 1` -- one edge per `(subject, object)` pair
        (AC-003-CLASS-1). A `|subjects| > 1` fact with `len(objects) == 0`
        is a judgment call HLD's own pseudocode does not explicitly guard
        (see this story's dev report): the cross product is empty, so
        this call returns `SemanticEdgeRouting(edges=())`, correctly
        reporting "no edge to create" rather than raising or fabricating
        one.

    Raises:
        ValueError: Propagated from `SemanticEdge.__post_init__` (e.g. a
            blank `fact.predicate` when an edge is being constructed) or
            `GeneralFact.__post_init__` (e.g. a blank `fact.statement` or
            `fact.subject_scope` when `|subjects|==0`) -- this function
            performs no separate, duplicate validation of its own.
    """
    subjects = fact.subjects
    objects = fact.objects

    if len(subjects) == 1 and len(objects) == 0:
        return ZoneFiveOnlyRouting(reason=REASON_SINGLE_SUBJECT_NO_OBJECT)

    if len(subjects) == 1 and len(objects) >= 1:
        if not fact.index_reverse:
            return ZoneFiveOnlyRouting(reason=REASON_SINGLE_SUBJECT_INDEX_REVERSE_FALSE)
        subject_ref = subjects[0]
        edges = tuple(
            SemanticEdge(
                tenant_id=fact.tenant_id,
                edge_id=edge_id_factory(),
                subject_ref=subject_ref,
                predicate=fact.predicate,
                object_ref=object_ref,
                qualifiers=fact.qualifiers,
            )
            for object_ref in objects
        )
        return SemanticEdgeRouting(edges=edges)

    if len(subjects) == 0:
        general_fact = GeneralFact(
            tenant_id=fact.tenant_id,
            fact_id=fact_id_factory(),
            statement=fact.statement,
            subject_scope=fact.subject_scope,
        )
        return GeneralFactRouting(fact=general_fact)

    # else: len(subjects) > 1
    edges = tuple(
        SemanticEdge(
            tenant_id=fact.tenant_id,
            edge_id=edge_id_factory(),
            subject_ref=subject_ref,
            predicate=fact.predicate,
            object_ref=object_ref,
            qualifiers=fact.qualifiers,
        )
        for subject_ref in subjects
        for object_ref in objects
    )
    return SemanticEdgeRouting(edges=edges)
