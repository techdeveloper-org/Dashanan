"""Test suite for DASH-STORY-009's guarded rotation state machine (HLD Section 6).

Traces to FR-012 in SRS.md. Covers, by AC ID:
  - AC-012: "An item crossing CompressThreshold (not ArchiveThreshold)
    transitions to Compressed and stays retrievable; it SHALL NOT reach
    Archived without passing through Compressed."
  - AC-012-ROT-1: "Zone 8 out of scope: an item that would cross
    ArchiveThreshold instead stays in Compressed indefinitely -- no
    Archived attempt with no destination."

Also covers must-not-deviate items 1-2 verbatim from ar1_assignments.json
AR1-009 (see `TestMustNotDeviateArchivedUnreachable` below).

Runtime assumptions (rule 33/40 test-roadmap conventions):
  - No Clock, no I/O -- `guarded_transition` is a pure function; no
    fixture seed is required.

PII NOTE: this module carries only a state-machine enum and pure guard
logic -- no item content, matching this story's PII posture.
"""

from __future__ import annotations

import itertools

import pytest

from dashanan.domain.rotation_state import (
    RotationState,
    RotationTransitionError,
    guarded_transition,
)


class TestGuardedTransitionAllowedPath:
    """AC-012: the sole legal transition, `Active -> Compressed`."""

    def test_active_to_compressed_is_allowed(self) -> None:
        result = guarded_transition(RotationState.ACTIVE, RotationState.COMPRESSED)
        assert result is RotationState.COMPRESSED

    def test_allowed_transition_returns_the_target_unchanged(self) -> None:
        result = guarded_transition(RotationState.ACTIVE, RotationState.COMPRESSED)
        assert result == RotationState.COMPRESSED


class TestMustNotDeviateArchivedUnreachable:
    """AR1-009 must-not-deviate items 1-2, verbatim:

    1. "Guarded state machine -- Archived must be structurally
       unreachable without passing through Compressed (AC-012)"
    2. "No code path may attempt an Archived transition in Sprint 1;
       Zone 8 does not exist (AC-012-ROT-1, HLD 12F)"
    """

    @pytest.mark.parametrize("current", list(RotationState))
    def test_no_state_can_transition_to_archived(self, current: RotationState) -> None:
        """Exhaustive: for EVERY possible current state, `-> ARCHIVED` is rejected."""
        with pytest.raises(RotationTransitionError):
            guarded_transition(current, RotationState.ARCHIVED)

    def test_compressed_to_archived_is_rejected(self) -> None:
        """The one transition a naive "Compressed is the last step before Archived" reading would allow."""
        with pytest.raises(RotationTransitionError) as exc_info:
            guarded_transition(RotationState.COMPRESSED, RotationState.ARCHIVED)
        assert exc_info.value.current is RotationState.COMPRESSED
        assert exc_info.value.target is RotationState.ARCHIVED

    def test_active_to_archived_directly_is_rejected(self) -> None:
        with pytest.raises(RotationTransitionError):
            guarded_transition(RotationState.ACTIVE, RotationState.ARCHIVED)

    def test_archived_has_no_outbound_transitions(self) -> None:
        """`ARCHIVED` never appears as a source in `_ALLOWED_TRANSITIONS` -- it is a dead state."""
        for target in RotationState:
            with pytest.raises(RotationTransitionError):
                guarded_transition(RotationState.ARCHIVED, target)


class TestGuardedTransitionCompressedIsTerminalInSprint1:
    """Must-not-deviate item 5 (compressed dwell is indefinite): `Compressed` has zero outbound transitions."""

    @pytest.mark.parametrize("target", list(RotationState))
    def test_compressed_has_no_allowed_outbound_transition(
        self, target: RotationState
    ) -> None:
        with pytest.raises(RotationTransitionError):
            guarded_transition(RotationState.COMPRESSED, target)


class TestGuardedTransitionRejectsDoubleCompression:
    """A concurrent double-pop / already-Compressed item must be rejected, not silently re-applied."""

    def test_compressed_to_compressed_is_rejected(self) -> None:
        """An item already Compressed cannot be "transitioned" to Compressed again."""
        with pytest.raises(RotationTransitionError):
            guarded_transition(RotationState.COMPRESSED, RotationState.COMPRESSED)

    def test_active_to_active_is_rejected(self) -> None:
        with pytest.raises(RotationTransitionError):
            guarded_transition(RotationState.ACTIVE, RotationState.ACTIVE)


class TestRotationTransitionErrorMessage:
    """The exception carries structured, PII-free context -- never item content."""

    def test_error_message_names_both_states(self) -> None:
        with pytest.raises(RotationTransitionError) as exc_info:
            guarded_transition(RotationState.COMPRESSED, RotationState.ARCHIVED)
        message = str(exc_info.value)
        assert "compressed" in message
        assert "archived" in message

    def test_error_exposes_current_and_target_as_attributes(self) -> None:
        with pytest.raises(RotationTransitionError) as exc_info:
            guarded_transition(RotationState.ACTIVE, RotationState.ARCHIVED)
        assert exc_info.value.current is RotationState.ACTIVE
        assert exc_info.value.target is RotationState.ARCHIVED


class TestRotationStateExhaustiveness:
    """Every `(current, target)` pair either transitions or raises -- no silent no-op path exists."""

    @pytest.mark.parametrize(
        "current,target", list(itertools.product(RotationState, RotationState))
    )
    def test_every_pair_either_succeeds_or_raises(
        self, current: RotationState, target: RotationState
    ) -> None:
        try:
            result = guarded_transition(current, target)
        except RotationTransitionError:
            return
        assert result is target
        assert current is RotationState.ACTIVE
        assert target is RotationState.COMPRESSED
