"""SqlZone2ComplianceEviction: the real Zone 2 compliance-role `Zone2EvictionPort` adapter.

Traces to AC-013 (SRS.md, cited verbatim in
`sql_subject_to_item_index_repository.py`'s own module docstring).

`episodic_schema.sql`'s own long comment names this exact follow-on
before it was ever built: a DPDP erasure workflow against
`episodic_entries` "is expected to run under a separate, narrowly-scoped
compliance role authorized for a PK-only DELETE -- not by widening this
application role's grants, and not by altering or dropping the
append-only trigger". `episodic_compliance_erasure_migration.sql` is
that migration (widens the trigger function's own exception narrowly,
never removes it); this module is the one call site that actually issues
the DELETE it authorizes, connected as `dashanan_compliance_erasure_role`
-- never as the ordinary app login role, which still cannot mutate this
table at all.

Implements `dashanan.application.zone2_capacity_backstop_sweep.
Zone2EvictionPort` -- the SAME Protocol `CrossZoneDpdpErasureCascade`
already depends on for its own Zone 2 leg (dependency inversion, not a
second parallel Zone-2 removal mechanism) -- so
`CrossZoneDpdpErasureCascade.fulfil_pending_erasure` can be composed with
this adapter instead of a Shape A in-memory one, for the first time
giving that cascade's Zone 2 leg a real database effect.

item_id mapping (judgment call, disclosed in the migration file's own
docstring): `episodic_entries` has no `item_id` column; `episode_id` is
unique per tenant and deterministic, so it is the identifier this
adapter's `evict(tenant_id, item_id)` treats as `item_id` -- the same
identifier `CrossZoneDpdpErasureCascade.fulfil_pending_erasure` already
receives as its own `item_id` parameter for the Zone 2/6 leg.

PII NOTE: this module never logs `episodic_entries` payload content --
only `tenant_id`/`item_id` (`episode_id`) routing metadata.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from dashanan.application.zone2_capacity_backstop_sweep import Zone2BackstopPortError

logger = logging.getLogger(__name__)

_DELETE_BY_EPISODE_ID_SQL = """
DELETE FROM episodic_entries
WHERE tenant_id = %s AND episode_id = %s
"""


@runtime_checkable
class SqlCursor(Protocol):
    """The minimal DB-API 2.0 (PEP 249) cursor surface this adapter needs."""

    def execute(self, sql: str, params: Sequence[object]) -> object:
        """Execute a parameterized statement. Must never receive interpolated SQL."""
        ...


@runtime_checkable
class SqlConnection(Protocol):
    """The minimal DB-API 2.0 connection surface this adapter needs, connected as the compliance role."""

    def cursor(self) -> SqlCursor:
        """Return a new cursor bound to this connection."""
        ...

    def commit(self) -> None:
        """Durably commit the DELETE this adapter issues."""
        ...


class SqlZone2ComplianceEviction:
    """Real `Zone2EvictionPort`: a PK-only, compliance-role `DELETE` against `episodic_entries`.

    Composed with an open `SqlConnection` authenticated as
    `dashanan_compliance_erasure_role` -- never the ordinary app login
    role, which `episodic_compliance_erasure_migration.sql` deliberately
    grants no new privilege to.
    """

    def __init__(self, connection: SqlConnection) -> None:
        self._connection = connection

    def evict(self, tenant_id: str, item_id: str) -> None:
        """`Zone2EvictionPort.evict`: remove `item_id`'s `episodic_entries` row for `tenant_id`.

        Idempotent: evicting an already-absent `(tenant_id, item_id)`
        deletes zero rows and raises nothing -- matches
        `Zone2EvictionPort`'s own "retry-safe" contract.

        Raises:
            ValueError: If `tenant_id` or `item_id` is blank.
            Zone2BackstopPortError: If the delete itself fails (e.g. the
                connection is not authenticated as
                `dashanan_compliance_erasure_role`, so the trigger's
                narrow DELETE exception does not apply and the mutation
                is rejected).
        """
        if not tenant_id.strip():
            raise ValueError("SqlZone2ComplianceEviction.evict requires a non-blank tenant_id")
        if not item_id.strip():
            raise ValueError("SqlZone2ComplianceEviction.evict requires a non-blank item_id")
        try:
            cursor = self._connection.cursor()
            cursor.execute(_DELETE_BY_EPISODE_ID_SQL, (tenant_id, item_id))
            self._connection.commit()
        except Exception as exc:  # noqa: BLE001 -- converted to typed domain error below
            raise Zone2BackstopPortError(
                f"SqlZone2ComplianceEviction.evict failed for tenant={tenant_id!r} "
                f"item={item_id!r}: {exc}"
            ) from exc
        logger.info(
            "zone2 compliance-role erasure delete applied",
            extra={"tenant_id": tenant_id},
        )
