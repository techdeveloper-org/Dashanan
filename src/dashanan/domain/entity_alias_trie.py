"""AliasTrie: Zone 5's alias prefix-resolution index (HLD Section 3.6, dsa-core).

HLD Section 3.6 names a Trie as Zone 5's alias-resolution data structure
(dev_prompt: "a Trie for alias prefix resolution"), traced to FR-005 in
SRS.md via DASH-STORY-017. This module holds the pure data structure only
-- no I/O, no tenant/entity storage concerns (those live in
`infrastructure.entity_memory_repository`, which owns one `AliasTrie` per
tenant for physical cross-tenant isolation, mirroring
`domain.retrieval_index_ports.VectorIndexPort`'s "never a shared index"
convention).

Complexity (dsa-core): `insert` and `exact_match` are `O(len(term))`.
`prefix_search` is `O(len(prefix) + k)` where `k` is the number of
entity_ids actually returned -- it navigates to `prefix`'s node in
`O(len(prefix))` and then walks only the subtree rooted there, never the
whole trie, so a prefix match's cost never depends on how many OTHER
aliases are registered elsewhere in the trie.

PII NOTE: this module stores and returns alias strings and entity_id
references only -- never any other entity attribute value (HLD Section
3.10 Hard Rule 2). No worked example here uses a real or HLD-sourced
alias, per this story's PII redaction discipline.
"""

from __future__ import annotations


class _AliasTrieNode:
    """One node: a character-transition table plus the entity_ids terminating here."""

    __slots__ = ("children", "entity_ids")

    def __init__(self) -> None:
        self.children: dict[str, _AliasTrieNode] = {}
        self.entity_ids: set[str] = set()


class AliasTrie:
    """A prefix tree mapping alias strings to entity_ids (AC-005-3, AC-005-4).

    Case-sensitive by design: this module applies no normalization (no
    casefolding, no Unicode NFKC) to an inserted or queried string,
    since HLD Section 3.6's own alias-normalization rule is outside this
    story's admitted context (PII redaction ceiling) -- see the
    DASH-STORY-017 dev report's judgment-call list. A caller that wants
    case-insensitive resolution normalizes before calling `insert`/
    `exact_match`/`prefix_search`.
    """

    def __init__(self) -> None:
        self._root = _AliasTrieNode()

    def insert(self, alias: str, entity_id: str) -> None:
        """Register `entity_id` under `alias`'s exact terminal node.

        Args:
            alias: The alias string. Every character becomes one trie
                edge; an empty string is rejected (there is no
                meaningful "empty alias").
            entity_id: The entity this alias resolves to.

        Raises:
            ValueError: If `alias` is empty or `entity_id` is blank.
        """
        if not alias:
            raise ValueError("AliasTrie.insert requires a non-empty alias")
        if not entity_id.strip():
            raise ValueError("AliasTrie.insert requires a non-blank entity_id")
        node = self._root
        for char in alias:
            node = node.children.setdefault(char, _AliasTrieNode())
        node.entity_ids.add(entity_id)

    def remove(self, alias: str, entity_id: str) -> None:
        """Unregister `entity_id` from `alias`'s terminal node, if present.

        Idempotent: removing an `(alias, entity_id)` pair that was never
        inserted (or already removed) is a silent no-op, mirroring
        `domain.retrieval_index_ports.VectorIndexPort.delete`'s own
        idempotent-delete convention. Exists so a repository can keep
        this index correct when an alias-bearing attribute is
        overwritten with a new alias, without ever needing to rebuild
        the whole trie.
        """
        node = self._navigate(alias)
        if node is not None:
            node.entity_ids.discard(entity_id)

    def exact_match(self, term: str) -> frozenset[str]:
        """Return entity_ids whose registered alias equals `term` exactly (AC-005-4).

        Distinct from `prefix_search`: a `term` that is a strict prefix
        of a longer registered alias (but was never itself inserted as
        a complete alias) yields an empty result here, even though
        `prefix_search(term)` would find the longer alias's entity_ids.

        Returns:
            An empty `frozenset` -- never raises -- when no alias equals
            `term` exactly.
        """
        node = self._navigate(term)
        if node is None:
            return frozenset()
        return frozenset(node.entity_ids)

    def prefix_search(self, prefix: str) -> frozenset[str]:
        """Return every entity_id reachable under `prefix` (AC-005-3).

        Returns:
            An empty `frozenset` -- never raises -- when no registered
            alias starts with `prefix` (AC-005-3's explicit "empty set,
            not error" outcome). An empty `prefix` matches every
            registered alias.
        """
        node = self._navigate(prefix)
        if node is None:
            return frozenset()
        collected: set[str] = set()
        self._collect(node, collected)
        return frozenset(collected)

    def _navigate(self, term: str) -> _AliasTrieNode | None:
        """Return the node for `term`, or `None` if no such path exists."""
        node = self._root
        for char in term:
            node = node.children.get(char)
            if node is None:
                return None
        return node

    def _collect(self, node: _AliasTrieNode, into: set[str]) -> None:
        """DFS the subtree rooted at `node`, accumulating every terminal entity_id."""
        into.update(node.entity_ids)
        for child in node.children.values():
            self._collect(child, into)
