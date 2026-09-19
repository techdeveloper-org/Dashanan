"""Test suite for DASH-STORY-009's closed-form rotation deadline (HLD Section 12C).

Traces to FR-012 in SRS.md. FR-012 (verbatim): "The system SHALL compute a
composite Memory Score for every item from six weighted terms (Recency,
Frequency, Importance, UserAffinity, TaskRelevance, ProvenanceConfidence),
each normalized to [0,1], and SHALL use this score to drive zone lifecycle
transitions."

Covers `dashanan.domain.rotation_deadline.compute_rotation_deadline`
against the mathematics-engineer's DASH-STORY-009 delegation (verbatim
implementation contract F1-F8, worked examples W0-W4) -- every test below
asserts against a number the delegation itself derived and back-
substituted, never a self-computed approximation (dev_prompt: "If a math
delegation applies, assert against the RETURNED derivation, never a
self-computed approximation").

Runtime assumptions (rule 33/40 test-roadmap conventions):
  - No Clock is involved -- `compute_rotation_deadline` is a pure function
    of its scalar arguments; no fixture seed is required.
  - `lambda_zone = 4.01e-6` (Zone 2 Episodic, HLD Section 12A) is reused
    across the worked-example tests below, matching the delegation's own
    W-series numbers.

PII NOTE: every value is an abstract numeric term (weights, decay rates,
durations) -- never conversational content, matching this story's PII
posture (dev_prompt: "the state-machine and scheduling logic never reads
the item body").
"""

from __future__ import annotations

import math

import pytest

from dashanan.domain.rotation_deadline import (
    compute_rotation_deadline,
    weighted_non_recency_sum,
)

_LAMBDA_ZONE_2 = 4.01e-6
"""HLD Section 12A's own worked computation for Zone 2 Episodic."""

_COMPRESS_THRESHOLD = 0.35
"""HLD Section 12A FINALIZED `CompressThreshold` (F4's Sprint 1 scope)."""

_EQUAL_WEIGHT = 1.0 / 6.0
"""HLD Section 12A's Sprint 1 MVP default weight profile."""


class TestClosedFormWorkedExamples:
    """W0-W3: the math delegation's own worked, back-substituted examples."""

    def test_w0_hld_worked_example_matches_hld_section_12c(self) -> None:
        """HLD Section 12C's own worked example: C=1.8 -> ~300,249s (3.47 days)."""
        c = 0.3 + 0.5 + 0.6 + 0.3 + 0.1
        c_weighted = weighted_non_recency_sum(
            w_frequency=_EQUAL_WEIGHT,
            frequency=0.3,
            w_importance=_EQUAL_WEIGHT,
            importance=0.5,
            w_user_affinity=_EQUAL_WEIGHT,
            user_affinity=0.3,
            w_task_relevance=_EQUAL_WEIGHT,
            task_relevance=0.1,
            w_provenance_confidence=_EQUAL_WEIGHT,
            provenance_confidence=0.6,
        )
        assert c_weighted == pytest.approx(c * _EQUAL_WEIGHT)

        dt = compute_rotation_deadline(
            lambda_zone=_LAMBDA_ZONE_2,
            theta=_COMPRESS_THRESHOLD,
            w_recency=_EQUAL_WEIGHT,
            c_weighted=c_weighted,
            max_age_remaining_s=1.5552e7,
            cooldown_remaining_s=0.0,
        )
        assert dt == pytest.approx(300_242.59, abs=1.0)

    def test_w1_branch3_schedule_low_importance_item(self) -> None:
        """W1 (Branch 3, SCHEDULE): C=1.63 -> ~188,284.9s (2.1792 days)."""
        c_weighted = (0.23 + 0.20 + 0.60 + 0.50 + 0.10) * _EQUAL_WEIGHT
        dt = compute_rotation_deadline(
            lambda_zone=_LAMBDA_ZONE_2,
            theta=_COMPRESS_THRESHOLD,
            w_recency=_EQUAL_WEIGHT,
            c_weighted=c_weighted,
            max_age_remaining_s=1.5552e7,
            cooldown_remaining_s=172_800.0,
        )
        assert dt == pytest.approx(188_284.9, abs=1.0)

    def test_w1_back_substitution_lands_exactly_on_theta(self) -> None:
        """The math delegation's own back-substitution check: S(dt*) == theta exactly."""
        c_weighted = (0.23 + 0.20 + 0.60 + 0.50 + 0.10) * _EQUAL_WEIGHT
        dt = compute_rotation_deadline(
            lambda_zone=_LAMBDA_ZONE_2,
            theta=_COMPRESS_THRESHOLD,
            w_recency=_EQUAL_WEIGHT,
            c_weighted=c_weighted,
            max_age_remaining_s=1.5552e7,
            cooldown_remaining_s=0.0,
        )
        assert dt is not None
        s_at_dt = _EQUAL_WEIGHT * math.exp(-_LAMBDA_ZONE_2 * dt) + c_weighted
        assert s_at_dt == pytest.approx(_COMPRESS_THRESHOLD, abs=1e-9)

    def test_w2_branch1_pinned_at_sprint1_defaults(self) -> None:
        """W2 (Branch 1, PINNED): C=2.30 -> None. The DEFAULT Sprint 1 profile."""
        c_weighted = (0.30 + 0.50 + 0.80 + 0.50 + 0.20) * _EQUAL_WEIGHT
        dt = compute_rotation_deadline(
            lambda_zone=_LAMBDA_ZONE_2,
            theta=_COMPRESS_THRESHOLD,
            w_recency=_EQUAL_WEIGHT,
            c_weighted=c_weighted,
            max_age_remaining_s=1.5552e7,
            cooldown_remaining_s=0.0,
        )
        assert dt is None

    def test_w3_branch2_immediate_clamped_by_cooldown(self) -> None:
        """W3 (Branch 2, IMMEDIATE): C=0.50 -> dt*=0, clamped to cooldown_remaining_s."""
        c_weighted = (0.0 + 0.20 + 0.30 + 0.0 + 0.0) * _EQUAL_WEIGHT
        dt = compute_rotation_deadline(
            lambda_zone=_LAMBDA_ZONE_2,
            theta=_COMPRESS_THRESHOLD,
            w_recency=_EQUAL_WEIGHT,
            c_weighted=c_weighted,
            max_age_remaining_s=1.5552e7,
            cooldown_remaining_s=172_800.0,
        )
        assert dt == pytest.approx(172_800.0)


