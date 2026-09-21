"""Dev pytest suite for DASH-STORY-017 (Entity Memory zone / Zone 5, FR-005, DSHN-66).

FR-005 (verbatim, SRS.md, cited exactly as the dev_prompt requires): "The
system SHALL provide an Entity Memory zone holding per-entity
(person/project/system) profile records, each the canonical sole owner
of that entity's own attributes." Traced by DASH-STORY-017, "Entity
Memory zone (Zone 5) - canonical per-entity attribute store with
sub-record provenance and alias Trie resolution."

Covers every acceptance criterion from the dev_prompt:
  AC-005-1: exact (tenant_id, entity_id) point lookup.
  AC-005-2: single-attribute update touches only that attribute.
  AC-005-3: alias Trie prefix resolution, empty set (not error) on no match.
  AC-005-4: exact-term resolution with zero Zone 6 dependency.
  AC-005-5: a write without provenance_id is rejected (422), reusing the
    DASH-STORY-006/AC-010 WriteRejected/WriteAccepted Result-type contract.
  AC-005-6: a successful write publishes a non-blocking projection event.

Runtime assumptions (matching tests/test_qa_working_memory_zone1.py's own
documented convention -- no HTTP/router/response-envelope layer exists in
this codebase yet):
  - clock: FakeClock, fixed at 2026-01-01T00:00:00+00:00 unless stated.
  - tenant_id: "tenant-1", entity_id: "entity-1" unless stated otherwise.
  - No fixture seed required; nothing in this suite is randomized.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from dashanan.domain.entity_alias_trie import AliasTrie
from dashanan.domain.entity_record import EntityAttributeRecord, EntityRecord
from dashanan.domain.ports import ZoneQuery
from dashanan.domain.write_gate import WriteAccepted, WriteRejected
from dashanan.domain.zone import ZoneId
from dashanan.infrastructure.entity_memory_repository import (
    ERROR_MISSING_ATTRIBUTE_PROVENANCE_ID,
    PROJECTION_EVENT_TYPE,
    EntityMemoryRepository,
)


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


def _zone_query(**overrides: object) -> ZoneQuery:
    defaults: dict[str, object] = {
        "tenant_id": "tenant-1",
        "task": None,
        "query_embedding": None,
        "max_items": 100,
        "min_provenance_conf": 0.0,
    }
    defaults.update(overrides)
    return ZoneQuery(**defaults)  # type: ignore[arg-type]


class TestAC005_1PointLookup:
    """AC-005-1 (verbatim): "Zone 5 returns an EntityRecord by exact
    (tenant_id, entity_id) via O(1) point lookup."
    """

    def test_get_entity_returns_none_before_any_write(
        self, repo: EntityMemoryRepository
    ) -> None:
        assert repo.get_entity("tenant-1", "entity-1") is None

    def test_get_entity_returns_record_after_write(
        self, repo: EntityMemoryRepository
    ) -> None:
        repo.write_attribute("tenant-1", "entity-1", "attr-a", "value-a", "prov-1")

        record = repo.get_entity("tenant-1", "entity-1")

        assert record is not None
        assert record.tenant_id == "tenant-1"
        assert record.entity_id == "entity-1"
        assert record.get_attribute("attr-a").value == "value-a"

    def test_get_entity_is_exact_key_only_no_cross_entity_leak(
        self, repo: EntityMemoryRepository
    ) -> None:
        repo.write_attribute("tenant-1", "entity-1", "attr-a", "value-a", "prov-1")
        repo.write_attribute("tenant-1", "entity-2", "attr-b", "value-b", "prov-2")

        record = repo.get_entity("tenant-1", "entity-1")

        assert record is not None
        assert record.get_attribute("attr-b") is None

    def test_get_entity_is_isolated_across_tenants(
        self, repo: EntityMemoryRepository
    ) -> None:
        repo.write_attribute("tenant-1", "entity-1", "attr-a", "value-a", "prov-1")

        assert repo.get_entity("tenant-2", "entity-1") is None

    def test_get_entity_rejects_blank_tenant_id(
        self, repo: EntityMemoryRepository
    ) -> None:
        with pytest.raises(ValueError, match="tenant_id must not be blank"):
            repo.get_entity("   ", "entity-1")


class TestEraseEntityDash025:
    """DASH-STORY-025's Zone 5 DPDP erasure leg (AC-025-2): `erase_entity`."""

    def test_erases_every_attribute_and_returns_their_item_ids(
        self, repo: EntityMemoryRepository
    ) -> None:
        repo.write_attribute("tenant-1", "entity-1", "attr-a", "value-a", "prov-1")
        repo.write_attribute("tenant-1", "entity-1", "attr-b", "value-b", "prov-2")

        erased = repo.erase_entity("tenant-1", "entity-1")

        assert set(erased) == {"entity-1:attr-a", "entity-1:attr-b"}
        assert repo.get_entity("tenant-1", "entity-1") is None

    def test_no_attribute_ever_written_is_a_clean_noop(
        self, repo: EntityMemoryRepository
    ) -> None:
        erased = repo.erase_entity("tenant-1", "never-written-entity")

        assert erased == ()
        assert repo.get_entity("tenant-1", "never-written-entity") is None

    def test_erasing_one_entity_never_touches_a_different_entitys_attributes(
        self, repo: EntityMemoryRepository
    ) -> None:
        repo.write_attribute("tenant-1", "entity-1", "attr-a", "value-a", "prov-1")
        repo.write_attribute("tenant-1", "entity-2", "attr-b", "value-b", "prov-2")

        repo.erase_entity("tenant-1", "entity-1")

        assert repo.get_entity("tenant-1", "entity-1") is None
        record = repo.get_entity("tenant-1", "entity-2")
        assert record is not None
        assert record.get_attribute("attr-b").value == "value-b"

    def test_erasing_one_tenants_entity_never_touches_another_tenants_same_entity_id(
        self, repo: EntityMemoryRepository
    ) -> None:
        repo.write_attribute("tenant-1", "entity-1", "attr-a", "value-a", "prov-1")
        repo.write_attribute("tenant-2", "entity-1", "attr-a", "value-a", "prov-2")

        repo.erase_entity("tenant-1", "entity-1")

        assert repo.get_entity("tenant-1", "entity-1") is None
        assert repo.get_entity("tenant-2", "entity-1") is not None

    def test_a_write_after_erasure_is_stored_again_normally(
        self, repo: EntityMemoryRepository
    ) -> None:
        repo.write_attribute("tenant-1", "entity-1", "attr-a", "value-a", "prov-1")
        repo.erase_entity("tenant-1", "entity-1")

        repo.write_attribute("tenant-1", "entity-1", "attr-c", "value-c", "prov-3")

        record = repo.get_entity("tenant-1", "entity-1")
        assert record is not None
        assert record.get_attribute("attr-c").value == "value-c"

    def test_rejects_blank_tenant_id(self, repo: EntityMemoryRepository) -> None:
        with pytest.raises(ValueError, match="tenant_id must not be blank"):
            repo.erase_entity("   ", "entity-1")

    def test_rejects_blank_entity_id(self, repo: EntityMemoryRepository) -> None:
        with pytest.raises(ValueError, match="entity_id must not be blank"):
            repo.erase_entity("tenant-1", "   ")

    def test_erasure_purges_the_entitys_aliases_from_prefix_resolution(
        self, repo: EntityMemoryRepository
    ) -> None:
        """DSHN-70 (HIGH) regression guard: `erase_entity` must also purge
        `tenant_id`'s own `AliasTrie` -- without this fix, an alias
        registered for the erased `entity_id` kept resolving via
        `resolve_alias_prefix` after the attribute data itself was gone."""
        repo.write_attribute("tenant-1", "entity-1", "attr-a", "value-a", "prov-1")
        repo.register_alias("tenant-1", "entity-1", "alpha-one")
        assert repo.resolve_alias_prefix("tenant-1", "alpha") == frozenset({"entity-1"})

        repo.erase_entity("tenant-1", "entity-1")

        assert repo.resolve_alias_prefix("tenant-1", "alpha") == frozenset()

    def test_erasure_purges_the_entitys_aliases_from_exact_term_resolution(
        self, repo: EntityMemoryRepository
    ) -> None:
        """Same DSHN-70 (HIGH) regression guard as above, via
        `resolve_exact_term` (AC-005-4's own resolution path)."""
        repo.write_attribute("tenant-1", "entity-1", "attr-a", "value-a", "prov-1")
        repo.register_alias("tenant-1", "entity-1", "exact-alias")
        assert repo.resolve_exact_term("tenant-1", "exact-alias") == frozenset(
            {"entity-1"}
        )

        repo.erase_entity("tenant-1", "entity-1")

        assert repo.resolve_exact_term("tenant-1", "exact-alias") == frozenset()

    def test_erasure_leaves_a_different_entitys_alias_on_the_same_term_resolving(
        self, repo: EntityMemoryRepository
    ) -> None:
        """Purging the erased entity's alias registration must not disturb a
        different, still-live entity that shares the exact same alias term."""
        repo.write_attribute("tenant-1", "entity-1", "attr-a", "value-a", "prov-1")
        repo.write_attribute("tenant-1", "entity-2", "attr-a", "value-a", "prov-2")
        repo.register_alias("tenant-1", "entity-1", "shared-alias")
        repo.register_alias("tenant-1", "entity-2", "shared-alias")

        repo.erase_entity("tenant-1", "entity-1")

        assert repo.resolve_exact_term("tenant-1", "shared-alias") == frozenset(
            {"entity-2"}
        )

    def test_erasure_of_entity_with_no_registered_alias_does_not_crash(
        self, repo: EntityMemoryRepository
    ) -> None:
        repo.write_attribute("tenant-1", "entity-1", "attr-a", "value-a", "prov-1")

        erased = repo.erase_entity("tenant-1", "entity-1")

        assert erased == ("entity-1:attr-a",)


