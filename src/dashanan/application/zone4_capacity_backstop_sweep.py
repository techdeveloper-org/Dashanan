"""Zone4CapacityBackstopSweep: orchestration for Zone 4's OAQ-4-class backstop (DASH-STORY-016).

HLD Section 12A (Zone 4 row: cap 50,000 / MaxAge 730 days) / HLD Section
12F (Compressed dwell bounded only by this backstop while Zone 8 is
absent) + HLD Section 7.6 (`memory.evicted` event contract), traced to
FR-004 in SRS.md.

FR-004 (verbatim): "The system SHALL provide a Procedural Memory zone
holding learned task procedures, tool-use patterns, and successful action
sequences."

AC-004-CAP-1 (verbatim): "When Zone 4 reaches its capacity cap (50,000) or
an item exceeds MaxAge (730 days) in Compressed, the backstop sweep
forcibly evicts the lowest-ranked-by-MemoryScore Procedure(s), mirroring
the Zone 2 backstop pattern."
AC-004-CAP-4 (verbatim): "Capacity cap and MaxAge are deployment
configuration; changing either takes effect on the next sweep without a
code change."

This module holds the orchestration `dashanan.domain.zone4_capacity_
backstop`'s own docstring assigns here: loading config fresh per sweep,
fetching candidates, invoking the eviction/retrieval-index ports, and
publishing `memory.evicted` -- mirroring the `domain.zone2_capacity_
backstop` (pure types/functions) + `application.
zone2_capacity_backstop_sweep` (orchestration + logging) split
DASH-STORY-004 already established, which AC-004-CAP-1 explicitly names
as the pattern this story mirrors. Unlike Zone 2's own sweep, this module
has NO DPDP-erasure branch: Zone 4 (Procedural) carries no DPDP erasure
acceptance criterion (dev_prompt PII note: "DASH-STORY-016 has no DPDP
acceptance criterion attached, since Zone 4 content is not classification-
ceiling material; no redaction is required"), so `_execute_one` below is a
plain two-step eviction (retrieval-index cleanup, then the zone-local
removal) with no obligation-check branch.

MUST-NOT-DEVIATE (dev_prompt, binding):
  1. "Cap and MaxAge are deployment config, read per sweep run, no code
     change (AC-004-CAP-4)" -- `run()` calls `Zone4BackstopConfigPort.
     load(tenant_id)` on every invocation; `Zone4CapacityBackstopSweep`
     never caches a config value across calls or in `__init__`.
  2. "Eviction ordering is lowest-MemoryScore-first" -- `run()` iterates
     `plan_backstop_eviction`'s returned list in the order the domain
     module already guarantees, and executes each decision sequentially
     (never reordered, never parallelized).
  3. "Do not assume Zone 4 reaches Archived in this story" -- every
     decision this sweep executes ends in exactly one outcome, `evict`
     (ordinary Zone 4 removal); nothing in this module ever writes an
     `Archived` state or a Zone 8 destination.
  4. "Do not duplicate the generic deadline-scheduled sweep engine built
     in DASH-STORY-009" -- this module's `run()` is a capacity/MaxAge
     backstop sweep only; it never pops a rotation-deadline timer wheel,
     never calls `guarded_transition`, and never publishes
     `memory.compressed` -- that lifecycle is `dashanan.application.
     rotation_engine.RotationEngine`'s sole responsibility, wired
     separately (see `tests/test_zone4_rotation_policy_dash016.py`'s own
     module docstring for why a concrete production adapter into that
     engine is explicitly out of this story's scope).

Zone 6 projection cleanup: `InMemoryProceduralMemoryRepository`'s own
docstring records Zone 4's HLD 3.10 ownership line, "PROJECTS INTO: Zone
6" -- every `commit` publishes a projection event so a Zone 6 index
worker can index the item. An ordinary backstop eviction that removes the
Zone 4 row without also removing Zone 6's own indexed copy would leave an
evicted Procedure indefinitely retrievable via Zone 6 search -- exactly
the partial-erasure defect DSHN-58's live adversarial review found (and
fixed) for Zone 2's own backstop (`zone2_capacity_backstop_sweep.py`'s own
"REMEDIATION (P1, DSHN-58 attempt 1)" section). `_execute_one` below
therefore calls `Zone4RetrievalIndexEvictionPort.delete_item`
UNCONDITIONALLY, before the zone-local `Zone4EvictionPort.evict` call, for
the exact same "safer-failure-mode" ordering `zone2_capacity_backstop_
sweep._execute_one` documents: if the Zone 6 call fails, Zone 4 is
untouched and the whole decision surfaces as a clean per-item
`EvictionFailure`, retried on the next sweep run.

JUDGMENT CALL: adding this Zone 6 cleanup call was not spelled out
verbatim in this story's own AC text (which only names the Zone 4 removal
itself); it is included here because AC-004-CAP-1 explicitly instructs
"mirroring the Zone 2 backstop pattern," and the CURRENT (DSHN-58-
remediated) Zone 2 pattern -- not its pre-remediation form -- is the one a
"mirror" instruction should reasonably track, given the same Zone-
projects-into-Zone-6 ownership shape applies identically to Zone 4. Flagged
here, and in this story's dev report, for reviewer visibility.

PII NOTE: every port and payload here carries only ranking/config/control
metadata -- `item_id`, `tenant_id`, `EvictionReason`, timestamps -- never
a Procedure's `steps` payload (dev_prompt's PII constraint, mirrored from
`zone2_capacity_backstop_sweep.py`'s identical PII posture).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from dashanan.domain.exceptions import DashananError
from dashanan.domain.ports import Clock, EventBus
from dashanan.domain.zone4_capacity_backstop import (
    EvictionDecision,
    EvictionReason,
    ProcedureEvictionCandidate,
    Zone4BackstopConfig,
    plan_backstop_eviction,
)

logger = logging.getLogger(__name__)

_EVICTED_EVENT_TYPE = "memory.evicted"
"""HLD Section 7.6's exact event name for this backstop's `memory.evicted` publication."""

_PROCEDURAL_ZONE_LABEL = "procedural"
"""HLD Section 7.6's `memory.evicted` payload `zone` field, for the zone this story owns."""


