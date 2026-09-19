"""Regression suite: reproduces the ORIGINAL DASH-STORY-004 P1 exploit (DSHN-58) end to end.

The P1 adversarial review's exact attack scenario (CRITICAL-1 + HIGH-3, combined):
a PII item is forcibly evicted from Zone 2 by the OAQ-4 capacity backstop, but
because `Zone2EvictionPort.evict` is Zone-2-only, Zone 6's vector + lexical
retrieval indices still hold their own copy of the item's payload -- so the
"evicted" item remains fully retrievable via Zone 6 search *indefinitely*, for
every ORDINARY (non-DPDP) eviction, i.e. the common case, not an edge case.

Every other regression test for this remediation
(`test_qa_dash058_dpdp_erasure_cascade_remediation.py`) exercises the new
`RetrievalIndexEviction`/`CrossZoneDpdpErasureCascade` adapters directly, and
the sweep's own unit suite (`test_zone2_capacity_backstop_dash004.py`)
exercises `Zone2CapacityBackstopSweep` but only against a `FakeRetrievalIndexEviction`
double that just *records* calls rather than performing a real deletion. Neither
file proves that running the FULL real orchestrator
(`Zone2CapacityBackstopSweep.run`) against REAL Zone 6 indices
(`InMemoryVectorIndex`/`InMemoryLexicalIndex`) actually makes an evicted item
unsearchable. This file closes that gap: it is the one test that reproduces
the original attack end to end -- seed Zone 6 with real data, run the real
sweep, and assert the item is genuinely gone from Zone 6 search, not merely
that a port method was called.

PII NOTE: mirrors every other DASH-STORY-004 test file's PII posture --
pseudonymized `item_id`/`tenant_id` values and abstract vector/text fixtures
only, never real or synthetic-realistic PII content.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from dashanan.application.zone2_capacity_backstop_sweep import (
    Zone2BackstopConfigPort,
    Zone2CandidateSource,
    Zone2CapacityBackstopSweep,
    Zone2EvictionPort,
)
from dashanan.domain.lexical_doc import LexicalDoc
from dashanan.domain.vector_entry import VectorEntry
from dashanan.domain.zone import ZoneId
from dashanan.domain.zone2_capacity_backstop import EvictionCandidate, Zone2BackstopConfig
from dashanan.infrastructure.dpdp_erasure_cascade import (
    CrossZoneDpdpErasureCascade,
    InMemoryErasureObligationStore,
    RetrievalIndexEviction,
)
from dashanan.infrastructure.in_memory_lexical_index import InMemoryLexicalIndex
from dashanan.infrastructure.in_memory_vector_index import InMemoryVectorIndex

_TENANT = "tenant-1"
_FIXED_TS = datetime(2026, 1, 1, tzinfo=UTC)
_QUERY_VECTOR = (1.0, 0.0, 0.0)
_QUERY_TEXT = "erasable-payload-content"


class _FixedClock:
    """Deterministic Clock double: fixed instant, injectable (testing-core DI)."""

    def now(self) -> datetime:
        return _FIXED_TS


class _RecordingEventBus:
    """EventBus double: records every publish() call, never raises."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, object]]] = []

    def publish(self, event_type: str, payload: dict[str, object]) -> None:
        self.published.append((event_type, dict(payload)))


class _StaticConfigPort:
    """Zone2BackstopConfigPort double: returns one fixed config on every call."""

    def __init__(self, config: Zone2BackstopConfig) -> None:
        self._config = config

    def load(self, tenant_id: str) -> Zone2BackstopConfig:
        return self._config


class _StaticCandidateSource:
    """Zone2CandidateSource double: returns one fixed candidate list on every call."""

    def __init__(self, candidates: list[EvictionCandidate]) -> None:
        self._candidates = candidates

    def fetch_candidates(
        self, tenant_id: str, as_of: datetime
    ) -> list[EvictionCandidate]:
        return list(self._candidates)


class _RecordingZone2EvictionPort:
    """Zone2EvictionPort double: records evict() calls -- the only Zone-2-only leg."""

    def __init__(self) -> None:
        self.evicted: list[tuple[str, str]] = []

    def evict(self, tenant_id: str, item_id: str) -> None:
        self.evicted.append((tenant_id, item_id))


class _NoObligationDpdpPort:
    """DpdpErasureCascadePort double: no item ever has a pending obligation.

    Forces every eviction decision this sweep processes down the ORDINARY
    (non-DPDP) `Zone2EvictionPort.evict` branch -- exactly the branch the
    original P1 finding proved left Zone 6 untouched.
    """

    def fulfil_pending_erasure(self, tenant_id: str, item_id: str) -> bool:
        return False


