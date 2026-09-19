"""QA regression suite for the DASH-STORY-004 P1 remediation (DSHN-58, attempt 1).

A live adversarial P1 security review rejected this story's original
implementation on three findings:

  CRITICAL-1: Zone 6's `VectorIndexPort`/`LexicalIndexPort` exposed no
    delete/remove capability at all -- a PII item evicted from Zone 2
    remained fully retrievable via Zone 6 search indefinitely. Already
    closed by DASH-STORY-007's remediation (DSHN-57), before this suite's
    own attempt 1 -- these tests independently re-prove that closure using
    the REAL `InMemoryVectorIndex`/`InMemoryLexicalIndex` adapters (no
    mocks), not just re-reading their source.

  CRITICAL-2: `DpdpErasureCascadePort` -- the story's sole DPDP-erasure
    mechanism -- was an unimplemented `Protocol` with zero concrete
    adapters; only a test fake existed anywhere in the repository, making
    AC-002-CAP-DPDP-1 structurally unverifiable. Closed by
    `dashanan.infrastructure.dpdp_erasure_cascade.CrossZoneDpdpErasureCascade`,
    a real Shape A adapter this suite exercises end to end against real
    Zone 6 indices (no test fakes standing in for the adapter itself).

  HIGH-3: `Zone2EvictionPort.evict` is explicitly Zone-2-only, so an
    ORDINARY (non-DPDP) eviction routinely left a partial-erasure state
    across Zone 2/6 as *normal operation*, not an edge case. Closed by
    `Zone2CapacityBackstopSweep._execute_one` now calling the new
    `RetrievalIndexEvictionPort` unconditionally for every decision --
    re-verified here with the REAL `RetrievalIndexEviction` adapter (not
    just the `FakeRetrievalIndexEviction` test double used in the sweep's
    own unit-test suite).

PII NOTE: mirrors every other DASH-STORY-004 test file's PII posture --
pseudonymized `item_id`/`tenant_id` values and abstract vector/text
fixtures only, never real or synthetic-realistic PII content.
"""

from __future__ import annotations

import pytest

from dashanan.application.zone2_capacity_backstop_sweep import Zone2BackstopPortError
from dashanan.domain.lexical_doc import LexicalDoc
from dashanan.domain.vector_entry import VectorEntry
from dashanan.domain.zone import ZoneId
from dashanan.infrastructure.dpdp_erasure_cascade import (
    CrossZoneDpdpErasureCascade,
    InMemoryErasureObligationStore,
    RetrievalIndexEviction,
)
from dashanan.infrastructure.in_memory_lexical_index import InMemoryLexicalIndex
from dashanan.infrastructure.in_memory_vector_index import InMemoryVectorIndex

_TENANT = "tenant-1"
_OTHER_TENANT = "tenant-2"


class RecordingZone2EvictionPort:
    """Zone2EvictionPort double: records evict() calls, can raise for specific items."""

    def __init__(self, raise_for: frozenset[str] = frozenset()) -> None:
        self.evicted: list[tuple[str, str]] = []
        self._raise_for = raise_for

    def evict(self, tenant_id: str, item_id: str) -> None:
        if item_id in self._raise_for:
            raise Zone2BackstopPortError(f"zone2 store unreachable for {item_id}")
        self.evicted.append((tenant_id, item_id))


def _index_item(
    vector_index: InMemoryVectorIndex,
    lexical_index: InMemoryLexicalIndex,
    tenant_id: str,
    item_id: str,
    text: str = "erasable-payload-content",
) -> None:
    """Seed both real Zone 6 surfaces with one item, mirroring
    `HybridRetrievalIndexRepository.index_item`'s own two writes."""
    vector_index.upsert(
        VectorEntry(
            tenant_id=tenant_id,
            item_id=item_id,
            source_zone=ZoneId.EPISODIC,
            vector=(1.0, 0.0, 0.0),
            model_id="test-embed-v1",
        )
    )
    lexical_index.upsert(
        LexicalDoc(
            tenant_id=tenant_id,
            item_id=item_id,
            source_zone=ZoneId.EPISODIC,
            text=text,
        )
    )


