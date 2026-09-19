"""VectorEntry: Zone 6's owned vector-index entity (HLD Section 3.7, FR-006, ADR-007).

`VectorEntry(tenant_id, item_id, source_zone, vector, model_id, quantization)` is
HLD Section 3.7's owned-entity shape for Zone 6, taken verbatim. Zone 6 is a
derived tier (HLD 3.7): every entry is a projection of an item owned by another
zone, never primary content of its own. HLD Section 10 (threat I-4) treats
embeddings as PII-equivalent -- "embedding-inversion techniques can reconstruct
substantial source text, so a vector is not an anonymization" -- which is why
this module carries no source text field at all; `LexicalDoc` (this story's
sibling module) is the only Zone 6 entity that stores text.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import Enum

from dashanan.domain.zone import ZoneId

IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
"""Allowed charset for `tenant_id`/`item_id` (DASH-STORY-007 remediation,
MEDIUM finding): alphanumeric, underscore, period, and hyphen only.

Defense-in-depth alongside `HybridRetrievalIndexRepository._catalog_key`'s
own escaping fix (P1 remediation) -- that escaping already makes the catalog
key construction collision-free for ANY character content, but this
allow-list additionally stops identifiers containing the catalog key's own
separator/escape characters (`:`, `\\`), control characters, or other
punctuation from ever reaching the index ports or the catalog at all,
consistent with `tenant_id`/`item_id` being opaque routing keys, never
free-form content (contrast with `LexicalDoc.text`, which is deliberately
unrestricted since it is indexed/tokenized content)."""

INDEXABLE_ZONES: frozenset[ZoneId] = frozenset(
    {
        ZoneId.EPISODIC,
        ZoneId.SEMANTIC,
        ZoneId.PROCEDURAL,
        ZoneId.ENTITY,
        ZoneId.CONSOLIDATION,
    }
)
"""The zones AC-006 requires Zone 6 to index: 2/3/4/5/8 (HLD Section 3.7).

Zone 1 is deliberately excluded (HLD 3.7: "Zone 1 is deliberately NOT indexed
in Zone 6"; must-not-deviate item 5, AR1-007). Zone 6 never indexes itself
(it owns no primary facts, HLD 3.7) and Zone 7 is never a Zone 6 source (HLD
3.7/3.10: provenance is a distinct derived/cross-cutting tier, OAQ-16)."""


class VectorQuantization(str, Enum):
    """Storage precision for one `VectorEntry` (HLD Section 12D capacity table)."""

    FLOAT32 = "float32"
    INT8 = "int8"


@dataclass(frozen=True, slots=True)
class VectorEntry:
    """One tenant's vector-index row for one item (HLD Section 3.7 owned entity).

    Attributes:
        tenant_id: Owning tenant. Every `VectorEntry` belongs to exactly one
            tenant's physical collection (ADR-007, ADR-013) -- there is no
            cross-tenant instance of this type.
        item_id: Identifier of the source item within `source_zone`.
        source_zone: Which zone owns the item this entry projects. Must be
            one of `INDEXABLE_ZONES`; Zone 1 and Zone 7 are structurally
            rejected by `__post_init__`.
        vector: The embedding, as an immutable tuple so a `VectorEntry` stays
            hashable-by-value like every other frozen domain entity in this
            package.
        model_id: Identifies which embedding adapter produced `vector`
            (ADR-015) -- required because a tenant's collection may mix
            entries from more than one model generation over its lifetime.
        quantization: Storage precision (HLD Section 12D: Profile B assumes
            int8 for its capacity budget). Defaults to float32 for the
            common case of no quantization applied yet.
    """

    tenant_id: str
    item_id: str
    source_zone: ZoneId
    vector: tuple[float, ...]
    model_id: str
    quantization: VectorQuantization = VectorQuantization.FLOAT32

    def __post_init__(self) -> None:
        """Enforce the invariants ADR-007/HLD 3.7 place on a Zone 6 vector row.

        Raises:
            ValueError: If `tenant_id`, `item_id`, or `model_id` is blank,
                `tenant_id`/`item_id` contain a character outside
                `IDENTIFIER_PATTERN`, `vector` is empty, or `source_zone` is
                not one of the five zones AC-006 requires Zone 6 to index.
        """
        if not self.tenant_id.strip():
            raise ValueError("VectorEntry.tenant_id must not be blank")
        if not IDENTIFIER_PATTERN.match(self.tenant_id):
            raise ValueError(
                f"VectorEntry.tenant_id {self.tenant_id!r} contains characters outside "
                f"the allowed identifier charset {IDENTIFIER_PATTERN.pattern!r}"
            )
        if not self.item_id.strip():
            raise ValueError("VectorEntry.item_id must not be blank")
        if not IDENTIFIER_PATTERN.match(self.item_id):
            raise ValueError(
                f"VectorEntry.item_id {self.item_id!r} contains characters outside "
                f"the allowed identifier charset {IDENTIFIER_PATTERN.pattern!r}"
            )
        if not self.model_id.strip():
            raise ValueError("VectorEntry.model_id must not be blank")
        if not self.vector:
            raise ValueError("VectorEntry.vector must not be empty")
        if self.source_zone not in INDEXABLE_ZONES:
            raise ValueError(
                f"VectorEntry.source_zone {self.source_zone.value!r} is not an "
                "indexable zone (HLD 3.7: Zone 1 excluded, Zone 7 never a "
                "source; must-not-deviate item 5, AR1-007); indexable zones "
                f"are {sorted(z.value for z in INDEXABLE_ZONES)}"
            )


def cosine_similarity(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    """Standard cosine similarity between two vectors, in `[-1, 1]`.

    The dense-retrieval primitive behind Zone 6's vector search (rag-core).
    Shared by the flat in-process index (`InMemoryVectorIndex`) and by any
    test that needs to assert on ranking order without duplicating the math.

    Args:
        a: First vector.
        b: Second vector, must be the same length as `a`.

    Returns:
        The cosine similarity, or `0.0` for a zero-magnitude vector rather
        than raising a division error -- a zero vector cannot be
        meaningfully similar to anything, and `0.0` keeps the caller's
        ranking total (every candidate still gets a score) instead of
        partial.

    Raises:
        ValueError: If `a` and `b` have different lengths.
    """
    if len(a) != len(b):
        raise ValueError(
            f"cosine_similarity requires equal-length vectors, got {len(a)} and {len(b)}"
        )
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)
