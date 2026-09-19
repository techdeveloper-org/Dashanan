"""Zone2CapacityBackstopSweep: orchestration for the OAQ-4 backstop (DASH-STORY-004).

HLD Section 12A/12F (capacity cap / MaxAge backstop) + HLD Section 6
(Template Method: "pop due items -> re-score -> evaluate guarded
transition -> publish -> reschedule") + HLD Section 7.6 (`memory.evicted`
event contract), traced to FR-002 in SRS.md.

FR-002 (verbatim): "The system SHALL provide an Episodic Memory zone that
stores chronological interaction records with recency-based decay, and
SHALL retrieve them via keyset (cursor) pagination or direct primary-key
lookup."

AC-002-CAP-1 (verbatim, ar1_assignments.json AR1-004 dev_prompt): "Given
Zone 2 reaches capacity cap or an item exceeds MaxAge while Compressed,
the backstop sweep forcibly evicts the lowest-ranked-by-MemoryScore
item(s) so the zone never grows unbounded."

This module holds the orchestration `dashanan.domain.zone2_capacity_
backstop`'s own docstring assigns here: loading config fresh per sweep,
fetching candidates, checking DPDP erasure obligations, invoking the
eviction/cascade ports, and publishing `memory.evicted` -- mirroring the
`domain/write_gate.py` (pure types/functions) + `application/
provenance_write_gate.py` (orchestration + logging) split DASH-STORY-006
already established. The domain module's pure selection functions
(`plan_backstop_eviction` et al.) are the single source of WHICH items to
evict and in what order; this module is the single place that decides
HOW each decision is carried out.

MUST-NOT-DEVIATE (ar1_assignments.json AR1-004, binding):
  1. "Cap and MaxAge are deployment config, read per sweep run, no code
     change (AC-002-CAP-2)" -- `run()` calls `Zone2BackstopConfigPort.
     load(tenant_id)` on every invocation; `Zone2CapacityBackstopSweep`
     never caches a config value across calls or in `__init__`.
  2. "Eviction ordering is lowest-MemoryScore-first" -- `run()` iterates
     `plan_backstop_eviction`'s returned list in the order the domain
     module already guarantees, and executes each decision sequentially
     (never reordered, never parallelized) so the ordering the domain
     layer establishes is also the ordering ports observe and events are
     published in.
  3. "No attempt at an Archived transition -- there is no destination in
     Sprint 1 (HLD 12F)" -- every decision this sweep executes ends in
     exactly one of two outcomes, `evict` (ordinary Zone 2 removal) or
     `fulfil_pending_erasure` (DPDP cascade, which itself also removes
     the item from Zone 2 per DPDP-3's "cascade must reach eight zones,"
     HLD Section 10) -- neither outcome, nor anything else in this
     module, writes an `Archived` state or a Zone 8 destination.
  4. "Eviction must satisfy, not bypass, a pending DPDP erasure
     obligation (AC-002-CAP-DPDP-1)" -- `_execute_one` below routes
     EVERY decision through `DpdpErasureCascadePort.fulfil_pending_
     erasure` FIRST; the ordinary `Zone2EvictionPort.evict` path only
     runs when that call reports no pending obligation existed
     (`fulfil_pending_erasure` returns `False`). This closes the
     check-then-act race a separate "has a pending obligation?" query
     followed by a separate "evict" call would leave open (an
     obligation created between the two calls would otherwise be
     bypassed) -- see this story's dev report, judgment-call list.

REMEDIATION (P1, DSHN-58 attempt 1): a live adversarial review found that
`Zone2EvictionPort.evict` is explicitly Zone-2-only -- its own docstring
says "Remove `item_id` from `tenant_id`'s Zone 2, and nowhere else" -- so
every ORDINARY (non-DPDP) eviction this sweep ever executes left Zone 6's
retrieval index (vector + lexical) still holding its own copy of the
evicted item's payload (`HybridRetrievalIndexRepository`'s own docstring:
"Zone 6's only locally-held copy of the content it indexes"). This was not
an edge case -- it was every non-DPDP eviction, i.e. the common case,
leaving a partial-erasure state across Zone 2/6 as *normal operation*.
`_execute_one` below now calls the new `RetrievalIndexEvictionPort`
UNCONDITIONALLY, after either branch (DPDP or ordinary) completes --
never only on the DPDP path. Zone 6 index/lexical deletion is idempotent
by contract (`VectorIndexPort.delete`/`LexicalIndexPort.delete`'s own
docstrings), so calling it again when the DPDP cascade has already
performed its own Zone-6 leg internally is a harmless, defense-in-depth
no-op, not a double-delete hazard.

PII NOTE: every port and payload here carries only ranking/config/control
metadata -- `item_id`, `tenant_id`, `EvictionReason`, timestamps -- never
an item's payload/fact content (dev_prompt's PII constraint, mirrored
from `zone2_capacity_backstop.py`'s and `provenance_write_gate.py`'s
identical PII posture). This module never reads or logs a DPDP subject's
erased content; `DpdpErasureCascadePort.fulfil_pending_erasure` is
responsible for that cascade's own content-handling, out of this
module's PII-cleared scope (dev_prompt: "You are not security-cleared
for PII; do not attempt to inspect or log evicted payloads").
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from dashanan.domain.exceptions import DashananError
from dashanan.domain.ports import Clock, EventBus
from dashanan.domain.zone2_capacity_backstop import (
    EvictionCandidate,
    EvictionDecision,
    EvictionReason,
    Zone2BackstopConfig,
    plan_backstop_eviction,
)

logger = logging.getLogger(__name__)

_EVICTED_EVENT_TYPE = "memory.evicted"
"""HLD Section 7.6's exact event name for this backstop's `memory.evicted` publication."""

_EPISODIC_ZONE_LABEL = "episodic"
"""HLD Section 7.6's `memory.evicted` payload `zone` field, for the one zone this story owns."""


