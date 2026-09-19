"""RotationZonePolicy: the per-zone Strategy configuration (HLD Section 6, 12A).

Traces to FR-012 in SRS.md. HLD Section 6, "Per-zone rotation policy" row:
"Each zone has a different `lambda_zone`, capacity, TTL, fast-track rule
and compression method. Strategy lets the single RotationEngine execute
eight policies without eight code paths, and lets an operator swap a
policy per deployment (NFR-009) without touching the engine."

This module holds pure domain logic only: the `RotationZonePolicy` Value
Object and its sole construction path, `RotationZonePolicy.for_zone`. It
carries exactly the four scalars `dashanan.domain.rotation_deadline.
compute_rotation_deadline` needs per zone (`lambda_zone`, `theta`,
`max_age_seconds`, `cooldown_seconds`) -- nothing else from HLD Section
12A's fuller per-zone table (capacity cap, sweep cadence, compression
method) belongs to this story's scope.

MUST-NOT-DEVIATE (ar1_assignments.json AR1-009, binding, F4 of the
mathematics-engineer delegation):
  "Sprint 1 scope restriction: theta := CompressThreshold ONLY... Do NOT
  compute or schedule an archive deadline at theta = ArchiveThreshold" --
  this dataclass has exactly one threshold field, `compress_threshold`.
  There is no `archive_threshold` field anywhere in this module, which
  makes computing an archive deadline a type error, not merely a
  convention this story asks callers to follow (must-not-deviate item 2,
  structural enforcement mirroring `rotation_state`'s closed transition
  table).

PII NOTE: every field is zone-level numeric configuration -- a decay
constant, a threshold, a duration -- never item content, mirroring every
other domain module in this story's identical PII posture.
"""

from __future__ import annotations

from dataclasses import dataclass

from dashanan.domain.memory_score import lambda_from_half_life


@dataclass(frozen=True, slots=True)
class RotationZonePolicy:
    """The four scalars one zone's rotation deadline computation needs.

    Immutable Value Object (HLD Section 6 convention, mirrors
    `dashanan.domain.memory_score.ScoreWeights`): constructed once per
    zone config load, passed unchanged through a sweep run.

    Attributes:
        lambda_zone: `ln(2) / t_half` for this zone (HLD Section 12A/12C),
            always the output of `lambda_from_half_life` -- never a
            caller-hardcoded literal (mirrors `memory_score.
            lambda_from_half_life`'s own docstring warning).
        compress_threshold: `CompressThreshold` for this zone (HLD Section
            12A: `0.35` at the Sprint 1 default). The ONLY threshold this
            story's rotation engine ever computes a deadline for (F4).
        max_age_seconds: The zone's MaxAge backstop duration (HLD Section
            12A), in seconds. Used as both the semantic ceiling on a
            computed deadline (F3) and the numeric `u_floor` guard's
            second term (D4) in `compute_rotation_deadline`.
        cooldown_seconds: HLD Section 12A's state-change cooldown, `1 *
            t_half(zone)`: "an item cannot change lifecycle state twice
            within one zone half-life."
    """

    lambda_zone: float
    compress_threshold: float
    max_age_seconds: float
    cooldown_seconds: float

    def __post_init__(self) -> None:
        """Raises:
        ValueError: If `lambda_zone`, `max_age_seconds` is not strictly
            positive, `compress_threshold` is outside `[0, 1]`, or
            `cooldown_seconds` is negative.
        """
        if self.lambda_zone <= 0.0:
            raise ValueError(
                f"RotationZonePolicy.lambda_zone must be positive, "
                f"got {self.lambda_zone}"
            )
        if not (0.0 <= self.compress_threshold <= 1.0):
            raise ValueError(
                f"RotationZonePolicy.compress_threshold must be in [0, 1], "
                f"got {self.compress_threshold}"
            )
        if self.max_age_seconds <= 0.0:
            raise ValueError(
                f"RotationZonePolicy.max_age_seconds must be positive, "
                f"got {self.max_age_seconds}"
            )
        if self.cooldown_seconds < 0.0:
            raise ValueError(
                f"RotationZonePolicy.cooldown_seconds must be >= 0, "
                f"got {self.cooldown_seconds}"
            )

    @classmethod
    def for_zone(
        cls,
        t_half_seconds: float,
        compress_threshold: float,
        max_age_seconds: float,
    ) -> RotationZonePolicy:
        """Build a `RotationZonePolicy` from a zone's operator-configured inputs.

        The preferred construction path: derives `lambda_zone` via
        `lambda_from_half_life` (never a hardcoded literal, mirroring HLD
        Section 12C's "DERIVE lambda FROM t_half AT CONFIG LOAD" contract)
        and sets `cooldown_seconds = t_half_seconds` per HLD Section 12A's
        "1 x `t_half(zone)`" rule, so a caller never has to restate the
        cooldown-equals-half-life relationship itself.

        Args:
            t_half_seconds: The zone's half-life, in seconds (HLD Section
                12A's per-zone table, e.g. Zone 2 Episodic: `172800`).
                Must be strictly positive -- a zone with no decay (Zones 6
                and 7) has no `RotationZonePolicy` at all; the Rotation
                Engine never schedules a deadline for those zones.
            compress_threshold: The zone's `CompressThreshold` (HLD
                Section 12A default `0.35`, operator-overridable per
                NFR-009).
            max_age_seconds: The zone's MaxAge backstop duration, in
                seconds (HLD Section 12A per-zone table).

        Returns:
            The resulting `RotationZonePolicy`.

        Raises:
            ValueError: Propagated from `lambda_from_half_life` (if
                `t_half_seconds <= 0`) or `__post_init__`.
        """
        return cls(
            lambda_zone=lambda_from_half_life(t_half_seconds),
            compress_threshold=compress_threshold,
            max_age_seconds=max_age_seconds,
            cooldown_seconds=t_half_seconds,
        )
