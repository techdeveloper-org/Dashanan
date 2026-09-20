"""QA subtask (20% of story points) for DASH-STORY-013, traced to FR-003.

FR-003 (SRS.md, verbatim): "The system SHALL provide a Semantic Memory
zone holding distilled, deduplicated cross-entity relationships and
general facts not owned by any single entity record, per the locked
Zone 3/Zone 5 ownership ADR."

This is the formal pytest suite the story's own qa_prompt calls for --
distinct from the DEV subtask's `tests/test_smoke_entity_ownership_specification.py`,
which only smoke-checks wiring and must-not-deviate structural properties.
This suite enumerates every AC as its own scenario set (happy path,
boundary, adverse) and keeps one golden regression test per AC, per the
qa_prompt's "one golden test case per AC as a regression guard" requirement.

Runtime assumptions (recorded per rule 33/40 test-roadmap conventions, so
failures are reproducible without external state):
  - clock: none needed -- `dashanan.domain.entity_ownership_specification`
    carries no timestamp field and performs no I/O.
  - tenant_id: "tenant-1" for every fact built in this suite unless a test
    states a different value explicitly.
  - id factories: deterministic, counter-based (`_counting_id_factory`),
    never `uuid4()`, so every expected edge_id/fact_id in this suite is
    reproducible and order-sensitive assertions are meaningful.

PII NOTE: per the dev_prompt's PII constraint, every illustration below
uses pseudonymized `<ENTITY_A>` / `<ENTITY_B>` / `<PLACE_A>` / `<PLACE_B>`
placeholders -- never HLD's own worked example's realistic names ("Sachin",
"Mumbai").

AC enumeration (qa_prompt "think step by step" requirement, restated
before the test code per rule):

  AC-003-CLASS-1 (happy path + boundary):
    - |subjects|>1 -> Zone 3 SemanticEdgeRouting, one edge per
      (subject, object) pair (full cross product, not just a diagonal).
    - |subjects|==0 -> Zone 3 GeneralFactRouting, exactly one GeneralFact.
    Assertion: routing type, edge count == len(subjects) * len(objects),
    every pair present exactly once; GeneralFact fields mirror the
    CandidateFact's own statement/subject_scope/tenant_id verbatim.

  AC-003-CLASS-2 (happy path + adverse index_reverse values):
    - |subjects|==1, |objects|==0 -> ZoneFiveOnlyRouting for BOTH
      index_reverse=False and index_reverse=True (the branch does not
      even inspect index_reverse; HLD line 239-240 says "ONLY Zone 5"
      unconditionally).
    Assertion: no SemanticEdge/GeneralFact is constructed (route type is
    ZoneFiveOnlyRouting, edge_id_factory/fact_id_factory never invoked)
    regardless of the index_reverse value.

  AC-003-CLASS-3 (boundary: default vs. explicit False):
    - |subjects|==1, |objects|==1, index_reverse left at its dataclass
      default (never explicitly passed) -> ZoneFiveOnlyRouting.
    - Same shape with index_reverse explicitly set to False -> identical
      outcome, proving the branch reads the *value*, not whether it was
      explicitly supplied.
    Assertion: both constructions produce ZoneFiveOnlyRouting with the
    same `reason` constant; no SemanticEdge is created.

  AC-003-CLASS-4 (happy path, worked example, adverse multi-object case):
    - |subjects|==1, |objects|==1, index_reverse=True -> Zone 3 creates
      exactly one SemanticEdge(located_in, <ENTITY_A>, <PLACE_A>).
    - |subjects|==1, |objects|>1, index_reverse=True (adverse/boundary,
      per the dev report's own flagged judgment call) -> one edge per
      object, not just the first.
    Assertion: edge count, predicate, subject_ref/object_ref identity
    (Hard Rule 2 -- no attribute-value copy, only entity_id references).

  Ownership boundary (dependency_integrity_check, sprint2_ar1_assignments.json):
    - This story owns exactly `entity_ownership_specification.py`. This
      suite asserts `classify` never imports, constructs, or returns any
      Zone 5/`Entity` domain type, and that a Zone 5 write path is never
      invoked as a side effect of any branch (must-not-deviate item 1).
"""

from __future__ import annotations

import dataclasses
import inspect
from collections.abc import Callable

import pytest

