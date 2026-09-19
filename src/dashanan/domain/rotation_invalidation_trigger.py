"""MutatedTerm: which non-Recency MemoryScore term changed (HLD Section 12C, ADR-012).

Traces to FR-012 in SRS.md. FR-012 (verbatim): "The system SHALL compute a
composite Memory Score for every item from six weighted terms (Recency,
Frequency, Importance, UserAffinity, TaskRelevance, ProvenanceConfidence),
each normalized to [0,1], and SHALL use this score to drive zone lifecycle
transitions."

HLD Section 12C (verbatim, the "one liability" this story closes): "any
mutation of a non-Recency term (Frequency on access, ProvenanceConfidence on
conflict detection, Importance on operator override) invalidates the stored
deadline and requires an O(log N) re-insert. This must be wired into every
term-mutation path. Missing one produces items rotating on stale deadlines --
a silent correctness bug, not a crash."

MUST-NOT-DEVIATE (ar1_assignments.json AR1-010, binding):
  "ALL THREE mutation paths must be wired, not a subset (HLD 12C: 'must be
  wired into every term-mutation path')" -- `MutatedTerm` below is the
  single, closed enumeration of every non-Recency term HLD Section 12C
  names as capable of invalidating a stored deadline. It has exactly three
  members, named after HLD Section 12C's own three parenthetical examples
  ("Frequency on access", "ProvenanceConfidence on conflict detection",
  "Importance on operator override"), never a fourth or a subset of two --
  mirrors `dashanan.domain.rotation_state.RotationState`'s identical
  "closed enumeration is the single source of truth" convention.

This module holds pure domain logic only: one Enum, no I/O, no Clock --
mirrors every other domain module's "domain-only" convention
(`rotation_state.py`, `rotation_zone_policy.py`).

PII NOTE: this enum names WHICH numeric score term changed, never the
content that changed it -- a member's value never carries the accessed
item's content, the conflicting fact's text, or the operator's override
rationale (dev_prompt's PII constraint, mirrored from `rotation_state.py`'s
identical posture).
"""

from __future__ import annotations

from enum import Enum


class MutatedTerm(str, Enum):
    """The three non-Recency MemoryScore terms HLD Section 12C names as
    deadline-invalidating on mutation.

    UserAffinity and TaskRelevance are deliberately absent: HLD Section 12C
    names only these three as term-mutation paths requiring deadline
    invalidation; Recency itself is absent by definition (it is the ONE
    term the stored deadline already tracks continuously between accesses,
    per HLD Section 12C: "Between accesses... only Recency varies" --
    adding a fourth member here would misrepresent what this story's own
    must-not-deviate list binds it to).
    """

    FREQUENCY = "frequency"
    PROVENANCE_CONFIDENCE = "provenance_confidence"
    IMPORTANCE = "importance"