class TestInMemoryErasureObligationStore:
    """The concrete, real (non-mock) `ErasureObligationStore` adapter."""

    def test_no_obligation_by_default(self) -> None:
        store = InMemoryErasureObligationStore()
        assert store.has_pending_erasure(_TENANT, "item-1") is False

    def test_request_erasure_creates_a_pending_obligation(self) -> None:
        store = InMemoryErasureObligationStore()
        store.request_erasure(_TENANT, "item-1")
        assert store.has_pending_erasure(_TENANT, "item-1") is True

    def test_clear_pending_erasure_removes_the_obligation(self) -> None:
        store = InMemoryErasureObligationStore()
        store.request_erasure(_TENANT, "item-1")
        store.clear_pending_erasure(_TENANT, "item-1")
        assert store.has_pending_erasure(_TENANT, "item-1") is False

    def test_clear_pending_erasure_is_idempotent_when_nothing_pending(self) -> None:
        store = InMemoryErasureObligationStore()
        store.clear_pending_erasure(_TENANT, "never-requested")  # must not raise

    def test_obligation_is_tenant_scoped(self) -> None:
        store = InMemoryErasureObligationStore()
        store.request_erasure(_TENANT, "shared-item-id")
        assert store.has_pending_erasure(_OTHER_TENANT, "shared-item-id") is False

    def test_request_erasure_rejects_blank_tenant_id(self) -> None:
        store = InMemoryErasureObligationStore()
        with pytest.raises(ValueError, match="tenant_id"):
            store.request_erasure("   ", "item-1")

    def test_request_erasure_rejects_blank_item_id(self) -> None:
        store = InMemoryErasureObligationStore()
        with pytest.raises(ValueError, match="item_id"):
            store.request_erasure(_TENANT, "   ")


class TestRetrievalIndexEvictionRealAdapter:
    """CRITICAL-1 re-verification: real Zone 6 delete via the shared adapter class."""

    def test_delete_item_removes_from_both_real_indices(self) -> None:
        vector_index = InMemoryVectorIndex()
        lexical_index = InMemoryLexicalIndex()
        _index_item(vector_index, lexical_index, _TENANT, "erase-me")
        eviction = RetrievalIndexEviction(vector_index, lexical_index)

        assert "erase-me" in vector_index.search(_TENANT, (1.0, 0.0, 0.0), top_k=10)
        assert "erase-me" in lexical_index.search(_TENANT, "erasable-payload-content", top_k=10)

        eviction.delete_item(_TENANT, "erase-me")

        assert vector_index.search(_TENANT, (1.0, 0.0, 0.0), top_k=10) == []
        assert lexical_index.search(_TENANT, "erasable-payload-content", top_k=10) == []

    def test_delete_item_never_touches_another_tenants_collection(self) -> None:
        vector_index = InMemoryVectorIndex()
        lexical_index = InMemoryLexicalIndex()
        _index_item(vector_index, lexical_index, _TENANT, "shared-item-id")
        _index_item(vector_index, lexical_index, _OTHER_TENANT, "shared-item-id")
        eviction = RetrievalIndexEviction(vector_index, lexical_index)

        eviction.delete_item(_TENANT, "shared-item-id")

        assert vector_index.search(_TENANT, (1.0, 0.0, 0.0), top_k=10) == []
        assert "shared-item-id" in vector_index.search(
            _OTHER_TENANT, (1.0, 0.0, 0.0), top_k=10
        ), "deleting tenant-1's item must never remove tenant-2's own copy (ADR-007/ADR-013)"

    def test_delete_item_of_never_indexed_item_is_a_silent_no_op(self) -> None:
        eviction = RetrievalIndexEviction(InMemoryVectorIndex(), InMemoryLexicalIndex())
        eviction.delete_item(_TENANT, "never-indexed")  # must not raise

    def test_delete_item_rejects_blank_tenant_id(self) -> None:
        eviction = RetrievalIndexEviction(InMemoryVectorIndex(), InMemoryLexicalIndex())
        with pytest.raises(ValueError, match="tenant_id"):
            eviction.delete_item("   ", "item-1")

    def test_underlying_index_failure_is_translated_to_zone2backstopporterror(self) -> None:
        class RaisingVectorIndex:
            def upsert(self, entry: object) -> None: ...

            def search(self, *args: object, **kwargs: object) -> list[str]:
                return []

            def delete(self, tenant_id: str, item_id: str) -> None:
                raise RuntimeError("simulated backend outage")

        eviction = RetrievalIndexEviction(RaisingVectorIndex(), InMemoryLexicalIndex())

        with pytest.raises(Zone2BackstopPortError, match="simulated backend outage"):
            eviction.delete_item(_TENANT, "item-1")


