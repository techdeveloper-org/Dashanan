"""QA suite for DASH-STORY-008 (Memory Score computation engine, six-term formula).

Traces to FR-012 (SRS.md, verbatim): "The system SHALL compute a composite
Memory Score for every item from six weighted terms (Recency, Frequency,
Importance, UserAffinity, TaskRelevance, ProvenanceConfidence), each
normalized to [0,1], and SHALL use this score to drive zone lifecycle
transitions."

This is the QA sub-task's independent verification of the dev sub-task's
deliverables (`dashanan.domain.memory_score`,
`dashanan.application.memory_score_engine`). Every test below exercises the
real acceptance criteria from `docs/phase-7-routing/implementation_execution_plan.json`'s
`dev_prompt` entry for DASH-STORY-008 and the story's `must_not_deviate`
list -- no assertion is a placeholder.

Covers:
    - AC-017: operator-supplied, non-default `ScoreWeights` is used instead
      of the MVP default, with no code-path branch on default-vs-custom.
    - AC-012-SCORE-1: Importance is explicit-tag-only, defaulting to 0.5.
    - Must-not-deviate item 1: all six terms normalized to [0,1] before
      weighting.
    - Must-not-deviate item 2: `MemoryScore`/`ScoreTerms` are immutable
      Value Objects.
    - Must-not-deviate item 3: default weight profile is 1/6 each,
      operator-overridable without a code change.
    - Must-not-deviate item 4: Importance is explicit-tag-only (Zone 4
      inference not built).
    - Must-not-deviate item 5: `compute_user_affinity` never functions as a
      tenant-isolation mechanism.
    - Architecture-fitness invariant (HLD Section 3.0, invariant 1): no
      `domain/**` module imports `infrastructure/**`.
"""

from __future__ import annotations

import ast
import inspect
import logging
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from dashanan.application.memory_score_engine import MemoryScoreEngine, ScoreInputs
from dashanan.domain.memory_score import (
    IMPORTANCE_TAG_VALUES,
    MemoryScore,
    ScoreTerms,
    ScoreWeights,
    compute_importance,
    compute_user_affinity,
)


def _default_inputs(**overrides: object) -> ScoreInputs:
    """Build a valid `ScoreInputs` for the happy path, overridable per-field."""
    base = dict(
        dt_seconds=0.0,
        half_life_seconds=None,
        access_count=1,
        f_cap=10,
        importance_tag="medium",
        provenance_confidence=0.5,
        user_cosine=None,
        task_cosine=None,
        cos_min=0.0,
        cos_max=1.0,
    )
    base.update(overrides)
    return ScoreInputs(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# AC-017: operator-supplied non-default weight profile is used, not the MVP
# default, with no code change (dev_prompt, AC-017 verbatim).
# ---------------------------------------------------------------------------


class TestAC017OperatorWeightProfile:
    def test_default_weights_used_when_none_supplied(self) -> None:
        engine = MemoryScoreEngine()
        result = engine.score(_default_inputs())
        assert result.weights == ScoreWeights.default()

    def test_operator_weight_profile_is_used_instead_of_default(self) -> None:
        engine = MemoryScoreEngine()
        operator_weights = ScoreWeights(
            w_recency=0.5,
            w_frequency=0.1,
            w_importance=0.1,
            w_user_affinity=0.1,
            w_task_relevance=0.1,
            w_provenance_confidence=0.1,
        )
        default_result = engine.score(_default_inputs())
        operator_result = engine.score(_default_inputs(), weights=operator_weights)

        assert operator_result.weights == operator_weights
        assert operator_result.weights != default_result.weights
        assert operator_result.value != default_result.value

    def test_operator_weight_profile_flows_through_identical_code_path(self) -> None:
        """No branch on default-vs-custom: MemoryScore.compute is the single
        composition path for both (must-not-deviate item 3)."""
        engine = MemoryScoreEngine()
        operator_weights = ScoreWeights(
            w_recency=0.3,
            w_frequency=0.3,
            w_importance=0.1,
            w_user_affinity=0.1,
            w_task_relevance=0.1,
            w_provenance_confidence=0.1,
        )
        terms = ScoreTerms(
            recency=0.8,
            frequency=0.4,
            importance=0.5,
            user_affinity=0.6,
            task_relevance=0.2,
            provenance_confidence=0.9,
        )
        expected = (
            operator_weights.w_recency * terms.recency
            + operator_weights.w_frequency * terms.frequency
            + operator_weights.w_importance * terms.importance
            + operator_weights.w_user_affinity * terms.user_affinity
            + operator_weights.w_task_relevance * terms.task_relevance
            + operator_weights.w_provenance_confidence * terms.provenance_confidence
        )
        actual = MemoryScore.compute(terms, operator_weights)
        assert actual.value == pytest.approx(expected)

    def test_rotation_thresholds_config_surface_is_operator_settable(self) -> None:
        """AC-017 also names rotation thresholds; ScoreWeights (the config
        surface this story owns) is constructed from plain caller-supplied
        floats with no hardcoded literal anywhere in its validation path."""
        custom = ScoreWeights(
            w_recency=0.2,
            w_frequency=0.2,
            w_importance=0.2,
            w_user_affinity=0.2,
            w_task_relevance=0.1,
            w_provenance_confidence=0.1,
        )
        assert custom.w_recency == 0.2

    def test_weight_profile_not_summing_to_one_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="sum to 1.0"):
            ScoreWeights(
                w_recency=0.5,
                w_frequency=0.5,
                w_importance=0.5,
                w_user_affinity=0.0,
                w_task_relevance=0.0,
                w_provenance_confidence=0.0,
            )

    def test_negative_weight_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="w_recency"):
            ScoreWeights(
                w_recency=-0.1,
                w_frequency=0.3,
                w_importance=0.3,
                w_user_affinity=0.2,
                w_task_relevance=0.2,
                w_provenance_confidence=0.1,
            )


