"""Test suite for DASH-STORY-010: MutatedTerm (HLD Section 12C's three named mutation triggers).

Traces to FR-012 in SRS.md. FR-012 (verbatim): "The system SHALL compute a
composite Memory Score for every item from six weighted terms (Recency,
Frequency, Importance, UserAffinity, TaskRelevance, ProvenanceConfidence),
each normalized to [0,1], and SHALL use this score to drive zone lifecycle
transitions."

Covers, by AC ID:
  - AC-012-INVAL-1: the closed enumeration of the three non-Recency terms
    named as deadline-invalidating on mutation.

Also covers the must-not-deviate item "ALL THREE mutation paths must be
wired, not a subset" at the domain-enum level (application-level wiring
coverage is in test_rotation_deadline_invalidation_dash010.py).

Runtime assumptions (rule 33/40 test-roadmap conventions): no fixture seed,
clock, or tenant_id is required -- this suite exercises a pure Enum with no
I/O and no runtime state.

PII NOTE: per the dev_prompt's PII constraint, this suite carries only enum
member names and values -- never item content, conflict text, or override
rationale.
"""

from __future__ import annotations

from dashanan.domain.rotation_invalidation_trigger import MutatedTerm


class TestMustNotDeviateAllThreeMutationPathsEnumerated:
    """Must-not-deviate: 'ALL THREE mutation paths must be wired, not a subset'."""

    def test_exactly_three_members(self) -> None:
        assert len(MutatedTerm) == 3

    def test_members_match_hld_12c_verbatim_examples(self) -> None:
        assert set(MutatedTerm) == {
            MutatedTerm.FREQUENCY,
            MutatedTerm.PROVENANCE_CONFIDENCE,
            MutatedTerm.IMPORTANCE,
        }

    def test_values_are_hld_12c_term_names(self) -> None:
        assert MutatedTerm.FREQUENCY.value == "frequency"
        assert MutatedTerm.PROVENANCE_CONFIDENCE.value == "provenance_confidence"
        assert MutatedTerm.IMPORTANCE.value == "importance"


class TestMustNotDeviateNoOutOfScopeTerm:
    """Recency is excluded by definition; UserAffinity/TaskRelevance are not
    named by HLD Section 12C as deadline-invalidating mutation paths."""

    def test_recency_is_not_a_member(self) -> None:
        assert "recency" not in {member.value for member in MutatedTerm}

    def test_user_affinity_is_not_a_member(self) -> None:
        assert "user_affinity" not in {member.value for member in MutatedTerm}

    def test_task_relevance_is_not_a_member(self) -> None:
        assert "task_relevance" not in {member.value for member in MutatedTerm}


class TestMutatedTermIsStrEnum:
    """Mirrors `dashanan.domain.rotation_state.RotationState`'s `str, Enum` shape
    so a `MutatedTerm` member serializes directly as its wire value."""

    def test_member_is_instance_of_str(self) -> None:
        assert isinstance(MutatedTerm.FREQUENCY, str)

    def test_member_equals_its_string_value(self) -> None:
        assert MutatedTerm.FREQUENCY == "frequency"
