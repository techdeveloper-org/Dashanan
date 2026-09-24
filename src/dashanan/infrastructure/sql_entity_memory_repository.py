"""SqlEntityMemoryRepository: the Zone 5 Shape B (Postgres) storage adapter (DASH-STORY-029-DEV, FR-005).

DESIGN STATUS WARNING: this module implements
`docs/phase-1.5-design/zone5-shape-b-storage-design.md` v4, itself marked
**DRAFT -- NOT IMPLEMENTATION-READY**, with 5 disclosed open items in that
document's Section 8. This module does not silently resolve any of them; it
follows the document's Section 3 (schema), Section 5 (compliance-role
grant), and Section 6 (interface-compatibility table) literally, and calls
out below where an open item bears on a specific method.

MUST-NOT-DEVIATE (sprint5_ar1_assignments.json DASH-STORY-029-DEV, binding):
  - `EntityMemoryRepository` (Shape A) is not modified by this module and is
    not inherited from -- the design doc's Section 8 item 1 explicitly
    rejects that option (Liskov Substitution / Dependency Inversion
    violation: a Postgres adapter has no use for Shape A's in-process
    dict/lock/AliasTrie internals).
  - `write_attribute`, `erase_entity`, `register_alias`,
    `resolve_alias_prefix`, `resolve_exact_term`, `get_entity`, `fetch`
    expose the identical signatures and return-value shapes
    `EntityMemoryRepository` already exposes (design doc Section 6's own
    interface-compatibility table), so `UnifiedSubjectErasureOrchestrator`'s
    `erase_entity` call site needs zero changes when the composition root's
    Zone 5 builder switches from Shape A to this adapter.
  - No Zone 4 (Procedural) schema, role, or repository code is built here --
    Zone 4 is permanently out of scope (design doc Section 1.1).
  - No second, Zone-5-specific compliance role is created -- `erase_entity`
    connects through the existing, project-wide
    `dashanan_compliance_erasure_role` (design doc Section 5;
    `dpdp-crypto-shredding-full-erasure-design.md` Section 5's own role),
    granted `DELETE` on `entity_attributes`/`entity_aliases` by
    `entity_schema.sql`.

PII NOTE: `value` is the canonical store for a real entity's real attribute
value (entity_memory_repository.py's own PII NOTE, reused verbatim here);
this module's own docstrings and any worked example use only structural
shape, never a real or PII-bearing example value.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, cast, runtime_checkable

from dashanan.domain.entity_record import EntityAttributeRecord, EntityRecord
from dashanan.domain.exceptions import ZoneRepositoryError
from dashanan.domain.memory_item import MemoryItem
from dashanan.domain.ports import Clock, EventBus, ZoneQuery
from dashanan.domain.write_gate import WriteAccepted, WriteGateResult, WriteRejected
from dashanan.domain.zone import ZoneId
from dashanan.infrastructure.entity_memory_repository import (
    ERROR_MISSING_ATTRIBUTE_PROVENANCE_ID,
    PROJECTION_EVENT_TYPE,
)
from dashanan.infrastructure.noop_event_bus import NoOpEventBus
from dashanan.infrastructure.system_clock import SystemClock

logger = logging.getLogger(__name__)

_REJECT_HTTP_STATUS = 422
"""Mirrors `entity_memory_repository._REJECT_HTTP_STATUS` -- the same fixed
rejection status AC-010 established for this repository's own AC-005-5
rejection path, reused verbatim (kept as a local module constant, not
imported, since the Shape A module's own constant is private)."""

_ATTRIBUTE_COLUMNS = "tenant_id, entity_id, attribute_name, value, provenance_id, updated_at"

_UPSERT_ATTRIBUTE_SQL = """
INSERT INTO entity_attributes
    (tenant_id, entity_id, attribute_name, value, provenance_id, subject_id, updated_at)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (tenant_id, entity_id, attribute_name) DO UPDATE SET
    value = EXCLUDED.value,
    provenance_id = EXCLUDED.provenance_id,
    subject_id = EXCLUDED.subject_id,
    updated_at = EXCLUDED.updated_at
"""

_GET_ENTITY_SQL = f"""
SELECT {_ATTRIBUTE_COLUMNS}
FROM entity_attributes
WHERE tenant_id = %s AND entity_id = %s
"""

_DELETE_ATTRIBUTES_BY_ENTITY_SQL = """
DELETE FROM entity_attributes
WHERE tenant_id = %s AND entity_id = %s
RETURNING attribute_name
"""

_DELETE_ALIASES_BY_ENTITY_SQL = """
DELETE FROM entity_aliases
WHERE tenant_id = %s AND entity_id = %s
"""

_INSERT_ALIAS_SQL = """
INSERT INTO entity_aliases (tenant_id, entity_id, alias, registered_at)
VALUES (%s, %s, %s, %s)
ON CONFLICT (tenant_id, alias, entity_id) DO NOTHING
"""

_RESOLVE_ALIAS_PREFIX_SQL = """
SELECT DISTINCT entity_id
FROM entity_aliases
WHERE tenant_id = %s AND alias LIKE %s
"""

_RESOLVE_EXACT_TERM_SQL = """
SELECT DISTINCT entity_id
FROM entity_aliases
WHERE tenant_id = %s AND alias = %s
"""

_LIKE_WILDCARD_ESCAPE = str.maketrans(
    {"%": r"\%", "_": r"\_", "\\": "\\\\"}
)
"""Escapes a caller-supplied prefix's own LIKE metacharacters (`%`, `_`,
`\\`) before this module appends its own trailing `%` wildcard -- otherwise
a prefix containing a literal `%` or `_` would silently match more rows
than `AliasTrie.prefix_search`'s exact-character-prefix semantics permit
(Shape A has no such escaping gap: it walks the trie node-by-node on the
literal characters supplied)."""


@runtime_checkable
class SqlCursor(Protocol):
    """The minimal DB-API 2.0 (PEP 249) cursor surface this adapter needs."""

    def execute(self, sql: str, params: Sequence[object]) -> object:
        """Execute a parameterized statement. Must never receive interpolated SQL."""
        ...

    def fetchall(self) -> list[tuple[object, ...]]:
        """Return every row produced by the last `execute` call."""
        ...


@runtime_checkable
class SqlConnection(Protocol):
    """The minimal DB-API 2.0 connection surface this adapter needs.

    Kept local to this infrastructure module rather than added to the
    frozen `dashanan.domain.ports` (AR1-G2) -- mirroring
    `SqlSemanticRepository`'s/`SqlEpisodicRepository`'s identical
    local-Protocol choice. The composition root wires a real driver's
    connection (psycopg) against Postgres, connected as
    `dashanan_entity_role`'s bound login for ordinary writes/reads, or as
    `dashanan_compliance_erasure_role`'s bound login for `erase_entity`
    (design doc Section 5) -- which credential this adapter's own
    `connection` was opened under is a composition-root concern, not this
    class's.
    """

    def cursor(self) -> SqlCursor:
        """Return a new cursor bound to this connection."""
        ...


class SqlEntityMemoryRepository:
    """Zone 5's Shape B storage adapter over `entity_attributes` and `entity_aliases`.

    Exposes the identical `write_attribute`/`erase_entity`/`register_alias`/
    `resolve_alias_prefix`/`resolve_exact_term`/`get_entity`/`fetch` surface
    `EntityMemoryRepository` (Shape A) exposes, per the design doc's Section
    6 interface-compatibility table -- so
    `UnifiedSubjectErasureOrchestrator`'s existing `erase_entity` call site
    needs no change when the composition root swaps Zone 5's builder from
    Shape A to this adapter (only the constructor's `zone5_repository`
    parameter's type annotation widens; see
    `unified_subject_erasure_orchestrator.Zone5ErasureCapable`).

    Concurrency (design doc Section 6, corrected for Shape B): Postgres's
    own row-level locking on `entity_attributes`/`entity_aliases` (implicit
    in every `UPDATE`/`DELETE`/upsert statement here) is this adapter's
    concurrency mechanism -- there is no per-tenant `threading.Lock` the way
    Shape A has one, because that lock has no meaning across a connection
    pool with multiple worker processes/pods. Design doc Section 8 item 3
    discloses this as unaudited against this schema's actual transaction
    boundaries; not resolved by this module.
    """

    def __init__(
        self,
        connection: SqlConnection,
        *,
        clock: Clock | None = None,
        event_bus: EventBus | None = None,
    ) -> None:
        """Bind the adapter to its SQL connection and (optionally) its Clock/EventBus ports.

        Args:
            connection: The DB-API connection this adapter issues
                parameterized queries against. `write_attribute`'s upsert
                and `erase_entity`'s deletes are committed by this method
                whenever the connection exposes a `commit()` -- mirroring
                `SqlSemanticRepository._execute_insert_or_raise`'s identical
                "commit only if the connection actually exposes it"
                convention, so a minimal test double that implements only
                `cursor()` is unaffected while a real driver connection is
                durably committed.
            clock: Injectable time source (testing-core DI) used as the
                default `updated_at` for `write_attribute` when the caller
                does not supply one explicitly. Defaults to `SystemClock()`,
                mirroring `EntityMemoryRepository`'s own default-injection
                convention at its composition-root call site.
            event_bus: The AC-005-6 projection sink `write_attribute`
                publishes to on acceptance, mirroring
                `EntityMemoryRepository`'s identical projection-event
                behavior for Shape A. Defaults to `NoOpEventBus()` when no
                broker is bound.
        """
        self._connection = connection
        self._clock = clock if clock is not None else SystemClock()
        self._event_bus = event_bus if event_bus is not None else NoOpEventBus()

    def write_attribute(
        self,
        tenant_id: str,
        entity_id: str,
        attribute_name: str,
        value: str,
        provenance_id: str,
        updated_at: datetime | None = None,
        subject_id: str | None = None,
    ) -> WriteGateResult:
        """Upsert exactly one attribute row (AC-005-2, AC-005-5, AC-005-6).

        Design doc Section 6's own interface-compatibility table: a single
        `INSERT ... ON CONFLICT (tenant_id, entity_id, attribute_name) DO
        UPDATE` touches exactly one row -- the same one-row-touched
        guarantee Shape A's single `dict` entry write provides.

        Args:
            tenant_id: Mandatory; enforced non-blank (HLD 3.0 invariant 2).
            entity_id: Mandatory; enforced non-blank.
            attribute_name: Mandatory; enforced non-blank.
            value: The attribute's new value. Opaque to this method.
            provenance_id: This attribute's own Zone 7 provenance record
                id. A blank value is rejected (AC-005-5) before any
                statement is executed, identical to Shape A.
            updated_at: When this write happened. Defaults to
                `self._clock.now()`.
            subject_id: The design doc's new parameter (Section 6/Section 8
                item 1) feeding `subject_item_index` population (design doc
                Section 3's partial index, `dpdp-crypto-shredding-full-
                erasure-design.md` Section 3) -- not present on Shape A's
                own signature today (design doc's own disclosed gap; Shape
                A's signature is out of this story's scope to change).
                `None` when this write carries no subject linkage.

        Returns:
            `WriteAccepted` once the row has been upserted and the AC-005-6
            projection event published. `WriteRejected` (422,
            `ERROR_MISSING_ATTRIBUTE_PROVENANCE_ID`) if `provenance_id` is
            blank -- nothing is stored and no event is published.

        Raises:
            ValueError: If `tenant_id`, `entity_id`, or `attribute_name` is
                blank.
            dashanan.domain.exceptions.ZoneRepositoryError: If the
                underlying upsert fails.
        """
        self._require_non_blank("tenant_id", tenant_id)
        self._require_non_blank("entity_id", entity_id)
        self._require_non_blank("attribute_name", attribute_name)

        if not provenance_id or not provenance_id.strip():
            return self._reject_missing_provenance_id(
                tenant_id=tenant_id, entity_id=entity_id, attribute_name=attribute_name
            )

        when = updated_at if updated_at is not None else self._clock.now()
        params = (
            tenant_id,
            entity_id,
            attribute_name,
            value,
            provenance_id,
            subject_id,
            when,
        )
        self._execute_write_or_raise(_UPSERT_ATTRIBUTE_SQL, params)

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

    def erase_entity(self, tenant_id: str, entity_id: str) -> tuple[str, ...]:
        """DASH-STORY-025's Zone 5 DPDP erasure leg, Shape B (design doc Section 5/6).

        Issues `DELETE ... RETURNING attribute_name` against
        `entity_attributes` (mirroring `SqlSemanticRepository.
        delete_edges_by_subject`'s identical DSHN-70 fix: the returned
        evidence is exactly what this call deleted, never a separate
        pre-delete `SELECT` snapshot that a concurrent write could desync
        from), then a second `DELETE` against `entity_aliases` for the same
        `(tenant_id, entity_id)` (the DSHN-70 alias-cleanup fix, mirrored
        from Shape A's own `erase_entity`).

        This connection is expected to be opened as
        `dashanan_compliance_erasure_role` (design doc Section 5) -- a
        composition-root concern, not enforced by this method itself.

        Args:
            tenant_id: Mandatory; enforced non-blank.
            entity_id: The data subject's `entity_id` to erase every
                attribute for. Mandatory; enforced non-blank.

        Returns:
            The `item_id` (`f"{entity_id}:{attribute_name}"`) of every
            attribute row actually removed. An empty tuple is a clean
            no-op, identical to Shape A.

        Raises:
            ValueError: If `tenant_id` or `entity_id` is blank.
            dashanan.domain.exceptions.ZoneRepositoryError: If either
                delete statement fails.
        """
        self._require_non_blank("tenant_id", tenant_id)
        self._require_non_blank("entity_id", entity_id)

        rows = self._execute_write_and_fetch_or_raise(
            _DELETE_ATTRIBUTES_BY_ENTITY_SQL, (tenant_id, entity_id)
        )
        self._execute_write_or_raise(
            _DELETE_ALIASES_BY_ENTITY_SQL, (tenant_id, entity_id)
        )

        if not rows:
            return ()
        return tuple(
            f"{entity_id}:{cast(str, row[0])}" for row in rows
        )

    def register_alias(self, tenant_id: str, entity_id: str, alias: str) -> None:
        """Index `alias` -> `entity_id` (AC-005-3 support), idempotently.

        `ON CONFLICT (tenant_id, alias, entity_id) DO NOTHING` mirrors
        `AliasTrie.insert`'s own idempotent-insert behavior (design doc
        Section 6): registering the same alias/entity pair twice is a
        no-op, not an error.

        Args:
            tenant_id: Mandatory; enforced non-blank.
            entity_id: Mandatory; enforced non-blank.
            alias: The alias string to index. Must be non-empty.

        Raises:
            ValueError: If `tenant_id`/`entity_id` is blank, or `alias` is
                empty.
            dashanan.domain.exceptions.ZoneRepositoryError: If the
                underlying insert fails.
        """
        self._require_non_blank("tenant_id", tenant_id)
        self._require_non_blank("entity_id", entity_id)
        if not alias:
            raise ValueError("register_alias requires a non-empty alias")

        when = self._clock.now()
        self._execute_write_or_raise(
            _INSERT_ALIAS_SQL, (tenant_id, entity_id, alias, when)
        )

    def get_entity(self, tenant_id: str, entity_id: str) -> EntityRecord | None:
        """Point-lookup one entity's full attribute set by exact key (AC-005-1).

        Args:
            tenant_id: Mandatory; enforced non-blank.
            entity_id: Mandatory; enforced non-blank.

        Returns:
            An `EntityRecord` assembled from every row currently stored for
            `(tenant_id, entity_id)`, or `None` if no attribute has ever
            been written for that pair.

        Raises:
            ValueError: If `tenant_id`/`entity_id` is blank.
            dashanan.domain.exceptions.ZoneRepositoryError: If the
                underlying query fails.
        """
        self._require_non_blank("tenant_id", tenant_id)
        self._require_non_blank("entity_id", entity_id)
        rows = self._execute_read_or_raise(_GET_ENTITY_SQL, (tenant_id, entity_id))
        if not rows:
            return None
        return EntityRecord(
            tenant_id=tenant_id,
            entity_id=entity_id,
            attributes=tuple(self._row_to_attribute(row) for row in rows),
        )

    def resolve_alias_prefix(self, tenant_id: str, prefix: str) -> frozenset[str]:
        """Return entity_ids whose registered alias starts with `prefix` (AC-005-3).

        Returns an empty `frozenset` -- never raises for "no match" -- both
        when `tenant_id` has no aliases at all and when no alias matches
        `prefix`, mirroring Shape A's own no-trie/no-match parity.

        Raises:
            ValueError: If `tenant_id` is blank.
            dashanan.domain.exceptions.ZoneRepositoryError: If the
                underlying query fails.
        """
        self._require_non_blank("tenant_id", tenant_id)
        escaped_prefix = prefix.translate(_LIKE_WILDCARD_ESCAPE)
        rows = self._execute_read_or_raise(
            _RESOLVE_ALIAS_PREFIX_SQL, (tenant_id, f"{escaped_prefix}%")
        )
        return frozenset(cast(str, row[0]) for row in rows)

    def resolve_exact_term(self, tenant_id: str, term: str) -> frozenset[str]:
        """Return entity_ids whose registered alias equals `term` exactly (AC-005-4).

        Raises:
            ValueError: If `tenant_id` is blank.
            dashanan.domain.exceptions.ZoneRepositoryError: If the
                underlying query fails.
        """
        self._require_non_blank("tenant_id", tenant_id)
        rows = self._execute_read_or_raise(_RESOLVE_EXACT_TERM_SQL, (tenant_id, term))
        return frozenset(cast(str, row[0]) for row in rows)

    def fetch(self, query: ZoneQuery) -> list[MemoryItem]:
        """Serve the `ZoneRepository` read contract for Zone 5 (HLD Section 7.1).

        Identical delegation to Shape A's own `fetch` (design doc Section
        6's own table): `query.task`, when non-blank, is treated as an
        alias-prefix term against `resolve_alias_prefix`; a blank/`None`
        `query.task` yields no candidates.

        Args:
            query: The stripped-down per-zone query from the Orchestrator.

        Returns:
            Up to `query.max_items` `MemoryItem`s for entities whose alias
            matches `query.task` as a prefix, each carrying that entity's
            full attribute set serialized as its `payload`.
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
                "provenance_id; Zone 5 requires every attribute row to "
                "carry a non-blank provenance_id (HLD Section 3.6)"
            ),
        )

    def _row_to_attribute(self, row: tuple[object, ...]) -> EntityAttributeRecord:
        """Map one result row back into the domain `EntityAttributeRecord` shape."""
        (tenant_id, entity_id, attribute_name, value, provenance_id, updated_at) = row
        return EntityAttributeRecord(
            tenant_id=cast(str, tenant_id),
            entity_id=cast(str, entity_id),
            attribute_name=cast(str, attribute_name),
            value=cast(str, value),
            provenance_id=cast(str, provenance_id),
            updated_at=cast(datetime, updated_at),
        )

    @staticmethod
    def _require_non_blank(name: str, value: str) -> None:
        """Enforce HLD 3.0 invariant 2 (mandatory tenant/entity identifiers).

        Raises:
            ValueError: If `value` is blank.
        """
        if not value.strip():
            raise ValueError(f"{name} must not be blank")

    def _execute_read_or_raise(
        self, sql: str, params: Sequence[object]
    ) -> list[tuple[object, ...]]:
        """Run one parameterized SELECT, converting any failure to a domain error."""
        try:
            cursor = self._connection.cursor()
            cursor.execute(sql, params)
            return cursor.fetchall()
        except Exception as exc:  # noqa: BLE001 -- converted to typed domain error below
            logger.warning(
                "Zone 5 (Entity) query failed",
                extra={
                    "zone": ZoneId.ENTITY.value,
                    "error_type": type(exc).__name__,
                    "reason": str(exc),
                },
            )
            raise ZoneRepositoryError(
                zone=ZoneId.ENTITY.value, reason=str(exc)
            ) from exc

    def _execute_write_or_raise(self, sql: str, params: Sequence[object]) -> None:
        """Run one no-RETURNING write and commit it, converting failure to a domain error.

        Mirrors `SqlSemanticRepository._execute_insert_or_raise`'s identical
        fix (GitHub #26): a plain write with no `RETURNING` clause must not
        go through a `cursor.fetchall()`-calling helper, which raises
        `psycopg.ProgrammingError` against a real driver when there is no
        result set to fetch. `commit()` is called only when the connection
        actually exposes it, so a real driver connection is durably
        committed while a minimal test double implementing only `cursor()`
        is unaffected.
        """
        try:
            cursor = self._connection.cursor()
            cursor.execute(sql, params)
            commit = getattr(self._connection, "commit", None)
            if commit is not None:
                commit()
        except Exception as exc:  # noqa: BLE001 -- converted to typed domain error below
            logger.warning(
                "Zone 5 (Entity) write failed",
                extra={
                    "zone": ZoneId.ENTITY.value,
                    "error_type": type(exc).__name__,
                    "reason": str(exc),
                },
            )
            raise ZoneRepositoryError(
                zone=ZoneId.ENTITY.value, reason=str(exc)
            ) from exc

    def _execute_write_and_fetch_or_raise(
        self, sql: str, params: Sequence[object]
    ) -> list[tuple[object, ...]]:
        """Run one `DELETE ... RETURNING`/upsert-with-RETURNING statement and commit it.

        Distinct from `_execute_write_or_raise`: this statement DOES carry a
        `RETURNING` clause, so `cursor.fetchall()` is valid and is the sole
        source of the affected-item evidence this method returns (DSHN-70's
        own "no separate pre-delete SELECT" fix, mirrored from
        `SqlSemanticRepository.delete_edges_by_subject`).
        """
        try:
            cursor = self._connection.cursor()
            cursor.execute(sql, params)
            rows = cursor.fetchall()
            commit = getattr(self._connection, "commit", None)
            if commit is not None:
                commit()
            return rows
        except Exception as exc:  # noqa: BLE001 -- converted to typed domain error below
            logger.warning(
                "Zone 5 (Entity) erasure delete failed",
                extra={
                    "zone": ZoneId.ENTITY.value,
                    "error_type": type(exc).__name__,
                    "reason": str(exc),
                },
            )
            raise ZoneRepositoryError(
                zone=ZoneId.ENTITY.value, reason=str(exc)
            ) from exc


def _serialize_attributes(record: EntityRecord) -> str:
    """Render `record`'s attributes as a deterministic JSON object string.

    Identical to `entity_memory_repository._serialize_attributes` --
    deterministic (`sort_keys=True`) so two calls for an unchanged
    `EntityRecord` produce byte-identical payloads.
    """
    return json.dumps(
        {attribute.attribute_name: attribute.value for attribute in record.attributes},
        sort_keys=True,
    )