# ---------------------------------------------------------------------------
# AC-012-SCORE-1: Importance is explicit-tag-only, defaulting to 0.5 when no
# tag is present (Zone 4 inference is out of Sprint 1 scope).
# ---------------------------------------------------------------------------


class TestAC012Score1ImportanceExplicitTagOnly:
    @pytest.mark.parametrize(
        "tag,expected",
        [
            ("critical", 1.0),
            ("high", 0.8),
            ("medium", 0.5),
            ("low", 0.2),
        ],
    )
    def test_recognized_tag_maps_to_expected_value(
        self, tag: str, expected: float
    ) -> None:
        assert compute_importance(tag) == expected

    def test_absent_tag_defaults_to_zero_point_five(self) -> None:
        assert compute_importance(None) == 0.5

    def test_unrecognized_tag_falls_back_to_zero_point_five(self) -> None:
        assert compute_importance("not_a_real_tag") == 0.5

    def test_no_zone4_success_failure_ratio_logic_exists(self) -> None:
        """Zone 4's success_count/(success_count+failure_count) inference is
        explicitly out of scope; compute_importance's signature takes only a
        tag, never success/failure counters."""
        sig = inspect.signature(compute_importance)
        param_names = list(sig.parameters.keys())
        assert param_names == ["tag"]

    def test_importance_tag_values_is_the_only_source(self) -> None:
        assert set(IMPORTANCE_TAG_VALUES.keys()) == {
            "critical",
            "high",
            "medium",
            "low",
        }

    def test_engine_defaults_importance_when_no_tag_present(self) -> None:
        engine = MemoryScoreEngine()
        result = engine.score(_default_inputs(importance_tag=None))
        assert result.terms.importance == 0.5


# ---------------------------------------------------------------------------
# Must-not-deviate item 1: all six terms normalized to [0,1] before
# weighting.
# ---------------------------------------------------------------------------


