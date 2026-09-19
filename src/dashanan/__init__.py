"""Dashanan: a multi-zone memory system for AI hosts.

Public package root. Re-exports the primary application-layer facade so
host integrators can depend on `dashanan.MemoryOrchestrator` without
reaching into the internal package layout.
"""

from dashanan.application.memory_orchestrator import MemoryOrchestrator

__all__ = ["MemoryOrchestrator"]

__version__ = "0.1.0"