class TestAC005_2SingleAttributeUpdate:
    """AC-005-2 (verbatim): "A single attribute update writes exactly one
    new provenance record for that attribute only; no sibling attribute's
    value/provenance_id/updated_at changes; no whole-record rewrite
    occurs."
    """

    def test_updating_one_attribute_leaves_sibling_untouched(
        self, repo: EntityMemoryRepository, clock: FakeClock
    ) -> None:
        repo.write_attribute("tenant-1", "entity-1", "attr-a", "value-a", "prov-1")
        sibling_before = repo.get_entity("tenant-1", "entity-1").get_attribute("attr-a")

        clock.advance(seconds=60)
        repo.write_attribute("tenant-1", "entity-1", "attr-b", "value-b", "prov-2")

        sibling_after = repo.get_entity("tenant-1", "entity-1").get_attribute("attr-a")
        assert sibling_after == sibling_before
        assert sibling_after.value == "value-a"
        assert sibling_after.provenance_id == "prov-1"
        assert sibling_after.updated_at == sibling_before.updated_at

    def test_no_whole_record_rewrite_sibling_object_identity_preserved(
        self, repo: EntityMemoryRepository
    ) -> None:
        """Stronger than equality: the SAME `EntityAttributeRecord` object
        for the untouched attribute must still be the one stored, proving
        the repository mutated only the one dict key `write_attribute`
        named -- never rebuilt the whole per-entity attribute dict.
        """
        repo.write_attribute("tenant-1", "entity-1", "attr-a", "value-a", "prov-1")
        stored_attrs = repo._entity_attributes[("tenant-1", "entity-1")]
        sibling_object = stored_attrs["attr-a"]

        repo.write_attribute("tenant-1", "entity-1", "attr-b", "value-b", "prov-2")

        assert stored_attrs["attr-a"] is sibling_object

    def test_overwriting_the_same_attribute_replaces_only_that_key(
        self, repo: EntityMemoryRepository, clock: FakeClock
    ) -> None:
        repo.write_attribute("tenant-1", "entity-1", "attr-a", "value-1", "prov-1")
        clock.advance(seconds=10)
        repo.write_attribute("tenant-1", "entity-1", "attr-a", "value-2", "prov-2")

        record = repo.get_entity("tenant-1", "entity-1")
        assert record.attributes == (record.get_attribute("attr-a"),)
        assert record.get_attribute("attr-a").value == "value-2"
        assert record.get_attribute("attr-a").provenance_id == "prov-2"


