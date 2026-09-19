"""Keyset pagination types for Zone 2 (HLD Section 5: "Keyset (cursor) pagination; range scan").

Offset pagination is forbidden on this zone (must-not-deviate item 2,
AR1-003; HLD Section 3.3: "Cursor (keyset) pagination only -- offset
pagination is forbidden on this zone"). These types exist so that
"never OFFSET" is a structural property of the API surface -- there is no
`page_number` or `offset` field anywhere for a caller to pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from dashanan.domain.episode import Episode


@dataclass(frozen=True, slots=True)
class EpisodicCursor:
    """Opaque continuation token for a keyset-paginated Episodic read.

    Encodes the last row seen on `(occurred_at, seq)`, the exact ordering
    key `fetch_page` sorts by, so the next page's predicate is a direct
    index-served comparison (`< (occurred_at, seq)` for the descending
    recency order this zone uses) rather than a counted skip.

    Attributes:
        after_occurred_at: `occurred_at` of the last row on the previous
            page.
        after_seq: `seq` of the last row on the previous page -- the
            tie-breaker for rows sharing the same `occurred_at`.
    """

    after_occurred_at: datetime
    after_seq: int


@dataclass(frozen=True, slots=True)
class EpisodicPage:
    """One page of a keyset-paginated Episodic read.

    Attributes:
        items: Episodes in this page, ordered most-recent-first (HLD
            Section 3.3's recency access pattern), each still carrying
            enough state (`occurred_at`) for the caller to call
            `Episode.decay(as_of)` itself -- AC-002's "each entry exposes
            its own decay attribute".
        next_cursor: Pass back into the next `fetch_page` call to continue.
            `None` when this page reached the end of the result set.
        has_more: Explicit rather than inferred from `len(items)`, so a
            page that exactly fills `page_size` and also happens to be
            the last page is still reported correctly.
    """

    items: list[Episode] = field(default_factory=list)
    next_cursor: EpisodicCursor | None = None
    has_more: bool = False
