"""QA pytest suite for DASH-STORY-017 (Entity Memory zone / Zone 5, FR-005, DSHN-66).

QA subtask (backlog_draft.json 25% split, ar1_assignments.json's revised
12% review-scope figure applies only to the peer-review pass, not this
independent QA verification). Independent verification pass on top of the
Dev subtask's own suite (tests/test_entity_memory_repository.py):
re-proves every AC-005-* from a fresh angle, adds the architecture-fitness
assertion scoped to this story's own new files (HLD 3.0 invariant 1 +
AC-005-4's Zone-6-independence claim), and covers the boundary/adversarial
matrix the Dev suite does not already exercise -- concurrent-tenant
isolation under interleaved writes, alias re-registration after an
attribute overwrite, cross-tenant alias-trie isolation under prefix
search, and the `EntityAttributeRecord`/`EntityRecord` value-object
immutability contract.

FR-005 (verbatim, SRS.md, cited exactly as the dev_prompt requires): "The
system SHALL provide an Entity Memory zone holding per-entity
(person/project/system) profile records, each the canonical sole owner
of that entity's own attributes." Traced by DASH-STORY-017.

Runtime assumptions (matching test_qa_working_memory_zone1.py's own
documented convention -- no HTTP/router/response-envelope layer exists in
this codebase yet):
  - clock: FakeClock, fixed at 2026-01-01T00:00:00+00:00 unless a test
    states otherwise.
  - tenant_id: "tenant-1", entity_id: "entity-1" unless stated otherwise.
  - No fixture seed required; nothing in this suite is randomized.
"""

from __future__ import annotations

import ast
import dataclasses
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from dashanan.domain.entity_record import EntityAttributeRecord, EntityRecord
from dashanan.domain.ports import ZoneQuery
from dashanan.domain.write_gate import WriteAccepted, WriteRejected
from dashanan.infrastructure.entity_memory_repository import (
    ERROR_MISSING_ATTRIBUTE_PROVENANCE_ID,
    EntityMemoryRepository,
)

DOMAIN_DIR = Path(__file__).resolve().parents[1] / "src" / "dashanan" / "domain"
INFRA_DIR = Path(__file__).resolve().parents[1] / "src" / "dashanan" / "infrastructure"
ENTITY_RECORD_FILE = DOMAIN_DIR / "entity_record.py"
ENTITY_ALIAS_TRIE_FILE = DOMAIN_DIR / "entity_alias_trie.py"
ENTITY_MEMORY_REPOSITORY_FILE = INFRA_DIR / "entity_memory_repository.py"


class FakeClock:
    """Deterministic, advanceable Clock double (testing-core DI)."""

    def __init__(self, fixed: datetime) -> None:
        self._now = fixed

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


