"""EntityMemoryRepository: Shape A Zone 5 storage adapter (HLD Section 3.6, FR-005).

FR-005 (verbatim, SRS.md): "The system SHALL provide an Entity Memory
zone holding per-entity (person/project/system) profile records, each
the canonical sole owner of that entity's own attributes." Traced by
DASH-STORY-017, "Entity Memory zone (Zone 5) - canonical per-entity
attribute store with sub-record provenance and alias Trie resolution."

Shape A only (in-process dict storage + `AliasTrie`), mirroring
`infrastructure.working_memory_lru_repository.WorkingMemoryLRURepository`'s
identical "Shape A only, a durable/distributed Shape B adapter is a
separate follow-on" scope note. Zone 5 is a leaf zone (HLD Section 3.11
Dependency Matrix; ar1_assignments.json AR1-S2-017: "Depends only on
DASH-STORY-001") -- this module imports nothing from Zone 6's
`retrieval_index_ports`/vector or lexical adapters, which is also the
structural proof behind AC-005-4 ("...even with Zone 6 unavailable"):
there is no code path here that could reach Zone 6 even if it existed.

MUST-NOT-DEVIATE (sprint2_ar1_assignments.json AR1-S2-017, binding):
  - SemanticEdge/Zone-3-Zone-5 write-conflict-detection sweep: NOT built
    here (FR-013, gated behind FR-003 and FR-005 both existing).
  - Zone 5 is never vector-primary: `resolve_exact_term`/
    `resolve_alias_prefix` are this repository's own mechanism, with no
    Zone 6 dependency of any kind.
  - No other zone may copy an attribute value directly: this module
    never accepts or returns another zone's data, and its own
    `EventBus.publish` projection payload (AC-005-6) carries envelope
    fields only (`item_id`, `zone`) -- never `value`.
  - `write_attribute` never performs a whole-record rewrite: it mutates
    exactly one `dict` key per call (see `write_attribute`'s docstring).
  - No Zone 6 indexing/consumption logic is built here -- only the
    projection event is emitted; a future Zone 6 story owns consuming
    it.
  - No new HTTP-status/Result-type contract is invented for AC-005-5's
    rejection: it reuses `dashanan.domain.write_gate.WriteRejected`/
    `WriteAccepted` (DASH-STORY-006/AC-010's own Result-type shape and
    422 status), even though the specific `error_code` this module
    defines (`ERROR_MISSING_ATTRIBUTE_PROVENANCE_ID`) describes a
    condition none of AC-010's six existing codes names -- see this
    story's dev report, judgment-call list, for why a new code was
    still required.

PII NOTE: this repository stores and serves real per-tenant attribute
values at runtime (that is Zone 5's whole purpose) -- the redaction
constraint governs this module's own documentation and test fixtures
(pseudonymized placeholders only), never its runtime data-handling
capability.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime

from dashanan.domain.entity_alias_trie import AliasTrie
from dashanan.domain.entity_record import EntityAttributeRecord, EntityRecord
from dashanan.domain.memory_item import MemoryItem
from dashanan.domain.ports import Clock, EventBus, ZoneQuery, ZoneRepository
from dashanan.domain.write_gate import WriteAccepted, WriteGateResult, WriteRejected
from dashanan.domain.zone import ZoneId

ERROR_MISSING_ATTRIBUTE_PROVENANCE_ID = "missing_attribute_provenance_id"
"""AC-005-5's rejection reason: a `write_attribute` call with a blank
`provenance_id`. Reuses `WriteRejected`'s 422 Result-type contract
(DASH-STORY-006/AC-010) rather than raising or inventing a new response
shape -- see this module's own docstring, must-not-deviate list."""

_REJECT_HTTP_STATUS = 422
"""Matches `dashanan.application.provenance_write_gate._REJECT_HTTP_STATUS`
-- the same fixed rejection status AC-010 established, reused verbatim."""

PROJECTION_EVENT_TYPE = "zone5.entity_attribute_projected"
"""AC-005-6's projection event type, published to `EventBus` for a future
Zone 6 indexing story to consume. The payload carries envelope fields
only (`tenant_id`, `entity_id`, `attribute_name`, `item_id`, `zone`) --
never `value` (HLD Section 3.10 Hard Rule 2)."""