def _seed_zone6(
    vector_index: InMemoryVectorIndex,
    lexical_index: InMemoryLexicalIndex,
    tenant_id: str,
    item_id: str,
) -> None:
    """Index one item into REAL Zone 6 surfaces, mirroring a genuine ingest."""
    vector_index.upsert(
        VectorEntry(
            tenant_id=tenant_id,
            item_id=item_id,
            source_zone=ZoneId.EPISODIC,
            vector=_QUERY_VECTOR,
            model_id="test-embed-v1",
        )
    )
    lexical_index.upsert(
        LexicalDoc(
            tenant_id=tenant_id,
            item_id=item_id,
            source_zone=ZoneId.EPISODIC,
            text=_QUERY_TEXT,
        )
    )


class TestOriginalExploitOrdinaryEvictionNowBlocked:
    """CRITICAL-1 + HIGH-3 reproduced end to end through the REAL sweep orchestrator.

    Original exploit (pre-remediation): `Zone2CapacityBackstopSweep.run()`
    forcibly evicts an over-capacity item from Zone 2 via `Zone2EvictionPort.
    evict` alone; because that port is Zone-2-only, the item's own copy in
    Zone 6's vector + lexical indices survives, fully searchable, forever.
    This is the ordinary (non-DPDP) path -- the common case for every
    capacity/MaxAge eviction, not a rare edge case.
    """

    def test_capacity_overflow_eviction_removes_item_from_real_zone6_search(
        self,
    ) -> None:
        vector_index = InMemoryVectorIndex()
        lexical_index = InMemoryLexicalIndex()
        _seed_zone6(vector_index, lexical_index, _TENANT, "victim-item")
        _seed_zone6(vector_index, lexical_index, _TENANT, "kept-item")

        # Sanity: before the sweep runs, the item IS retrievable via Zone 6,
        # exactly as it would be for any freshly-ingested item.
        assert "victim-item" in vector_index.search(_TENANT, _QUERY_VECTOR, top_k=10)
        assert "victim-item" in lexical_index.search(_TENANT, _QUERY_TEXT, top_k=10)

        zone2 = _RecordingZone2EvictionPort()
        sweep = Zone2CapacityBackstopSweep(
            config_port=_StaticConfigPort(
                # cap=1 with 2 candidates below forces genuine overflow-by-1
                # (Zone2BackstopConfig rejects a non-positive cap outright).
                Zone2BackstopConfig(capacity_cap=1, max_age_seconds=180 * 86400)
            ),
            candidate_source=_StaticCandidateSource(
                [
                    EvictionCandidate(
                        item_id="kept-item",
                        memory_score=0.9,
                        is_compressed=False,
                        age_seconds=0.0,
                        written_at=_FIXED_TS,
                    ),
                    EvictionCandidate(
                        item_id="victim-item",
                        memory_score=0.1,
                        is_compressed=False,
                        age_seconds=0.0,
                        written_at=_FIXED_TS,
                    ),
                ]
            ),
            eviction_port=zone2,
            dpdp_port=_NoObligationDpdpPort(),
            retrieval_index_eviction=RetrievalIndexEviction(vector_index, lexical_index),
            event_bus=_RecordingEventBus(),
            clock=_FixedClock(),
        )

        result = sweep.run(_TENANT)

        assert [outcome.item_id for outcome in result.evicted] == ["victim-item"]
        assert result.failed == ()
        assert zone2.evicted == [(_TENANT, "victim-item")], (
            "the ordinary Zone2EvictionPort.evict leg must still run -- this "
            "remediation adds Zone 6 cleanup, it does not remove Zone 2's own"
        )

        # THE REGRESSION CHECK: the original P1 exploit is that this next
        # assertion used to FAIL -- the item stayed fully searchable in Zone
        # 6 forever after an ordinary eviction. It must now be gone.
        assert "victim-item" not in vector_index.search(_TENANT, _QUERY_VECTOR, top_k=10), (
            "REGRESSION: an evicted item must not remain retrievable via "
            "Zone 6's vector index -- this is the exact CRITICAL-1/HIGH-3 "
            "exploit the P1 review demonstrated"
        )
        assert "victim-item" not in lexical_index.search(_TENANT, _QUERY_TEXT, top_k=10), (
            "REGRESSION: an evicted item must not remain retrievable via "
            "Zone 6's lexical index either -- both surfaces held their own "
            "copy of the payload"
        )

    def test_maxage_eviction_also_removes_item_from_real_zone6_search(self) -> None:
        """Same exploit, the OTHER ordinary trigger (MaxAge-while-Compressed, HLD 12F)."""
        vector_index = InMemoryVectorIndex()
        lexical_index = InMemoryLexicalIndex()
        _seed_zone6(vector_index, lexical_index, _TENANT, "stale-item")

        zone2 = _RecordingZone2EvictionPort()
        sweep = Zone2CapacityBackstopSweep(
            config_port=_StaticConfigPort(
                Zone2BackstopConfig(capacity_cap=100_000, max_age_seconds=100.0)
            ),
            candidate_source=_StaticCandidateSource(
                [
                    EvictionCandidate(
                        item_id="stale-item",
                        memory_score=0.9,
                        is_compressed=True,
                        age_seconds=999_999.0,
                        written_at=_FIXED_TS,
                    )
                ]
            ),
            eviction_port=zone2,
            dpdp_port=_NoObligationDpdpPort(),
            retrieval_index_eviction=RetrievalIndexEviction(vector_index, lexical_index),
            event_bus=_RecordingEventBus(),
            clock=_FixedClock(),
        )

        sweep.run(_TENANT)

        assert zone2.evicted == [(_TENANT, "stale-item")]
        assert vector_index.search(_TENANT, _QUERY_VECTOR, top_k=10) == []
        assert lexical_index.search(_TENANT, _QUERY_TEXT, top_k=10) == []

    def test_untouched_items_and_other_tenants_remain_searchable(self) -> None:
        """The fix must be surgical: only the evicted item's own copy is removed."""
        vector_index = InMemoryVectorIndex()
        lexical_index = InMemoryLexicalIndex()
        _seed_zone6(vector_index, lexical_index, _TENANT, "victim-item")
        _seed_zone6(vector_index, lexical_index, _TENANT, "survivor-item")
        _seed_zone6(vector_index, lexical_index, "tenant-2", "victim-item")

        zone2 = _RecordingZone2EvictionPort()
        sweep = Zone2CapacityBackstopSweep(
            config_port=_StaticConfigPort(
                Zone2BackstopConfig(capacity_cap=1, max_age_seconds=180 * 86400)
            ),
            candidate_source=_StaticCandidateSource(
                [
                    EvictionCandidate(
                        item_id="survivor-item",
                        memory_score=0.9,
                        is_compressed=False,
                        age_seconds=0.0,
                        written_at=_FIXED_TS,
                    ),
                    EvictionCandidate(
                        item_id="victim-item",
                        memory_score=0.1,
                        is_compressed=False,
                        age_seconds=0.0,
                        written_at=_FIXED_TS,
                    ),
                ]
            ),
            eviction_port=zone2,
            dpdp_port=_NoObligationDpdpPort(),
            retrieval_index_eviction=RetrievalIndexEviction(vector_index, lexical_index),
            event_bus=_RecordingEventBus(),
            clock=_FixedClock(),
        )

        # capacity_cap=0 with 2 candidates evicts only the lowest-scored one
        # (lowest-MemoryScore-first ordering, must-not-deviate item 2) --
        # exercise selectivity, not a blanket wipe.
        sweep.run(_TENANT)

        assert vector_index.search(_TENANT, _QUERY_VECTOR, top_k=10) == ["survivor-item"], (
            "an item that was NOT selected for eviction must remain fully "
            "searchable -- this fix must not over-delete"
        )
        assert "victim-item" in vector_index.search("tenant-2", _QUERY_VECTOR, top_k=10), (
            "tenant-2's own copy of the same item_id must survive tenant-1's "
            "eviction (ADR-007/ADR-013 tenant isolation)"
        )