class TestCrossZoneDpdpErasureCascadeNoObligation:
    """CRITICAL-2: the real adapter's `False` (no obligation) path."""

    def test_no_pending_obligation_returns_false_and_touches_nothing(self) -> None:
        vector_index = InMemoryVectorIndex()
        lexical_index = InMemoryLexicalIndex()
        _index_item(vector_index, lexical_index, _TENANT, "unrelated-item")
        zone2 = RecordingZone2EvictionPort()
        cascade = CrossZoneDpdpErasureCascade(
            obligation_store=InMemoryErasureObligationStore(),
            zone2_eviction=zone2,
            retrieval_index_eviction=RetrievalIndexEviction(vector_index, lexical_index),
        )

        result = cascade.fulfil_pending_erasure(_TENANT, "unrelated-item")

        assert result is False
        assert zone2.evicted == []
        assert "unrelated-item" in vector_index.search(_TENANT, (1.0, 0.0, 0.0), top_k=10), (
            "an item with NO pending erasure obligation must be left "
            "completely untouched by the cascade"
        )


class TestCrossZoneDpdpErasureCascadeRealEndToEnd:
    """CRITICAL-2 core proof: AC-002-CAP-DPDP-1 is now genuinely verifiable.

    Every port here is a REAL implementation (`InMemoryVectorIndex`,
    `InMemoryLexicalIndex`, `InMemoryErasureObligationStore`) except the
    Zone-2 leg, which is the one leg this remediation's own docstring
    documents as depending on whatever concrete `Zone2EvictionPort` a
    future story wires (the append-only `episodic_schema.sql` trigger
    structurally blocks a same-transaction SQL DELETE -- see that file's
    and this module's docstrings) -- a recording double stands in for it
    here, which is sufficient to prove the CASCADE's own orchestration is
    real and correct.
    """

    def test_pending_obligation_is_fulfilled_across_zone2_and_zone6(self) -> None:
        vector_index = InMemoryVectorIndex()
        lexical_index = InMemoryLexicalIndex()
        _index_item(vector_index, lexical_index, _TENANT, "subject-item")
        obligations = InMemoryErasureObligationStore()
        obligations.request_erasure(_TENANT, "subject-item")
        zone2 = RecordingZone2EvictionPort()
        cascade = CrossZoneDpdpErasureCascade(
            obligation_store=obligations,
            zone2_eviction=zone2,
            retrieval_index_eviction=RetrievalIndexEviction(vector_index, lexical_index),
        )

        result = cascade.fulfil_pending_erasure(_TENANT, "subject-item")

        assert result is True
        assert zone2.evicted == [(_TENANT, "subject-item")]
        assert vector_index.search(_TENANT, (1.0, 0.0, 0.0), top_k=10) == [], (
            "Zone 6's vector surface must be genuinely empty after a real "
            "cascade run, not merely asserted via a mock call"
        )
        assert lexical_index.search(_TENANT, "erasable-payload-content", top_k=10) == []
        assert obligations.has_pending_erasure(_TENANT, "subject-item") is False

    def test_fulfilled_obligation_is_cleared_so_a_replay_is_a_no_op(self) -> None:
        vector_index = InMemoryVectorIndex()
        lexical_index = InMemoryLexicalIndex()
        _index_item(vector_index, lexical_index, _TENANT, "subject-item")
        obligations = InMemoryErasureObligationStore()
        obligations.request_erasure(_TENANT, "subject-item")
        zone2 = RecordingZone2EvictionPort()
        cascade = CrossZoneDpdpErasureCascade(
            obligation_store=obligations,
            zone2_eviction=zone2,
            retrieval_index_eviction=RetrievalIndexEviction(vector_index, lexical_index),
        )

        first = cascade.fulfil_pending_erasure(_TENANT, "subject-item")
        second = cascade.fulfil_pending_erasure(_TENANT, "subject-item")

        assert first is True
        assert second is False, (
            "a second call for the same item, after the obligation was "
            "already fulfilled, must report no pending obligation -- never "
            "re-run the cascade for an already-erased item"
        )
        assert zone2.evicted == [(_TENANT, "subject-item")], "zone2.evict must run exactly once"

    def test_zone2_failure_leaves_obligation_pending_for_a_retry(self) -> None:
        """Safer-failure-mode proof (module docstring): Zone 6 removed first,
        so a Zone-2 failure still leaves the obligation retryable, with Zone
        6 already clean (no live searchable PII surface) in the meantime.
        """
        vector_index = InMemoryVectorIndex()
        lexical_index = InMemoryLexicalIndex()
        _index_item(vector_index, lexical_index, _TENANT, "subject-item")
        obligations = InMemoryErasureObligationStore()
        obligations.request_erasure(_TENANT, "subject-item")
        zone2 = RecordingZone2EvictionPort(raise_for=frozenset({"subject-item"}))
        cascade = CrossZoneDpdpErasureCascade(
            obligation_store=obligations,
            zone2_eviction=zone2,
            retrieval_index_eviction=RetrievalIndexEviction(vector_index, lexical_index),
        )

        with pytest.raises(Zone2BackstopPortError):
            cascade.fulfil_pending_erasure(_TENANT, "subject-item")

        assert obligations.has_pending_erasure(_TENANT, "subject-item") is True, (
            "a failed cascade must leave the obligation pending -- never "
            "silently mark a failed erasure as fulfilled"
        )
        assert vector_index.search(_TENANT, (1.0, 0.0, 0.0), top_k=10) == [], (
            "Zone 6 must already be clean even though Zone 2 failed -- "
            "the safer-failure-mode ordering this cascade uses"
        )

    def test_zone6_failure_leaves_zone2_untouched_and_obligation_pending(self) -> None:
        class RaisingLexicalIndex:
            def upsert(self, doc: object) -> None: ...

            def search(self, *args: object, **kwargs: object) -> list[str]:
                return []

            def delete(self, tenant_id: str, item_id: str) -> None:
                raise RuntimeError("simulated lexical index outage")

        vector_index = InMemoryVectorIndex()
        _index_item(vector_index, InMemoryLexicalIndex(), _TENANT, "subject-item")
        obligations = InMemoryErasureObligationStore()
        obligations.request_erasure(_TENANT, "subject-item")
        zone2 = RecordingZone2EvictionPort()
        cascade = CrossZoneDpdpErasureCascade(
            obligation_store=obligations,
            zone2_eviction=zone2,
            retrieval_index_eviction=RetrievalIndexEviction(vector_index, RaisingLexicalIndex()),
        )

        with pytest.raises(Zone2BackstopPortError):
            cascade.fulfil_pending_erasure(_TENANT, "subject-item")

        assert zone2.evicted == [], (
            "Zone 6 failing must prevent the Zone-2 leg from running at all "
            "-- the cascade must not partially complete"
        )
        assert obligations.has_pending_erasure(_TENANT, "subject-item") is True

    def test_cascade_never_touches_another_tenants_zone6_data(self) -> None:
        vector_index = InMemoryVectorIndex()
        lexical_index = InMemoryLexicalIndex()
        _index_item(vector_index, lexical_index, _TENANT, "shared-item-id")
        _index_item(vector_index, lexical_index, _OTHER_TENANT, "shared-item-id")
        obligations = InMemoryErasureObligationStore()
        obligations.request_erasure(_TENANT, "shared-item-id")
        cascade = CrossZoneDpdpErasureCascade(
            obligation_store=obligations,
            zone2_eviction=RecordingZone2EvictionPort(),
            retrieval_index_eviction=RetrievalIndexEviction(vector_index, lexical_index),
        )

        cascade.fulfil_pending_erasure(_TENANT, "shared-item-id")

        assert "shared-item-id" in vector_index.search(_OTHER_TENANT, (1.0, 0.0, 0.0), top_k=10), (
            "tenant-2's own copy of the same item_id must survive "
            "tenant-1's erasure (ADR-007/ADR-013 tenant isolation)"
        )

    def test_rejects_blank_tenant_id(self) -> None:
        cascade = CrossZoneDpdpErasureCascade(
            obligation_store=InMemoryErasureObligationStore(),
            zone2_eviction=RecordingZone2EvictionPort(),
            retrieval_index_eviction=RetrievalIndexEviction(
                InMemoryVectorIndex(), InMemoryLexicalIndex()
            ),
        )
        with pytest.raises(ValueError, match="tenant_id"):
            cascade.fulfil_pending_erasure("   ", "item-1")

    def test_rejects_blank_item_id(self) -> None:
        cascade = CrossZoneDpdpErasureCascade(
            obligation_store=InMemoryErasureObligationStore(),
            zone2_eviction=RecordingZone2EvictionPort(),
            retrieval_index_eviction=RetrievalIndexEviction(
                InMemoryVectorIndex(), InMemoryLexicalIndex()
            ),
        )
        with pytest.raises(ValueError, match="item_id"):
            cascade.fulfil_pending_erasure(_TENANT, "   ")
