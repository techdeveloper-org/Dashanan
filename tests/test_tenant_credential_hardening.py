"""Tests for the DSHN-60 HIGH remediation on HLD Threat S-1 (tenant impersonation).

Covers `domain.tenant_credential` (the pure HMAC sign/verify primitive,
mirroring `write_gate`'s already-shipped S-2 `UserTurnAttestation`) and its
wiring into `MemoryOrchestrator.assemble_context` (`_verify_tenant_
credential`).

Runtime assumptions recorded per rule 33/40 test-roadmap conventions:
  - clock: FakeClock, fixed at 2026-01-01T00:00:00+00:00 unless a test
    states otherwise.
  - tenant_id: "tenant-1" for all requests unless stated otherwise.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from dashanan.application.context_assembly_request import ContextAssemblyRequest
from dashanan.application.memory_orchestrator import MemoryOrchestrator
from dashanan.domain.tenant_credential import (
    TenantAuthenticationError,
    TenantCredential,
    sign_tenant_credential,
    verify_tenant_credential,
)

_FIXED_TS = datetime(2026, 1, 1, tzinfo=UTC)
_SIGNING_KEY = b"tenant-credential-suite-test-only-signing-key-32"
_OTHER_KEY = b"a-different-signing-key-the-orchestrator-never-holds"


class FakeClock:
    def __init__(self, fixed: datetime) -> None:
        self._fixed = fixed

    def now(self) -> datetime:
        return self._fixed


class RecordingEventBus:
    def publish(self, event_type: str, payload: dict[str, object]) -> None:
        return None


def _request(**overrides: object) -> ContextAssemblyRequest:
    defaults: dict[str, object] = {
        "tenant_id": "tenant-1",
        "session_id": "session-1",
        "task": "recall the last order",
        "token_budget": 1000,
    }
    defaults.update(overrides)
    return ContextAssemblyRequest(**defaults)  # type: ignore[arg-type]


class TestVerifyTenantCredentialPure:
    def test_genuine_credential_verifies(self) -> None:
        credential = sign_tenant_credential(
            secret=_SIGNING_KEY, tenant_id="tenant-1", issued_at=_FIXED_TS
        )
        assert (
            verify_tenant_credential(
                credential,
                secret=_SIGNING_KEY,
                tenant_id="tenant-1",
                now=_FIXED_TS,
            )
            is True
        )

    def test_missing_credential_does_not_verify(self) -> None:
        assert (
            verify_tenant_credential(
                None, secret=_SIGNING_KEY, tenant_id="tenant-1", now=_FIXED_TS
            )
            is False
        )

    def test_credential_genuinely_signed_for_a_different_tenant_id_does_not_verify(
        self,
    ) -> None:
        """The exact HLD Threat S-1 scenario: a credential minted for
        tenant A must never verify against a claim of tenant B."""
        credential_for_tenant_a = sign_tenant_credential(
            secret=_SIGNING_KEY, tenant_id="tenant-A", issued_at=_FIXED_TS
        )
        assert (
            verify_tenant_credential(
                credential_for_tenant_a,
                secret=_SIGNING_KEY,
                tenant_id="tenant-B",
                now=_FIXED_TS,
            )
            is False
        )

    def test_credential_signed_with_a_different_key_does_not_verify(self) -> None:
        forged = sign_tenant_credential(
            secret=_OTHER_KEY, tenant_id="tenant-1", issued_at=_FIXED_TS
        )
        assert (
            verify_tenant_credential(
                forged, secret=_SIGNING_KEY, tenant_id="tenant-1", now=_FIXED_TS
            )
            is False
        )

    def test_expired_credential_does_not_verify(self) -> None:
        credential = sign_tenant_credential(
            secret=_SIGNING_KEY, tenant_id="tenant-1", issued_at=_FIXED_TS
        )
        far_future = _FIXED_TS + timedelta(seconds=301)
        assert (
            verify_tenant_credential(
                credential,
                secret=_SIGNING_KEY,
                tenant_id="tenant-1",
                now=far_future,
            )
            is False
        )

    def test_credential_dated_in_the_future_does_not_verify(self) -> None:
        future_issued = _FIXED_TS + timedelta(seconds=10)
        credential = TenantCredential(issued_at=future_issued, signature="deadbeef")
        assert (
            verify_tenant_credential(
                credential, secret=_SIGNING_KEY, tenant_id="tenant-1", now=_FIXED_TS
            )
            is False
        )

    def test_tampered_signature_does_not_verify(self) -> None:
        credential = sign_tenant_credential(
            secret=_SIGNING_KEY, tenant_id="tenant-1", issued_at=_FIXED_TS
        )
        tampered = TenantCredential(
            issued_at=credential.issued_at,
            signature="0" + credential.signature[1:],
        )
        assert (
            verify_tenant_credential(
                tampered, secret=_SIGNING_KEY, tenant_id="tenant-1", now=_FIXED_TS
            )
            is False
        )


class TestMemoryOrchestratorTenantVerificationExplicitlyDisabled:
    def test_assemble_context_succeeds_with_no_credential_when_signing_key_explicitly_none(
        self,
    ) -> None:
        """An Orchestrator explicitly constructed with
        `tenant_credential_signing_key=None` (DSHN-60 attempt 3: this
        parameter is required, no silent default) still supports the
        unverified posture -- no credential required -- but only when a
        caller states that choice visibly, not by omission."""
        orchestrator = MemoryOrchestrator(
            zone_repositories={},
            event_bus=RecordingEventBus(),
            clock=FakeClock(_FIXED_TS),
            tenant_credential_signing_key=None,
        )
        result = orchestrator.assemble_context(_request())
        assert result.degraded is True  # no zones registered -- unrelated to auth


class TestMemoryOrchestratorTenantVerificationEnabled:
    def _orchestrator(self) -> MemoryOrchestrator:
        return MemoryOrchestrator(
            zone_repositories={},
            event_bus=RecordingEventBus(),
            clock=FakeClock(_FIXED_TS),
            tenant_credential_signing_key=_SIGNING_KEY,
        )

    def test_missing_tenant_credential_is_rejected(self) -> None:
        orchestrator = self._orchestrator()
        with pytest.raises(TenantAuthenticationError):
            orchestrator.assemble_context(_request(tenant_credential=None))

    def test_genuine_tenant_credential_is_accepted(self) -> None:
        orchestrator = self._orchestrator()
        credential = sign_tenant_credential(
            secret=_SIGNING_KEY, tenant_id="tenant-1", issued_at=_FIXED_TS
        )
        result = orchestrator.assemble_context(
            _request(tenant_credential=credential)
        )
        assert result is not None

    def test_credential_for_a_different_tenant_is_rejected(self) -> None:
        """HLD Threat S-1 end to end: a caller with a genuine credential
        for tenant A cannot use it to claim to be tenant B."""
        orchestrator = self._orchestrator()
        credential_for_tenant_a = sign_tenant_credential(
            secret=_SIGNING_KEY, tenant_id="tenant-A", issued_at=_FIXED_TS
        )
        with pytest.raises(TenantAuthenticationError) as exc_info:
            orchestrator.assemble_context(
                _request(tenant_id="tenant-B", tenant_credential=credential_for_tenant_a)
            )
        assert exc_info.value.tenant_id == "tenant-B"

    def test_forged_credential_is_rejected_before_any_zone_is_queried(self) -> None:
        orchestrator = self._orchestrator()
        forged = sign_tenant_credential(
            secret=_OTHER_KEY, tenant_id="tenant-1", issued_at=_FIXED_TS
        )
        with pytest.raises(TenantAuthenticationError):
            orchestrator.assemble_context(
                _request(tenant_credential=forged)
            )


class TestMemoryOrchestratorSigningKeyMinimumLength:
    def test_a_signing_key_shorter_than_32_bytes_is_rejected_at_construction(
        self,
    ) -> None:
        with pytest.raises(ValueError, match="32 bytes"):
            MemoryOrchestrator(
                zone_repositories={},
                event_bus=RecordingEventBus(),
                clock=FakeClock(_FIXED_TS),
                tenant_credential_signing_key=b"too-short",
            )
