"""Smoke assertions for DASH-STORY-008, inline per the dev subtask scope.

The formal pytest suite covering FR-012/AC-017/AC-012-SCORE-1 in full is
the QA subtask's responsibility (see the story's `qa_prompt`) -- the exact
scope split `tests/test_smoke_write_gate.py` used for DASH-STORY-006 and
`tests/test_smoke_provenance.py` used for DASH-STORY-005. These checks
confirm the package is importable, wired correctly, reproduce the
mathematics-engineer delegation's own worked numeric examples (Examples A
and B, AR1-008), and confirm the must-not-deviate structural properties
hold.

PII NOTE: per the dev_prompt's PII constraint, every value in this suite is
an abstract numeric term (a cosine similarity, an access count, a decay
half-life) -- no conversational content or item payload, real or
synthetic-realistic, appears anywhere below.
"""

from __future__ import annotations

import inspect
import math
from dataclasses import FrozenInstanceError

import pytest

from dashanan.application.memory_score_engine import MemoryScoreEngine, ScoreInputs
from dashanan.domain.memory_score import (
    IMPORTANCE_TAG_VALUES,
    TASK_RELEVANCE_NO_QUERY_DEFAULT,
    USER_AFFINITY_COLD_START_DEFAULT,
    MemoryScore,
    ScoreTerms,
    ScoreWeights,
    clamp01,
    compute_frequency,
    compute_importance,
    compute_recency,
    compute_task_relevance,
    compute_user_affinity,
    is_degenerate_calibration,
    lambda_from_half_life,
)

_ZONE2_T_HALF_SECONDS = 172_800.0
_ZONE2_LAMBDA = 4.011268e-06
"""HLD Section 12A worked value: `ln(2)/172800 = 4.011268e-06`."""


class TestPackageWiring:
    """Baseline: the domain module and application facade import and construct."""

    def test_score_weights_default_constructs(self) -> None:
        assert ScoreWeights.default() is not None

    def test_memory_score_engine_constructs(self) -> None:
        assert MemoryScoreEngine() is not None


class TestClampAndLambdaDerivation:
    """`clamp01` and `lambda_from_half_life` (HLD Section 12G/12C)."""

    def test_clamp01_passes_through_in_range_values(self) -> None:
        assert clamp01(0.42) == pytest.approx(0.42)

    def test_clamp01_clamps_above_one(self) -> None:
        assert clamp01(1.5) == 1.0

    def test_clamp01_clamps_below_zero(self) -> None:
        assert clamp01(-0.5) == 0.0

    def test_zone2_half_life_matches_hld_worked_value(self) -> None:
        assert lambda_from_half_life(_ZONE2_T_HALF_SECONDS) == pytest.approx(
            _ZONE2_LAMBDA, rel=1e-6
        )

    def test_none_half_life_derives_zero_lambda_zone7_never_decays(self) -> None:
        assert lambda_from_half_life(None) == 0.0

    def test_zero_half_life_raises(self) -> None:
        with pytest.raises(ValueError, match="t_half_seconds"):
            lambda_from_half_life(0.0)

    def test_negative_half_life_raises(self) -> None:
        with pytest.raises(ValueError, match="t_half_seconds"):
            lambda_from_half_life(-1.0)


class TestRecencyTerm:
    """Recency = exp(-lambda * max(0, dt)) (HLD Section 12C, math delegation Part A.1)."""

    def test_zero_dt_gives_recency_one(self) -> None:
        assert compute_recency(0.0, _ZONE2_LAMBDA) == pytest.approx(1.0)

    def test_zero_lambda_gives_recency_one_for_any_dt_zone7(self) -> None:
        assert compute_recency(dt_seconds=1_000_000.0, lambda_zone=0.0) == 1.0

    def test_negative_dt_clock_skew_is_clamped_not_a_score_above_one(self) -> None:
        skewed = compute_recency(dt_seconds=-500.0, lambda_zone=_ZONE2_LAMBDA)
        assert skewed == pytest.approx(1.0)
        assert skewed <= 1.0

    def test_one_half_life_gives_recency_one_half_exactly(self) -> None:
        lam = lambda_from_half_life(_ZONE2_T_HALF_SECONDS)
        assert compute_recency(_ZONE2_T_HALF_SECONDS, lam) == pytest.approx(
            0.5, rel=1e-9
        )

    def test_negative_lambda_raises(self) -> None:
        with pytest.raises(ValueError, match="lambda_zone"):
            compute_recency(0.0, -0.1)