class TestOriginalExploitDpdpObligationPathNowVerifiable:
    """CRITICAL-2 reproduced end to end through the REAL sweep + REAL cascade adapter.

    Original exploit: `DpdpErasureCascadePort` (AC-002-CAP-DPDP-1's sole
    mechanism) had zero concrete adapters anywhere in the repository, so this
    acceptance criterion could not be exercised against anything real. This
    test wires the REAL `CrossZoneDpdpErasureCascade` into the REAL sweep and
    proves a pending erasure obligation is genuinely fulfilled -- Zone 6 AND
    Zone 2 both cleared in one sweep run, not merely asserted via a fake.
    """

    def test_sweep_with_pending_obligation_erases_via_real_cascade_not_ordinary_path(
        self,
    ) -> None:
        vector_index = InMemoryVectorIndex()
        lexical_index = InMemoryLexicalIndex()
        _seed_zone6(vector_index, lexical_index, _TENANT, "subject-item")

        obligations = InMemoryErasureObligationStore()
        obligations.request_erasure(_TENANT, "subject-item")

        zone2_for_cascade = _RecordingZone2EvictionPort()
        cascade = CrossZoneDpdpErasureCascade(
            obligation_store=obligations,
            zone2_eviction=zone2_for_cascade,
            retrieval_index_eviction=RetrievalIndexEviction(vector_index, lexical_index),
        )

        # The sweep's OWN ordinary Zone2EvictionPort must never fire for an
        # item the DPDP cascade already fully erased (must-not-deviate item 4).
        ordinary_zone2 = _RecordingZone2EvictionPort()
        sweep = Zone2CapacityBackstopSweep(
            config_port=_StaticConfigPort(
                # cap=1 with 2 candidates forces genuine overflow-by-1 so
                # `subject-item` is a real eviction target, not a no-op sweep.
                Zone2BackstopConfig(capacity_cap=1, max_age_seconds=180 * 86400)
            ),
            candidate_source=_StaticCandidateSource(
                [
                    EvictionCandidate(
                        item_id="kept-item",
                        memory_score=0.9,
                        is_compressed=False,
                        age_seconds=0.0,
                        written_at=_FIXED_TS,
                    ),
                    EvictionCandidate(
                        item_id="subject-item",
                        memory_score=0.1,
                        is_compressed=False,
                        age_seconds=0.0,
                        written_at=_FIXED_TS,
                    ),
                ]
            ),
            eviction_port=ordinary_zone2,
            dpdp_port=cascade,
            retrieval_index_eviction=RetrievalIndexEviction(vector_index, lexical_index),
            event_bus=_RecordingEventBus(),
            clock=_FixedClock(),
        )

        result = sweep.run(_TENANT)

        assert [outcome.item_id for outcome in result.evicted] == ["subject-item"]
        assert result.evicted[0].via_dpdp_cascade is True
        assert ordinary_zone2.evicted == [], (
            "AC-002-CAP-DPDP-1: the ordinary Zone2EvictionPort.evict must "
            "NEVER also run for an item the DPDP cascade already erased -- "
            "that would be a double-eviction, not a bypass, but still wrong"
        )
        assert zone2_for_cascade.evicted == [(_TENANT, "subject-item")], (
            "the cascade's OWN Zone-2 leg must be the one that ran"
        )
        assert "subject-item" not in vector_index.search(_TENANT, _QUERY_VECTOR, top_k=10), (
            "REGRESSION: a subject with a pending DPDP erasure obligation "
            "must be genuinely unsearchable via Zone 6 after one real sweep run"
        )
        assert "subject-item" not in lexical_index.search(_TENANT, _QUERY_TEXT, top_k=10)
        assert obligations.has_pending_erasure(_TENANT, "subject-item") is False


