"""Test suite for DASH-STORY-019 domain layer: `dashanan.domain.archive_transition`.

Traces to FR-008 in SRS.md. FR-008 (verbatim): "The system SHALL provide
a Consolidation Memory zone as the long-term, cross-session consolidated
store that content reaches only via the Archived state of the rotation
state machine (FR-012)."

Covers, by AC ID:
  - AC-008-ROT-1: a Compressed item crossing ArchiveThreshold (0.25) is
    eligible to archive (`is_archive_eligible`).
  - AC-008-ROT-2: an item with payload_tokens < 64 is OAQ-12 fast-track
    eligible (`is_oaq12_fast_track_eligible`).

Also covers every must-not-deviate item verbatim from
sprint2_ar1_assignments.json AR1-S2-019 (see each `TestMustNotDeviate*`
class below), plus boundary and adverse cases per rules 33/40's roadmap.

Runtime assumptions (rule 33/40 test-roadmap conventions):
  - No Clock, no fixture seed, and no randomization anywhere in this
    suite -- every function under test is a pure, deterministic
    computation.

PII NOTE: per the dev_prompt's PII constraint, this suite carries only
abstract numeric inputs -- threshold floats, token counts, `RotationState`
enum members -- never zone payload/fact content, anywhere below.
"""

from __future__ import annotations

import pytest

from dashanan.domain.archive_transition import (
    ARCHIVE_THRESHOLD,
    OAQ12_FAST_TRACK_TOKEN_LIMIT,
    ArchiveTransitionError,
    guarded_archive_transition,
    is_archive_eligible,
    is_oaq12_fast_track_eligible,
)
from dashanan.domain.rotation_state import RotationState
from dashanan.domain.rotation_zone_policy import RotationZonePolicy


class TestMustNotDeviateArchiveThresholdFixedAt025:
    """Must-not-deviate item 1: "ArchiveThreshold is fixed at 0.25, not the
    research brief's provisional 0.15 (structurally unreachable per
    Section 12B.2)".
    """

    def test_archive_threshold_constant_equals_0_25(self) -> None:
        assert ARCHIVE_THRESHOLD == 0.25

    def test_archive_threshold_is_never_0_15(self) -> None:
        assert ARCHIVE_THRESHOLD != 0.15

    def test_rotation_zone_policy_has_no_archive_threshold_field(self) -> None:
        """Structural enforcement: `ArchiveThreshold` is never an operator-
        overridable `RotationZonePolicy` field -- it lives only as this
        module's fixed constant.
        """
        policy = RotationZonePolicy.for_zone(
            t_half_seconds=172_800.0,
            compress_threshold=0.35,
            max_age_seconds=1.5552e7,
        )
        assert not hasattr(policy, "archive_threshold")

    def test_is_archive_eligible_takes_no_threshold_parameter(self) -> None:
        """Structural enforcement: `is_archive_eligible` cannot be called with
        a caller-supplied threshold -- only `memory_score`.
        """
        import inspect

        params = list(inspect.signature(is_archive_eligible).parameters)
        assert params == ["memory_score"]


class TestAc008Rot1ArchiveEligibility:
    """AC-008-ROT-1: a Compressed item crossing ArchiveThreshold (0.25) is archive-eligible."""

    def test_score_below_threshold_is_eligible(self) -> None:
        assert is_archive_eligible(0.10) is True

    def test_score_exactly_at_threshold_is_eligible(self) -> None:
        assert is_archive_eligible(0.25) is True

    def test_score_above_threshold_is_not_eligible(self) -> None:
        assert is_archive_eligible(0.26) is False

    def test_score_far_above_threshold_is_not_eligible(self) -> None:
        assert is_archive_eligible(0.90) is False

    def test_score_at_zero_is_eligible(self) -> None:
        assert is_archive_eligible(0.0) is True