class TestFrequencyTerm:
    """Frequency = min(1, log(1+a)/log(1+f_cap)) (HLD Section 12G, math delegation Part A.2)."""

    def test_zero_access_count_is_zero(self) -> None:
        assert compute_frequency(0, 100) == 0.0

    def test_access_count_equal_to_cap_saturates_at_one(self) -> None:
        assert compute_frequency(100, 100) == pytest.approx(1.0)

    def test_access_count_beyond_cap_stays_capped_at_one(self) -> None:
        assert compute_frequency(10_000, 100) == 1.0

    def test_worked_example_a_access_count_4_f_cap_100(self) -> None:
        """Math delegation Example A: F = ln(5)/ln(101) = 0.3487315."""
        assert compute_frequency(4, 100) == pytest.approx(0.3487315, rel=1e-6)

    def test_ratchet_pin_boundary_9_reads_below_half(self) -> None:
        """Math delegation Example C: F(9) = 0.4989220 < 0.5."""
        assert compute_frequency(9, 100) == pytest.approx(0.4989220, rel=1e-6)

    def test_ratchet_pin_boundary_10_reads_at_or_above_half(self) -> None:
        """Math delegation Example C: F(10) = 0.5195737 >= 0.5."""
        assert compute_frequency(10, 100) == pytest.approx(0.5195737, rel=1e-6)

    def test_negative_access_count_raises(self) -> None:
        with pytest.raises(ValueError, match="access_count"):
            compute_frequency(-1, 100)

    def test_f_cap_zero_raises_instead_of_dividing_by_zero(self) -> None:
        with pytest.raises(ValueError, match="f_cap"):
            compute_frequency(5, 0)

    def test_f_cap_negative_raises(self) -> None:
        with pytest.raises(ValueError, match="f_cap"):
            compute_frequency(5, -1)


class TestImportanceTermAC012Score1:
    """AC-012-SCORE-1: Importance is explicit-tag-only, defaulting to 0.5."""

    @pytest.mark.parametrize(
        ("tag", "expected"),
        [
            ("critical", 1.0),
            ("high", 0.8),
            ("medium", 0.5),
            ("low", 0.2),
        ],
    )
    def test_recognized_tags_map_to_hld_fixed_values(
        self, tag: str, expected: float
    ) -> None:
        assert compute_importance(tag) == expected

    def test_absent_tag_defaults_to_medium_zero_point_five(self) -> None:
        assert compute_importance(None) == 0.5

    def test_unrecognized_tag_falls_back_to_default_not_an_error(self) -> None:
        assert compute_importance("not_a_real_tag") == 0.5

    def test_importance_is_never_zero_in_sprint_1(self) -> None:
        """Math delegation Part A.3: minimum attainable value is 0.2 ('low')."""
        assert min(IMPORTANCE_TAG_VALUES.values()) == 0.2
        assert 0.0 not in IMPORTANCE_TAG_VALUES.values()