from dashanan.domain.entity_ownership_specification import (
    REASON_SINGLE_SUBJECT_INDEX_REVERSE_FALSE,
    REASON_SINGLE_SUBJECT_NO_OBJECT,
    CandidateFact,
    EntityOwnershipRouting,
    GeneralFactRouting,
    SemanticEdgeRouting,
    ZoneFiveOnlyRouting,
    classify,
)
from dashanan.domain.semantic_memory import GeneralFact, SemanticEdge

_TENANT = "tenant-1"
_ENTITY_A = "<ENTITY_A>"
_ENTITY_B = "<ENTITY_B>"
_ENTITY_C = "<ENTITY_C>"
_PLACE_A = "<PLACE_A>"
_PLACE_B = "<PLACE_B>"
_PREDICATE = "located_in"


def _counting_id_factory(prefix: str) -> Callable[[], str]:
    """A deterministic edge_id_factory/fact_id_factory: f"{prefix}-1", f"{prefix}-2", ..."""
    counter = {"n": 0}

    def _next() -> str:
        counter["n"] += 1
        return f"{prefix}-{counter['n']}"

    return _next


def _classify(fact: CandidateFact) -> EntityOwnershipRouting:
    return classify(
        fact,
        edge_id_factory=_counting_id_factory("edge"),
        fact_id_factory=_counting_id_factory("fact"),
    )


def _fact(
    *,
    subjects: tuple[str, ...] = (),
    objects: tuple[str, ...] = (),
    predicate: str = _PREDICATE,
    statement: str = "",
    subject_scope: str = "",
    index_reverse: bool = False,
    tenant_id: str = _TENANT,
) -> CandidateFact:
    return CandidateFact(
        tenant_id=tenant_id,
        subjects=subjects,
        objects=objects,
        predicate=predicate,
        statement=statement,
        subject_scope=subject_scope,
        index_reverse=index_reverse,
    )


class TestAC003Class1MultiSubjectRoutesToSemanticEdgePerPair:
    """AC-003-CLASS-1 (first half): |subjects|>1 -> one SemanticEdge per (subject, object) pair."""

    def test_golden_two_subjects_one_object_produces_two_edges(self) -> None:
        """Golden regression case for the |subjects|>1 branch."""
        fact = _fact(subjects=(_ENTITY_A, _ENTITY_B), objects=(_PLACE_A,))

        result = _classify(fact)

        assert isinstance(result, SemanticEdgeRouting)
        assert len(result.edges) == 2
        pairs = {(edge.subject_ref, edge.object_ref) for edge in result.edges}
        assert pairs == {(_ENTITY_A, _PLACE_A), (_ENTITY_B, _PLACE_A)}
        assert all(isinstance(edge, SemanticEdge) for edge in result.edges)
        assert all(edge.tenant_id == _TENANT for edge in result.edges)
        assert all(edge.predicate == _PREDICATE for edge in result.edges)

    def test_three_subjects_two_objects_produce_the_full_cross_product_not_a_diagonal(
        self,
    ) -> None:
        """Boundary: verifies the FULL cross product (6 edges), not len(subjects) edges."""
        fact = _fact(subjects=(_ENTITY_A, _ENTITY_B, _ENTITY_C), objects=(_PLACE_A, _PLACE_B))

        result = _classify(fact)

        assert isinstance(result, SemanticEdgeRouting)
        assert len(result.edges) == 6
        pairs = {(edge.subject_ref, edge.object_ref) for edge in result.edges}
        assert pairs == {
            (_ENTITY_A, _PLACE_A),
            (_ENTITY_A, _PLACE_B),
            (_ENTITY_B, _PLACE_A),
            (_ENTITY_B, _PLACE_B),
            (_ENTITY_C, _PLACE_A),
            (_ENTITY_C, _PLACE_B),
        }

    def test_edge_ids_are_unique_across_the_full_cross_product(self) -> None:
        fact = _fact(subjects=(_ENTITY_A, _ENTITY_B), objects=(_PLACE_A, _PLACE_B))

        result = _classify(fact)

        assert isinstance(result, SemanticEdgeRouting)
        edge_ids = [edge.edge_id for edge in result.edges]
        assert len(edge_ids) == len(set(edge_ids)), "edge_id_factory must be invoked once per edge"

    def test_multi_subject_ignores_index_reverse_entirely(self) -> None:
        """The |subjects|>1 branch is unconditional -- index_reverse is only read by the
        |subjects|==1 branch (HLD Section 3.4's own pseudocode ordering)."""
        fact_true = _fact(subjects=(_ENTITY_A, _ENTITY_B), objects=(_PLACE_A,), index_reverse=True)
        fact_false = _fact(subjects=(_ENTITY_A, _ENTITY_B), objects=(_PLACE_A,), index_reverse=False)

        result_true = _classify(fact_true)
        result_false = _classify(fact_false)

        assert isinstance(result_true, SemanticEdgeRouting)
        assert isinstance(result_false, SemanticEdgeRouting)
        assert len(result_true.edges) == len(result_false.edges) == 2


