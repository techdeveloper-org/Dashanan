"""Tests for AC-006-COST-1's cost report (DASH-STORY-007), against the
mathematics-engineer delegation's own worked numeric example (HLD 12D
Profile B, AR1-007). Every expected figure below is copied from that
delegation's "WORKED NUMERIC EXAMPLE" section, not independently derived
here -- this suite exists to prove the implementation reproduces the
delegated derivation, not to re-derive it.

PII NOTE: every input is a numeric cost/volume figure; no example
conversational content appears anywhere in this file.
"""

from __future__ import annotations

import pytest

from dashanan.application.retrieval_index_cost_estimator import (
    Zone6CostInputs,
    compute_zone6_cost_report,
    degraded_c_var,
    per_tenant_average_cost,
)

_PROFILE_B_ASSUMED_Q = 8_672_400.0
_PROFILE_B_ASSUMED_H_L2 = 0.25
_PROFILE_B_ASSUMED_C_FIXED_VECTOR = 280.0
_PROFILE_B_ASSUMED_C_FIXED_LEXICAL = 140.0
_PROFILE_B_ASSUMED_C_EMB = 2.0e-5
_PROFILE_B_ASSUMED_H_HOST = 0.30
_PROFILE_B_ASSUMED_H_L1 = 0.40


def _profile_b_inputs(total_assemble_requests: float = _PROFILE_B_ASSUMED_Q) -> Zone6CostInputs:
    return Zone6CostInputs(
        period_hours=730.0,
        total_assemble_requests=total_assemble_requests,
        l2_cache_hit_rate=_PROFILE_B_ASSUMED_H_L2,
        fixed_cost_vector=_PROFILE_B_ASSUMED_C_FIXED_VECTOR,
        fixed_cost_lexical=_PROFILE_B_ASSUMED_C_FIXED_LEXICAL,
        embedding_cost_per_call=_PROFILE_B_ASSUMED_C_EMB,
        host_supplied_embedding_rate=_PROFILE_B_ASSUMED_H_HOST,
        l1_cache_hit_rate=_PROFILE_B_ASSUMED_H_L1,
    )


class TestDenominatorDefinition:
    """Eq. 4: V = Q * (1 - h_L2), never Q itself."""

    def test_zone6_queries_excludes_l2_cache_hits(self) -> None:
        inputs = _profile_b_inputs()
        assert inputs.zone6_queries == pytest.approx(6_504_300.0)

    def test_zero_l2_hit_rate_makes_v_equal_q(self) -> None:
        inputs = Zone6CostInputs(
            period_hours=730.0, total_assemble_requests=1000.0, l2_cache_hit_rate=0.0,
            fixed_cost_vector=1.0, fixed_cost_lexical=1.0, embedding_cost_per_call=1e-5,
            host_supplied_embedding_rate=0.0, l1_cache_hit_rate=0.0,
        )
        assert inputs.zone6_queries == pytest.approx(1000.0)


class TestCVarComponents:
    """Eq. 5: c_var = c_emb*(1-h_host)*(1-h_L1) + c_vec_q + c_lex_q + c_egress."""

    def test_profile_b_c_var_matches_worked_example(self) -> None:
        breakdown = _profile_b_inputs().c_var_breakdown
        assert breakdown.total == pytest.approx(8.40e-6, rel=1e-6)

    def test_node_hour_billing_zeroes_vector_and_lexical_terms_by_default(self) -> None:
        breakdown = _profile_b_inputs().c_var_breakdown
        assert breakdown.vector_query_cost == 0.0
        assert breakdown.lexical_query_cost == 0.0

    def test_local_embedding_adapter_collapses_c_var_toward_zero(self) -> None:
        """Structural finding (derivation Step 3): c_emb -> 0 (local adapter,
        or DPDP `embedding_residency: local`) drives c_var -> 0, i.e.
        AC(V) ~= F/V, 100% amortization. This is a correct architectural
        property to report, not a defect."""
        local_adapter_inputs = Zone6CostInputs(
            period_hours=730.0, total_assemble_requests=_PROFILE_B_ASSUMED_Q,
            l2_cache_hit_rate=_PROFILE_B_ASSUMED_H_L2,
            fixed_cost_vector=_PROFILE_B_ASSUMED_C_FIXED_VECTOR,
            fixed_cost_lexical=_PROFILE_B_ASSUMED_C_FIXED_LEXICAL,
            embedding_cost_per_call=0.0,
            host_supplied_embedding_rate=_PROFILE_B_ASSUMED_H_HOST,
            l1_cache_hit_rate=_PROFILE_B_ASSUMED_H_L1,
        )
        assert local_adapter_inputs.c_var_breakdown.total == 0.0