class TestUserAffinityAndTaskRelevanceTerms:
    """HLD Section 12G (OAQ-19 Resolved: Adopted); shared calibration code path."""

    def test_worked_example_a_user_affinity(self) -> None:
        """Math delegation Example A: (0.62-0.05)/0.80 = 0.7125."""
        assert compute_user_affinity(0.62, cos_min=0.05, cos_max=0.85) == pytest.approx(
            0.7125, rel=1e-6
        )

    def test_worked_example_a_task_relevance(self) -> None:
        """Math delegation Example A: (0.41-0.05)/0.80 = 0.45."""
        assert compute_task_relevance(0.41, cos_min=0.05, cos_max=0.85) == pytest.approx(
            0.45, rel=1e-6
        )

    def test_user_affinity_cold_start_default(self) -> None:
        assert (
            compute_user_affinity(None, cos_min=0.05, cos_max=0.85)
            == USER_AFFINITY_COLD_START_DEFAULT
        )

    def test_task_relevance_no_active_query_default(self) -> None:
        assert (
            compute_task_relevance(None, cos_min=0.05, cos_max=0.85)
            == TASK_RELEVANCE_NO_QUERY_DEFAULT
        )

    def test_is_degenerate_calibration_true_when_min_equals_max(self) -> None:
        assert is_degenerate_calibration(0.5, 0.5) is True

    def test_is_degenerate_calibration_false_otherwise(self) -> None:
        assert is_degenerate_calibration(0.05, 0.85) is False

    def test_degenerate_calibration_falls_back_to_cosine_plus_one_over_two(
        self,
    ) -> None:
        result = compute_user_affinity(0.3, cos_min=0.5, cos_max=0.5)
        assert result == pytest.approx((0.3 + 1.0) / 2.0)

    def test_user_affinity_and_task_relevance_share_identical_calibration_math(
        self,
    ) -> None:
        """HLD Section 12G: 'the two formulas must never diverge on this point'."""
        cosine, cos_min, cos_max = 0.55, 0.10, 0.90
        assert compute_user_affinity(
            cosine, cos_min=cos_min, cos_max=cos_max
        ) == compute_task_relevance(cosine, cos_min=cos_min, cos_max=cos_max)

    def test_result_is_always_clamped_to_unit_interval_even_outside_calibration(
        self,
    ) -> None:
        below = compute_user_affinity(-0.9, cos_min=0.05, cos_max=0.85)
        above = compute_task_relevance(2.0, cos_min=0.05, cos_max=0.85)
        assert below == 0.0
        assert above == 1.0


class TestMustNotDeviateUserAffinityNeverTenantIsolation:
    """Must-not-deviate item 5 (ADR-013 / threat I-2): purely numeric, no tenant concept."""

    def test_compute_user_affinity_signature_carries_no_tenant_parameter(self) -> None:
        params = inspect.signature(compute_user_affinity).parameters
        assert not any("tenant" in name.lower() for name in params)

    def test_compute_user_affinity_performs_no_tenant_comparison(self) -> None:
        """Structural: the same cosine/calibration pair always yields the same
        term value regardless of any caller-side tenant context, because the
        function has no tenant input to vary on."""
        first = compute_user_affinity(0.62, cos_min=0.05, cos_max=0.85)
        second = compute_user_affinity(0.62, cos_min=0.05, cos_max=0.85)
        assert first == second