class Zone4BackstopPortError(DashananError):
    """Raised by one of this module's ports on an expected, operational failure.

    Kept local to this module rather than added to the shared
    `dashanan.domain.exceptions` module -- mirrors `zone2_capacity_
    backstop_sweep.Zone2BackstopPortError`'s identical file-disjointness
    and fail-safe-isolation rationale: a port raising this type is an
    operational failure isolated to the one item being processed, so one
    bad item cannot block the rest of the backstop sweep. A port raising
    anything else is treated as a programming bug (error-handling-
    patterns section 2, fail-fast) and propagates out of `run()`
    immediately, stopping the sweep.
    """


@runtime_checkable
class Zone4BackstopConfigPort(Protocol):
    """Loads the operator-configurable cap/MaxAge pair fresh, per sweep run.

    Kept local to this module (not `dashanan.domain.ports`) per this
    story's file-disjointness requirement -- mirrors `zone2_capacity_
    backstop_sweep.Zone2BackstopConfigPort`'s identical local-Protocol
    choice, and keeps `ZoneRepository` in the shared module untouched
    (AR1-G2).
    """

    def load(self, tenant_id: str) -> Zone4BackstopConfig:
        """Return the CURRENT cap/MaxAge config for `tenant_id`.

        Must read the live operator-configured value on every call (HLD
        Section 7.3's `/v1/zones/{zone}/config` surface, NFR-009) -- never
        a value cached from a previous call (must-not-deviate item 1).

        Raises:
            Zone4BackstopPortError: If the current config cannot be
                loaded (e.g. the config store is unreachable).
        """
        ...


@runtime_checkable
class Zone4CandidateSource(Protocol):
    """Supplies every current Zone 4 item for one tenant, already scored.

    Deliberately narrow (Interface Segregation, mirrors `zone2_capacity_
    backstop_sweep.Zone2CandidateSource`): this port's sole responsibility
    is producing `ProcedureEvictionCandidate` values, decoupled from how
    those values are computed or stored.
    """

    def fetch_candidates(
        self, tenant_id: str, as_of: datetime
    ) -> list[ProcedureEvictionCandidate]:
        """Return every current Zone 4 item for `tenant_id`, scored as of `as_of`.

        Args:
            tenant_id: The tenant whose Zone 4 items to fetch. No zone
                query may omit it (HLD 3.0 invariant 2).
            as_of: The instant to compute each candidate's `age_seconds`
                relative to -- always the sweep's own `Clock.now()`,
                injected so results are deterministic in tests
                (testing-core: DI over patching `datetime.now`).

        Raises:
            Zone4BackstopPortError: If candidates cannot be fetched
                (e.g. the zone's repository is unreachable).
        """
        ...