class TestAC003Class1ZeroSubjectsRoutesToGeneralFact:
    """AC-003-CLASS-1 (second half): |subjects|==0 -> Zone 3 GeneralFact."""

    def test_golden_zero_subjects_routes_to_a_single_general_fact(self) -> None:
        """Golden regression case for the |subjects|==0 branch."""
        fact = _fact(
            subjects=(),
            objects=(),
            statement="<PII_EXAMPLE_REDACTED>",
            subject_scope="global",
        )

        result = _classify(fact)

        assert isinstance(result, GeneralFactRouting)
        assert isinstance(result.fact, GeneralFact)
        assert result.fact.tenant_id == _TENANT
        assert result.fact.fact_id == "fact-1"
        assert result.fact.statement == "<PII_EXAMPLE_REDACTED>"
        assert result.fact.subject_scope == "global"

    def test_zero_subjects_with_nonempty_objects_still_routes_to_general_fact(self) -> None:
        """Boundary: HLD's pseudocode checks |subjects|==0 before inspecting |objects| at
        all -- a stray object list must not divert this branch."""
        fact = _fact(
            subjects=(),
            objects=(_PLACE_A,),
            statement="<PII_EXAMPLE_REDACTED>",
            subject_scope="global",
        )

        result = _classify(fact)

        assert isinstance(result, GeneralFactRouting)

    def test_zero_subjects_never_invokes_the_edge_id_factory(self) -> None:
        calls = {"n": 0}

        def _edge_factory() -> str:
            calls["n"] += 1
            return f"edge-{calls['n']}"

        classify(
            _fact(statement="<PII_EXAMPLE_REDACTED>", subject_scope="global"),
            edge_id_factory=_edge_factory,
            fact_id_factory=_counting_id_factory("fact"),
        )

        assert calls["n"] == 0

    def test_fact_id_factory_invoked_exactly_once(self) -> None:
        calls = {"n": 0}

        def _fact_factory() -> str:
            calls["n"] += 1
            return f"fact-{calls['n']}"

        classify(
            _fact(statement="<PII_EXAMPLE_REDACTED>", subject_scope="global"),
            edge_id_factory=_counting_id_factory("edge"),
            fact_id_factory=_fact_factory,
        )

        assert calls["n"] == 1


class TestAC003Class2SingleSubjectNoObjectIsZoneFiveOnlyForAnyIndexReverse:
    """AC-003-CLASS-2: |subjects|==1, |objects|==0 -> ZoneFiveOnlyRouting, any index_reverse."""

    def test_golden_single_subject_no_object_index_reverse_false(self) -> None:
        """Golden regression case: the unconditional 'ONLY Zone 5' branch, default index_reverse."""
        fact = _fact(subjects=(_ENTITY_A,), objects=())

        result = _classify(fact)

        assert isinstance(result, ZoneFiveOnlyRouting)
        assert result.reason == REASON_SINGLE_SUBJECT_NO_OBJECT

    def test_single_subject_no_object_index_reverse_true_is_still_zone_five_only(self) -> None:
        """Adverse case: a caller might mistakenly expect index_reverse=True to force a
        SemanticEdge here -- HLD line 239-240 says 'ONLY Zone 5' unconditionally, so it
        must not."""
        fact = _fact(subjects=(_ENTITY_A,), objects=(), index_reverse=True)

        result = _classify(fact)

        assert isinstance(result, ZoneFiveOnlyRouting)
        assert result.reason == REASON_SINGLE_SUBJECT_NO_OBJECT

    def test_single_subject_no_object_never_invokes_either_id_factory(self) -> None:
        calls = {"edge": 0, "fact": 0}

        def _edge_factory() -> str:
            calls["edge"] += 1
            return "unused"

        def _fact_factory() -> str:
            calls["fact"] += 1
            return "unused"

        classify(
            _fact(subjects=(_ENTITY_A,), objects=()),
            edge_id_factory=_edge_factory,
            fact_id_factory=_fact_factory,
        )

        assert calls == {"edge": 0, "fact": 0}

    def test_single_subject_no_object_result_carries_no_write_capable_method(self) -> None:
        """Must-not-deviate item 1: the routing outcome is a pure marker with no
        write/persist/apply method anywhere on it."""
        fact = _fact(subjects=(_ENTITY_A,), objects=())

        result = _classify(fact)

        assert isinstance(result, ZoneFiveOnlyRouting)
        assert not hasattr(result, "write")
        assert not hasattr(result, "persist")
        assert not hasattr(result, "apply")