class TestProfileBWorkedExample:
    """Every assertion here is copied verbatim from the delegation's worked example."""

    def test_average_cost_per_million_queries(self) -> None:
        report = compute_zone6_cost_report(_profile_b_inputs())
        assert report.average_cost_per_million_queries == pytest.approx(72.97, rel=1e-3)

    def test_marginal_cost_per_million_queries(self) -> None:
        report = compute_zone6_cost_report(_profile_b_inputs())
        assert report.marginal_cost_per_million_queries == pytest.approx(8.40, rel=1e-3)

    def test_amortization_share_phi(self) -> None:
        report = compute_zone6_cost_report(_profile_b_inputs())
        assert report.amortization_share == pytest.approx(0.8849, rel=1e-3)

    def test_crossover_volume_v_half(self) -> None:
        report = compute_zone6_cost_report(_profile_b_inputs())
        assert report.crossover_volume == pytest.approx(50_000_000.0, rel=1e-6)

    def test_distortion_factor_ac_over_mc(self) -> None:
        report = compute_zone6_cost_report(_profile_b_inputs())
        assert report.distortion_factor == pytest.approx(8.687, rel=1e-3)

    def test_average_cost_is_never_exposed_without_phi_and_distortion(self) -> None:
        """AC-006-COST-1: average alone is FORBIDDEN -- both fields are always
        present on the same report object by construction (no separate call
        can produce AC without them)."""
        report = compute_zone6_cost_report(_profile_b_inputs())
        assert report.average_cost_per_query > 0.0
        assert report.amortization_share is not None
        assert report.distortion_factor is not None

    def test_cross_check_ac_over_mc_equals_one_over_one_minus_phi(self) -> None:
        report = compute_zone6_cost_report(_profile_b_inputs())
        assert report.distortion_factor == pytest.approx(
            1.0 / (1.0 - report.amortization_share), rel=1e-9
        )


class TestSprint1PilotVolumeRows:
    """The three-row Sprint 1 table (1%, 10%, 100% of Profile B volume)."""

    @pytest.mark.parametrize(
        ("volume_fraction", "expected_ac_per_million", "expected_phi", "expected_distortion"),
        [
            (0.01, 6465.67, 0.9987, 769.72),
            (0.10, 654.13, 0.9872, 77.9),
            (1.00, 72.97, 0.8849, 8.687),
        ],
    )
    def test_row_matches_worked_table(
        self,
        volume_fraction: float,
        expected_ac_per_million: float,
        expected_phi: float,
        expected_distortion: float,
    ) -> None:
        inputs = _profile_b_inputs(total_assemble_requests=_PROFILE_B_ASSUMED_Q * volume_fraction)
        report = compute_zone6_cost_report(inputs)

        assert report.average_cost_per_million_queries == pytest.approx(
            expected_ac_per_million, rel=2e-3
        )
        assert report.amortization_share == pytest.approx(expected_phi, rel=2e-3)
        assert report.distortion_factor == pytest.approx(expected_distortion, rel=2e-2)

    def test_marginal_cost_is_volume_invariant_across_all_three_rows(self) -> None:
        """dMC/dV = 0 on a segment -- the only genuinely volume-invariant figure."""
        mcs = [
            compute_zone6_cost_report(
                _profile_b_inputs(total_assemble_requests=_PROFILE_B_ASSUMED_Q * fraction)
            ).marginal_cost_per_million_queries
            for fraction in (0.01, 0.10, 1.00)
        ]
        assert mcs[0] == pytest.approx(mcs[1]) == pytest.approx(mcs[2])

    def test_average_cost_is_strictly_decreasing_in_volume(self) -> None:
        """Proof (derivation Step 6): dAC/dV = -F/V^2 < 0 strictly whenever F > 0
        -- a falling AC as V grows is guaranteed, not an efficiency signal."""
        acs = [
            compute_zone6_cost_report(
                _profile_b_inputs(total_assemble_requests=_PROFILE_B_ASSUMED_Q * fraction)
            ).average_cost_per_query
            for fraction in (0.01, 0.10, 1.00)
        ]
        assert acs[0] > acs[1] > acs[2]


class TestAttributionIdentity:
    """Eq. 15: Delta_AC decomposes exactly into volume + capacity + efficiency effects."""

    def test_10x_growth_with_unchanged_infrastructure_is_pure_volume_effect(self) -> None:
        prior = _profile_b_inputs(total_assemble_requests=_PROFILE_B_ASSUMED_Q * 0.01)
        current = _profile_b_inputs(total_assemble_requests=_PROFILE_B_ASSUMED_Q * 0.10)

        report = compute_zone6_cost_report(current, prior=prior)

        assert report.attribution is not None
        assert report.attribution.capacity_effect == pytest.approx(0.0, abs=1e-12)
        assert report.attribution.efficiency_effect == pytest.approx(0.0, abs=1e-12)
        assert report.attribution.volume_effect != 0.0

    def test_attribution_identity_sums_exactly_to_the_observed_delta_ac(self) -> None:
        prior = _profile_b_inputs(total_assemble_requests=_PROFILE_B_ASSUMED_Q * 0.01)
        current = _profile_b_inputs(total_assemble_requests=_PROFILE_B_ASSUMED_Q * 0.10)

        prior_report = compute_zone6_cost_report(prior)
        current_report = compute_zone6_cost_report(current, prior=prior)

        observed_delta = (
            current_report.average_cost_per_query - prior_report.average_cost_per_query
        )
        assert current_report.attribution.total_delta_ac == pytest.approx(
            observed_delta, rel=1e-9
        )

    def test_report_without_prior_has_no_attribution(self) -> None:
        report = compute_zone6_cost_report(_profile_b_inputs())
        assert report.attribution is None


