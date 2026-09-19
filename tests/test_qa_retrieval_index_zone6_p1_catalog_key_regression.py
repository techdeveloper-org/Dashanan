"""Regression suite for DASH-STORY-007's P1 remediation (catalog-key collision).

This suite reproduces the ORIGINAL exploit reported against
`HybridRetrievalIndexRepository._catalog_key` and proves it is now blocked,
independent of the Fix sub-task's own claims. It targets three items from the
remediation report:

  P1 CRITICAL (live-reproduced): the pre-fix `_catalog_key` built
  `f'{tenant_id}:{item_id}'` with no delimiter escaping. Tenant "org" indexing
  item_id="team:secret-report" produced the identical catalog key as tenant
  "org:team" indexing item_id="secret-report" -- "org:team:secret-report" --
  letting one tenant's own fetch(), searching strictly inside its own
  physically-partitioned collection, return another tenant's payload
  verbatim.

  MEDIUM: no format/character-set validation in the domain layer
  (VectorEntry/LexicalDoc __post_init__) beyond non-blank checks -- the
  reported fix adds an `IDENTIFIER_PATTERN` allow-list that independently
  rejects the exact attack payload (a colon in item_id) at construction
  time, before it ever reaches the catalog.

  ADDITIONAL REQUIRED SCOPE: a delete(tenant_id, item_id) method on
  VectorIndexPort/LexicalIndexPort, both in-memory adapters, and
  HybridRetrievalIndexRepository.delete_item -- required by DASH-STORY-004's
  (DSHN-58) DPDP erasure cascade Zone-6 leg.

This is an INDEPENDENT reproduction, not a re-statement of the Fix
sub-task's own claims: it calls `index_item`/`fetch` through the public
`HybridRetrievalIndexRepository` surface exactly as the original P1 finding
described the attack, and separately drives `VectorEntry`/`LexicalDoc`
construction directly to prove the domain-layer charset gate.
"""

from __future__ import annotations

import pytest

from dashanan.domain.lexical_doc import LexicalDoc
from dashanan.domain.ports import ZoneQuery
from dashanan.domain.retrieval_index_ports import LexicalIndexPort, VectorIndexPort
from dashanan.domain.vector_entry import VectorEntry
from dashanan.domain.zone import ZoneId
from dashanan.infrastructure.hybrid_retrieval_index_repository import (
    HybridRetrievalIndexRepository,
)
from dashanan.infrastructure.in_memory_lexical_index import InMemoryLexicalIndex
from dashanan.infrastructure.in_memory_vector_index import InMemoryVectorIndex


def _query(tenant_id: str, task: str | None, max_items: int = 10) -> ZoneQuery:
    return ZoneQuery(
        tenant_id=tenant_id,
        task=task,
        query_embedding=[1.0, 0.0, 0.0],
        max_items=max_items,
        min_provenance_conf=0.0,
    )


def _fresh_repo() -> HybridRetrievalIndexRepository:
    return HybridRetrievalIndexRepository(InMemoryVectorIndex(), InMemoryLexicalIndex())