class TestAC005_3AliasPrefixResolution:
    """AC-005-3 (verbatim): "Alias prefix resolution returns matching
    entity_id(s) via Trie prefix match, empty set (not error) when no
    match."
    """

    def test_prefix_search_returns_matching_entity_ids(
        self, repo: EntityMemoryRepository
    ) -> None:
        repo.register_alias("tenant-1", "entity-1", "alpha-one")
        repo.register_alias("tenant-1", "entity-2", "alpha-two")
        repo.register_alias("tenant-1", "entity-3", "beta-one")

        matches = repo.resolve_alias_prefix("tenant-1", "alpha")

        assert matches == frozenset({"entity-1", "entity-2"})

    def test_prefix_search_with_no_match_returns_empty_set_not_error(
        self, repo: EntityMemoryRepository
    ) -> None:
        repo.register_alias("tenant-1", "entity-1", "alpha-one")

        matches = repo.resolve_alias_prefix("tenant-1", "zzz-nonexistent")

        assert matches == frozenset()

    def test_prefix_search_on_tenant_with_no_aliases_returns_empty_set(
        self, repo: EntityMemoryRepository
    ) -> None:
        assert repo.resolve_alias_prefix("tenant-unknown", "alpha") == frozenset()

    def test_alias_resolution_is_isolated_per_tenant(
        self, repo: EntityMemoryRepository
    ) -> None:
        repo.register_alias("tenant-1", "entity-1", "alpha-one")
        repo.register_alias("tenant-2", "entity-9", "alpha-one")

        assert repo.resolve_alias_prefix("tenant-1", "alpha") == frozenset({"entity-1"})
        assert repo.resolve_alias_prefix("tenant-2", "alpha") == frozenset({"entity-9"})


