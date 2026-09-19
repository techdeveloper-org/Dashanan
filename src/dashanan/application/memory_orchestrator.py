"""MemoryOrchestrator: the Facade over Dashanan's eight memory zones (FR-009)."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from uuid import uuid4

from dashanan.application.assembly_result import AssemblyResult
from dashanan.application.context_assembly_builder import ContextAssemblyBuilder
from dashanan.application.context_assembly_request import ContextAssemblyRequest
from dashanan.domain.exceptions import ZoneRepositoryError
from dashanan.domain.ports import Clock, EventBus, ZoneQuery, ZoneRepository
from dashanan.domain.tenant_credential import (
    TenantAuthenticationError,
    verify_tenant_credential,
)
from dashanan.domain.zone import ZoneId

logger = logging.getLogger(__name__)

_MIN_SIGNING_KEY_BYTES = 32
"""Mirrors `application.provenance_write_gate._MIN_SIGNING_KEY_BYTES`'s
identical RFC 2104-derived HMAC-SHA256 minimum-key-length rationale."""


class MemoryOrchestrator:
    """Single entry point for cross-zone reads (HLD Section 3.1, FR-009).

    FR-009 (verbatim): "The system SHALL provide a central Memory
    Orchestrator that routes reads and writes across all 8 zones and
    exposes a single, unified context-assembly API to the host AI
    system."

    This is the Facade named in HLD Section 6: a host calls
    `assemble_context` without naming an individual zone (AC-009), and
    the Orchestrator resolves which registered `ZoneRepository` adapters
    to query. A zone with no registered adapter -- every zone, in this
    story, since no zone story has landed yet -- is reported as
    unavailable in a typed `AssemblyResult`, never as an unhandled
    exception (AC-009-SUPP-1). Each zone story after this one registers
    its adapter with this constructor; the Facade itself does not change.
    """

    def __init__(
        self,
        zone_repositories: Mapping[ZoneId, ZoneRepository],
        event_bus: EventBus,
        clock: Clock,
        *,
        tenant_credential_signing_key: bytes | None,
    ) -> None:
        """Compose the Orchestrator from its ports.

        Args:
            zone_repositories: The zones this deployment can currently
                serve. An empty mapping is valid -- every zone then
                degrades per AC-009-SUPP-1 -- and is exactly the state
                of a fresh deployment before any zone story lands.
            event_bus: Where `context.assembled` lifecycle events are
                published. Inject `NoOpEventBus` for Shape A / offline
                deployments (HLD Section 6, "Embedded/offline mode" row).
            clock: Injectable time source, so assembly timestamps are
                deterministic under test (testing-core).
            tenant_credential_signing_key: The host's exclusive secret
                used to verify `ContextAssemblyRequest.tenant_credential`
                (HLD Threat S-1). When provided, every `assemble_context`
                call is rejected (`TenantAuthenticationError`) unless it
                carries a valid, matching `TenantCredential` -- see
                `domain.tenant_credential`'s module docstring.

                DSHN-60 remediation, attempt 3: this parameter carries NO
                default. The prior `= None` default (application-security-
                core: "Secure Defaults... require explicit opt-out for
                less secure options") let every caller in this codebase --
                including a would-be composition root -- silently get
                HLD Threat S-1 verification DISABLED without writing a
                single visible line about it (attempt 2's own re-audit
                confirmed no caller anywhere ever supplied a key). A
                required keyword-only argument cannot have that failure
                mode: every construction site, present or future, must
                now write `tenant_credential_signing_key=None` explicitly
                to get the unverified posture, which is exactly the kind
                of grep-able, code-reviewable, explicit opt-out the
                Secure Defaults principle asks for. Passing an explicit
                `None` still disables verification and still logs the
                one-time warning below -- this change closes the SILENT
                default, not the ability to run without a key, which
                remains a legitimate choice for a single-tenant/embedded
                deployment that has no host-issued credential to check.
        """
        if (
            tenant_credential_signing_key is not None
            and len(tenant_credential_signing_key) < _MIN_SIGNING_KEY_BYTES
        ):
            raise ValueError(
                "MemoryOrchestrator.tenant_credential_signing_key must be at "
                f"least {_MIN_SIGNING_KEY_BYTES} bytes, got "
                f"{len(tenant_credential_signing_key)}"
            )
        self._zone_repositories: dict[ZoneId, ZoneRepository] = dict(
            zone_repositories
        )
        self._event_bus = event_bus
        self._clock = clock
        self._tenant_credential_signing_key = tenant_credential_signing_key
        if tenant_credential_signing_key is None:
            logger.warning(
                "MemoryOrchestrator constructed without "
                "tenant_credential_signing_key -- HLD Threat S-1 tenant-"
                "impersonation verification is DISABLED; every tenant_id "
                "is trusted as caller-asserted with no credential check. "
                "Provide a signing key at the composition root to enable "
                "verification."
            )

    def assemble_context(self, request: ContextAssemblyRequest) -> AssemblyResult:
        """Resolve and assemble context without the host naming a zone.

        Implements AC-009: routes to whichever zones are registered,
        without the caller needing zone-level knowledge. Implements
        AC-009-SUPP-1: a zone with no registered adapter, or whose
        adapter raises `ZoneRepositoryError`, is recorded in
        `zones_unavailable` and never surfaces as an unhandled exception.
        Implements AC-009-R1-1: every returned `AssemblyResult`, success
        or degraded, carries a fresh `assembly_id` and `trace_id`. A
        duplicate `ZoneId` in `request.zones` -- `ContextAssemblyRequest`
        does not itself reject or dedupe the list -- is collapsed to a
        single fetch here, order preserved, so a repeated zone entry can
        never double-fetch and double-add the same items into the
        assembled result.

        Args:
            request: The host's context-assembly call.

        Returns:
            The budget-fitted `AssemblyResult`. `degraded` is True and
            `zones_unavailable` is non-empty whenever at least one
            targeted zone could not be reached.

        Raises:
            TenantAuthenticationError: If this Orchestrator was
                constructed with a `tenant_credential_signing_key` and
                `request.tenant_credential` is missing, expired, or does
                not verify against `request.tenant_id` (HLD Threat S-1).
                Raised BEFORE any zone is queried -- unlike a zone
                failure (AC-009-SUPP-1's degraded-but-never-raises
                contract), a rejected tenant claim is a request this
                method must refuse to serve at all, never a partial
                result.
        """
        self._verify_tenant_credential(request)

        assembly_id = str(uuid4())
        trace_id = str(uuid4())
        target_zones = (
            list(dict.fromkeys(request.zones))
            if request.zones is not None
            else list(ZoneId)
        )

        builder = (
            ContextAssemblyBuilder()
            .with_token_budget(request.token_budget)
            .with_identifiers(assembly_id, trace_id)
        )
        zone_query = ZoneQuery(
            tenant_id=request.tenant_id,
            task=request.task,
            query_embedding=request.query_embedding,
            max_items=request.max_items,
            min_provenance_conf=request.min_provenance_conf,
            as_of=request.as_of,
        )

        for zone in target_zones:
            self._fetch_zone(zone, zone_query, builder, assembly_id, trace_id)

        result = builder.build()
        self._publish_assembled_event(result)
        return result

    def _verify_tenant_credential(self, request: ContextAssemblyRequest) -> None:
        """Enforce HLD Threat S-1 when this Orchestrator has a signing key configured.

        A no-op when `self._tenant_credential_signing_key` is `None`
        (verification not configured for this deployment -- see
        `__init__`'s docstring for that trust-boundary trade-off).
        """
        if self._tenant_credential_signing_key is None:
            return
        if not verify_tenant_credential(
            request.tenant_credential,
            secret=self._tenant_credential_signing_key,
            tenant_id=request.tenant_id,
            now=self._clock.now(),
        ):
            logger.warning(
                "assemble_context rejected: tenant credential missing or "
                "invalid (HLD Threat S-1)",
                extra={"tenant_id": request.tenant_id},
            )
            raise TenantAuthenticationError(
                tenant_id=request.tenant_id,
                reason=(
                    "missing, expired, or invalid TenantCredential for the "
                    "claimed tenant_id"
                ),
            )

    def _fetch_zone(
        self,
        zone: ZoneId,
        zone_query: ZoneQuery,
        builder: ContextAssemblyBuilder,
        assembly_id: str,
        trace_id: str,
    ) -> None:
        """Fetch one zone's candidates into `builder`, degrading on failure.

        Never raises: a missing adapter or a `ZoneRepositoryError` from
        an adapter that does exist both resolve to
        `builder.mark_zone_unavailable(zone)`, per AC-009-SUPP-1.
        """
        repository = self._zone_repositories.get(zone)
        if repository is None:
            logger.warning(
                "zone not available: no ZoneRepository registered",
                extra={
                    "zone": zone.value,
                    "assembly_id": assembly_id,
                    "trace_id": trace_id,
                },
            )
            builder.mark_zone_unavailable(zone)
            return

        try:
            candidates = repository.fetch(zone_query)
        except ZoneRepositoryError:
            logger.error(
                "zone repository fetch failed",
                exc_info=True,
                extra={
                    "zone": zone.value,
                    "assembly_id": assembly_id,
                    "trace_id": trace_id,
                },
            )
            builder.mark_zone_unavailable(zone)
            return

        builder.add_candidates(candidates)

    def _publish_assembled_event(self, result: AssemblyResult) -> None:
        """Publish a `context.assembled` event summarizing this assembly."""
        self._event_bus.publish(
            "context.assembled",
            {
                "assembly_id": result.assembly_id,
                "trace_id": result.trace_id,
                "degraded": result.degraded,
                "zones_unavailable": [
                    zone.value for zone in result.zones_unavailable
                ],
                "items_returned": len(result.items),
                "assembled_at": self._clock.now().isoformat(),
            },
        )
