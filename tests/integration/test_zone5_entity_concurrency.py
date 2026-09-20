"""Zone 5 (`EntityMemoryRepository`) entity-scoped write concurrency probe.

Scope (per the dispatch): prove `write_attribute`/`register_alias` never
silently drop a concurrent write for the SAME `(tenant_id, entity_id)`
under real thread contention, and prove the per-tenant lock (module
docstring, `entity_memory_repository.py`: "Locks for different tenants
are independent") gives genuine physical cross-tenant isolation -- never
a torn read of another tenant's in-flight write -- for two tenants racing
at the same instant.

Mirrors Sprint 1's own concurrency-test pattern exactly
(`tests/integration/test_adversarial_dshn59_reaudit.py`,
`TestWriteGateConcurrencyBeyondScenario6`): `threading.Barrier` to force
every thread to submit at (as close as the GIL allows to) the same
instant, plus one per-thread result slot in a shared `list`/`dict`
(never `unittest.mock`, never a bare `list.append` from multiple
threads racing on the list itself).

Uses the real `Sprint2WiredSystem` composition
(`tests/integration/conftest_sprint2.py`) -- this module registers that
file as a plugin exactly as `test_smoke_fixture_wiring_sprint2.py`
already does, since `conftest_sprint2.py` is not named `conftest.py` and
pytest does not auto-discover it.

PII NOTE: every attribute value and alias string below is synthetic
routing/index metadata (`f"attr-value-{i}"`, `f"alias-{i}"`) -- no
example conversational content appears anywhere in this file.
"""

from __future__ import annotations

import threading

import pytest

from dashanan.domain.write_gate import WriteAccepted

from tests.integration.conftest import DEFAULT_TENANT_ID

from .conftest_sprint2 import Sprint2WiredSystem

pytest_plugins = ["tests.integration.conftest_sprint2"]

_THREAD_COUNT = 16
"""The dispatch's own suggested N -- large enough to make a lost-write
or cross-tenant-leakage race visible under real `threading.Thread`
contention without making the suite slow."""