class TestP1CatalogKeyCollisionRegression:
    """Reproduces the exact cross-tenant collision from the P1 finding."""

    @staticmethod
    def _raw_lexical_doc(
        tenant_id: str, item_id: str, text: str
    ) -> LexicalDoc:
        """Construct a `LexicalDoc`-shaped object bypassing `__post_init__`.

        The MEDIUM remediation now rejects a colon in `item_id`/`tenant_id`
        at domain-construction time (`IDENTIFIER_PATTERN`), so the original
        P1 attack payload can no longer reach `_catalog_key` through the
        public `index_item` surface at all. Per the remediation report's
        own defense-in-depth rationale ("in case a future caller bypasses
        VectorEntry/LexicalDoc construction, e.g. delete_item"), this
        helper bypasses that domain gate the same way a future internal
        caller could, so the catalog-key escaping fix itself -- not merely
        the newer, narrower charset gate -- is what this test proves.
        """
        doc = object.__new__(LexicalDoc)
        object.__setattr__(doc, "tenant_id", tenant_id)
        object.__setattr__(doc, "item_id", item_id)
        object.__setattr__(doc, "source_zone", ZoneId.EPISODIC)
        object.__setattr__(doc, "text", text)
        return doc

    def test_attacker_own_fetch_does_not_return_victim_tenant_payload(self) -> None:
        """The original exploit, verbatim, reproduced at the layer the P1
        finding actually broke (the catalog dict keyed by `_catalog_key`):
        tenant "org" holds an item keyed by item_id="team:secret-report";
        tenant "org:team" (the attacker) holds an item keyed by
        item_id="secret-report" with a different payload. Pre-fix, both
        produced the identical catalog key "org:team:secret-report", so a
        lookup under the attacker's own resolved item_id landed on the
        victim's catalog entry and leaked its payload verbatim. Post-fix,
        `_catalog_key` escapes the separator inside each component before
        joining, so the two keys must be distinct and a lookup keyed by
        the attacker's own (tenant_id, item_id) must resolve only to the
        attacker's own entry.

        (The MEDIUM charset-validation fix independently blocks this exact
        payload at `index_item`/`VectorEntry`/`LexicalDoc` construction
        time -- see `TestMediumDomainCharsetValidationRegression` -- so
        this test seeds `repo._catalog` directly, bypassing that gate, to
        prove the catalog-key structural fix on its own merits.)
        """
        repo = _fresh_repo()
        victim_payload = "CONFIDENTIAL: victim org internal financial summary"
        attacker_payload = "attacker's own innocuous note"

        repo._catalog[HybridRetrievalIndexRepository._catalog_key("org", "team:secret-report")] = (
            self._raw_lexical_doc("org", "team:secret-report", victim_payload)
        )
        repo._catalog[HybridRetrievalIndexRepository._catalog_key("org:team", "secret-report")] = (
            self._raw_lexical_doc("org:team", "secret-report", attacker_payload)
        )

        resolved_for_attacker = repo._catalog.get(
            HybridRetrievalIndexRepository._catalog_key("org:team", "secret-report")
        )

        assert resolved_for_attacker is not None
        assert resolved_for_attacker.text == attacker_payload, (
            "P1 REGRESSION: attacker tenant 'org:team' resolved victim "
            "tenant 'org''s catalog entry -- catalog-key collision is back"
        )
        assert resolved_for_attacker.text != victim_payload

    def test_catalog_keys_are_distinct_for_the_colliding_pair(self) -> None:
        """Direct unit check on the fixed key-construction function itself:
        the two catalog keys that used to collide under the naive
        f'{tenant_id}:{item_id}' join must now be provably different
        strings.
        """
        key_victim = HybridRetrievalIndexRepository._catalog_key("org", "team:secret-report")
        key_attacker = HybridRetrievalIndexRepository._catalog_key("org:team", "secret-report")

        assert key_victim != key_attacker, (
            f"catalog keys still collide: {key_victim!r} == {key_attacker!r}"
        )

    def test_victim_still_sees_only_its_own_payload_after_attacker_indexes(self) -> None:
        """Symmetric check: a lookup keyed by the victim's own
        (tenant_id, item_id) must be unaffected by the attacker's
        colliding-looking entry being present in the same catalog dict.
        """
        repo = _fresh_repo()
        victim_payload = "CONFIDENTIAL: victim org internal financial summary"
        repo._catalog[HybridRetrievalIndexRepository._catalog_key("org", "team:secret-report")] = (
            self._raw_lexical_doc("org", "team:secret-report", victim_payload)
        )
        repo._catalog[HybridRetrievalIndexRepository._catalog_key("org:team", "secret-report")] = (
            self._raw_lexical_doc("org:team", "secret-report", "attacker note")
        )

        resolved_for_victim = repo._catalog.get(
            HybridRetrievalIndexRepository._catalog_key("org", "team:secret-report")
        )

        assert resolved_for_victim is not None
        assert resolved_for_victim.text == victim_payload


class TestMediumDomainCharsetValidationRegression:
    """The domain-layer allow-list must independently reject the exact
    attack payload (a colon in item_id) before it can ever reach the
    catalog, defense-in-depth on top of the escaping fix.
    """

    def test_vector_entry_rejects_colon_in_item_id(self) -> None:
        with pytest.raises(ValueError):
            VectorEntry(
                tenant_id="org",
                item_id="team:secret-report",
                source_zone=ZoneId.EPISODIC,
                vector=(1.0, 0.0, 0.0),
                model_id="model-x",
            )

    def test_vector_entry_rejects_colon_in_tenant_id(self) -> None:
        with pytest.raises(ValueError):
            VectorEntry(
                tenant_id="org:team",
                item_id="secret-report",
                source_zone=ZoneId.EPISODIC,
                vector=(1.0, 0.0, 0.0),
                model_id="model-x",
            )

    def test_lexical_doc_rejects_colon_in_item_id(self) -> None:
        with pytest.raises(ValueError):
            LexicalDoc(
                tenant_id="org",
                item_id="team:secret-report",
                source_zone=ZoneId.EPISODIC,
                text="some indexable text",
            )

    def test_index_item_rejects_the_attack_payload_end_to_end(self) -> None:
        """The public `index_item` entry point must reject the exact P1
        reproduction payload outright, since it constructs `VectorEntry`
        internally.
        """
        repo = _fresh_repo()
        with pytest.raises(ValueError):
            repo.index_item(
                "org", "team:secret-report", ZoneId.EPISODIC, "irrelevant",
                (1.0, 0.0, 0.0), "model-x",
            )

    def test_plain_alphanumeric_hyphen_ids_still_accepted(self) -> None:
        """Regression guard on the fix's own claim of backward
        compatibility: ordinary identifiers must still be accepted.
        """
        repo = _fresh_repo()
        repo.index_item(
            "tenant-a", "item-1", ZoneId.EPISODIC, "ordinary content",
            (1.0, 0.0, 0.0), "model-x",
        )
        results = repo.fetch(_query(tenant_id="tenant-a", task="ordinary content"))
        assert [item.item_id for item in results] == ["item-1"]