class TestClosedFormBranchBoundaries:
    """F2/F3: the three-branch validity domain, exercised at its own boundaries."""

    def test_u_exactly_zero_is_pinned(self) -> None:
        """`u = 0` (theta == c_weighted) is Branch 1 (`u <= 0`), not Branch 3."""
        dt = compute_rotation_deadline(
            lambda_zone=1e-4,
            theta=0.35,
            w_recency=0.2,
            c_weighted=0.35,
            max_age_remaining_s=1e9,
            cooldown_remaining_s=0.0,
        )
        assert dt is None

    def test_u_exactly_one_is_immediate(self) -> None:
        """`u = 1` (theta - c_weighted == w_recency) is Branch 2, `dt* = 0`."""
        dt = compute_rotation_deadline(
            lambda_zone=1e-4,
            theta=0.5,
            w_recency=0.2,
            c_weighted=0.3,
            max_age_remaining_s=1e9,
            cooldown_remaining_s=0.0,
        )
        assert dt == pytest.approx(0.0)

    def test_u_negative_is_pinned(self) -> None:
        """`u < 0` (a static floor above theta) is Branch 1, PINNED."""
        dt = compute_rotation_deadline(
            lambda_zone=1e-4,
            theta=0.2,
            w_recency=0.2,
            c_weighted=0.5,
            max_age_remaining_s=1e9,
            cooldown_remaining_s=0.0,
        )
        assert dt is None

    def test_delta_t_beyond_max_age_is_pinned(self) -> None:
        """F3: a computed `delta_t*` beyond `max_age_remaining_s` is unreachable -- pinned.

        `u_floor(zone) = max(u_eps, exp(-lambda*MaxAge))` (D4): the second
        term is exactly the `u` value at which `delta_t* == MaxAge`, so a
        `u` at or below it is pinned via the `u_floor` guard itself. This
        test picks `u` comfortably below that computed `u_floor` to
        exercise exactly that guard, matching D4's own Zone-2/Zone-1
        cross-check numbers in kind (a `u` numerically small relative to
        `exp(-lambda*MaxAge)`).
        """
        lambda_zone = 1e-4
        max_age_remaining_s = 100.0
        u_floor_from_max_age = math.exp(-lambda_zone * max_age_remaining_s)
        assert 0.0 < u_floor_from_max_age < 1.0

        theta = 0.35
        w_recency = 1.0 / 6.0
        u_target = u_floor_from_max_age / 10.0
        c_weighted = theta - u_target * w_recency

        dt = compute_rotation_deadline(
            lambda_zone=lambda_zone,
            theta=theta,
            w_recency=w_recency,
            c_weighted=c_weighted,
            max_age_remaining_s=max_age_remaining_s,
            cooldown_remaining_s=0.0,
        )
        assert dt is None