class Zone2BackstopPortError(DashananError):
    """Raised by one of this module's ports on an expected, operational failure.

    Kept local to this module rather than added to the shared
    `dashanan.domain.exceptions` -- mirrors `zone2_capacity_backstop.
    Zone2CapacityBackstopError`'s identical file-disjointness rationale.
    A port raising anything OTHER than this type is treated as a
    programming bug (error-handling-patterns section 2, fail-fast) and
    propagates out of `run()` immediately, stopping the sweep; a port
    raising THIS type is treated as an operational failure (fail-safe)
    isolated to the one item being processed, so one bad item cannot
    block the rest of the backstop sweep -- HLD Threat D-1/D-2's DoS
    controls depend on a stuck or failing single item never halting the
    zone-wide capacity/MaxAge guarantee this backstop exists to provide.
    """


@runtime_checkable
class Zone2BackstopConfigPort(Protocol):
    """Loads the operator-configurable cap/MaxAge pair fresh, per sweep run.

    Kept local to this module (not `dashanan.domain.ports`) per this
    story's file-disjointness requirement -- mirrors `write_gate.
    ProvenanceJournalPort`'s identical local-Protocol choice, and keeps
    `ZoneRepository` in the shared module untouched (AR1-G2).
    """

    def load(self, tenant_id: str) -> Zone2BackstopConfig:
        """Return the CURRENT cap/MaxAge config for `tenant_id`.

        Must read the live operator-configured value on every call
        (HLD Section 7.3's `/v1/zones/{zone}/config` surface, NFR-009)
        -- never a value cached from a previous call. This is the sole
        mechanism must-not-deviate item 1 depends on: this module's
        `run()` calls `load` exactly once per invocation and never
        memoizes the result across calls.

        Raises:
            Zone2BackstopPortError: If the current config cannot be
                loaded (e.g. the config store is unreachable).
        """
        ...


@runtime_checkable
class Zone2CandidateSource(Protocol):
    """Supplies every current Zone 2 item for one tenant, already scored.

    Deliberately narrow (Interface Segregation): this port's sole
    responsibility is producing `EvictionCandidate` values, decoupled
    from how those values are computed (MemoryScore engine composition,
    `Episode` state lookup) or stored (SQL, in-memory) -- those concerns
    belong to whatever concrete adapter implements this port, not to
    this orchestration module.
    """

    def fetch_candidates(
        self, tenant_id: str, as_of: datetime
    ) -> list[EvictionCandidate]:
        """Return every current Zone 2 item for `tenant_id`, scored as of `as_of`.

        Args:
            tenant_id: The tenant whose Zone 2 items to fetch. No zone
                query may omit it (AR1-003 must-not-deviate item 5,
                consumed here rather than re-litigated).
            as_of: The instant to compute each candidate's `age_seconds`
                and `memory_score` relative to -- always the sweep's own
                `Clock.now()`, injected so results are deterministic in
                tests (testing-core: DI over patching `datetime.now`).

        Raises:
            Zone2BackstopPortError: If candidates cannot be fetched
                (e.g. the zone's repository is unreachable).
        """
        ...