@runtime_checkable
class Zone4EvictionPort(Protocol):
    """Performs the forced removal of one Zone 4 item.

    Mirrors `zone2_capacity_backstop_sweep.Zone2EvictionPort`'s identical
    single-zone-removal contract, minus the DPDP-obligation-check branch
    Zone 2's own port docstring describes -- this story's own PII note
    establishes Zone 4 carries no DPDP erasure obligation, so there is no
    "ordinary vs. DPDP-cascade" branch to route between here.
    """

    def evict(self, tenant_id: str, item_id: str) -> None:
        """Remove `item_id` from `tenant_id`'s Zone 4, and nowhere else.

        Raises:
            Zone4BackstopPortError: If the removal cannot be completed
                (e.g. the underlying store is unreachable).
        """
        ...


@runtime_checkable
class Zone4RetrievalIndexEvictionPort(Protocol):
    """Removes one item from Zone 6's retrieval index (vector + lexical surfaces).

    Mirrors `zone2_capacity_backstop_sweep.RetrievalIndexEvictionPort`,
    applied here because Zone 4 also "PROJECTS INTO: Zone 6" (HLD 3.10) --
    see this module's docstring, "Zone 6 projection cleanup" section, for
    why an ordinary Zone 4 eviction must also reach Zone 6.
    """

    def delete_item(self, tenant_id: str, item_id: str) -> None:
        """Remove `item_id` from `tenant_id`'s Zone 6 vector + lexical surfaces.

        Idempotent: removing an item that was never indexed, or already
        removed, is a silent no-op (mirrors `VectorIndexPort.delete`/
        `LexicalIndexPort.delete`'s own idempotent contract).

        Raises:
            Zone4BackstopPortError: If the removal cannot be completed
                (e.g. an underlying index is unreachable).
        """
        ...


@dataclass(frozen=True, slots=True)
class EvictionOutcome:
    """One item this sweep successfully evicted, and how.

    Attributes:
        item_id: The evicted item's identifier.
        reason: Which trigger produced the eviction decision.
    """

    item_id: str
    reason: EvictionReason


@dataclass(frozen=True, slots=True)
class EvictionFailure:
    """One item this sweep attempted to evict but could not.

    A Result-type field (HLD Section 6, "Degraded responses" row) rather
    than a raised exception propagating out of `run()`: an individual
    item's operational failure is an expected, partial outcome for a
    background sweep, not a reason to abort every other item's eviction
    in the same run.

    Attributes:
        item_id: The item this sweep failed to evict.
        reason: Which trigger had selected this item.
        error: `str(exc)` from the `Zone4BackstopPortError` that was
            raised. Ports are contractually responsible for keeping this
            message PII-free.
    """

    item_id: str
    reason: EvictionReason
    error: str


@dataclass(frozen=True, slots=True)
class BackstopSweepResult:
    """The outcome of one `Zone4CapacityBackstopSweep.run()` call.

    Attributes:
        tenant_id: The tenant this sweep ran for.
        evicted: Every item this run evicted, in the same lowest-
            MemoryScore-first order `plan_backstop_eviction` produced.
        failed: Every item this run attempted but could not evict.
    """

    tenant_id: str
    evicted: tuple[EvictionOutcome, ...]
    failed: tuple[EvictionFailure, ...]