class TestAC003Class3DefaultIndexReverseSuppressesSemanticEdge:
    """AC-003-CLASS-3: worked example (|subjects|=1,|objects|=1), index_reverse not true."""

    def test_golden_index_reverse_defaults_to_false_and_creates_no_edge(self) -> None:
        """Golden regression case: index_reverse omitted entirely (relying on the
        dataclass default), never a SemanticEdge."""
        fact = CandidateFact(
            tenant_id=_TENANT,
            subjects=(_ENTITY_A,),
            objects=(_PLACE_A,),
            predicate=_PREDICATE,
        )

        result = _classify(fact)

        assert isinstance(result, ZoneFiveOnlyRouting)
        assert result.reason == REASON_SINGLE_SUBJECT_INDEX_REVERSE_FALSE

    def test_index_reverse_explicitly_false_produces_the_identical_outcome_as_the_default(
        self,
    ) -> None:
        """Boundary: an explicit False must behave identically to an omitted (defaulted)
        value -- the branch reads the value, not whether it was supplied."""
        fact_default = CandidateFact(
            tenant_id=_TENANT, subjects=(_ENTITY_A,), objects=(_PLACE_A,), predicate=_PREDICATE
        )
        fact_explicit_false = _fact(subjects=(_ENTITY_A,), objects=(_PLACE_A,), index_reverse=False)

        result_default = _classify(fact_default)
        result_explicit = _classify(fact_explicit_false)

        assert isinstance(result_default, ZoneFiveOnlyRouting)
        assert isinstance(result_explicit, ZoneFiveOnlyRouting)
        assert result_default.reason == result_explicit.reason == REASON_SINGLE_SUBJECT_INDEX_REVERSE_FALSE

    def test_candidate_fact_dataclass_field_default_is_false_not_a_runtime_check(self) -> None:
        """Structural proof the default is enforced by the dataclass field default itself
        (must-not-deviate item 2), not by a bypassable runtime check."""
        fields_by_name = {f.name: f for f in dataclasses.fields(CandidateFact)}
        assert "index_reverse" in fields_by_name
        assert fields_by_name["index_reverse"].default is False

    def test_index_reverse_false_never_invokes_the_edge_id_factory(self) -> None:
        calls = {"n": 0}

        def _edge_factory() -> str:
            calls["n"] += 1
            return f"edge-{calls['n']}"

        classify(
            _fact(subjects=(_ENTITY_A,), objects=(_PLACE_A,), index_reverse=False),
            edge_id_factory=_edge_factory,
            fact_id_factory=_counting_id_factory("fact"),
        )

        assert calls["n"] == 0