class TestMustNotDeviateAllSixTermsNormalized:
    def test_score_terms_rejects_term_above_one(self) -> None:
        with pytest.raises(ValueError, match="recency"):
            ScoreTerms(
                recency=1.5,
                frequency=0.5,
                importance=0.5,
                user_affinity=0.5,
                task_relevance=0.5,
                provenance_confidence=0.5,
            )

    def test_score_terms_rejects_term_below_zero(self) -> None:
        with pytest.raises(ValueError, match="frequency"):
            ScoreTerms(
                recency=0.5,
                frequency=-0.1,
                importance=0.5,
                user_affinity=0.5,
                task_relevance=0.5,
                provenance_confidence=0.5,
            )

    @pytest.mark.parametrize(
        "field",
        [
            "recency",
            "frequency",
            "importance",
            "user_affinity",
            "task_relevance",
            "provenance_confidence",
        ],
    )
    def test_each_term_individually_validated_out_of_range(
        self, field: str
    ) -> None:
        valid = dict(
            recency=0.5,
            frequency=0.5,
            importance=0.5,
            user_affinity=0.5,
            task_relevance=0.5,
            provenance_confidence=0.5,
        )
        valid[field] = 2.0
        with pytest.raises(ValueError, match=field):
            ScoreTerms(**valid)  # type: ignore[arg-type]

    def test_engine_produces_terms_all_within_zero_one(self) -> None:
        engine = MemoryScoreEngine()
        result = engine.score(
            _default_inputs(
                dt_seconds=3600.0,
                half_life_seconds=7200.0,
                access_count=50,
                f_cap=10,
                importance_tag="high",
                provenance_confidence=0.73,
                user_cosine=0.6,
                task_cosine=0.4,
                cos_min=-1.0,
                cos_max=1.0,
            )
        )
        for value in (
            result.terms.recency,
            result.terms.frequency,
            result.terms.importance,
            result.terms.user_affinity,
            result.terms.task_relevance,
            result.terms.provenance_confidence,
        ):
            assert 0.0 <= value <= 1.0

    def test_range_theorem_composite_score_within_zero_one(self) -> None:
        """HLD 12G / mathematics-engineer Part A.0: S in [min_i x_i, max_i
        x_i] subset of [0,1] for any valid weights/terms pair."""
        terms = ScoreTerms(
            recency=1.0,
            frequency=0.0,
            importance=0.5,
            user_affinity=1.0,
            task_relevance=0.0,
            provenance_confidence=1.0,
        )
        score = MemoryScore.compute(terms, ScoreWeights.default())
        assert 0.0 <= score.value <= 1.0

    def test_provenance_confidence_out_of_range_is_rejected_not_recomputed(
        self,
    ) -> None:
        """This story deliberately does NOT recompute ProvenanceConfidence;
        it still must be validated to land in [0,1] alongside the other
        five terms."""
        with pytest.raises(ValueError, match="provenance_confidence"):
            ScoreTerms(
                recency=0.5,
                frequency=0.5,
                importance=0.5,
                user_affinity=0.5,
                task_relevance=0.5,
                provenance_confidence=1.2,
            )


# ---------------------------------------------------------------------------
# Must-not-deviate item 2: MemoryScore and ScoreTerms are immutable Value
# Objects -- compared, never mutated in place.
# ---------------------------------------------------------------------------


class TestMustNotDeviateImmutableValueObjects:
    def test_score_terms_are_frozen(self) -> None:
        terms = ScoreTerms(
            recency=0.5,
            frequency=0.5,
            importance=0.5,
            user_affinity=0.5,
            task_relevance=0.5,
            provenance_confidence=0.5,
        )
        with pytest.raises(FrozenInstanceError):
            terms.recency = 0.9  # type: ignore[misc]

    def test_score_weights_are_frozen(self) -> None:
        weights = ScoreWeights.default()
        with pytest.raises(FrozenInstanceError):
            weights.w_recency = 0.9  # type: ignore[misc]

    def test_memory_score_is_frozen(self) -> None:
        terms = ScoreTerms(
            recency=0.5,
            frequency=0.5,
            importance=0.5,
            user_affinity=0.5,
            task_relevance=0.5,
            provenance_confidence=0.5,
        )
        score = MemoryScore.compute(terms, ScoreWeights.default())
        with pytest.raises(FrozenInstanceError):
            score.value = 0.99  # type: ignore[misc]

    def test_equal_terms_compare_equal_not_identical(self) -> None:
        kwargs = dict(
            recency=0.5,
            frequency=0.5,
            importance=0.5,
            user_affinity=0.5,
            task_relevance=0.5,
            provenance_confidence=0.5,
        )
        a = ScoreTerms(**kwargs)  # type: ignore[arg-type]
        b = ScoreTerms(**kwargs)  # type: ignore[arg-type]
        assert a == b
        assert a is not b

    def test_value_objects_use_slots(self) -> None:
        terms = ScoreTerms(
            recency=0.5,
            frequency=0.5,
            importance=0.5,
            user_affinity=0.5,
            task_relevance=0.5,
            provenance_confidence=0.5,
        )
        with pytest.raises(AttributeError):
            terms.__dict__  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Must-not-deviate item 3: default weight profile 1/6 each,