class TestClosedFormGeneralWeightForm:
    """F1: the general-weight form -- the literal "6" is `1/w_recency`, never hardcoded.

    NFR-009/AC-017 make the weight profile operator-overridable without a
    code change; this class asserts the implementation reflects `1 /
    w_recency` for a NON-default weight profile, not just the equal-weight
    `1/6` shortcut.
    """

    def test_non_default_weight_profile_uses_1_over_w_recency(self) -> None:
        """An operator profile with `w_recency = 0.5` implies the "6" becomes "2"."""
        w_recency = 0.5
        theta = 0.5
        c_weighted = 0.1
        dt = compute_rotation_deadline(
            lambda_zone=1e-4,
            theta=theta,
            w_recency=w_recency,
            c_weighted=c_weighted,
            max_age_remaining_s=1e9,
            cooldown_remaining_s=0.0,
        )
        u = (theta - c_weighted) / w_recency
        expected_dt = -math.log(u) / 1e-4
        assert dt == pytest.approx(expected_dt)

    def test_hardcoded_six_would_have_given_a_wrong_answer(self) -> None:
        """Directly demonstrates the delegation's warning: hardcoding `6` is wrong for `w_recency != 1/6`.

        `w_recency = 0.5` here (not `1/6`), with five equally-weighted
        non-Recency terms summing to `c_weighted = 0.1`. The correct
        general form (F1) computes `u = 0.8` -> a positive, finite
        `delta_t*` (Branch 3, SCHEDULE). A naive hardcoded-`6`
        implementation, applied to the five terms' RAW (unweighted) sum
        `C_raw = 1.0`, computes `u = 6*0.5 - 1.0 = 2.0` -> Branch 2
        (IMMEDIATE, `dt*=0`) -- a materially different outcome, exactly
        the silent-failure class this delegation was declared blocking to
        prevent.
        """
        w_recency = 0.5
        theta = 0.5
        c_weighted = 0.1
        c_raw = 1.0

        dt = compute_rotation_deadline(
            lambda_zone=1e-4,
            theta=theta,
            w_recency=w_recency,
            c_weighted=c_weighted,
            max_age_remaining_s=1e9,
            cooldown_remaining_s=0.0,
        )
        u_correct = (theta - c_weighted) / w_recency
        assert u_correct == pytest.approx(0.8)
        assert dt == pytest.approx(-math.log(0.8) / 1e-4)

        wrong_u = 6 * theta - c_raw
        assert wrong_u == pytest.approx(2.0)
        assert wrong_u >= 1.0
        assert dt != pytest.approx(0.0)


class TestClosedFormNumericGuards:
    """D4: the `u_floor` numeric-stability guard and validation."""

    def test_lambda_zone_zero_raises_value_error(self) -> None:
        """Zones 6/7 (`lambda = n/a`) must never reach this function -- guarded here."""
        with pytest.raises(ValueError, match="lambda_zone"):
            compute_rotation_deadline(
                lambda_zone=0.0,
                theta=0.35,
                w_recency=1.0 / 6.0,
                c_weighted=0.1,
                max_age_remaining_s=1e9,
                cooldown_remaining_s=0.0,
            )

    def test_lambda_zone_negative_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="lambda_zone"):
            compute_rotation_deadline(
                lambda_zone=-1e-4,
                theta=0.35,
                w_recency=1.0 / 6.0,
                c_weighted=0.1,
                max_age_remaining_s=1e9,
                cooldown_remaining_s=0.0,
            )

    def test_w_recency_zero_raises_value_error(self) -> None:
        """A zero Recency weight removes all time dependence -- no deadline exists."""
        with pytest.raises(ValueError, match="w_recency"):
            compute_rotation_deadline(
                lambda_zone=1e-4,
                theta=0.35,
                w_recency=0.0,
                c_weighted=0.1,
                max_age_remaining_s=1e9,
                cooldown_remaining_s=0.0,
            )

    def test_u_below_precision_floor_is_pinned_not_a_tiny_deadline(self) -> None:
        """D4: `u` indistinguishable from zero within float64 precision is treated as pinned."""
        dt = compute_rotation_deadline(
            lambda_zone=1e-6,
            theta=0.35,
            w_recency=1.0 / 6.0,
            c_weighted=0.35 - 1e-16,
            max_age_remaining_s=1e12,
            cooldown_remaining_s=0.0,
        )
        assert dt is None


class TestWeightedNonRecencySum:
    """`weighted_non_recency_sum`: the five-term weighted-sum helper `c_weighted` callers use."""

    def test_equal_weights_matches_manual_computation(self) -> None:
        result = weighted_non_recency_sum(
            w_frequency=1.0 / 6.0,
            frequency=0.3,
            w_importance=1.0 / 6.0,
            importance=0.5,
            w_user_affinity=1.0 / 6.0,
            user_affinity=0.3,
            w_task_relevance=1.0 / 6.0,
            task_relevance=0.1,
            w_provenance_confidence=1.0 / 6.0,
            provenance_confidence=0.6,
        )
        expected = (0.3 + 0.5 + 0.3 + 0.1 + 0.6) / 6.0
        assert result == pytest.approx(expected)

    def test_zero_weights_give_zero_sum(self) -> None:
        result = weighted_non_recency_sum(
            w_frequency=0.0,
            frequency=1.0,
            w_importance=0.0,
            importance=1.0,
            w_user_affinity=0.0,
            user_affinity=1.0,
            w_task_relevance=0.0,
            task_relevance=1.0,
            w_provenance_confidence=0.0,
            provenance_confidence=1.0,
        )
        assert result == pytest.approx(0.0)