class EntityMemoryRepository(ZoneRepository):
    """Shape A `ZoneRepository` adapter for Zone 5 (HLD 3.6, FR-005).

    Storage shape: one `dict[str, EntityAttributeRecord]` per `(tenant_id,
    entity_id)`, keyed by `attribute_name` -- this is what makes
    `write_attribute`'s single-attribute update touch exactly one `dict`
    entry (AC-005-2) and `get_entity`'s point lookup (AC-005-1) an `O(1)`
    dict access into that entity's own attribute count, never a scan
    proportional to the total number of entities or attributes stored
    across the whole repository.

    Alias resolution: one `AliasTrie` per tenant (`_alias_tries`),
    mirroring `VectorIndexPort`'s "never a shared index" isolation --
    `resolve_alias_prefix`/`resolve_exact_term` for `tenant_id` can only
    ever see entity_ids registered under that same `tenant_id`'s own
    trie, so cross-tenant isolation here is physical (a separate trie
    object), not a query-time filter.

    Concurrency (ADR-018, mirroring `WorkingMemoryLRURepository`'s
    identical per-key-lock convention): one `threading.Lock` per
    `tenant_id`, lazily created and cached under the short-held
    `_tenant_locks_guard`, serializes every read-modify-write against
    that tenant's own attribute dict AND its own alias trie together --
    a single lock per tenant rather than per entity, because alias
    registration and prefix/exact resolution are tenant-scoped
    operations (one shared trie per tenant), not entity-scoped ones.
    Locks for different tenants are independent, so concurrent writes
    for different tenants are never serialized against each other.
    """

    def __init__(self, clock: Clock, event_bus: EventBus) -> None:
        """Compose the repository from its Clock and EventBus ports.

        Args:
            clock: Injectable time source (testing-core DI) used as the
                default `updated_at` for `write_attribute` when the
                caller does not supply one explicitly.
            event_bus: The AC-005-6 projection sink. `NoOpEventBus`
                satisfies this for Shape A/offline deployments with no
                broker bound, per `EventBus`'s own Null Object
                convention.
        """
        self._clock = clock
        self._event_bus = event_bus
        self._tenant_locks_guard = threading.Lock()
        self._tenant_locks: dict[str, threading.Lock] = {}
        self._entity_attributes: dict[tuple[str, str], dict[str, EntityAttributeRecord]] = {}
        self._alias_tries: dict[str, AliasTrie] = {}

    def _lock_for_tenant(self, tenant_id: str) -> threading.Lock:
        """Return the one `Lock` serializing all access to `tenant_id`'s own state.

        Lazily creates and caches one `threading.Lock` per `tenant_id`
        for this repository instance's lifetime, guarded by
        `_tenant_locks_guard` against two threads racing to create the
        lock itself for the same tenant -- the same check-then-create
        race `WorkingMemoryLRURepository._lock_for_session` closes for
        its own per-key locks.
        """
        with self._tenant_locks_guard:
            lock = self._tenant_locks.get(tenant_id)
            if lock is None:
                lock = threading.Lock()
                self._tenant_locks[tenant_id] = lock
            return lock

    def _trie_for_tenant_locked(self, tenant_id: str) -> AliasTrie:
        """Return `tenant_id`'s `AliasTrie`, creating it if absent.

        Caller must already hold `tenant_id`'s lock (from
        `_lock_for_tenant`) -- this method performs no locking of its
        own.
        """
        trie = self._alias_tries.get(tenant_id)
        if trie is None:
            trie = AliasTrie()
            self._alias_tries[tenant_id] = trie
        return trie

    def write_attribute(
        self,
        tenant_id: str,
        entity_id: str,
        attribute_name: str,
        value: str,
        provenance_id: str,
        updated_at: datetime | None = None,
    ) -> WriteGateResult:
        """Write (or overwrite) exactly one attribute sub-record (AC-005-2, AC-005-5, AC-005-6).

        Args:
            tenant_id: Mandatory; enforced non-blank (HLD 3.0 invariant 2).
            entity_id: Mandatory; enforced non-blank.
            attribute_name: Mandatory; enforced non-blank, propagated
                from `EntityAttributeRecord.__post_init__`.
            value: The attribute's new value. Opaque to this method.
            provenance_id: This attribute's own Zone 7 provenance record
                id. A blank value is rejected (AC-005-5) rather than
                stored -- this is the sole validation this method
                performs beyond the identifier checks above.
            updated_at: When this write happened. Defaults to
                `self._clock.now()` (testing-core: dependency injection
                over reading wall-clock time directly).

        Returns:
            `WriteAccepted` once the attribute has been stored and the
            AC-005-6 projection event published. `WriteRejected` (422,
            `ERROR_MISSING_ATTRIBUTE_PROVENANCE_ID`) if `provenance_id`
            is blank -- in that case nothing is stored and no event is
            published (AC-010's own "rejected before persistence"
            pattern, reused here).

        Raises:
            ValueError: If `tenant_id`, `entity_id`, or `attribute_name`
                is blank.
        """
        self._require_non_blank("tenant_id", tenant_id)
        self._require_non_blank("entity_id", entity_id)
        self._require_non_blank("attribute_name", attribute_name)

        if not provenance_id or not provenance_id.strip():
            return self._reject_missing_provenance_id(
                tenant_id=tenant_id, entity_id=entity_id, attribute_name=attribute_name
            )

        when = updated_at if updated_at is not None else self._clock.now()
        record = EntityAttributeRecord(
            tenant_id=tenant_id,
            entity_id=entity_id,
            attribute_name=attribute_name,
            value=value,
            provenance_id=provenance_id,
            updated_at=when,
        )

        with self._lock_for_tenant(tenant_id):
            entity_attrs = self._entity_attributes.setdefault((tenant_id, entity_id), {})
            # AC-005-2: this is the ONLY dict entry this call ever touches --
            # no sibling attribute_name key is read or rewritten, and no
            # whole-entity object is reconstructed.
            entity_attrs[attribute_name] = record

        self._event_bus.publish(
            PROJECTION_EVENT_TYPE,
            {
                "tenant_id": tenant_id,
                "entity_id": entity_id,
                "attribute_name": attribute_name,
                "item_id": f"{entity_id}:{attribute_name}",
                "zone": ZoneId.ENTITY.value,
            },
        )

        return WriteAccepted(write_id=provenance_id, accepted_at=when)

    def register_alias(self, tenant_id: str, entity_id: str, alias: str) -> None:
        """Index `alias` -> `entity_id` in `tenant_id`'s own `AliasTrie` (AC-005-3 support).

        A pure indexing operation over already-provenanced entity data
        -- unlike `write_attribute`, it carries no `provenance_id` of
        its own and is never rejected with a 422; it is the caller's
        responsibility to have already written whatever attribute an
        alias is meant to make prefix-searchable.

        Args:
            tenant_id: Mandatory; enforced non-blank.
            entity_id: Mandatory; enforced non-blank.
            alias: The alias string to index. Must be non-empty
                (propagated from `AliasTrie.insert`).

        Raises:
            ValueError: If `tenant_id`/`entity_id` is blank, or `alias`
                is empty.
        """
        self._require_non_blank("tenant_id", tenant_id)
        self._require_non_blank("entity_id", entity_id)
        with self._lock_for_tenant(tenant_id):
            trie = self._trie_for_tenant_locked(tenant_id)
            trie.insert(alias, entity_id)

    def get_entity(self, tenant_id: str, entity_id: str) -> EntityRecord | None:
        """Point-lookup one entity's full attribute set by exact key (AC-005-1).

        Args:
            tenant_id: Mandatory; enforced non-blank.
            entity_id: Mandatory; enforced non-blank.

        Returns:
            An `EntityRecord` assembled from every attribute currently
            stored for `(tenant_id, entity_id)`, or `None` if no
            attribute has ever been written for that pair.
        """
        self._require_non_blank("tenant_id", tenant_id)
        self._require_non_blank("entity_id", entity_id)
        with self._lock_for_tenant(tenant_id):
            entity_attrs = self._entity_attributes.get((tenant_id, entity_id))
            if entity_attrs is None:
                return None
            return EntityRecord(
                tenant_id=tenant_id,
                entity_id=entity_id,
                attributes=tuple(entity_attrs.values()),
            )

    def resolve_alias_prefix(self, tenant_id: str, prefix: str) -> frozenset[str]:
        """Return entity_ids whose registered alias starts with `prefix` (AC-005-3).

        Returns an empty `frozenset` -- never raises -- both when
        `tenant_id` has no `AliasTrie` yet and when the trie has no
        matching alias.
        """
        self._require_non_blank("tenant_id", tenant_id)
        with self._lock_for_tenant(tenant_id):
            trie = self._alias_tries.get(tenant_id)
            if trie is None:
                return frozenset()
            return trie.prefix_search(prefix)

    def resolve_exact_term(self, tenant_id: str, term: str) -> frozenset[str]:
        """Return entity_ids whose registered alias equals `term` exactly (AC-005-4).

        This method (like every other one on this class) has no
        dependency on Zone 6's `retrieval_index_ports` or any vector/
        lexical adapter -- it answers directly from this tenant's own
        `AliasTrie`, which is the structural demonstration AC-005-4
        calls for ("...even with Zone 6 unavailable").
        """
        self._require_non_blank("tenant_id", tenant_id)
        with self._lock_for_tenant(tenant_id):
            trie = self._alias_tries.get(tenant_id)
            if trie is None:
                return frozenset()
            return trie.exact_match(term)

    def fetch(self, query: ZoneQuery) -> list[MemoryItem]:
        """Serve the `ZoneRepository` read contract for Zone 5 (HLD Section 7.1).

        Zone 5 has no vector embedding of its own (AC-005-4: it is
        deliberately not vector-primary), so `query.query_embedding` is
        ignored here; `query.task`, when supplied, is treated as an
        alias-prefix term against `resolve_alias_prefix` -- the same
        exact-term/prefix mechanism AC-005-3/AC-005-4 exercise directly,
        reused here rather than duplicated for the generic assembly
        path. A blank/`None` `query.task` yields no candidates: this
        adapter has no other query dimension to fall back to.

        Args:
            query: The stripped-down per-zone query from the
                Orchestrator (`domain.ports.ZoneQuery`).

        Returns:
            Up to `query.max_items` `MemoryItem`s for entities whose
            alias matches `query.task` as a prefix, each carrying that
            entity's full attribute set serialized as its `payload`.
            Empty when `query.task` is blank or matches no alias.
        """
        if not query.task or not query.task.strip():
            return []

        entity_ids = self.resolve_alias_prefix(query.tenant_id, query.task)
        items: list[MemoryItem] = []
        for entity_id in sorted(entity_ids):
            if len(items) >= query.max_items:
                break
            record = self.get_entity(query.tenant_id, entity_id)
            if record is None:
                continue
            payload = _serialize_attributes(record)
            items.append(
                MemoryItem(
                    item_id=entity_id,
                    source_zone=ZoneId.ENTITY,
                    payload=payload,
                    # A naive length-based proxy, not a certified tokenizer
                    # -- Zone 5's actual token-costing mechanism is outside
                    # this story's admitted context. See this story's dev
                    # report, judgment-call list.
                    token_count=max(1, len(payload)),
                )
            )
        return items

    def _reject_missing_provenance_id(
        self, *, tenant_id: str, entity_id: str, attribute_name: str
    ) -> WriteRejected:
        """Build AC-005-5's rejection outcome. Nothing is stored or published before this."""
        return WriteRejected(
            http_status=_REJECT_HTTP_STATUS,
            error_code=ERROR_MISSING_ATTRIBUTE_PROVENANCE_ID,
            reason=(
                f"attribute {attribute_name!r} for entity {entity_id!r} "
                f"(tenant {tenant_id!r}) was submitted without its own "
                "provenance_id; Zone 5 requires every attribute sub-record "
                "to carry a non-blank provenance_id (HLD Section 3.6)"
            ),
        )

    @staticmethod
    def _require_non_blank(name: str, value: str) -> None:
        """Enforce HLD 3.0 invariant 2 (mandatory tenant/entity identifiers).

        Raises:
            ValueError: If `value` is blank.
        """
        if not value.strip():
            raise ValueError(f"{name} must not be blank")


def _serialize_attributes(record: EntityRecord) -> str:
    """Render `record`'s attributes as a deterministic JSON object string.

    Deterministic (`sort_keys=True`) so two calls for an unchanged
    `EntityRecord` produce byte-identical payloads -- useful for any
    caller-side caching/deduplication, mirroring the determinism
    `dashanan.domain.provenance_record.compute_record_hash` requires of
    its own canonical encoding.
    """
    return json.dumps(
        {attribute.attribute_name: attribute.value for attribute in record.attributes},
        sort_keys=True,
    )