@runtime_checkable
class Zone2EvictionPort(Protocol):
    """Performs the ordinary, DPDP-unrelated forced eviction of one Zone 2 item.

    Used ONLY when `DpdpErasureCascadePort.fulfil_pending_erasure` has
    already reported no pending erasure obligation exists for this item
    (must-not-deviate item 4) -- this port never runs the DPDP cascade
    and never needs to know whether the item is PII-bearing.
    """

    def evict(self, tenant_id: str, item_id: str) -> None:
        """Remove `item_id` from `tenant_id`'s Zone 2, and nowhere else.

        This is a single-zone removal only -- HLD Section 12F's "no
        Archived destination in Sprint 1" applies here structurally:
        this method has no parameter and no return value that could
        carry an Archived/Zone-8 outcome (must-not-deviate item 3).

        Raises:
            Zone2BackstopPortError: If the removal cannot be completed
                (e.g. the underlying store is unreachable).
        """
        ...


@runtime_checkable
class DpdpErasureCascadePort(Protocol):
    """Checks for, and atomically fulfils, a pending DPDP erasure obligation.

    HLD Section 10's DPDP-2 (crypto-shredding erasure) + DPDP-3
    ("the cascade must reach eight zones, the vector index, the lexical
    index and Zone 8 archives... the `subject_id` secondary index on
    every zone table [is] mandatory"). A concrete adapter for this port
    is the cross-zone erasure cascade component DPDP-3 describes; this
    story defines the port this backstop sweep depends on, not that
    adapter's implementation -- see this story's dev report, judgment-
    call list, for why that split is this story's correct scope.
    """

    def fulfil_pending_erasure(self, tenant_id: str, item_id: str) -> bool:
        """Atomically check-and-fulfil a pending erasure obligation for `item_id`.

        A single atomic operation, not a separate check-then-act pair
        (must-not-deviate item 4's rationale in this module's
        docstring): if `item_id`'s subject has a pending DPDP erasure
        obligation, this call performs the full cross-zone cascade
        (DPDP-3) -- which also removes the item from Zone 2, satisfying
        this backstop's own forced-eviction requirement in the same
        operation, so no window exists where Zone 2 is evicted but the
        cascade has not yet reached the zones that referenced it, or
        vice versa.

        Returns:
            `True` if a pending obligation existed and was fulfilled
            (the item is now fully erased, including from Zone 2 -- the
            caller must NOT also call `Zone2EvictionPort.evict` for
            this `item_id`). `False` if no pending obligation existed
            for this item (the caller MUST separately call
            `Zone2EvictionPort.evict` to perform the ordinary backstop
            eviction).

        Raises:
            Zone2BackstopPortError: If the check or the cascade cannot
                be completed (e.g. the erasure-obligation store or a
                referenced zone is unreachable). Must NOT return `False`
                on failure -- a caller that treats a failed check as
                "no obligation" would then wrongly call the ordinary
                `evict` path, which is exactly the bypass AC-002-CAP-
                DPDP-1 forbids.
        """
        ...


@runtime_checkable
class RetrievalIndexEvictionPort(Protocol):
    """Removes one item from Zone 6's retrieval index (vector + lexical surfaces).

    P1 remediation addition (DSHN-58, attempt 1): closes the finding that
    `Zone2EvictionPort.evict` is Zone-2-only, so an ordinary (non-DPDP)
    eviction previously left Zone 6's own payload copy of the item fully
    retrievable via search indefinitely. `run()` below calls this port for
    EVERY eviction decision, DPDP or ordinary alike (must-not-deviate item
    4's cascade already performs its own Zone-6 leg for the DPDP branch,
    so this call is a harmless idempotent no-op there, and the ONLY Zone-6
    cleanup for the ordinary branch).

    A `HybridRetrievalIndexRepository` instance already satisfies this
    Protocol structurally (`delete_item(tenant_id, item_id) -> None`) --
    no change to that class was required to compose it here.
    """

    def delete_item(self, tenant_id: str, item_id: str) -> None:
        """Remove `item_id` from `tenant_id`'s Zone 6 vector + lexical surfaces.

        Idempotent: removing an item that was never indexed, or already
        removed, is a silent no-op (mirrors `VectorIndexPort.delete`/
        `LexicalIndexPort.delete`'s own idempotent contract).

        Raises:
            Zone2BackstopPortError: If the removal cannot be completed
                (e.g. an underlying index is unreachable). Concrete
                adapters that do not natively raise this type must
                translate their own failures into it, mirroring every
                other port in this module.
        """
        ...