class TestAC003Class4IndexReverseTrueCreatesTheConditionalSemanticEdge:
    """AC-003-CLASS-4: worked example with index_reverse=True -> exactly one SemanticEdge."""

    def test_golden_worked_example_creates_exactly_one_edge_located_in(self) -> None:
        """Golden regression case: the HLD worked example itself, pseudonymized."""
        fact = _fact(subjects=(_ENTITY_A,), objects=(_PLACE_A,), index_reverse=True)

        result = _classify(fact)

        assert isinstance(result, SemanticEdgeRouting)
        assert len(result.edges) == 1
        edge = result.edges[0]
        assert isinstance(edge, SemanticEdge)
        assert edge.predicate == _PREDICATE
        assert edge.subject_ref == _ENTITY_A
        assert edge.object_ref == _PLACE_A
        assert edge.tenant_id == _TENANT
        assert edge.edge_id == "edge-1"

    def test_hard_rule_2_edge_refs_are_the_exact_input_entity_id_strings_not_copies(
        self,
    ) -> None:
        """Hard Rule 2 (HLD Section 3.10): subject_ref/object_ref carry only entity_id
        references -- identity check (`is`), not merely equality, proves no
        transformation or attribute-value substitution occurred."""
        fact = _fact(subjects=(_ENTITY_A,), objects=(_PLACE_A,), index_reverse=True)

        result = _classify(fact)

        assert isinstance(result, SemanticEdgeRouting)
        edge = result.edges[0]
        assert edge.subject_ref is fact.subjects[0]
        assert edge.object_ref is fact.objects[0]

    def test_adverse_multiple_objects_with_index_reverse_true_creates_one_edge_per_object(
        self,
    ) -> None:
        """Adverse/boundary case flagged in the dev report's judgment calls: HLD's worked
        example only shows |objects|==1; for |objects|>1 this implementation creates one
        edge per object (mirroring the |subjects|>1 branch's own per-pair construction)
        rather than silently dropping every object past the first."""
        fact = _fact(subjects=(_ENTITY_A,), objects=(_PLACE_A, _PLACE_B), index_reverse=True)

        result = _classify(fact)

        assert isinstance(result, SemanticEdgeRouting)
        assert len(result.edges) == 2
        object_refs = {edge.subject_ref: None for edge in result.edges}  # noqa: F841
        pairs = {(edge.subject_ref, edge.object_ref) for edge in result.edges}
        assert pairs == {(_ENTITY_A, _PLACE_A), (_ENTITY_A, _PLACE_B)}
        assert all(edge.subject_ref == _ENTITY_A for edge in result.edges)

    def test_qualifiers_are_forwarded_verbatim_onto_the_semantic_edge(self) -> None:
        fact = CandidateFact(
            tenant_id=_TENANT,
            subjects=(_ENTITY_A,),
            objects=(_PLACE_A,),
            predicate=_PREDICATE,
            qualifiers={"confidence": "high"},
            index_reverse=True,
        )

        result = _classify(fact)

        assert isinstance(result, SemanticEdgeRouting)
        assert result.edges[0].qualifiers == {"confidence": "high"}


class TestMustNotDeviateNoZoneFiveWritePathAnywhere:
    """Must-not-deviate item 1 (sprint2_ar1_assignments.json AR1-S2-013): no Zone 5
    attribute write, stub, or fake is implemented for the |subjects|==1 branch in
    either sub-case -- that targets unscheduled FR-005."""

    def test_module_defines_no_entity_or_zone_five_domain_type(self) -> None:
        import dashanan.domain.entity_ownership_specification as module

        type_names = {
            name
            for name in vars(module)
            if isinstance(getattr(module, name), type) and not name.startswith("_")
        }
        assert "Entity" not in type_names
        assert not any(
            "zone5" in name.lower() or "entityrecord" in name.lower() for name in type_names
        )

    def test_classify_source_contains_no_zone_five_write_call(self) -> None:
        """A stronger structural guard than the smoke suite's type-import check: scans
        `classify`'s own source text for any of the write-shaped method names a stubbed
        Zone 5 write would plausibly use."""
        source = inspect.getsource(classify)
        forbidden_substrings = ("zone5", "entity_repository", ".persist(", ".save(", "write_attribute")
        lowered = source.lower()
        for forbidden in forbidden_substrings:
            assert forbidden not in lowered, f"classify() source unexpectedly contains {forbidden!r}"

    def test_zone_five_only_routing_has_no_zone_five_domain_reference(self) -> None:
        routing = ZoneFiveOnlyRouting(reason=REASON_SINGLE_SUBJECT_NO_OBJECT)
        field_names = {f.name for f in dataclasses.fields(routing)}
        assert field_names == {"reason"}


class TestDependencyIntegrityNoMutationOutsideStoryOwnership:
    """dependency_integrity_check (sprint2_ar1_assignments.json): classify() is a pure
    function -- it must not mutate its own input, and it must not require or touch any
    port/repository outside this story's ownership (ZoneRepository stays frozen, AR1-G2)."""

    def test_classify_does_not_mutate_the_input_candidate_fact(self) -> None:
        fact = _fact(subjects=(_ENTITY_A, _ENTITY_B), objects=(_PLACE_A,))
        subjects_before = tuple(fact.subjects)
        objects_before = tuple(fact.objects)

        _classify(fact)

        assert fact.subjects == subjects_before
        assert fact.objects == objects_before

    def test_candidate_fact_is_frozen_and_cannot_be_mutated_after_construction(self) -> None:
        fact = _fact(subjects=(_ENTITY_A,), objects=(_PLACE_A,))

        with pytest.raises(dataclasses.FrozenInstanceError):
            fact.index_reverse = True  # type: ignore[misc]

    def test_classify_signature_takes_no_repository_or_port_argument(self) -> None:
        """Structural proof `classify` cannot reach a repository even by accident: its
        only non-fact parameters are the two caller-supplied id factories."""
        signature = inspect.signature(classify)
        param_names = set(signature.parameters.keys())
        assert param_names == {"fact", "edge_id_factory", "fact_id_factory"}