class TestAC005_4ExactTermIndependentOfZone6:
    """AC-005-4 (verbatim): "Exact entity-name/exact-term queries are
    answered directly by Zone 5's own mechanism even with Zone 6
    unavailable, demonstrating the deliberately-not-vector-primary design
    as observable behavior."
    """

    def test_exact_term_resolves_with_no_zone6_port_ever_constructed(
        self, clock: FakeClock, event_bus: RecordingEventBus
    ) -> None:
        """This repository is built from only a Clock and an EventBus --
        no `VectorIndexPort`/`LexicalIndexPort` is passed to it, and none
        could be: the constructor accepts no such argument. A successful
        exact-term resolution here is therefore structural proof the
        query path never depends on Zone 6 being reachable.
        """
        repo = EntityMemoryRepository(clock=clock, event_bus=event_bus)
        repo.register_alias("tenant-1", "entity-1", "exact-term")

        assert repo.resolve_exact_term("tenant-1", "exact-term") == frozenset({"entity-1"})

    def test_exact_term_does_not_match_a_strict_prefix_of_a_longer_alias(
        self, repo: EntityMemoryRepository
    ) -> None:
        repo.register_alias("tenant-1", "entity-1", "alpha-one")

        assert repo.resolve_exact_term("tenant-1", "alpha") == frozenset()

    def test_exact_term_with_no_match_returns_empty_set_not_error(
        self, repo: EntityMemoryRepository
    ) -> None:
        assert repo.resolve_exact_term("tenant-1", "nonexistent") == frozenset()


class TestAC005_5MissingProvenanceIdRejected:
    """AC-005-5 (verbatim): "A write attempting to persist an EntityRecord
    attribute without its own provenance_id is rejected using the same
    rejection contract DASH-STORY-006/AC-010 established (HTTP 422)."
    """

    def test_blank_provenance_id_is_rejected_with_422(
        self, repo: EntityMemoryRepository
    ) -> None:
        result = repo.write_attribute("tenant-1", "entity-1", "attr-a", "value-a", "")

        assert isinstance(result, WriteRejected)
        assert result.http_status == 422
        assert result.error_code == ERROR_MISSING_ATTRIBUTE_PROVENANCE_ID

    def test_whitespace_only_provenance_id_is_rejected(
        self, repo: EntityMemoryRepository
    ) -> None:
        result = repo.write_attribute("tenant-1", "entity-1", "attr-a", "value-a", "   ")

        assert isinstance(result, WriteRejected)
        assert result.http_status == 422

    def test_rejected_write_persists_nothing(self, repo: EntityMemoryRepository) -> None:
        repo.write_attribute("tenant-1", "entity-1", "attr-a", "value-a", "")

        assert repo.get_entity("tenant-1", "entity-1") is None

    def test_rejected_write_publishes_no_projection_event(
        self, repo: EntityMemoryRepository, event_bus: RecordingEventBus
    ) -> None:
        repo.write_attribute("tenant-1", "entity-1", "attr-a", "value-a", "")

        assert event_bus.published == []

    def test_valid_provenance_id_is_accepted(self, repo: EntityMemoryRepository) -> None:
        result = repo.write_attribute("tenant-1", "entity-1", "attr-a", "value-a", "prov-1")

        assert isinstance(result, WriteAccepted)
        assert result.write_id == "prov-1"


