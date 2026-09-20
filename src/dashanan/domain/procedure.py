"""Procedure: Zone 4's owned entity (HLD Section 3.5, FR-004).

FR-004 (verbatim, SRS.md): "The system SHALL provide a Procedural Memory
zone holding learned task procedures, tool-use patterns, and successful
action sequences." (HLD Section 3.5 Zone 4).

HLD 3.5 (verbatim): "Owned entities: Procedure(tenant_id, procedure_id,
task_signature_hash, steps[], success_count, failure_count, last_used_at,
state, score_terms). Access pattern: O(1) point lookup by
task_signature_hash; secondary similarity search via Zone 6.
success_count/(success_count+failure_count) feeds the Importance term for
this zone ... this ratio is naturally bounded to [0,1] and needs no
further clamping."
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class ProcedureState(str, Enum):
    """The three states of the locked `Active -> Compressed -> Archived` lifecycle.

    Mirrors `dashanan.domain.episode.EpisodeState`'s identical three-member
    shape (HLD Section 6, "MemoryItem lifecycle" row: one zone-agnostic
    state machine; each zone's own entity carries its own state field with
    the same values) rather than importing that enum directly -- the same
    Interface Segregation `dashanan.domain.rotation_state.RotationState`'s
    own docstring documents for its identical decoupling from
    `EpisodeState`.

    DASH-STORY-015 never transitions this field (no story in this sprint
    wires a Rotation Engine policy for Zone 4); it is carried here only so
    `Procedure` matches the HLD 3.5 owned-entity shape in full, exactly as
    `Episode.state` was included in DASH-STORY-003 ahead of its own
    rotation-policy story.
    """

    ACTIVE = "active"
    COMPRESSED = "compressed"
    ARCHIVED = "archived"


@dataclass(frozen=True, slots=True)
class Procedure:
    """A single learned task procedure held in Zone 4 (Procedural Memory).

    Frozen so every mutation (recording a fresh execution outcome) goes
    through `dataclasses.replace`, matching this codebase's Value-Object
    entity convention (`WorkingItem`, `Episode`).

    Attributes:
        tenant_id: Owning tenant. Mandatory at every Zone 4 operation
            (HLD 3.0 invariant 2) -- never defaulted.
        task_signature_hash: The O(1) point-lookup key (HLD Section 5,
            "Zone 4 Procedural" DSA row: "HashMap keyed by
            task_signature_hash"). See `hash_task_signature` for how a
            caller derives this from a raw task description.
        steps: The ordered action sequence (HLD Section 5: "ordered array
            for step sequences" -- ordering is significant; there is no
            insert-in-middle use case per that same row).
        success_count: Times this procedure was executed to a successful
            outcome. Feeds `importance()` (AC-004-3).
        failure_count: Times this procedure was executed to a failed
            outcome. Feeds `importance()` (AC-004-3).
        last_used_at: When this procedure was last recorded as executed.
        state: Current position in the locked rotation lifecycle. Never
            transitioned by this story -- see `ProcedureState`.
        score_terms: Placeholder six-term inputs for the future
            MemoryScore (HLD Section 12G), unused by this story's own
            Importance computation, mirroring `WorkingItem.score_terms`
            and `Episode.score_terms`'s identical placeholder role.

    Judgment call (DASH-STORY-015 dev subtask): HLD 3.5 also lists a
    `procedure_id` field on this entity. No AC-004 acceptance criterion
    references it, and this story's own point-lookup key is
    `task_signature_hash`, not a separate synthetic id -- inventing a
    `procedure_id` generation scheme here (a UUID? a deterministic
    encoding like `Episode.episode_id_for`?) is exactly the class of
    invented mechanism the must-not-deviate list warns against for
    analogous zones ("do not invent a conflict-detection or
    provenance-linkage mechanism analogous to Zone 3/5 or Zone 7"). It is
    therefore deliberately omitted; a later story that needs it can add it
    as a new field with a default, which is additive and does not require
    reopening this file's existing call sites.
    """

    tenant_id: str
    task_signature_hash: str
    steps: tuple[str, ...]
    success_count: int
    failure_count: int
    last_used_at: datetime
    state: ProcedureState = ProcedureState.ACTIVE
    score_terms: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Enforce the invariants this zone's point lookup and Importance ratio depend on.

        Raises:
            ValueError: If `tenant_id` or `task_signature_hash` is blank,
                `steps` is empty, or `success_count`/`failure_count` is
                negative.
        """
        if not self.tenant_id.strip():
            raise ValueError("Procedure.tenant_id must not be blank")
        if not self.task_signature_hash.strip():
            raise ValueError("Procedure.task_signature_hash must not be blank")
        if not self.steps:
            raise ValueError("Procedure.steps must not be empty")
        if self.success_count < 0:
            raise ValueError(
                "Procedure.success_count must not be negative, got "
                f"{self.success_count}"
            )
        if self.failure_count < 0:
            raise ValueError(
                "Procedure.failure_count must not be negative, got "
                f"{self.failure_count}"
            )

    def importance(self) -> float:
        """Compute Importance = success_count/(success_count+failure_count).

        AC-004-3 (verbatim): "Importance = success_count/
        (success_count+failure_count), naturally bounded [0,1], no clamp
        function required." HLD Section 12G confirms this term is left
        "unchanged" by the MemoryScore normalization contract precisely
        because the ratio is already bounded by construction whenever the
        denominator is positive -- `__post_init__` already forbids a
        negative `success_count`/`failure_count`, so the only value this
        method must itself guard is a zero denominator.

        Returns:
            The ratio, always in `[0.0, 1.0]` once at least one outcome
            has been recorded. Returns `0.0` when
            `success_count == failure_count == 0` -- a neutral "no
            evidence yet" default, not a clamp of an out-of-range result,
            since 0/0 is undefined rather than out of bounds.
        """
        total = self.success_count + self.failure_count
        if total == 0:
            return 0.0
        return self.success_count / total


def hash_task_signature(task: str) -> str:
    """Derive the O(1) lookup key HLD Section 5 names `task_signature_hash`.

    A pure function so the Orchestrator's `ZoneQuery.task` (free text) and
    a caller's own task description hash to the identical key a prior
    `commit` used -- this is what makes the point lookup "exact" (AC-004-1)
    rather than a fuzzy match. Whitespace-stripped and lower-cased before
    hashing (judgment call: two task descriptions differing only in
    surrounding whitespace or letter case are the same task signature for
    this story's purposes; no AC specifies normalization, so this choice
    is documented here rather than left implicit).

    Args:
        task: The raw task description/signature to hash.

    Returns:
        The 64-character lowercase hex SHA-256 digest of the normalized
        `task`.

    Raises:
        ValueError: If `task` is blank (before or after normalization).
    """
    normalized = task.strip().lower()
    if not normalized:
        raise ValueError("hash_task_signature requires a non-blank task")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