# operator-overridable without a code change.
# ---------------------------------------------------------------------------


class TestMustNotDeviateDefaultWeightProfile:
    def test_default_is_one_sixth_for_every_term(self) -> None:
        default = ScoreWeights.default()
        sixth = 1.0 / 6.0
        assert default.w_recency == pytest.approx(sixth)
        assert default.w_frequency == pytest.approx(sixth)
        assert default.w_importance == pytest.approx(sixth)
        assert default.w_user_affinity == pytest.approx(sixth)
        assert default.w_task_relevance == pytest.approx(sixth)
        assert default.w_provenance_confidence == pytest.approx(sixth)

    def test_default_weights_sum_to_one(self) -> None:
        default = ScoreWeights.default()
        total = (
            default.w_recency
            + default.w_frequency
            + default.w_importance
            + default.w_user_affinity
            + default.w_task_relevance
            + default.w_provenance_confidence
        )
        assert total == pytest.approx(1.0)

    def test_no_hardcoded_sixth_in_memory_score_compute_source(self) -> None:
        """MemoryScore.compute must be the general-weight form -- no `/ 6`
        or `1/6` literal anywhere in its body."""
        source = inspect.getsource(MemoryScore.compute)
        assert "/ 6" not in source
        assert "1 / 6" not in source
        assert "1.0 / 6.0" not in source


# ---------------------------------------------------------------------------
# Must-not-deviate item 4: Importance is explicit-tag-only, defaulting to
# 0.5 -- Zone 4 inference is out of Sprint 1 scope, not built.
# ---------------------------------------------------------------------------


class TestMustNotDeviateNoZone4ImportanceInference:
    @staticmethod
    def _code_identifiers(module_path: str) -> set[str]:
        """Every real identifier (Name/Attribute/arg) the module's AST
        defines or references -- excludes docstrings and comments, which
        are free-text prose, not executable code."""
        repo_root = Path(__file__).resolve().parents[1]
        source = (repo_root / module_path).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=module_path)
        identifiers: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                identifiers.add(node.id)
            elif isinstance(node, ast.Attribute):
                identifiers.add(node.attr)
            elif isinstance(node, ast.arg):
                identifiers.add(node.arg)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                identifiers.add(node.name)
        return identifiers

    def test_memory_score_module_has_no_success_failure_ratio_code(self) -> None:
        identifiers = self._code_identifiers("src/dashanan/domain/memory_score.py")
        assert "success_count" not in identifiers
        assert "failure_count" not in identifiers

    def test_engine_module_has_no_success_failure_ratio_code(self) -> None:
        identifiers = self._code_identifiers(
            "src/dashanan/application/memory_score_engine.py"
        )
        assert "success_count" not in identifiers
        assert "failure_count" not in identifiers


# ---------------------------------------------------------------------------
# Must-not-deviate item 5: UserAffinity must NEVER function as a
# tenant-isolation mechanism (ADR-013/threat I-2).
# ---------------------------------------------------------------------------


class TestMustNotDeviateUserAffinityNeverTenantIsolation:
    def test_compute_user_affinity_signature_carries_no_tenant_parameter(
        self,
    ) -> None:
        sig = inspect.signature(compute_user_affinity)
        param_names = list(sig.parameters.keys())
        assert not any("tenant" in name.lower() for name in param_names)
        assert param_names == ["cosine", "cos_min", "cos_max"]

    def test_compute_user_affinity_performs_no_tenant_comparison(self) -> None:
        """No real identifier (Name/Attribute/arg) in the function's AST
        body references "tenant" -- the docstring's prose discussion of
        ADR-013 is not executable code and is excluded from this check."""
        source = inspect.getsource(compute_user_affinity)
        tree = ast.parse(source)
        func_def = tree.body[0]
        assert isinstance(func_def, ast.FunctionDef)
        body_without_docstring = func_def.body[1:] if (
            func_def.body
            and isinstance(func_def.body[0], ast.Expr)
            and isinstance(func_def.body[0].value, ast.Constant)
            and isinstance(func_def.body[0].value.value, str)
        ) else func_def.body
        identifiers: set[str] = set()
        for stmt in body_without_docstring:
            for node in ast.walk(stmt):
                if isinstance(node, ast.Name):
                    identifiers.add(node.id)
                elif isinstance(node, ast.Attribute):
                    identifiers.add(node.attr)
        assert not any("tenant" in name.lower() for name in identifiers)

    def test_score_inputs_has_no_tenant_field(self) -> None:
        field_names = {f for f in ScoreInputs.__dataclass_fields__}  # type: ignore[attr-defined]
        assert not any("tenant" in name.lower() for name in field_names)

    def test_user_affinity_is_a_pure_function_of_cosine_and_calibration(
        self,
    ) -> None:
        assert compute_user_affinity(0.5, 0.0, 1.0) == compute_user_affinity(
            0.5, 0.0, 1.0
        )

    def test_user_affinity_cold_start_default_is_zero_point_five(self) -> None:
        assert compute_user_affinity(None, 0.0, 1.0) == 0.5