class TestAC005_6ProjectionEventOnCommit:
    """AC-005-6 (verbatim): "On write commit, a projection event is
    published to the EventBus for future Zone 6 indexing, asynchronously
    and non-blocking."
    """

    def test_successful_write_publishes_exactly_one_projection_event(
        self, repo: EntityMemoryRepository, event_bus: RecordingEventBus
    ) -> None:
        repo.write_attribute("tenant-1", "entity-1", "attr-a", "value-a", "prov-1")

        assert len(event_bus.published) == 1
        event_type, payload = event_bus.published[0]
        assert event_type == PROJECTION_EVENT_TYPE
        assert payload["tenant_id"] == "tenant-1"
        assert payload["entity_id"] == "entity-1"
        assert payload["attribute_name"] == "attr-a"
        assert payload["zone"] == ZoneId.ENTITY.value

    def test_projection_event_payload_never_carries_the_attribute_value(
        self, repo: EntityMemoryRepository, event_bus: RecordingEventBus
    ) -> None:
        """HLD Section 3.10 Hard Rule 2: cross-zone references use
        entity_id only -- the projection event is exactly such a
        cross-zone reference, so it must never leak `value`.
        """
        repo.write_attribute(
            "tenant-1", "entity-1", "attr-a", "sensitive-value", "prov-1"
        )

        _, payload = event_bus.published[0]
        assert "value" not in payload
        assert "sensitive-value" not in repr(payload)

    def test_each_write_publishes_its_own_event(
        self, repo: EntityMemoryRepository, event_bus: RecordingEventBus
    ) -> None:
        repo.write_attribute("tenant-1", "entity-1", "attr-a", "value-a", "prov-1")
        repo.write_attribute("tenant-1", "entity-1", "attr-b", "value-b", "prov-2")

        assert len(event_bus.published) == 2


class TestZoneRepositoryFetchContract:
    """`ZoneRepository.fetch` conformance (HLD Section 7.1), exercised
    through the alias-prefix mechanism `fetch` documents using.
    """

    def test_fetch_with_matching_task_returns_entity_items(
        self, repo: EntityMemoryRepository
    ) -> None:
        repo.write_attribute("tenant-1", "entity-1", "attr-a", "value-a", "prov-1")
        repo.register_alias("tenant-1", "entity-1", "alpha-one")

        results = repo.fetch(_zone_query(task="alpha"))

        assert [item.item_id for item in results] == ["entity-1"]
        assert results[0].source_zone == ZoneId.ENTITY

    def test_fetch_with_blank_task_returns_empty_list(
        self, repo: EntityMemoryRepository
    ) -> None:
        assert repo.fetch(_zone_query(task=None)) == []
        assert repo.fetch(_zone_query(task="   ")) == []

    def test_fetch_respects_max_items(self, repo: EntityMemoryRepository) -> None:
        for i in range(3):
            entity_id = f"entity-{i}"
            repo.write_attribute("tenant-1", entity_id, "attr-a", "v", f"prov-{i}")
            repo.register_alias("tenant-1", entity_id, f"alpha-{i}")

        results = repo.fetch(_zone_query(task="alpha", max_items=2))

        assert len(results) == 2