class TestAc008Rot2Oaq12FastTrackEligibility:
    """AC-008-ROT-2 / OAQ-12: an item with payload_tokens < 64 is fast-track eligible."""

    def test_token_count_below_limit_is_eligible(self) -> None:
        assert is_oaq12_fast_track_eligible(10) is True

    def test_token_count_at_limit_is_not_eligible(self) -> None:
        """Strictly `< 64`, never `<= 64` (HLD/AC wording: "payload_tokens < 64")."""
        assert is_oaq12_fast_track_eligible(OAQ12_FAST_TRACK_TOKEN_LIMIT) is False

    def test_token_count_one_below_limit_is_eligible(self) -> None:
        assert is_oaq12_fast_track_eligible(OAQ12_FAST_TRACK_TOKEN_LIMIT - 1) is True

    def test_token_count_above_limit_is_not_eligible(self) -> None:
        assert is_oaq12_fast_track_eligible(1000) is False

    def test_zero_tokens_is_eligible(self) -> None:
        assert is_oaq12_fast_track_eligible(0) is True


class TestMustNotDeviateOaq12GatedOnTokensOnly:
    """Must-not-deviate item 3: "The OAQ-12 fast-track is gated strictly by
    payload_tokens < 64, not by score-collapse velocity".
    """

    def test_fast_track_predicate_accepts_exactly_one_parameter(self) -> None:
        import inspect

        params = list(inspect.signature(is_oaq12_fast_track_eligible).parameters)
        assert params == ["payload_tokens"]

    def test_fast_track_eligibility_is_independent_of_memory_score(self) -> None:
        """A high-scoring item (nowhere near archiving) is still fast-track
        eligible purely from its token count -- there is no velocity/score
        signal this predicate could consult even if a caller wanted it to.
        """
        assert is_oaq12_fast_track_eligible(10) is True


class TestMustNotDeviateCannotReachArchivedWithoutCompressed:
    """Must-not-deviate item 2: "An item SHALL NOT reach Archived without
    having passed through Compressed".
    """

    def test_compressed_to_archived_is_the_only_legal_edge(self) -> None:
        result = guarded_archive_transition(
            RotationState.COMPRESSED, RotationState.ARCHIVED
        )
        assert result is RotationState.ARCHIVED

    def test_active_to_archived_is_rejected(self) -> None:
        with pytest.raises(ArchiveTransitionError):
            guarded_archive_transition(RotationState.ACTIVE, RotationState.ARCHIVED)

    def test_archived_to_archived_is_rejected(self) -> None:
        """An already-Archived item cannot be "re-archived" through this table."""
        with pytest.raises(ArchiveTransitionError):
            guarded_archive_transition(RotationState.ARCHIVED, RotationState.ARCHIVED)

    def test_compressed_to_active_is_rejected(self) -> None:
        """This table has no reverse edge -- Compressed cannot go back to Active."""
        with pytest.raises(ArchiveTransitionError):
            guarded_archive_transition(RotationState.COMPRESSED, RotationState.ACTIVE)

    def test_error_names_the_attempted_current_and_target_states(self) -> None:
        try:
            guarded_archive_transition(RotationState.ACTIVE, RotationState.ARCHIVED)
        except ArchiveTransitionError as exc:
            assert exc.current is RotationState.ACTIVE
            assert exc.target is RotationState.ARCHIVED
        else:
            pytest.fail("expected ArchiveTransitionError")


class TestFileDisjointnessRotationStateUntouched:
    """FILE-DISJOINTNESS NOTE: this story's guarded-transition table lives in
    its own file; `dashanan.domain.rotation_state`'s own closed table is
    never edited or reopened.
    """

    def test_rotation_state_module_still_has_no_archived_transition(self) -> None:
        from dashanan.domain.rotation_state import guarded_transition
        from dashanan.domain.rotation_state import RotationTransitionError

        with pytest.raises(RotationTransitionError):
            guarded_transition(RotationState.COMPRESSED, RotationState.ARCHIVED)