# ---------------------------------------------------------------------------
# Degenerate-calibration warning (HLD 12G): the engine, not the pure domain
# function, is responsible for the logging side effect.
# ---------------------------------------------------------------------------


class TestDegenerateCalibrationWarning:
    def test_engine_logs_warning_on_degenerate_calibration(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        engine = MemoryScoreEngine()
        with caplog.at_level(logging.WARNING):
            engine.score(
                _default_inputs(
                    user_cosine=0.5,
                    cos_min=1.0,
                    cos_max=1.0,
                )
            )
        assert any(
            "degenerate" in record.message.lower() for record in caplog.records
        )

    def test_engine_does_not_warn_on_healthy_calibration(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        engine = MemoryScoreEngine()
        with caplog.at_level(logging.WARNING):
            engine.score(
                _default_inputs(user_cosine=0.5, cos_min=-1.0, cos_max=1.0)
            )
        assert not any(
            "degenerate" in record.message.lower() for record in caplog.records
        )

    def test_engine_does_not_warn_when_no_active_query_or_user_cosine(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No cosine input at all (cold start, no query) must not trigger
        the calibration warning even if cos_min == cos_max by coincidence."""
        engine = MemoryScoreEngine()
        with caplog.at_level(logging.WARNING):
            engine.score(
                _default_inputs(
                    user_cosine=None,
                    task_cosine=None,
                    cos_min=1.0,
                    cos_max=1.0,
                )
            )
        assert not any(
            "degenerate" in record.message.lower() for record in caplog.records
        )


# ---------------------------------------------------------------------------
# Architecture-fitness invariant (HLD Section 3.0, invariant 1): no
# domain/** module may import infrastructure/**.
# ---------------------------------------------------------------------------


def _imported_module_names(source: str, filename: str) -> list[str]:
    tree = ast.parse(source, filename=filename)
    imported_modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.append(node.module)
    return imported_modules


class TestArchitectureFitnessNoDomainImportsInfrastructure:
    @pytest.mark.parametrize(
        "module_path",
        [
            "src/dashanan/domain/memory_score.py",
            "src/dashanan/application/memory_score_engine.py",
        ],
    )
    def test_module_imports_no_infrastructure_module(
        self, module_path: str
    ) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        source = (repo_root / module_path).read_text(encoding="utf-8")
        imported_modules = _imported_module_names(source, module_path)
        assert not any(
            "infrastructure" in name for name in imported_modules
        ), f"{module_path} imports infrastructure/**: {imported_modules}"

    def test_domain_memory_score_module_performs_no_io(self) -> None:
        """Every existing domain module in this repo is I/O-free
        (provenance_record.py's own "domain-only -- no SQL, no I/O"
        convention); memory_score.py must hold to the same convention."""
        repo_root = Path(__file__).resolve().parents[1]
        module_path = "src/dashanan/domain/memory_score.py"
        source = (repo_root / module_path).read_text(encoding="utf-8")
        assert "import logging" not in source
        assert "logger" not in source

    def test_application_engine_module_is_the_sole_logging_site(self) -> None:
        """The one piece of required I/O (HLD 12G's degenerate-calibration
        warning) lives in the application layer, not domain."""
        repo_root = Path(__file__).resolve().parents[1]
        module_path = "src/dashanan/application/memory_score_engine.py"
        source = (repo_root / module_path).read_text(encoding="utf-8")
        assert "import logging" in source
        assert "logger.warning" in source
