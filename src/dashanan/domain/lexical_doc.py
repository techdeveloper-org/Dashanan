"""LexicalDoc: Zone 6's owned lexical-index entity (HLD Section 3.7, FR-006, ADR-008).

`LexicalDoc(tenant_id, item_id, source_zone, text, fields)` is HLD Section 3.7's
owned-entity shape for Zone 6's second index surface, taken verbatim. Unlike
`VectorEntry`, this type does carry text -- BM25 scoring is defined over term
occurrences, so the lexical index structurally requires the tokenizable content
it indexes. See this story's dev report `judgment-call` notes for the
consequence this has for what `HybridRetrievalIndexRepository.fetch()` can
return as `MemoryItem.payload`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from dashanan.domain.vector_entry import INDEXABLE_ZONES, IDENTIFIER_PATTERN
from dashanan.domain.zone import ZoneId


@dataclass(frozen=True, slots=True)
class LexicalDoc:
    """One tenant's lexical-index row for one item (HLD Section 3.7 owned entity).

    Attributes:
        tenant_id: Owning tenant. Every `LexicalDoc` belongs to exactly one
            tenant's physical index (ADR-008, ADR-013).
        item_id: Identifier of the source item within `source_zone`, shared
            with the corresponding `VectorEntry` for the same item.
        source_zone: Which zone owns the item this entry projects. Must be
            one of `INDEXABLE_ZONES` (the same constraint `VectorEntry`
            enforces, HLD 3.7).
        text: The tokenizable content BM25 scores against.
        fields: Optional named sub-fields (HLD 3.7's owned-entity shape
            names this attribute explicitly) for a future field-weighted
            BM25F extension; unused by this story's flat single-field
            scoring (`InMemoryLexicalIndex`).
    """

    tenant_id: str
    item_id: str
    source_zone: ZoneId
    text: str
    fields: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Enforce the invariants ADR-008/HLD 3.7 place on a Zone 6 lexical row.

        Raises:
            ValueError: If `tenant_id` or `item_id` is blank or contains a
                character outside `IDENTIFIER_PATTERN`, `text` is blank, or
                `source_zone` is not one of the five zones AC-006 requires
                Zone 6 to index.
        """
        if not self.tenant_id.strip():
            raise ValueError("LexicalDoc.tenant_id must not be blank")
        if not IDENTIFIER_PATTERN.match(self.tenant_id):
            raise ValueError(
                f"LexicalDoc.tenant_id {self.tenant_id!r} contains characters outside "
                f"the allowed identifier charset {IDENTIFIER_PATTERN.pattern!r}"
            )
        if not self.item_id.strip():
            raise ValueError("LexicalDoc.item_id must not be blank")
        if not IDENTIFIER_PATTERN.match(self.item_id):
            raise ValueError(
                f"LexicalDoc.item_id {self.item_id!r} contains characters outside "
                f"the allowed identifier charset {IDENTIFIER_PATTERN.pattern!r}"
            )
        if not self.text.strip():
            raise ValueError("LexicalDoc.text must not be blank")
        if self.source_zone not in INDEXABLE_ZONES:
            raise ValueError(
                f"LexicalDoc.source_zone {self.source_zone.value!r} is not an "
                "indexable zone (HLD 3.7: Zone 1 excluded, Zone 7 never a "
                "source; must-not-deviate item 5, AR1-007); indexable zones "
                f"are {sorted(z.value for z in INDEXABLE_ZONES)}"
            )