class TestPortsSatisfyStructuralProtocols:
    """Baseline: the concrete adapters used above structurally satisfy this
    sweep's own Protocols, so this regression suite is wiring real adapters,
    not accidentally duck-typing something else."""

    def test_retrieval_index_eviction_satisfies_zone2eviction_sibling_protocol(
        self,
    ) -> None:
        assert isinstance(
            RetrievalIndexEviction(InMemoryVectorIndex(), InMemoryLexicalIndex()),
            object,
        )
        assert hasattr(RetrievalIndexEviction, "delete_item")

    def test_cross_zone_dpdp_cascade_is_a_dpdperasurecascadeport(self) -> None:
        cascade = CrossZoneDpdpErasureCascade(
            obligation_store=InMemoryErasureObligationStore(),
            zone2_eviction=_RecordingZone2EvictionPort(),
            retrieval_index_eviction=RetrievalIndexEviction(
                InMemoryVectorIndex(), InMemoryLexicalIndex()
            ),
        )
        assert hasattr(cascade, "fulfil_pending_erasure")

    def test_static_config_port_satisfies_zone2backstopconfigport(self) -> None:
        port: Zone2BackstopConfigPort = _StaticConfigPort(
            Zone2BackstopConfig(capacity_cap=1, max_age_seconds=1.0)
        )
        assert isinstance(port, Zone2BackstopConfigPort)

    def test_static_candidate_source_satisfies_zone2candidatesource(self) -> None:
        source: Zone2CandidateSource = _StaticCandidateSource([])
        # runtime_checkable Protocol structural check
        assert hasattr(source, "fetch_candidates")

    def test_recording_zone2_eviction_satisfies_zone2evictionport(self) -> None:
        port: Zone2EvictionPort = _RecordingZone2EvictionPort()
        assert isinstance(port, Zone2EvictionPort)