class TestScoreWeightsValidation:
    """AC-017 / NFR-009: operator-overridable weight profile, structurally validated."""

    def test_default_is_one_sixth_each(self) -> None:
        weights = ScoreWeights.default()
        sixth = 1.0 / 6.0
        assert weights.w_recency == pytest.approx(sixth)
        assert weights.w_frequency == pytest.approx(sixth)
        assert weights.w_importance == pytest.approx(sixth)
        assert weights.w_user_affinity == pytest.approx(sixth)
        assert weights.w_task_relevance == pytest.approx(sixth)
        assert weights.w_provenance_confidence == pytest.approx(sixth)

    def test_default_weights_sum_to_one(self) -> None:
        weights = ScoreWeights.default()
        total = (
            weights.w_recency
            + weights.w_frequency
            + weights.w_importance
            + weights.w_user_affinity
            + weights.w_task_relevance
            + weights.w_provenance_confidence
        )
        assert total == pytest.approx(1.0, abs=1e-9)

    def test_operator_profile_summing_to_one_is_accepted(self) -> None:
        weights = ScoreWeights(
            w_recency=0.5,
            w_frequency=0.1,
            w_importance=0.1,
            w_user_affinity=0.1,
            w_task_relevance=0.1,
            w_provenance_confidence=0.1,
        )
        assert weights.w_recency == 0.5

    def test_weights_not_summing_to_one_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="sum to 1.0"):
            ScoreWeights(
                w_recency=0.5,
                w_frequency=0.5,
                w_importance=0.5,
                w_user_affinity=0.0,
                w_task_relevance=0.0,
                w_provenance_confidence=0.0,
            )

    def test_negative_weight_is_rejected_independent_of_sum(self) -> None:
        with pytest.raises(ValueError, match="w_recency"):
            ScoreWeights(
                w_recency=-0.1,
                w_frequency=0.22,
                w_importance=0.22,
                w_user_affinity=0.22,
                w_task_relevance=0.22,
                w_provenance_confidence=0.22,
            )

    def test_weights_are_immutable(self) -> None:
        weights = ScoreWeights.default()
        with pytest.raises(FrozenInstanceError):
            weights.w_recency = 0.9  # type: ignore[misc]


class TestScoreTermsValidation:
    """Must-not-deviate item 1: all six terms normalized to [0,1] before weighting."""

    def _valid_terms(self, **overrides: float) -> dict[str, float]:
        base = {
            "recency": 0.5,
            "frequency": 0.5,
            "importance": 0.5,
            "user_affinity": 0.5,
            "task_relevance": 0.5,
            "provenance_confidence": 0.5,
        }
        base.update(overrides)
        return base

    def test_all_terms_at_midpoint_is_valid(self) -> None:
        terms = ScoreTerms(**self._valid_terms())
        assert terms.recency == 0.5

    def test_all_terms_at_zero_is_valid(self) -> None:
        terms = ScoreTerms(**self._valid_terms(recency=0.0, frequency=0.0))
        assert terms.frequency == 0.0

    def test_all_terms_at_one_is_valid(self) -> None:
        terms = ScoreTerms(**self._valid_terms(importance=1.0))
        assert terms.importance == 1.0

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
    def test_each_term_above_one_is_rejected(self, field: str) -> None:
        with pytest.raises(ValueError, match=field):
            ScoreTerms(**self._valid_terms(**{field: 1.5}))

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
    def test_each_term_below_zero_is_rejected(self, field: str) -> None:
        with pytest.raises(ValueError, match=field):
            ScoreTerms(**self._valid_terms(**{field: -0.5}))

    def test_score_terms_are_immutable(self) -> None:
        terms = ScoreTerms(**self._valid_terms())
        with pytest.raises(FrozenInstanceError):
            terms.recency = 0.9  # type: ignore[misc]


class TestWorkedExampleAMemoryScoreComposite:
    """Reproduces mathematics-engineer delegation Example A verbatim (AR1-008)."""

    def _example_a_terms(self) -> ScoreTerms:
        return ScoreTerms(
            recency=1.0,
            frequency=compute_frequency(4, 100),
            importance=compute_importance("medium"),
            user_affinity=compute_user_affinity(0.62, cos_min=0.05, cos_max=0.85),
            task_relevance=compute_task_relevance(0.41, cos_min=0.05, cos_max=0.85),
            provenance_confidence=0.6,
        )

    def test_composite_matches_delegation_worked_value(self) -> None:
        score = MemoryScore.compute(self._example_a_terms(), ScoreWeights.default())
        assert score.value == pytest.approx(0.6018719, rel=1e-6)

    def test_composite_falls_in_active_band_not_promote_not_compress(self) -> None:
        """HLD Section 12A: PromoteThreshold=0.70, CompressThreshold=0.35."""
        score = MemoryScore.compute(self._example_a_terms(), ScoreWeights.default())
        assert 0.35 <= score.value < 0.70

    def test_memory_score_is_immutable(self) -> None:
        score = MemoryScore.compute(self._example_a_terms(), ScoreWeights.default())
        with pytest.raises(FrozenInstanceError):
            score.value = 1.0  # type: ignore[misc]

    def test_memory_score_carries_terms_and_weights_for_score_explain(self) -> None:
        """HLD Section 7.1 score:explain must reproduce from this object alone."""
        terms = self._example_a_terms()
        weights = ScoreWeights.default()
        score = MemoryScore.compute(terms, weights)
        assert score.terms is terms
        assert score.weights is weights