@dataclass(frozen=True, slots=True)
class EvictionOutcome:
    """One item this sweep successfully evicted, and how.

    Attributes:
        item_id: The evicted item's identifier.
        reason: Which trigger produced the eviction decision (from
            `EvictionDecision.reason`).
        via_dpdp_cascade: `True` if this item went through
            `DpdpErasureCascadePort.fulfil_pending_erasure` (a pending
            DPDP erasure obligation existed), `False` if it went
            through the ordinary `Zone2EvictionPort.evict` path.
    """

    item_id: str
    reason: EvictionReason
    via_dpdp_cascade: bool


@dataclass(frozen=True, slots=True)
class EvictionFailure:
    """One item this sweep attempted to evict but could not.

    A Result-type field (HLD Section 6, "Degraded responses" row)
    rather than a raised exception propagating out of `run()`: an
    individual item's operational failure is an expected, partial
    outcome for a background sweep (must-not-deviate item... see this
    module's `Zone2BackstopPortError` docstring), not a reason to abort
    every other item's eviction in the same run.

    Attributes:
        item_id: The item this sweep failed to evict.
        reason: Which trigger had selected this item.
        error: `str(exc)` from the `Zone2BackstopPortError` that was
            raised. Ports are contractually responsible for keeping
            this message PII-free (this module's PII note); it is never
            item payload content, only an operational error string.
    """

    item_id: str
    reason: EvictionReason
    error: str


@dataclass(frozen=True, slots=True)
class BackstopSweepResult:
    """The outcome of one `Zone2CapacityBackstopSweep.run()` call.

    Attributes:
        tenant_id: The tenant this sweep ran for.
        evicted: Every item this run evicted, in the same lowest-
            MemoryScore-first order `plan_backstop_eviction` produced
            (must-not-deviate item 2).
        failed: Every item this run attempted but could not evict
            (`Zone2BackstopPortError` only -- see `EvictionFailure`).
    """

    tenant_id: str
    evicted: tuple[EvictionOutcome, ...]
    failed: tuple[EvictionFailure, ...]