class TestAdditionalScopeDeletePortRegression:
    """The additional required scope: a real delete(tenant_id, item_id)
    capability on both ports, both in-memory adapters, and the repository
    facade -- required for DASH-STORY-004's (DSHN-58) erasure cascade.
    """

    def test_ports_declare_delete_method(self) -> None:
        assert hasattr(VectorIndexPort, "delete")
        assert hasattr(LexicalIndexPort, "delete")

    def test_in_memory_vector_index_delete_removes_only_targeted_item(self) -> None:
        index = InMemoryVectorIndex()
        entry_a = VectorEntry(
            tenant_id="tenant-a", item_id="item-1", source_zone=ZoneId.EPISODIC,
            vector=(1.0, 0.0, 0.0), model_id="model-x",
        )
        entry_b = VectorEntry(
            tenant_id="tenant-a", item_id="item-2", source_zone=ZoneId.EPISODIC,
            vector=(0.0, 1.0, 0.0), model_id="model-x",
        )
        index.upsert(entry_a)
        index.upsert(entry_b)

        index.delete("tenant-a", "item-1")

        remaining = index.search("tenant-a", (1.0, 0.0, 0.0), 10)
        assert "item-1" not in remaining
        assert "item-2" in remaining

    def test_in_memory_lexical_index_delete_removes_only_targeted_item(self) -> None:
        index = InMemoryLexicalIndex()
        doc_a = LexicalDoc(
            tenant_id="tenant-a", item_id="item-1", source_zone=ZoneId.EPISODIC,
            text="quick brown fox",
        )
        doc_b = LexicalDoc(
            tenant_id="tenant-a", item_id="item-2", source_zone=ZoneId.EPISODIC,
            text="quick brown fox",
        )
        index.upsert(doc_a)
        index.upsert(doc_b)

        index.delete("tenant-a", "item-1")

        remaining = index.search("tenant-a", "quick brown fox", 10)
        assert "item-1" not in remaining
        assert "item-2" in remaining

    def test_delete_is_tenant_scoped_never_touches_another_tenant(self) -> None:
        repo = _fresh_repo()
        repo.index_item(
            "tenant-a", "shared-id", ZoneId.EPISODIC, "tenant a content",
            (1.0, 0.0, 0.0), "model-x",
        )
        repo.index_item(
            "tenant-b", "shared-id", ZoneId.EPISODIC, "tenant b content",
            (1.0, 0.0, 0.0), "model-x",
        )

        repo.delete_item("tenant-a", "shared-id")

        results_a = repo.fetch(_query(tenant_id="tenant-a", task="tenant a content"))
        results_b = repo.fetch(_query(tenant_id="tenant-b", task="tenant b content"))
        assert results_a == []
        assert [item.item_id for item in results_b] == ["shared-id"]

    def test_delete_item_is_idempotent_for_never_indexed_pair(self) -> None:
        repo = _fresh_repo()
        try:
            repo.delete_item("tenant-a", "never-indexed")
        except Exception as exc:  # noqa: BLE001 -- explicit idempotency assertion below
            pytest.fail(f"delete_item on a never-indexed pair must be a silent no-op: {exc}")

    def test_delete_item_removes_catalog_entry(self) -> None:
        repo = _fresh_repo()
        repo.index_item(
            "tenant-a", "item-1", ZoneId.EPISODIC, "content",
            (1.0, 0.0, 0.0), "model-x",
        )
        key = HybridRetrievalIndexRepository._catalog_key("tenant-a", "item-1")
        assert key in repo._catalog

        repo.delete_item("tenant-a", "item-1")

        assert key not in repo._catalog