class TestWorkedExampleBExactHalfLifeBoundary:
    """Reproduces mathematics-engineer delegation Example B's closed-form check.

    'u = 0.5 <=> dt* = exactly one half-life' is a free unit test that
    requires no tolerance and catches any lambda-rounding regression.
    """

    def _example_b_terms_at(self, dt_seconds: float) -> ScoreTerms:
        lam = lambda_from_half_life(_ZONE2_T_HALF_SECONDS)
        return ScoreTerms(
            recency=compute_recency(dt_seconds, lam),
            frequency=compute_frequency(0, 100),
            importance=compute_importance("medium"),
            user_affinity=compute_user_affinity(None, cos_min=0.05, cos_max=0.85),
            task_relevance=compute_task_relevance(None, cos_min=0.05, cos_max=0.85),
            provenance_confidence=0.6,
        )

    def test_score_at_dt_zero_matches_delegation_worked_value(self) -> None:
        score = MemoryScore.compute(
            self._example_b_terms_at(0.0), ScoreWeights.default()
        )
        assert score.value == pytest.approx(0.4333333, rel=1e-6)

    def test_score_at_exactly_one_half_life_equals_compress_threshold_exactly(
        self,
    ) -> None:
        score = MemoryScore.compute(
            self._example_b_terms_at(_ZONE2_T_HALF_SECONDS), ScoreWeights.default()
        )
        assert score.value == pytest.approx(0.35, rel=1e-9)


