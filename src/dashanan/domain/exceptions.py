"""Domain-level exception hierarchy (error-handling-patterns: no bare except)."""

from __future__ import annotations


class DashananError(Exception):
    """Base class for every exception the Dashanan domain raises."""


class ZoneRepositoryError(DashananError):
    """Raised by a `ZoneRepository` adapter when it cannot serve a fetch.

    The Memory Orchestrator catches this specific type -- never a bare
    `Exception` -- to convert an individual zone failure into a
    `zones_unavailable` entry (AC-009-SUPP-1) instead of an unhandled
    error that would fail the whole assembly.
    """

    def __init__(self, zone: str, reason: str) -> None:
        self.zone = zone
        self.reason = reason
        super().__init__(f"zone '{zone}' repository failed: {reason}")
