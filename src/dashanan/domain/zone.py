"""Zone identifiers for the eight Dashanan memory zones (HLD Section 3.2-3.9)."""

from __future__ import annotations

from enum import Enum


class ZoneId(str, Enum):
    """The eight memory zones the Memory Orchestrator can route to.

    Ordering and membership are fixed by HLD Section 3 (component
    boundaries: 3.2 through 3.9) and traced individually to FR-001
    through FR-008. Inherits from `str` so a ZoneId serializes directly
    as its wire value without an explicit converter.
    """

    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"
    ENTITY = "entity"
    RETRIEVAL_INDEX = "retrieval_index"
    PROVENANCE = "provenance"
    CONSOLIDATION = "consolidation"