class TestMemoryScoreEngineIntegration:
    """`MemoryScoreEngine.score` end-to-end, via `ScoreInputs`."""

    def _example_a_inputs(self) -> ScoreInputs:
        return ScoreInputs(
            dt_seconds=0.0,
            half_life_seconds=_ZONE2_T_HALF_SECONDS,
            access_count=4,
            f_cap=100,
            importance_tag="medium",
            provenance_confidence=0.6,
            user_cosine=0.62,
            task_cosine=0.41,
            cos_min=0.05,
            cos_max=0.85,
        )

    def test_engine_reproduces_example_a_composite(self) -> None:
        engine = MemoryScoreEngine()
        score = engine.score(self._example_a_inputs())
        assert score.value == pytest.approx(0.6018719, rel=1e-6)

    def test_engine_uses_default_weights_when_none_supplied(self) -> None:
        engine = MemoryScoreEngine()
        score = engine.score(self._example_a_inputs())
        assert score.weights == ScoreWeights.default()

    def test_engine_uses_operator_supplied_weights_ac017(self) -> None:
        """AC-017: operator-set weight profile is used instead of MVP defaults."""
        engine = MemoryScoreEngine()
        recency_heavy = ScoreWeights(
            w_recency=0.75,
            w_frequency=0.05,
            w_importance=0.05,
            w_user_affinity=0.05,
            w_task_relevance=0.05,
            w_provenance_confidence=0.05,
        )
        default_score = engine.score(self._example_a_inputs())
        overridden_score = engine.score(self._example_a_inputs(), weights=recency_heavy)

        assert overridden_score.weights == recency_heavy
        assert overridden_score.value != pytest.approx(default_score.value)

    def test_engine_zone7_never_decays_item_scores_recency_one(self) -> None:
        engine = MemoryScoreEngine()
        inputs = ScoreInputs(
            dt_seconds=999_999.0,
            half_life_seconds=None,
            access_count=0,
            f_cap=100,
            importance_tag=None,
            provenance_confidence=1.0,
            user_cosine=None,
            task_cosine=None,
            cos_min=0.05,
            cos_max=0.85,
        )
        score = engine.score(inputs)
        assert score.terms.recency == 1.0

    def test_engine_logs_warning_on_degenerate_calibration(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        engine = MemoryScoreEngine()
        inputs = ScoreInputs(
            dt_seconds=0.0,
            half_life_seconds=_ZONE2_T_HALF_SECONDS,
            access_count=1,
            f_cap=100,
            importance_tag="medium",
            provenance_confidence=0.6,
            user_cosine=0.3,
            task_cosine=None,
            cos_min=0.5,
            cos_max=0.5,
            )
        with caplog.at_level("WARNING"):
            engine.score(inputs)

        assert any(
            "degenerate cosine calibration" in record.message
            for record in caplog.records
        )

    def test_engine_does_not_warn_when_calibration_is_not_degenerate(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        engine = MemoryScoreEngine()
        with caplog.at_level("WARNING"):
            engine.score(self._example_a_inputs())

        assert not any(
            "degenerate cosine calibration" in record.message
            for record in caplog.records
        )

    def test_engine_propagates_value_error_for_invalid_f_cap(self) -> None:
        engine = MemoryScoreEngine()
        inputs = ScoreInputs(
            dt_seconds=0.0,
            half_life_seconds=_ZONE2_T_HALF_SECONDS,
            access_count=1,
            f_cap=0,
            importance_tag="medium",
            provenance_confidence=0.6,
            user_cosine=None,
            task_cosine=None,
            cos_min=0.05,
            cos_max=0.85,
        )
        with pytest.raises(ValueError, match="f_cap"):
            engine.score(inputs)


class TestRangeTheoremBoundaryProfiles:
    """HLD Section 12G / math delegation Part A.7: S in [min x_i, max x_i] subset [0,1]."""

    def test_all_terms_zero_gives_score_zero(self) -> None:
        terms = ScoreTerms(0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        score = MemoryScore.compute(terms, ScoreWeights.default())
        assert score.value == pytest.approx(0.0)

    def test_all_terms_one_gives_score_one(self) -> None:
        terms = ScoreTerms(1.0, 1.0, 1.0, 1.0, 1.0, 1.0)
        score = MemoryScore.compute(terms, ScoreWeights.default())
        assert score.value == pytest.approx(1.0)

    def test_mixed_terms_score_never_exceeds_max_term(self) -> None:
        terms = ScoreTerms(
            recency=0.9,
            frequency=0.1,
            importance=0.5,
            user_affinity=0.2,
            task_relevance=0.3,
            provenance_confidence=0.4,
        )
        score = MemoryScore.compute(terms, ScoreWeights.default())
        assert score.value <= max(
            terms.recency,
            terms.frequency,
            terms.importance,
            terms.user_affinity,
            terms.task_relevance,
            terms.provenance_confidence,
        )
        assert score.value >= min(
            terms.recency,
            terms.frequency,
            terms.importance,
            terms.user_affinity,
            terms.task_relevance,
            terms.provenance_confidence,
        )


class TestDefaultStaticFloorCrossCheck:
    """Math delegation Example D's 'DEFAULT' row (F=0,T=0,U=0.5 cold start): floor 0.300000."""

    def test_default_profile_floor_matches_delegation_table(self) -> None:
        terms = ScoreTerms(
            recency=0.0,
            frequency=compute_frequency(0, 100),
            importance=compute_importance(None),
            user_affinity=compute_user_affinity(None, cos_min=0.05, cos_max=0.85),
            task_relevance=compute_task_relevance(None, cos_min=0.05, cos_max=0.85),
            provenance_confidence=0.8,
        )
        floor = MemoryScore.compute(terms, ScoreWeights.default())
        assert floor.value == pytest.approx(0.300000, rel=1e-6)