class Zone4CapacityBackstopSweep:
    """The Zone 4 backstop: one sweep run per `run(tenant_id)` call.

    Composed from four ports plus `Clock` -- `Zone4BackstopConfigPort`,
    `Zone4CandidateSource`, `Zone4EvictionPort`, `Zone4RetrievalIndex
    EvictionPort` -- and the existing `EventBus`/`Clock`
    (`dashanan.domain.ports`, reused unmodified). Construction never
    touches I/O; every fresh-per-run requirement (must-not-deviate item 1)
    is satisfied inside `run`, never in `__init__`.
    """

    def __init__(
        self,
        config_port: Zone4BackstopConfigPort,
        candidate_source: Zone4CandidateSource,
        eviction_port: Zone4EvictionPort,
        retrieval_index_eviction: Zone4RetrievalIndexEvictionPort,
        event_bus: EventBus,
        clock: Clock,
    ) -> None:
        self._config_port = config_port
        self._candidate_source = candidate_source
        self._eviction_port = eviction_port
        self._retrieval_index_eviction = retrieval_index_eviction
        self._event_bus = event_bus
        self._clock = clock

    def run(self, tenant_id: str) -> BackstopSweepResult:
        """Run one backstop sweep for `tenant_id` (Template Method, HLD Section 6).

        Steps, specialized for Zone 4's cap/MaxAge backstop:

          1. Load the cap/MaxAge config fresh (must-not-deviate item 1).
          2. Fetch every current candidate, scored as of `Clock.now()`.
          3. Plan the eviction list via the domain module's
             `plan_backstop_eviction` (ordering: must-not-deviate item 2;
             no Archived outcome anywhere in that function's return type:
             must-not-deviate item 3).
          4. Execute each decision in order, publishing one
             `memory.evicted` event per success and recording one
             `EvictionFailure` per operational failure.

        Args:
            tenant_id: The tenant to sweep. Never blank.

        Returns:
            A `BackstopSweepResult` covering every decision this run
            produced -- `evicted` and `failed` together always account
            for every `EvictionDecision` `plan_backstop_eviction`
            returned.

        Raises:
            ValueError: If `tenant_id` is blank.
            Zone4BackstopPortError: If `config_port.load` or
                `candidate_source.fetch_candidates` fails -- these are
                whole-sweep preconditions, not per-item operations, so
                their failure aborts the entire run rather than being
                recorded as a per-item `EvictionFailure`.
        """
        if not tenant_id.strip():
            raise ValueError(
                "Zone4CapacityBackstopSweep.run requires a non-blank tenant_id"
            )

        config = self._config_port.load(tenant_id)
        as_of = self._clock.now()
        candidates = self._candidate_source.fetch_candidates(tenant_id, as_of)
        decisions = plan_backstop_eviction(candidates, config)

        evicted: list[EvictionOutcome] = []
        failed: list[EvictionFailure] = []
        for decision in decisions:
            try:
                self._execute_one(tenant_id, decision)
            except Zone4BackstopPortError as exc:
                logger.error(
                    "zone4 backstop sweep failed to evict one item",
                    extra={
                        "tenant_id": tenant_id,
                        "item_id": decision.item_id,
                        "reason": decision.reason.value,
                    },
                    exc_info=True,
                )
                failed.append(
                    EvictionFailure(
                        item_id=decision.item_id,
                        reason=decision.reason,
                        error=str(exc),
                    )
                )
                continue

            self._event_bus.publish(
                _EVICTED_EVENT_TYPE,
                {
                    "tenant_id": tenant_id,
                    "item_id": decision.item_id,
                    "zone": _PROCEDURAL_ZONE_LABEL,
                    "reason": decision.reason.value,
                    "evicted_at": as_of.isoformat(),
                },
            )
            logger.info(
                "zone4 backstop sweep evicted item",
                extra={
                    "tenant_id": tenant_id,
                    "item_id": decision.item_id,
                    "reason": decision.reason.value,
                },
            )
            evicted.append(
                EvictionOutcome(item_id=decision.item_id, reason=decision.reason)
            )

        return BackstopSweepResult(
            tenant_id=tenant_id,
            evicted=tuple(evicted),
            failed=tuple(failed),
        )

    def _execute_one(self, tenant_id: str, decision: EvictionDecision) -> None:
        """Evict one Procedure from Zone 4 and clean up its Zone 6 projection.

        Ordering (safer-failure-mode rationale, mirrored from
        `zone2_capacity_backstop_sweep._execute_one`'s identical choice):
        Zone 6 is removed BEFORE `Zone4EvictionPort.evict`. If the Zone-6
        call then fails, Zone 4 is untouched -- the whole decision is a
        clean per-item `EvictionFailure`, retried on the next sweep run,
        with no state where Zone 4 has already lost the item while the
        sweep still reports it as failed.
        """
        self._retrieval_index_eviction.delete_item(tenant_id, decision.item_id)
        self._eviction_port.evict(tenant_id, decision.item_id)