class TestPerTenantCostAndNumericalGuards:
    """Eq. 18 + the mandatory V_t=0 guard (delegation Step 11(i))."""

    def test_idle_tenant_returns_descriptive_undefined_string_never_zero_or_inf(self) -> None:
        result = per_tenant_average_cost(tenant_fixed_cost=21.0, c_var=8.4e-6, tenant_zone6_queries=0.0)
        assert isinstance(result, str)
        assert result == "undefined (V_t = 0); fixed cost 21.0 still incurred"

    def test_active_tenant_returns_a_float(self) -> None:
        result = per_tenant_average_cost(tenant_fixed_cost=21.0, c_var=8.4e-6, tenant_zone6_queries=1000.0)
        assert isinstance(result, float)
        assert result == pytest.approx(0.0210084, rel=1e-4)

    def test_two_tenants_same_infra_different_volume_show_the_715x_spread(self) -> None:
        """Same cluster, same c_var, same efficiency -- the spread is purely V_t."""
        tenant_x = per_tenant_average_cost(21.0, 8.4e-6, 1000.0)
        tenant_y = per_tenant_average_cost(21.0, 8.4e-6, 1_000_000.0)
        assert isinstance(tenant_x, float) and isinstance(tenant_y, float)
        assert tenant_x / tenant_y == pytest.approx(714.6, rel=1e-2)

    def test_negative_tenant_volume_raises(self) -> None:
        with pytest.raises(ValueError, match="tenant_zone6_queries"):
            per_tenant_average_cost(21.0, 8.4e-6, -1.0)

    def test_zero_aggregate_volume_raises_rather_than_returning_a_misleading_number(self) -> None:
        zero_volume_inputs = _profile_b_inputs(total_assemble_requests=0.0)
        with pytest.raises(ValueError, match="zone6_queries > 0"):
            compute_zone6_cost_report(zero_volume_inputs)


class TestDegradedModeCVar:
    """Eq. 19 (HLD 8.4 circuit breakers): F stays fixed, only c_var moves."""

    def test_no_degradation_matches_healthy_c_var(self) -> None:
        inputs = _profile_b_inputs()
        assert degraded_c_var(inputs, 0.0, 0.0) == pytest.approx(inputs.c_var_breakdown.total)

    def test_full_vector_breaker_open_uses_only_lexical_index_cost(self) -> None:
        inputs = _profile_b_inputs()
        c_var_eff = degraded_c_var(inputs, vector_breaker_open_rate=1.0, lexical_breaker_open_rate=0.0)
        expected = inputs.c_var_breakdown.embedding_term + inputs.lexical_query_cost
        assert c_var_eff == pytest.approx(expected)

    def test_rates_summing_over_one_raises(self) -> None:
        with pytest.raises(ValueError, match="<= 1.0"):
            degraded_c_var(_profile_b_inputs(), 0.6, 0.6)

    def test_rate_outside_unit_interval_raises(self) -> None:
        with pytest.raises(ValueError, match=r"\[0, 1\]"):
            degraded_c_var(_profile_b_inputs(), -0.1, 0.0)


class TestValidityPreconditionsAndInputValidation:
    """Delegation Step 10 (P1-P4): documented, testable input constraints."""

    def test_non_positive_period_hours_raises(self) -> None:
        with pytest.raises(ValueError, match="period_hours"):
            Zone6CostInputs(
                period_hours=0.0, total_assemble_requests=1000.0, l2_cache_hit_rate=0.25,
                fixed_cost_vector=1.0, fixed_cost_lexical=1.0, embedding_cost_per_call=1e-5,
                host_supplied_embedding_rate=0.3, l1_cache_hit_rate=0.4,
            )

    def test_hit_rate_outside_unit_interval_raises(self) -> None:
        with pytest.raises(ValueError, match=r"\[0, 1\]"):
            Zone6CostInputs(
                period_hours=730.0, total_assemble_requests=1000.0, l2_cache_hit_rate=1.5,
                fixed_cost_vector=1.0, fixed_cost_lexical=1.0, embedding_cost_per_call=1e-5,
                host_supplied_embedding_rate=0.3, l1_cache_hit_rate=0.4,
            )

    def test_negative_total_assemble_requests_raises(self) -> None:
        with pytest.raises(ValueError, match="total_assemble_requests"):
            Zone6CostInputs(
                period_hours=730.0, total_assemble_requests=-1.0, l2_cache_hit_rate=0.25,
                fixed_cost_vector=1.0, fixed_cost_lexical=1.0, embedding_cost_per_call=1e-5,
                host_supplied_embedding_rate=0.3, l1_cache_hit_rate=0.4,
            )