class TestBoundaryAndAdverseInputValidation:
    """Boundary/adverse cases per rule 33/40 test-roadmap conventions (blank identifiers,
    empty-cross-product judgment call, propagated domain invariants)."""

    def test_candidate_fact_rejects_blank_tenant_id(self) -> None:
        with pytest.raises(ValueError, match="tenant_id"):
            CandidateFact(tenant_id="", subjects=(), objects=())

    def test_candidate_fact_rejects_whitespace_only_subject_entry(self) -> None:
        with pytest.raises(ValueError, match="subjects"):
            CandidateFact(tenant_id=_TENANT, subjects=("   ",), objects=())

    def test_candidate_fact_rejects_blank_object_entry(self) -> None:
        with pytest.raises(ValueError, match="objects"):
            CandidateFact(tenant_id=_TENANT, subjects=(_ENTITY_A,), objects=("",))

    def test_multi_subject_with_zero_objects_yields_an_empty_edge_tuple_not_an_error(
        self,
    ) -> None:
        """Judgment call documented in the dev report: HLD's else-branch (|subjects|>1)
        does not itself guard |objects|==0; the empty cross product is the correct,
        non-fabricating outcome."""
        fact = _fact(subjects=(_ENTITY_A, _ENTITY_B), objects=())

        result = _classify(fact)

        assert isinstance(result, SemanticEdgeRouting)
        assert result.edges == ()

    def test_index_reverse_true_with_blank_predicate_raises_via_semantic_edge_invariant(
        self,
    ) -> None:
        """classify() performs no separate, duplicate validation -- SemanticEdge's own
        __post_init__ invariant is the enforcement point."""
        fact = _fact(subjects=(_ENTITY_A,), objects=(_PLACE_A,), predicate="", index_reverse=True)

        with pytest.raises(ValueError, match="predicate"):
            _classify(fact)

    def test_zero_subjects_with_blank_statement_raises_via_general_fact_invariant(self) -> None:
        fact = _fact(subjects=(), objects=(), statement="   ", subject_scope="global")

        with pytest.raises(ValueError, match="statement"):
            _classify(fact)

    def test_zero_subjects_with_blank_subject_scope_raises_via_general_fact_invariant(
        self,
    ) -> None:
        fact = _fact(subjects=(), objects=(), statement="<PII_EXAMPLE_REDACTED>", subject_scope="")

        with pytest.raises(ValueError, match="subject_scope"):
            _classify(fact)

    def test_multi_subject_with_blank_predicate_raises_via_semantic_edge_invariant(self) -> None:
        fact = _fact(subjects=(_ENTITY_A, _ENTITY_B), objects=(_PLACE_A,), predicate="")

        with pytest.raises(ValueError, match="predicate"):
            _classify(fact)

    def test_edge_ids_come_from_the_injected_factory_in_call_order(self) -> None:
        fact = _fact(subjects=(_ENTITY_A, _ENTITY_B), objects=(_PLACE_A,))

        result = _classify(fact)

        assert isinstance(result, SemanticEdgeRouting)
        assert {edge.edge_id for edge in result.edges} == {"edge-1", "edge-2"}


class TestResultTypeUnionShape:
    """Structural check on the Result-type union itself (contract the dev report claims)."""

    def test_result_union_has_exactly_the_three_documented_members(self) -> None:
        import dashanan.domain.entity_ownership_specification as module

        for name in ("SemanticEdgeRouting", "GeneralFactRouting", "ZoneFiveOnlyRouting"):
            assert hasattr(module, name), f"Missing expected result-type member: {name}"

    def test_every_branch_returns_one_of_the_three_documented_result_types(self) -> None:
        cases = [
            _fact(subjects=(_ENTITY_A, _ENTITY_B), objects=(_PLACE_A,)),
            _fact(subjects=(), objects=(), statement="<PII_EXAMPLE_REDACTED>", subject_scope="global"),
            _fact(subjects=(_ENTITY_A,), objects=()),
            _fact(subjects=(_ENTITY_A,), objects=(_PLACE_A,), index_reverse=False),
            _fact(subjects=(_ENTITY_A,), objects=(_PLACE_A,), index_reverse=True),
        ]

        for fact in cases:
            result = _classify(fact)
            assert isinstance(result, (SemanticEdgeRouting, GeneralFactRouting, ZoneFiveOnlyRouting))