class TestZone5EntityConcurrency:
    """Real-thread races against `EntityMemoryRepository` for one entity, and across tenants."""

    def test_concurrent_write_attribute_distinct_names_same_entity_no_writes_lost(
        self, sprint2_wired_system: Sprint2WiredSystem
    ) -> None:
        """N threads race `write_attribute` for the SAME tenant_id/entity_id.

        Each thread writes its OWN `attribute_name` (`attr-0` .. `attr-15`)
        so that "no writes lost" has an unambiguous meaning: distinct dict
        keys must all survive the race. (Two threads racing to write the
        SAME `attribute_name` is deliberately a separate, narrower
        last-writer-wins case -- covered below -- since a dict key
        overwrite by design is not itself a "lost write".)
        """
        ws = sprint2_wired_system
        entity_id = "concurrent-entity-1"
        results: list[object] = [None] * _THREAD_COUNT
        errors: list[BaseException] = []
        errors_lock = threading.Lock()
        barrier = threading.Barrier(_THREAD_COUNT)

        def run(index: int) -> None:
            try:
                barrier.wait(timeout=5)
                results[index] = ws.zone5_entity_repository.write_attribute(
                    tenant_id=DEFAULT_TENANT_ID,
                    entity_id=entity_id,
                    attribute_name=f"attr-{index}",
                    value=f"attr-value-{index}",
                    provenance_id=f"prov-{index}",
                )
            except BaseException as exc:  # noqa: BLE001 -- captured for the assertion below
                with errors_lock:
                    errors.append(exc)

        threads = [threading.Thread(target=run, args=(i,)) for i in range(_THREAD_COUNT)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert errors == [], f"write_attribute raised under contention: {errors!r}"
        assert all(isinstance(r, WriteAccepted) for r in results), results

        stored = ws.zone5_entity_repository.get_entity(DEFAULT_TENANT_ID, entity_id)
        assert stored is not None
        stored_by_name = {a.attribute_name: a.value for a in stored.attributes}

        assert len(stored_by_name) == _THREAD_COUNT, (
            "expected all "
            f"{_THREAD_COUNT} distinct attribute_name writes to survive the race, "
            f"found {len(stored_by_name)}: {sorted(stored_by_name)}"
        )
        for i in range(_THREAD_COUNT):
            assert stored_by_name.get(f"attr-{i}") == f"attr-value-{i}", (
                f"attr-{i} missing or corrupted after concurrent writes: "
                f"got {stored_by_name.get(f'attr-{i}')!r}"
            )

    def test_concurrent_write_attribute_same_name_same_entity_last_writer_wins_no_corruption(
        self, sprint2_wired_system: Sprint2WiredSystem
    ) -> None:
        """N threads race `write_attribute` for the SAME `attribute_name` on one entity.

        By design (`write_attribute`'s own docstring: "this is the ONLY
        dict entry this call ever touches") this is a last-writer-wins
        dict-key overwrite, not a lost write -- the adversarial property
        under test is that the surviving value is exactly one thread's own
        whole, uncorrupted `(value, provenance_id)` pair, never a torn mix
        of two different threads' writes.
        """
        ws = sprint2_wired_system
        entity_id = "concurrent-entity-shared-attr"
        results: list[object] = [None] * _THREAD_COUNT
        barrier = threading.Barrier(_THREAD_COUNT)

        def run(index: int) -> None:
            barrier.wait(timeout=5)
            results[index] = ws.zone5_entity_repository.write_attribute(
                tenant_id=DEFAULT_TENANT_ID,
                entity_id=entity_id,
                attribute_name="shared-attr",
                value=f"attr-value-{index}",
                provenance_id=f"prov-{index}",
            )

        threads = [threading.Thread(target=run, args=(i,)) for i in range(_THREAD_COUNT)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert all(isinstance(r, WriteAccepted) for r in results), results

        stored = ws.zone5_entity_repository.get_entity(DEFAULT_TENANT_ID, entity_id)
        assert stored is not None
        assert len(stored.attributes) == 1, (
            "exactly one attribute_name key must survive a same-key race, "
            f"got {len(stored.attributes)}: {[a.attribute_name for a in stored.attributes]}"
        )
        surviving = stored.attributes[0]
        surviving_index = int(surviving.value.removeprefix("attr-value-"))
        # The surviving value and provenance_id must belong to the SAME
        # thread -- a torn write would let, e.g., thread 3's value survive
        # paired with thread 7's provenance_id, which no single
        # `EntityAttributeRecord(...)` construction in `write_attribute`
        # (built once, then assigned as a single dict-value reference)
        # could actually produce, but is exactly the shape of corruption a
        # broken lock would let slip through.
        assert surviving.provenance_id == f"prov-{surviving_index}", (
            f"corrupted write: value {surviving.value!r} paired with "
            f"provenance_id {surviving.provenance_id!r} -- these must come "
            "from the same thread's single write_attribute call"
        )

    def test_concurrent_register_alias_distinct_aliases_same_entity_no_writes_lost(
        self, sprint2_wired_system: Sprint2WiredSystem
    ) -> None:
        """N threads race `register_alias` for the SAME tenant_id/entity_id.

        Each thread registers its OWN alias string (`alias-0` ..
        `alias-15`) into the one shared per-tenant `AliasTrie` -- proves
        the trie's own `insert` (a `dict.setdefault` walk per character,
        `entity_ids.add` at the terminal node) never drops a sibling
        thread's alias under the shared tenant lock.
        """
        ws = sprint2_wired_system
        entity_id = "concurrent-entity-aliases"
        errors: list[BaseException] = []
        errors_lock = threading.Lock()
        barrier = threading.Barrier(_THREAD_COUNT)

        def run(index: int) -> None:
            try:
                barrier.wait(timeout=5)
                ws.zone5_entity_repository.register_alias(
                    tenant_id=DEFAULT_TENANT_ID,
                    entity_id=entity_id,
                    alias=f"alias-{index}",
                )
            except BaseException as exc:  # noqa: BLE001 -- captured for the assertion below
                with errors_lock:
                    errors.append(exc)

        threads = [threading.Thread(target=run, args=(i,)) for i in range(_THREAD_COUNT)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert errors == [], f"register_alias raised under contention: {errors!r}"

        for i in range(_THREAD_COUNT):
            resolved = ws.zone5_entity_repository.resolve_exact_term(
                DEFAULT_TENANT_ID, f"alias-{i}"
            )
            assert resolved == frozenset({entity_id}), (
                f"alias-{i} was lost or corrupted under concurrent registration: "
                f"resolve_exact_term returned {resolved!r}"
            )

        prefix_resolved = ws.zone5_entity_repository.resolve_alias_prefix(
            DEFAULT_TENANT_ID, "alias-"
        )
        assert prefix_resolved == frozenset({entity_id}), (
            "prefix_search over all 16 concurrently-registered aliases must "
            f"still resolve to exactly the one entity_id, got {prefix_resolved!r}"
        )

    def test_concurrent_two_tenants_racing_no_cross_tenant_leakage(
        self, sprint2_wired_system: Sprint2WiredSystem
    ) -> None:
        """Two tenants' threads race `write_attribute` + `register_alias` concurrently.

        Asserts tenant A's `AliasTrie`/attribute-dict state never
        contains, and is never affected by, tenant B's writes -- the
        module docstring's own claim ("Locks for different tenants are
        independent, so concurrent writes for different tenants are
        never serialized against each other") is a claim about
        liveness/throughput, not isolation; this test is the isolation
        proof: even racing at the identical instant, under the SAME
        entity_id string reused across both tenants (the harshest
        adversarial case -- a broken tenant key in `_entity_attributes`/
        `_alias_tries` would silently merge the two tenants' data), each
        tenant's own reads see only its own writes.
        """
        ws = sprint2_wired_system
        tenants = ["adv-tenant-A", "adv-tenant-B"]
        shared_entity_id = "shared-entity-id-both-tenants"
        thread_count = len(tenants) * _THREAD_COUNT
        errors: list[BaseException] = []
        errors_lock = threading.Lock()
        barrier = threading.Barrier(thread_count)

        def run(tenant_id: str, index: int) -> None:
            try:
                barrier.wait(timeout=5)
                ws.zone5_entity_repository.write_attribute(
                    tenant_id=tenant_id,
                    entity_id=shared_entity_id,
                    attribute_name=f"attr-{index}",
                    value=f"{tenant_id}-value-{index}",
                    provenance_id=f"{tenant_id}-prov-{index}",
                )
                ws.zone5_entity_repository.register_alias(
                    tenant_id=tenant_id,
                    entity_id=shared_entity_id,
                    alias=f"{tenant_id}-alias-{index}",
                )
            except BaseException as exc:  # noqa: BLE001 -- captured for the assertion below
                with errors_lock:
                    errors.append(exc)

        threads = [
            threading.Thread(target=run, args=(tenant_id, i))
            for tenant_id in tenants
            for i in range(_THREAD_COUNT)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert errors == [], f"cross-tenant race raised: {errors!r}"

        for tenant_id in tenants:
            other_tenant = tenants[1] if tenant_id == tenants[0] else tenants[0]

            stored = ws.zone5_entity_repository.get_entity(tenant_id, shared_entity_id)
            assert stored is not None
            assert len(stored.attributes) == _THREAD_COUNT, (
                f"{tenant_id}: expected {_THREAD_COUNT} of its own attribute writes, "
                f"got {len(stored.attributes)}"
            )
            for attribute in stored.attributes:
                assert attribute.value.startswith(f"{tenant_id}-value-"), (
                    f"{tenant_id}'s entity record contains a value that does not "
                    f"belong to it: {attribute.value!r} -- cross-tenant leakage"
                )
                assert not attribute.value.startswith(f"{other_tenant}-value-"), (
                    f"{tenant_id}'s entity record leaked {other_tenant}'s value: "
                    f"{attribute.value!r}"
                )

            for i in range(_THREAD_COUNT):
                own_alias_resolved = ws.zone5_entity_repository.resolve_exact_term(
                    tenant_id, f"{tenant_id}-alias-{i}"
                )
                assert own_alias_resolved == frozenset({shared_entity_id}), (
                    f"{tenant_id}: own alias {tenant_id}-alias-{i} was lost, "
                    f"got {own_alias_resolved!r}"
                )

                other_alias_probe = ws.zone5_entity_repository.resolve_exact_term(
                    tenant_id, f"{other_tenant}-alias-{i}"
                )
                assert other_alias_probe == frozenset(), (
                    f"{tenant_id}'s AliasTrie resolved {other_tenant}'s alias "
                    f"{other_tenant}-alias-{i} -- cross-tenant alias leakage: "
                    f"{other_alias_probe!r}"
                )