class Zone2CapacityBackstopSweep:
    """The Zone 2 OAQ-4 backstop: one sweep run per `run(tenant_id)` call.

    Composed from six ports plus `Clock` -- `Zone2BackstopConfigPort`,
    `Zone2CandidateSource`, `Zone2EvictionPort`, `DpdpErasureCascadePort`,
    `RetrievalIndexEvictionPort` (P1 remediation addition, DSHN-58), and
    the existing `EventBus`/`Clock` (`dashanan.domain.ports`, reused
    unmodified). Construction never touches I/O; every fresh-per-run
    requirement (must-not-deviate item 1) is satisfied inside `run`,
    never in `__init__`.
    """

    def __init__(
        self,
        config_port: Zone2BackstopConfigPort,
        candidate_source: Zone2CandidateSource,
        eviction_port: Zone2EvictionPort,
        dpdp_port: DpdpErasureCascadePort,
        retrieval_index_eviction: RetrievalIndexEvictionPort,
        event_bus: EventBus,
        clock: Clock,
    ) -> None:
        self._config_port = config_port
        self._candidate_source = candidate_source
        self._eviction_port = eviction_port
        self._dpdp_port = dpdp_port
        self._retrieval_index_eviction = retrieval_index_eviction
        self._event_bus = event_bus
        self._clock = clock

    def run(self, tenant_id: str) -> BackstopSweepResult:
        """Run one backstop sweep for `tenant_id` (Template Method, HLD Section 6).

        Steps, each corresponding to the sweep skeleton HLD Section 6
        names for rotation sweeps generally ("pop due items -> re-score
        -> evaluate guarded transition -> publish -> reschedule"),
        specialized here for the OAQ-4 backstop:

          1. Load the cap/MaxAge config fresh (must-not-deviate item 1).
          2. Fetch every current candidate, scored as of `Clock.now()`.
          3. Plan the eviction list via the domain module's
             `plan_backstop_eviction` (ordering: must-not-deviate item
             2; no Archived outcome exists anywhere in that function's
             return type: must-not-deviate item 3).
          4. Execute each decision in order (DPDP-first: must-not-
             deviate item 4), publishing one `memory.evicted` event per
             success and recording one `EvictionFailure` per operational
             failure.

        "Reschedule" (the sweep skeleton's fifth step) is a caller
        concern -- the periodic trigger that invokes `run` again is
        outside this class, mirroring `plan_backstop_eviction`'s own
        "caller" framing for `Zone2BackstopConfig` freshness.

        Args:
            tenant_id: The tenant to sweep. Never blank.

        Returns:
            A `BackstopSweepResult` covering every decision this run
            produced -- `evicted` and `failed` together always account
            for every `EvictionDecision` `plan_backstop_eviction`
            returned.

        Raises:
            ValueError: If `tenant_id` is blank.
            Zone2BackstopPortError: If `config_port.load` or
                `candidate_source.fetch_candidates` fails -- these are
                whole-sweep preconditions, not per-item operations, so
                their failure aborts the entire run rather than being
                recorded as a per-item `EvictionFailure`.
        """
        if not tenant_id.strip():
            raise ValueError("Zone2CapacityBackstopSweep.run requires a non-blank tenant_id")

        config = self._config_port.load(tenant_id)
        as_of = self._clock.now()
        candidates = self._candidate_source.fetch_candidates(tenant_id, as_of)
        decisions = plan_backstop_eviction(candidates, config)

        evicted: list[EvictionOutcome] = []
        failed: list[EvictionFailure] = []
        for decision in decisions:
            try:
                via_dpdp_cascade = self._execute_one(tenant_id, decision)
            except Zone2BackstopPortError as exc:
                logger.error(
                    "zone2 backstop sweep failed to evict one item",
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
                    "zone": _EPISODIC_ZONE_LABEL,
                    "reason": decision.reason.value,
                    "evicted_at": as_of.isoformat(),
                },
            )
            logger.info(
                "zone2 backstop sweep evicted item",
                extra={
                    "tenant_id": tenant_id,
                    "item_id": decision.item_id,
                    "reason": decision.reason.value,
                    "via_dpdp_cascade": via_dpdp_cascade,
                },
            )
            evicted.append(
                EvictionOutcome(
                    item_id=decision.item_id,
                    reason=decision.reason,
                    via_dpdp_cascade=via_dpdp_cascade,
                )
            )

        return BackstopSweepResult(
            tenant_id=tenant_id,
            evicted=tuple(evicted),
            failed=tuple(failed),
        )

    def _execute_one(self, tenant_id: str, decision: EvictionDecision) -> bool:
        """Route one decision through the DPDP-first ordering (must-not-deviate item 4).

        P1 remediation (DSHN-58, attempt 1): this method now calls
        `RetrievalIndexEvictionPort.delete_item` for EVERY decision, DPDP
        or ordinary alike -- Zone 6's own payload copy of `decision.
        item_id` is removed regardless of branch. Previously the ordinary
        branch called only `Zone2EvictionPort.evict` (Zone-2-only by its
        own contract), leaving Zone 6 indefinitely retrievable for every
        routine capacity/MaxAge eviction -- the common case, not an edge
        case.

        Ordering on the ordinary branch (safer-failure-mode rationale,
        mirrored from `CrossZoneDpdpErasureCascade`'s own docstring):
        Zone 6 is removed BEFORE `Zone2EvictionPort.evict`. If the Zone-6
        call then fails, Zone 2 is untouched -- the whole decision is a
        clean per-item `EvictionFailure`, retried on the next sweep run,
        with no state where Zone 2 has already lost the item while the
        sweep still reports it as failed.

        Returns:
            `True` if `decision.item_id` was fulfilled via the DPDP
            erasure cascade, `False` if it went through the ordinary
            `Zone2EvictionPort.evict` path.
        """
        fulfilled_via_dpdp = self._dpdp_port.fulfil_pending_erasure(
            tenant_id, decision.item_id
        )
        self._retrieval_index_eviction.delete_item(tenant_id, decision.item_id)
        if not fulfilled_via_dpdp:
            self._eviction_port.evict(tenant_id, decision.item_id)
        return fulfilled_via_dpdp
