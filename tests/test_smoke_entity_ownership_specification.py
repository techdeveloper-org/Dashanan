"""Smoke assertions for DASH-STORY-013, inline per the dev subtask scope.

The formal pytest suite covering AC-003-CLASS-1/2/3/4 in full is the QA
subtask's responsibility (see the story's `qa_prompt`). These checks
confirm the package is importable, wired correctly, and that the
must-not-deviate structural properties (no Zone 5 write/stub anywhere in
this module, `index_reverse` defaulting to `False`, no attribute-value
copying into a `SemanticEdge`) hold, ahead of that formal suite landing --
the exact scope split `tests/test_smoke_semantic.py` used for
DASH-STORY-012.

Runtime assumptions recorded per rule 33/40 test-roadmap conventions:
  - clock: none needed -- this module carries no timestamp field and
    performs no I/O.
  - tenant_id: "tenant-1" for all requests unless a test states otherwise.
  - id factories: deterministic counters (`_counting_id_factory`), not
    `uuid4()`, so every test's expected IDs are reproducible without a
    fixture seed.

PII NOTE: per the dev_prompt's PII constraint, every illustration below
uses pseudonymized `<ENTITY_A>` / `<PLACE_A>` placeholders -- never HLD's
own worked example's realistic names ("Sachin", "Mumbai").
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable

import pytest

from dashanan.domain.entity_ownership_specification import (
    REASON_SINGLE_SUBJECT_INDEX_REVERSE_FALSE,
    REASON_SINGLE_SUBJECT_NO_OBJECT,
    CandidateFact,
    GeneralFactRouting,
    SemanticEdgeRouting,
    ZoneFiveOnlyRouting,
    classify,
)

_TENANT = "tenant-1"
_ENTITY_A = "<ENTITY_A>"
_ENTITY_B = "<ENTITY_B>"
_PLACE_A = "<PLACE_A>"
_PREDICATE = "located_in"


def _counting_id_factory(prefix: str) -> Callable[[], str]:
    """A deterministic `edge_id_factory`/`fact_id_factory`: `f"{prefix}-1"`, `f"{prefix}-2"`, ..."""
    counter = {"n": 0}

    def _next() -> str:
        counter["n"] += 1
        return f"{prefix}-{counter['n']}"

    return _next


def _fact(
    *,
    subjects: tuple[str, ...] = (),
    objects: tuple[str, ...] = (),
    predicate: str = _PREDICATE,
    statement: str = "",
    subject_scope: str = "",
    index_reverse: bool = False,
) -> CandidateFact:
    return CandidateFact(
        tenant_id=_TENANT,
        subjects=subjects,
        objects=objects,
        predicate=predicate,
        statement=statement,
        subject_scope=subject_scope,
        index_reverse=index_reverse,
    )


class TestPackageWiring:
    """Baseline: the module is importable and `classify` is callable."""

    def test_classify_is_callable(self) -> None:
        assert callable(classify)

    def test_result_type_union_has_exactly_three_members(self) -> None:
        import dashanan.domain.entity_ownership_specification as module

        result_names = {"SemanticEdgeRouting", "GeneralFactRouting", "ZoneFiveOnlyRouting"}
        for name in result_names:
            assert hasattr(module, name)


class TestAC003Class1MultiSubjectAndZeroSubjectRouting:
    """AC-003-CLASS-1: |subjects|>1 -> one edge per pair; |subjects|==0 -> GeneralFact."""

    def test_multiple_subjects_and_one_object_produce_one_edge_per_subject(self) -> None:
        fact = _fact(subjects=(_ENTITY_A, _ENTITY_B), objects=(_PLACE_A,))

        result = classify(
            fact,
            edge_id_factory=_counting_id_factory("edge"),
            fact_id_factory=_counting_id_factory("fact"),
        )

        assert isinstance(result, SemanticEdgeRouting)
        assert len(result.edges) == 2
        subject_refs = {edge.subject_ref for edge in result.edges}
        assert subject_refs == {_ENTITY_A, _ENTITY_B}
        assert all(edge.object_ref == _PLACE_A for edge in result.edges)
        assert all(edge.predicate == _PREDICATE for edge in result.edges)

    def test_multiple_subjects_and_multiple_objects_produce_the_full_cross_product(
        self,
    ) -> None:
        fact = _fact(subjects=(_ENTITY_A, _ENTITY_B), objects=(_PLACE_A, "<PLACE_B>"))

        result = classify(
            fact,
            edge_id_factory=_counting_id_factory("edge"),
            fact_id_factory=_counting_id_factory("fact"),
        )

        assert isinstance(result, SemanticEdgeRouting)
        assert len(result.edges) == 4
        pairs = {(edge.subject_ref, edge.object_ref) for edge in result.edges}
        assert pairs == {
            (_ENTITY_A, _PLACE_A),
            (_ENTITY_A, "<PLACE_B>"),
            (_ENTITY_B, _PLACE_A),
            (_ENTITY_B, "<PLACE_B>"),
        }

    def test_zero_subjects_routes_to_a_general_fact(self) -> None:
        fact = _fact(
            subjects=(),
            objects=(),
            statement="<PII_EXAMPLE_REDACTED>",
            subject_scope="global",
        )

        result = classify(
            fact,
            edge_id_factory=_counting_id_factory("edge"),
            fact_id_factory=_counting_id_factory("fact"),
        )

        assert isinstance(result, GeneralFactRouting)
        assert result.fact.tenant_id == _TENANT
        assert result.fact.fact_id == "fact-1"
        assert result.fact.statement == "<PII_EXAMPLE_REDACTED>"
        assert result.fact.subject_scope == "global"

    def test_zero_subjects_never_invokes_the_edge_id_factory(self) -> None:
        edge_calls = {"n": 0}

        def counting_edge_factory() -> str:
            edge_calls["n"] += 1
            return f"edge-{edge_calls['n']}"

        classify(
            _fact(statement="<PII_EXAMPLE_REDACTED>", subject_scope="global"),
            edge_id_factory=counting_edge_factory,
            fact_id_factory=_counting_id_factory("fact"),
        )

        assert edge_calls["n"] == 0


class TestAC003Class2SingleSubjectNoObjectIsZoneFiveOnly:
    """AC-003-CLASS-2: |subjects|==1, |objects|==0 -> no Zone 3 write, any index_reverse."""

    @pytest.mark.parametrize("index_reverse", [False, True])
    def test_single_subject_no_object_never_writes_to_zone_3(self, index_reverse: bool) -> None:
        fact = _fact(subjects=(_ENTITY_A,), objects=(), index_reverse=index_reverse)

        result = classify(
            fact,
            edge_id_factory=_counting_id_factory("edge"),
            fact_id_factory=_counting_id_factory("fact"),
        )

        assert isinstance(result, ZoneFiveOnlyRouting)
        assert result.reason == REASON_SINGLE_SUBJECT_NO_OBJECT

    def test_single_subject_no_object_never_invokes_either_id_factory(self) -> None:
        calls = {"edge": 0, "fact": 0}

        def edge_factory() -> str:
            calls["edge"] += 1
            return "unused"

        def fact_factory() -> str:
            calls["fact"] += 1
            return "unused"

        classify(
            _fact(subjects=(_ENTITY_A,), objects=()),
            edge_id_factory=edge_factory,
            fact_id_factory=fact_factory,
        )

        assert calls == {"edge": 0, "fact": 0}


class TestAC003Class3And4WorkedExampleConditionalReverseIndex:
    """AC-003-CLASS-3/4: the HLD worked example, index_reverse False vs True."""

    def test_index_reverse_defaults_to_false_on_candidate_fact(self) -> None:
        fields_by_name = {f.name: f for f in dataclasses.fields(CandidateFact)}
        assert fields_by_name["index_reverse"].default is False

    def test_worked_example_with_index_reverse_false_creates_no_semantic_edge(self) -> None:
        fact = _fact(subjects=(_ENTITY_A,), objects=(_PLACE_A,), index_reverse=False)

        result = classify(
            fact,
            edge_id_factory=_counting_id_factory("edge"),
            fact_id_factory=_counting_id_factory("fact"),
        )

        assert isinstance(result, ZoneFiveOnlyRouting)
        assert result.reason == REASON_SINGLE_SUBJECT_INDEX_REVERSE_FALSE

    def test_worked_example_with_default_omitted_index_reverse_creates_no_semantic_edge(
        self,
    ) -> None:
        """Confirms the *default* (not an explicit `False`) also takes this branch."""
        fact = CandidateFact(
            tenant_id=_TENANT,
            subjects=(_ENTITY_A,),
            objects=(_PLACE_A,),
            predicate=_PREDICATE,
        )

        result = classify(
            fact,
            edge_id_factory=_counting_id_factory("edge"),
            fact_id_factory=_counting_id_factory("fact"),
        )

        assert isinstance(result, ZoneFiveOnlyRouting)

    def test_worked_example_with_index_reverse_true_creates_exactly_one_semantic_edge(
        self,
    ) -> None:
        fact = _fact(subjects=(_ENTITY_A,), objects=(_PLACE_A,), index_reverse=True)

        result = classify(
            fact,
            edge_id_factory=_counting_id_factory("edge"),
            fact_id_factory=_counting_id_factory("fact"),
        )

        assert isinstance(result, SemanticEdgeRouting)
        assert len(result.edges) == 1
        edge = result.edges[0]
        assert edge.predicate == _PREDICATE
        assert edge.subject_ref == _ENTITY_A
        assert edge.object_ref == _PLACE_A

    def test_worked_example_edge_never_carries_a_copied_attribute_value(self) -> None:
        """Hard Rule 2 (HLD Section 3.10): subject_ref/object_ref are entity_id
        references only -- this asserts the produced edge's refs are exactly the
        opaque entity_id strings the caller supplied, nothing else."""
        fact = _fact(subjects=(_ENTITY_A,), objects=(_PLACE_A,), index_reverse=True)

        result = classify(
            fact,
            edge_id_factory=_counting_id_factory("edge"),
            fact_id_factory=_counting_id_factory("fact"),
        )

        assert isinstance(result, SemanticEdgeRouting)
        edge = result.edges[0]
        assert edge.subject_ref is fact.subjects[0]
        assert edge.object_ref is fact.objects[0]


class TestMustNotDeviateNoZoneFiveWriteAnywhere:
    """Must-not-deviate item 1: no Zone 5 write, stub, or fake anywhere in this module."""

    def test_module_imports_no_entity_or_zone_five_domain_type(self) -> None:
        import dashanan.domain.entity_ownership_specification as module

        source_names = {
            name
            for name in vars(module)
            if isinstance(getattr(module, name), type) and not name.startswith("_")
        }
        assert "Entity" not in source_names
        assert not any("zone5" in name.lower() or "entity_record" in name.lower() for name in source_names)

    def test_zone_five_only_routing_is_a_pure_marker_with_no_write_method(self) -> None:
        routing = ZoneFiveOnlyRouting(reason=REASON_SINGLE_SUBJECT_NO_OBJECT)
        assert not hasattr(routing, "write")
        assert not hasattr(routing, "persist")
        assert not hasattr(routing, "apply")


class TestBoundaryAndNegativeCases:
    """Boundary/negative cases per rule 33/40 test-roadmap conventions."""

    def test_candidate_fact_rejects_blank_tenant_id(self) -> None:
        with pytest.raises(ValueError, match="tenant_id"):
            CandidateFact(tenant_id="", subjects=(), objects=())

    def test_candidate_fact_rejects_blank_subject_entry(self) -> None:
        with pytest.raises(ValueError, match="subjects"):
            CandidateFact(tenant_id=_TENANT, subjects=("  ",), objects=())

    def test_candidate_fact_rejects_blank_object_entry(self) -> None:
        with pytest.raises(ValueError, match="objects"):
            CandidateFact(tenant_id=_TENANT, subjects=(_ENTITY_A,), objects=("",))

    def test_multi_subject_with_zero_objects_yields_an_empty_edge_tuple(self) -> None:
        """Judgment call (see dev report): HLD's else-branch does not itself
        guard |objects|==0, so the cross product is legitimately empty rather
        than an error."""
        fact = _fact(subjects=(_ENTITY_A, _ENTITY_B), objects=())

        result = classify(
            fact,
            edge_id_factory=_counting_id_factory("edge"),
            fact_id_factory=_counting_id_factory("fact"),
        )

        assert isinstance(result, SemanticEdgeRouting)
        assert result.edges == ()

    def test_index_reverse_true_with_blank_predicate_raises_via_semantic_edge_invariant(
        self,
    ) -> None:
        fact = _fact(
            subjects=(_ENTITY_A,), objects=(_PLACE_A,), predicate="", index_reverse=True
        )

        with pytest.raises(ValueError, match="predicate"):
            classify(
                fact,
                edge_id_factory=_counting_id_factory("edge"),
                fact_id_factory=_counting_id_factory("fact"),
            )

    def test_zero_subjects_with_blank_statement_raises_via_general_fact_invariant(
        self,
    ) -> None:
        fact = _fact(subjects=(), objects=(), statement="   ", subject_scope="global")

        with pytest.raises(ValueError, match="statement"):
            classify(
                fact,
                edge_id_factory=_counting_id_factory("edge"),
                fact_id_factory=_counting_id_factory("fact"),
            )

    def test_edge_ids_come_from_the_injected_factory_in_call_order(self) -> None:
        fact = _fact(subjects=(_ENTITY_A, _ENTITY_B), objects=(_PLACE_A,))

        result = classify(
            fact,
            edge_id_factory=_counting_id_factory("edge"),
            fact_id_factory=_counting_id_factory("fact"),
        )

        assert isinstance(result, SemanticEdgeRouting)
        assert {edge.edge_id for edge in result.edges} == {"edge-1", "edge-2"}