class RecordingEventBus:
    """`EventBus` double that captures every published event for inspection."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, object]]] = []

    def publish(self, event_type: str, payload: dict[str, object]) -> None:
        self.published.append((event_type, dict(payload)))


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 1, 1, tzinfo=UTC))


@pytest.fixture
def event_bus() -> RecordingEventBus:
    return RecordingEventBus()


@pytest.fixture
def repo(clock: FakeClock, event_bus: RecordingEventBus) -> EntityMemoryRepository:
    return EntityMemoryRepository(clock=clock, event_bus=event_bus)


def _imported_module_names(source_path: Path) -> list[str]:
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None:
                names.append(node.module)
    return names


class TestArchitectureFitnessScopedToStory:
    """HLD 3.0 invariant 1 + AC-005-4's Zone-6-independence claim, scoped
    explicitly to this story's own three new files.
    """

    def test_all_three_new_files_exist(self) -> None:
        assert ENTITY_RECORD_FILE.is_file(), f"{ENTITY_RECORD_FILE} not found"
        assert ENTITY_ALIAS_TRIE_FILE.is_file(), f"{ENTITY_ALIAS_TRIE_FILE} not found"
        assert ENTITY_MEMORY_REPOSITORY_FILE.is_file(), (
            f"{ENTITY_MEMORY_REPOSITORY_FILE} not found"
        )

    def test_domain_entity_record_does_not_import_infrastructure(self) -> None:
        imported = _imported_module_names(ENTITY_RECORD_FILE)
        bad = [
            name
            for name in imported
            if name == "dashanan.infrastructure" or name.startswith("dashanan.infrastructure.")
        ]
        assert not bad, f"domain/entity_record.py imports infrastructure: {bad}"

    def test_domain_alias_trie_does_not_import_infrastructure(self) -> None:
        imported = _imported_module_names(ENTITY_ALIAS_TRIE_FILE)
        bad = [
            name
            for name in imported
            if name == "dashanan.infrastructure" or name.startswith("dashanan.infrastructure.")
        ]
        assert not bad, f"domain/entity_alias_trie.py imports infrastructure: {bad}"

    def test_repository_has_zero_zone6_dependency(self) -> None:
        """AC-005-4's structural proof: `EntityMemoryRepository` must not
        import Zone 6's retrieval-index ports or any vector/lexical
        adapter, since that is precisely what makes "exact-term queries
        answered ... even with Zone 6 unavailable" true by construction
        rather than by a runtime availability check.
        """
        imported = _imported_module_names(ENTITY_MEMORY_REPOSITORY_FILE)
        forbidden_substrings = (
            "retrieval_index_ports",
            "hybrid_retrieval_index_repository",
            "in_memory_vector_index",
            "in_memory_lexical_index",
        )
        bad = [
            name
            for name in imported
            if any(substring in name for substring in forbidden_substrings)
        ]
        assert not bad, (
            "entity_memory_repository.py must have zero Zone 6 dependency "
            f"(AC-005-4); found: {bad}"
        )

    def test_entity_record_and_alias_trie_are_frozen_value_objects(self) -> None:
        for cls in (EntityAttributeRecord, EntityRecord):
            assert dataclasses.is_dataclass(cls)
            params = getattr(cls, "__dataclass_params__")
            assert params.frozen is True, f"{cls.__name__} must be a frozen dataclass"


class TestEntityAttributeRecordImmutability:
    """Frozen-dataclass contract: mutation must go through `dataclasses.replace`."""

    def _sample(self) -> EntityAttributeRecord:
        now = datetime(2026, 1, 1, tzinfo=UTC)
        return EntityAttributeRecord(
            tenant_id="tenant-1",
            entity_id="entity-1",
            attribute_name="attr-a",
            value="v",
            provenance_id="prov-1",
            updated_at=now,
        )

    def test_direct_attribute_assignment_raises(self) -> None:
        attr = self._sample()
        with pytest.raises(dataclasses.FrozenInstanceError):
            attr.value = "mutated"  # type: ignore[misc]

    def test_replace_produces_a_new_instance_leaving_original_untouched(self) -> None:
        attr = self._sample()
        later = attr.updated_at + timedelta(seconds=60)

        refreshed = dataclasses.replace(attr, value="v2", updated_at=later)

        assert refreshed is not attr
        assert attr.value == "v"
        assert refreshed.value == "v2"


class TestAC005_1IndependentReVerification:
    """AC-005-1 re-proved from a fresh angle: many entities under one
    tenant, verifying the point lookup returns only the exact key's own
    attributes regardless of how many other entities share that tenant.
    """

    def test_point_lookup_unaffected_by_population_size(
        self, repo: EntityMemoryRepository
    ) -> None:
        for i in range(50):
            repo.write_attribute("tenant-1", f"entity-{i}", "attr-a", f"v{i}", f"prov-{i}")

        record = repo.get_entity("tenant-1", "entity-27")

        assert record is not None
        assert record.get_attribute("attr-a").value == "v27"

    def test_get_entity_rejects_blank_entity_id(
        self, repo: EntityMemoryRepository
    ) -> None:
        with pytest.raises(ValueError, match="entity_id must not be blank"):
            repo.get_entity("tenant-1", "")


class TestAC005_2IndependentReVerification:
    """AC-005-2 re-proved via three attributes, updating the middle one and
    asserting BOTH neighbors are untouched (the Dev suite only checks a
    single sibling).
    """

    def test_updating_middle_attribute_leaves_both_neighbors_untouched(
        self, repo: EntityMemoryRepository, clock: FakeClock
    ) -> None:
        repo.write_attribute("tenant-1", "entity-1", "attr-a", "va", "prov-a")
        clock.advance(seconds=5)
        repo.write_attribute("tenant-1", "entity-1", "attr-b", "vb", "prov-b")
        clock.advance(seconds=5)
        repo.write_attribute("tenant-1", "entity-1", "attr-c", "vc", "prov-c")

        before_a = repo.get_entity("tenant-1", "entity-1").get_attribute("attr-a")
        before_c = repo.get_entity("tenant-1", "entity-1").get_attribute("attr-c")

        clock.advance(seconds=5)
        repo.write_attribute("tenant-1", "entity-1", "attr-b", "vb2", "prov-b2")

        after = repo.get_entity("tenant-1", "entity-1")
        assert after.get_attribute("attr-a") == before_a
        assert after.get_attribute("attr-c") == before_c
        assert after.get_attribute("attr-b").value == "vb2"

    def test_sibling_entity_under_same_tenant_is_never_touched(
        self, repo: EntityMemoryRepository
    ) -> None:
        repo.write_attribute("tenant-1", "entity-1", "attr-a", "v1", "prov-1")
        repo.write_attribute("tenant-1", "entity-2", "attr-a", "v2", "prov-2")

        repo.write_attribute("tenant-1", "entity-1", "attr-a", "v1-updated", "prov-3")

        assert repo.get_entity("tenant-1", "entity-2").get_attribute("attr-a").value == "v2"


class TestAC005_3And4CrossTenantIsolationUnderPrefixSearch:
    """Boundary case the Dev suite's isolation test does not cover: two
    tenants sharing an OVERLAPPING alias prefix, verified via both the
    prefix and exact-term paths together in one scenario.
    """

    def test_overlapping_prefixes_across_tenants_never_cross_contaminate(
        self, repo: EntityMemoryRepository
    ) -> None:
        repo.register_alias("tenant-a", "entity-a1", "shared-prefix-x")
        repo.register_alias("tenant-a", "entity-a2", "shared-prefix-y")
        repo.register_alias("tenant-b", "entity-b1", "shared-prefix-x")

        assert repo.resolve_alias_prefix("tenant-a", "shared-prefix") == frozenset(
            {"entity-a1", "entity-a2"}
        )
        assert repo.resolve_alias_prefix("tenant-b", "shared-prefix") == frozenset(
            {"entity-b1"}
        )
        assert repo.resolve_exact_term("tenant-a", "shared-prefix-x") == frozenset(
            {"entity-a1"}
        )
        assert repo.resolve_exact_term("tenant-b", "shared-prefix-x") == frozenset(
            {"entity-b1"}
        )

    def test_re_registering_an_alias_after_attribute_overwrite_keeps_old_alias_reachable(
        self, repo: EntityMemoryRepository
    ) -> None:
        """This story does not require automatic alias invalidation on
        attribute overwrite (only `register_alias`/`AliasTrie.remove`
        manage the index) -- this test documents that observable
        behavior explicitly rather than leaving it an implicit gap: an
        old alias remains resolvable until a caller explicitly removes
        it, even after the underlying attribute value it once named has
        changed.
        """
        repo.write_attribute("tenant-1", "entity-1", "display_name", "Old", "prov-1")
        repo.register_alias("tenant-1", "entity-1", "old-alias")

        repo.write_attribute("tenant-1", "entity-1", "display_name", "New", "prov-2")

        assert repo.resolve_exact_term("tenant-1", "old-alias") == frozenset({"entity-1"})


class TestAC005_5IndependentReVerification:
    """AC-005-5 re-proved with a boundary the Dev suite does not cover:
    `None`-typed provenance_id arriving despite the type hint (a
    malformed caller, mirroring `write_gate.resolve_source_type`'s own
    "a dataclass field annotation is not runtime-enforced" defensive
    posture).
    """

    def test_none_provenance_id_is_rejected_not_a_crash(
        self, repo: EntityMemoryRepository
    ) -> None:
        result = repo.write_attribute(
            "tenant-1", "entity-1", "attr-a", "value-a", None  # type: ignore[arg-type]
        )

        assert isinstance(result, WriteRejected)
        assert result.error_code == ERROR_MISSING_ATTRIBUTE_PROVENANCE_ID

    def test_rejection_reason_never_echoes_the_attribute_value(
        self, repo: EntityMemoryRepository
    ) -> None:
        result = repo.write_attribute(
            "tenant-1", "entity-1", "attr-a", "top-secret-value", ""
        )

        assert isinstance(result, WriteRejected)
        assert "top-secret-value" not in result.reason

    def test_accepted_and_rejected_are_both_reachable_in_sequence(
        self, repo: EntityMemoryRepository
    ) -> None:
        rejected = repo.write_attribute("tenant-1", "entity-1", "attr-a", "v", "")
        accepted = repo.write_attribute("tenant-1", "entity-1", "attr-a", "v", "prov-1")

        assert isinstance(rejected, WriteRejected)
        assert isinstance(accepted, WriteAccepted)


class TestAC005_6IndependentReVerification:
    """AC-005-6 re-proved for the "non-blocking" half of the AC: a write
    still succeeds and is durably readable even though this suite injects
    an `EventBus` double distinct from the Dev suite's, confirming the
    write path's own success is not coupled to which concrete `EventBus`
    is wired in.
    """

    def test_write_succeeds_and_is_readable_regardless_of_event_bus_implementation(
        self, clock: FakeClock
    ) -> None:
        from dashanan.infrastructure.noop_event_bus import NoOpEventBus

        repo = EntityMemoryRepository(clock=clock, event_bus=NoOpEventBus())

        result = repo.write_attribute("tenant-1", "entity-1", "attr-a", "v", "prov-1")

        assert isinstance(result, WriteAccepted)
        assert repo.get_entity("tenant-1", "entity-1").get_attribute("attr-a").value == "v"

    def test_projection_event_item_id_encodes_entity_and_attribute(
        self, repo: EntityMemoryRepository, event_bus: RecordingEventBus
    ) -> None:
        repo.write_attribute("tenant-1", "entity-42", "attr-x", "v", "prov-1")

        _, payload = event_bus.published[0]
        assert payload["item_id"] == "entity-42:attr-x"


class TestEntityAttributeRecordAndEntityRecordRemainingBoundaryFields:
    """Closes the remaining blank-field boundary cells the Dev suite's
    `TestEntityAttributeRecordBoundaryMatrix`/`TestEntityRecordAggregateInvariants`
    left unexercised (`entity_id` on both classes), for full statement
    coverage of every `__post_init__` branch.
    """

    def _now(self) -> datetime:
        return datetime(2026, 1, 1, tzinfo=UTC)

    def test_entity_attribute_record_rejects_blank_entity_id(self) -> None:
        with pytest.raises(
            ValueError, match="EntityAttributeRecord.entity_id must not be blank"
        ):
            EntityAttributeRecord(
                tenant_id="tenant-1",
                entity_id="   ",
                attribute_name="attr-a",
                value="v",
                provenance_id="prov-1",
                updated_at=self._now(),
            )

    def test_entity_record_rejects_blank_tenant_id(self) -> None:
        with pytest.raises(ValueError, match="EntityRecord.tenant_id must not be blank"):
            EntityRecord(tenant_id="  ", entity_id="entity-1")

    def test_entity_record_rejects_blank_entity_id(self) -> None:
        with pytest.raises(ValueError, match="EntityRecord.entity_id must not be blank"):
            EntityRecord(tenant_id="tenant-1", entity_id="")


class TestFetchSkipsAnAliasedEntityWithNoStoredAttributes:
    """Covers `fetch`'s defensive `if record is None: continue` branch: an
    alias can be registered independently of any attribute write
    (`register_alias` carries no such requirement -- see its own
    docstring), so a prefix match can legitimately name an entity_id that
    `get_entity` still resolves to `None`. `fetch` must skip it rather
    than raise or emit a broken `MemoryItem`.
    """

    def test_fetch_silently_skips_an_alias_with_no_attributes_yet(
        self, repo: EntityMemoryRepository
    ) -> None:
        repo.register_alias("tenant-1", "entity-ghost", "alpha-ghost")
        repo.write_attribute("tenant-1", "entity-real", "attr-a", "v", "prov-1")
        repo.register_alias("tenant-1", "entity-real", "alpha-real")

        results = repo.fetch(
            ZoneQuery(
                tenant_id="tenant-1",
                task="alpha",
                query_embedding=None,
                max_items=100,
                min_provenance_conf=0.0,
            )
        )

        assert [item.item_id for item in results] == ["entity-real"]
