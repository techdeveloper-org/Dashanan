"""ContextAssemblyRequest: input DTO for the unified context-assembly API."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from dashanan.domain.zone import ZoneId


@dataclass(frozen=True, slots=True)
class ContextAssemblyRequest:
    """A host's call into `MemoryOrchestrator.assemble_context`.

    Mirrors the `POST /v1/context/assemble` request shape (HLD Section
    7.1) at the in-process facade level, ahead of the wire/gRPC layer a
    later story adds. `zones` is optional by design (AC-009): a host
    that omits it gets every zone the Orchestrator has registered, with
    no zone-level knowledge required.

    Attributes:
        tenant_id: The verified tenant, normally injected from the
            credential at the wire boundary (HLD 7.1); passed explicitly
            here since that boundary does not exist yet this story.
        session_id: The host's session identifier.
        task: Free-text task/query description. Required unless
            `query_embedding` is supplied.
        query_embedding: Precomputed embedding, skips the embedding step.
        token_budget: The caller's available context budget.
        zones: Explicit zone subset to route to. `None` means all zones
            currently registered with the Orchestrator.
        max_items: Upper bound on returned items. Defaults to 50 per HLD
            7.1.
        min_provenance_conf: Host-side trust floor on provenance
            confidence. Defaults to 0.0 (no filtering).
        as_of: Optional temporal query bound (episodic zone).
    """

    tenant_id: str
    session_id: str
    task: str | None
    token_budget: int
    query_embedding: list[float] | None = None
    zones: list[ZoneId] | None = None
    max_items: int = 50
    min_provenance_conf: float = 0.0
    as_of: datetime | None = None

    def __post_init__(self) -> None:
        """Validate the preconditions this DTO must uphold before any zone adapter runs.

        Raises:
            ValueError: If neither `task` nor `query_embedding` is given,
                if `token_budget` is not positive, or if `max_items` is not
                positive.
        """
        if self.task is None and self.query_embedding is None:
            raise ValueError(
                "ContextAssemblyRequest requires 'task' or 'query_embedding'"
            )
        if self.token_budget <= 0:
            raise ValueError(
                f"token_budget must be positive, got {self.token_budget}"
            )
        if self.max_items <= 0:
            raise ValueError(
                f"max_items must be positive, got {self.max_items}"
            )