class TestEntityAttributeRecordBoundaryMatrix:
    """Boundary/negative matrix for `EntityAttributeRecord.__post_init__`."""

    def _now(self) -> datetime:
        return datetime(2026, 1, 1, tzinfo=UTC)

    def test_rejects_blank_tenant_id(self) -> None:
        with pytest.raises(ValueError, match="tenant_id must not be blank"):
            EntityAttributeRecord(
                tenant_id="  ",
                entity_id="entity-1",
                attribute_name="attr-a",
                value="v",
                provenance_id="prov-1",
                updated_at=self._now(),
            )

    def test_rejects_blank_provenance_id(self) -> None:
        with pytest.raises(ValueError, match="provenance_id must not be blank"):
            EntityAttributeRecord(
                tenant_id="tenant-1",
                entity_id="entity-1",
                attribute_name="attr-a",
                value="v",
                provenance_id="",
                updated_at=self._now(),
            )

    def test_rejects_blank_attribute_name(self) -> None:
        with pytest.raises(ValueError, match="attribute_name must not be blank"):
            EntityAttributeRecord(
                tenant_id="tenant-1",
                entity_id="entity-1",
                attribute_name="",
                value="v",
                provenance_id="prov-1",
                updated_at=self._now(),
            )


class TestEntityRecordAggregateInvariants:
    """`EntityRecord.__post_init__`'s aggregate-level checks."""

    def _now(self) -> datetime:
        return datetime(2026, 1, 1, tzinfo=UTC)

    def _attr(self, **overrides: object) -> EntityAttributeRecord:
        defaults: dict[str, object] = {
            "tenant_id": "tenant-1",
            "entity_id": "entity-1",
            "attribute_name": "attr-a",
            "value": "v",
            "provenance_id": "prov-1",
            "updated_at": self._now(),
        }
        defaults.update(overrides)
        return EntityAttributeRecord(**defaults)  # type: ignore[arg-type]

    def test_empty_attributes_is_valid(self) -> None:
        record = EntityRecord(tenant_id="tenant-1", entity_id="entity-1")
        assert record.attributes == ()
        assert record.get_attribute("missing") is None

    def test_rejects_attribute_belonging_to_a_different_entity(self) -> None:
        mismatched = self._attr(entity_id="entity-OTHER")
        with pytest.raises(ValueError, match="must all share this record's"):
            EntityRecord(
                tenant_id="tenant-1", entity_id="entity-1", attributes=(mismatched,)
            )

    def test_rejects_duplicate_attribute_name(self) -> None:
        one = self._attr(value="v1")
        two = self._attr(value="v2")
        with pytest.raises(ValueError, match="duplicate attribute_name"):
            EntityRecord(tenant_id="tenant-1", entity_id="entity-1", attributes=(one, two))


class TestAliasTrieUnit:
    """Direct unit coverage of `AliasTrie`, independent of the repository."""

    def test_insert_and_exact_match(self) -> None:
        trie = AliasTrie()
        trie.insert("alpha", "entity-1")

        assert trie.exact_match("alpha") == frozenset({"entity-1"})

    def test_prefix_search_collects_multiple_entities(self) -> None:
        trie = AliasTrie()
        trie.insert("alpha-one", "entity-1")
        trie.insert("alpha-two", "entity-2")

        assert trie.prefix_search("alpha") == frozenset({"entity-1", "entity-2"})

    def test_prefix_search_empty_prefix_matches_everything(self) -> None:
        trie = AliasTrie()
        trie.insert("alpha", "entity-1")
        trie.insert("beta", "entity-2")

        assert trie.prefix_search("") == frozenset({"entity-1", "entity-2"})

    def test_no_match_returns_empty_frozenset_not_error(self) -> None:
        trie = AliasTrie()
        assert trie.prefix_search("anything") == frozenset()
        assert trie.exact_match("anything") == frozenset()

    def test_remove_is_idempotent_and_unregisters(self) -> None:
        trie = AliasTrie()
        trie.insert("alpha", "entity-1")

        trie.remove("alpha", "entity-1")
        assert trie.exact_match("alpha") == frozenset()

        # Removing again (already absent) must not raise.
        trie.remove("alpha", "entity-1")
        trie.remove("never-inserted", "entity-9")

    def test_insert_rejects_empty_alias(self) -> None:
        trie = AliasTrie()
        with pytest.raises(ValueError, match="non-empty alias"):
            trie.insert("", "entity-1")

    def test_insert_rejects_blank_entity_id(self) -> None:
        trie = AliasTrie()
        with pytest.raises(ValueError, match="non-blank entity_id"):
            trie.insert("alpha", "  ")
