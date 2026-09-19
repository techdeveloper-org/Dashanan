"""Adversarial fuzz/edge-case re-audit of the cross-zone integration fixture
(DSHN-59 composition report), written and executed against the REAL
`wired_system` fixture -- never a re-derivation from the fixture's own
docstrings. Every test below is new: none duplicates an assertion already
made in `test_cross_zone_scenarios.py`.

Scope (per the dispatch): malformed/boundary `ProvenanceWriteGate` writes,
concurrent writes under varying key/tenant combinations, a zone adapter
raising mid-`assemble_context`, and cross-tenant probing against the fully
assembled multi-zone system.
"""

from __future__ import annotations

import hashlib
import threading
from datetime import UTC, datetime, timedelta

import pytest

from dashanan.application.context_assembly_request import ContextAssemblyRequest
from dashanan.domain.exceptions import ZoneRepositoryError
from dashanan.domain.ports import ZoneQuery
from dashanan.domain.write_gate import (
    UserTurnAttestation,
    WriteAccepted,
    WriteRejected,
    WriteRequest,
    sign_user_turn_attestation,
)
from dashanan.domain.zone import ZoneId

from .conftest import (
    DEFAULT_TENANT_ID,
    PLACEHOLDER_PAYLOAD,
    WiredSystem,
    _TEST_ONLY_USER_TURN_SIGNING_KEY,
)


def _hash(seed: bytes) -> str:
    return hashlib.sha256(seed).hexdigest()


# ---------------------------------------------------------------------------
# A. Malformed / boundary write requests through the real ProvenanceWriteGate
# ---------------------------------------------------------------------------


class TestMalformedWriteRequests:
    def test_non_string_source_type_raw_is_rejected_not_crashed(
        self, wired_system: WiredSystem
    ) -> None:
        """A caller that ignores the `str` type hint (e.g. sends an int over
        the wire, coerced by a lax JSON decoder) must be rejected 422, not
        crash the gate with a TypeError/AttributeError."""
        ws = wired_system
        request = WriteRequest(
            tenant_id=DEFAULT_TENANT_ID,
            item_id="adv-item-1",
            source_zone=ZoneId.WORKING,
            source_type_raw=12345,  # type: ignore[arg-type]
            caller_identity="adv-caller",
            retrieval_context_hash=_hash(b"adv-1"),
            idempotency_key="adv-key-1",
        )
        result = ws.write_gate.submit_write(request, lambda: None)
        assert isinstance(result, WriteRejected)
        assert result.http_status == 422
        assert result.error_code == "unresolvable_source_type"

    def test_whitespace_only_source_type_raw_is_rejected(
        self, wired_system: WiredSystem
    ) -> None:
        ws = wired_system
        request = WriteRequest(
            tenant_id=DEFAULT_TENANT_ID,
            item_id="adv-item-2",
            source_zone=ZoneId.WORKING,
            source_type_raw="   ",
            caller_identity="adv-caller",
            retrieval_context_hash=_hash(b"adv-2"),
            idempotency_key="adv-key-2",
        )
        result = ws.write_gate.submit_write(request, lambda: None)
        assert isinstance(result, WriteRejected)
        assert result.error_code == "unresolvable_source_type"

    @pytest.mark.parametrize(
        "bad_hash",
        [
            "a" * 63,  # one char short
            "a" * 65,  # one char long
            "A" * 64,  # uppercase hex -- is_hex_sha256 requires lowercase
            "g" * 64,  # non-hex character
            "",  # blank
        ],
    )
    def test_malformed_retrieval_context_hash_is_rejected(
        self, wired_system: WiredSystem, bad_hash: str
    ) -> None:
        ws = wired_system
        request = WriteRequest(
            tenant_id=DEFAULT_TENANT_ID,
            item_id="adv-item-3",
            source_zone=ZoneId.WORKING,
            source_type_raw="tool_output",
            caller_identity="adv-caller",
            retrieval_context_hash=bad_hash,
            idempotency_key=f"adv-key-3-{bad_hash!r}",
        )
        result = ws.write_gate.submit_write(request, lambda: None)
        assert isinstance(result, WriteRejected), (
            f"malformed hash {bad_hash!r} must be rejected, got {result!r}"
        )
        assert result.error_code == "missing_caller_binding"

    def test_whitespace_only_caller_identity_is_rejected(
        self, wired_system: WiredSystem
    ) -> None:
        ws = wired_system
        request = WriteRequest(
            tenant_id=DEFAULT_TENANT_ID,
            item_id="adv-item-4",
            source_zone=ZoneId.WORKING,
            source_type_raw="tool_output",
            caller_identity="   ",
            retrieval_context_hash=_hash(b"adv-4"),
            idempotency_key="adv-key-4",
        )
        result = ws.write_gate.submit_write(request, lambda: None)
        assert isinstance(result, WriteRejected)
        assert result.error_code == "missing_caller_binding"

    def test_user_stated_marker_true_no_attestation_is_rejected_forged(
        self, wired_system: WiredSystem
    ) -> None:
        """Attempted forgery: a caller sets user_turn_marker=True with no
        attestation at all -- the exact HLD Threat S-2 forgery this gate
        exists to close."""
        ws = wired_system
        request = WriteRequest(
            tenant_id=DEFAULT_TENANT_ID,
            item_id="adv-item-5",
            source_zone=ZoneId.WORKING,
            source_type_raw="user_stated",
            caller_identity="adv-caller",
            retrieval_context_hash=_hash(b"adv-5"),
            idempotency_key="adv-key-5",
            user_turn_marker=True,
            user_turn_attestation=None,
        )
        result = ws.write_gate.submit_write(request, lambda: None)
        assert isinstance(result, WriteRejected)
        # marker=True with attestation=None fails the attestation-
        # verification branch (verify_user_turn_attestation(None, ...) is
        # False), not the bare "marker is falsy" branch -- both are
        # rejections, but under the "forged" error code.
        assert result.error_code == "forged_user_turn_marker"

    def test_user_stated_marker_false_with_source_type_user_stated_is_rejected_missing(
        self, wired_system: WiredSystem
    ) -> None:
        ws = wired_system
        request = WriteRequest(
            tenant_id=DEFAULT_TENANT_ID,
            item_id="adv-item-5b",
            source_zone=ZoneId.WORKING,
            source_type_raw="user_stated",
            caller_identity="adv-caller",
            retrieval_context_hash=_hash(b"adv-5b"),
            idempotency_key="adv-key-5b",
            user_turn_marker=False,
        )
        result = ws.write_gate.submit_write(request, lambda: None)
        assert isinstance(result, WriteRejected)
        assert result.error_code == "missing_user_turn_marker"

    def test_user_stated_attestation_signed_with_wrong_secret_is_rejected(
        self, wired_system: WiredSystem
    ) -> None:
        """Attempted forgery: an attacker who does not hold the host's real
        signing key still produces a syntactically-valid attestation with a
        secret they control. Must be rejected -- the HMAC will not verify."""
        ws = wired_system
        attacker_secret = b"attacker-controlled-key-not-the-real-one"
        forged = sign_user_turn_attestation(
            secret=attacker_secret,
            tenant_id=DEFAULT_TENANT_ID,
            item_id="adv-item-6",
            caller_identity="adv-caller",
            retrieval_context_hash=_hash(b"adv-6"),
            issued_at=ws.clock.now(),
        )
        request = WriteRequest(
            tenant_id=DEFAULT_TENANT_ID,
            item_id="adv-item-6",
            source_zone=ZoneId.WORKING,
            source_type_raw="user_stated",
            caller_identity="adv-caller",
            retrieval_context_hash=_hash(b"adv-6"),
            idempotency_key="adv-key-6",
            user_turn_marker=True,
            user_turn_attestation=forged,
        )
        result = ws.write_gate.submit_write(request, lambda: None)
        assert isinstance(result, WriteRejected)
        assert result.error_code == "forged_user_turn_marker"

    def test_user_stated_attestation_replayed_for_different_item_is_rejected(
        self, wired_system: WiredSystem
    ) -> None:
        """A genuine attestation for item A, replayed unmodified against a
        WriteRequest for item B -- the canonical payload binds item_id, so
        this must fail verification even with the REAL signing key."""
        ws = wired_system
        genuine_for_item_a = sign_user_turn_attestation(
            secret=_TEST_ONLY_USER_TURN_SIGNING_KEY,
            tenant_id=DEFAULT_TENANT_ID,
            item_id="adv-item-7a",
            caller_identity="adv-caller",
            retrieval_context_hash=_hash(b"adv-7"),
            issued_at=ws.clock.now(),
        )
        request_for_item_b = WriteRequest(
            tenant_id=DEFAULT_TENANT_ID,
            item_id="adv-item-7b",  # different item than the attestation covers
            source_zone=ZoneId.WORKING,
            source_type_raw="user_stated",
            caller_identity="adv-caller",
            retrieval_context_hash=_hash(b"adv-7"),
            idempotency_key="adv-key-7",
            user_turn_marker=True,
            user_turn_attestation=genuine_for_item_a,
        )
        result = ws.write_gate.submit_write(request_for_item_b, lambda: None)
        assert isinstance(result, WriteRejected)
        assert result.error_code == "forged_user_turn_marker"

    def test_user_stated_attestation_older_than_max_age_is_rejected(
        self, wired_system: WiredSystem
    ) -> None:
        """A genuine attestation, correctly bound, but captured and replayed
        301 seconds later (one past the 300s freshness window) must be
        rejected -- otherwise a captured attestation is a standing forgery
        capability with no expiry."""
        ws = wired_system
        issued_at = ws.clock.now()
        genuine = sign_user_turn_attestation(
            secret=_TEST_ONLY_USER_TURN_SIGNING_KEY,
            tenant_id=DEFAULT_TENANT_ID,
            item_id="adv-item-8",
            caller_identity="adv-caller",
            retrieval_context_hash=_hash(b"adv-8"),
            issued_at=issued_at,
        )
        ws.clock.advance(301.0)
        request = WriteRequest(
            tenant_id=DEFAULT_TENANT_ID,
            item_id="adv-item-8",
            source_zone=ZoneId.WORKING,
            source_type_raw="user_stated",
            caller_identity="adv-caller",
            retrieval_context_hash=_hash(b"adv-8"),
            idempotency_key="adv-key-8",
            user_turn_marker=True,
            user_turn_attestation=genuine,
        )
        result = ws.write_gate.submit_write(request, lambda: None)
        assert isinstance(result, WriteRejected)
        assert result.error_code == "forged_user_turn_marker"

    def test_user_stated_attestation_dated_in_the_future_is_rejected(
        self, wired_system: WiredSystem
    ) -> None:
        ws = wired_system
        future_issued_at = ws.clock.now() + timedelta(seconds=60)
        forged_future = sign_user_turn_attestation(
            secret=_TEST_ONLY_USER_TURN_SIGNING_KEY,
            tenant_id=DEFAULT_TENANT_ID,
            item_id="adv-item-9",
            caller_identity="adv-caller",
            retrieval_context_hash=_hash(b"adv-9"),
            issued_at=future_issued_at,
        )
        request = WriteRequest(
            tenant_id=DEFAULT_TENANT_ID,
            item_id="adv-item-9",
            source_zone=ZoneId.WORKING,
            source_type_raw="user_stated",
            caller_identity="adv-caller",
            retrieval_context_hash=_hash(b"adv-9"),
            idempotency_key="adv-key-9",
            user_turn_marker=True,
            user_turn_attestation=forged_future,
        )
        result = ws.write_gate.submit_write(request, lambda: None)
        assert isinstance(result, WriteRejected)
        assert result.error_code == "forged_user_turn_marker"

    def test_genuine_attestation_within_window_is_accepted_positive_control(
        self, wired_system: WiredSystem
    ) -> None:
        """Positive control: proves the seven forgery-rejection tests above
        are actually exercising the verification path, not a gate that
        rejects unconditionally."""
        ws = wired_system
        genuine = sign_user_turn_attestation(
            secret=_TEST_ONLY_USER_TURN_SIGNING_KEY,
            tenant_id=DEFAULT_TENANT_ID,
            item_id="adv-item-10",
            caller_identity="adv-caller",
            retrieval_context_hash=_hash(b"adv-10"),
            issued_at=ws.clock.now(),
        )
        request = WriteRequest(
            tenant_id=DEFAULT_TENANT_ID,
            item_id="adv-item-10",
            source_zone=ZoneId.WORKING,
            source_type_raw="user_stated",
            caller_identity="adv-caller",
            retrieval_context_hash=_hash(b"adv-10"),
            idempotency_key="adv-key-10",
            user_turn_marker=True,
            user_turn_attestation=genuine,
        )
        result = ws.write_gate.submit_write(request, lambda: None)
        assert isinstance(result, WriteAccepted), result


# ---------------------------------------------------------------------------
# B. Concurrency: distinct keys, cross-tenant key reuse, reentrant replay
# ---------------------------------------------------------------------------


class TestWriteGateConcurrencyBeyondScenario6:
    def test_concurrent_writes_under_distinct_idempotency_keys_are_independent(
        self, wired_system: WiredSystem
    ) -> None:
        """The class docstring's own claim: 'Locks for different keys are
        independent, so concurrent writes under different idempotency keys
        are never serialized against each other.' Prove every thread gets
        its OWN write_id (no accidental cross-key collapsing) and every
        journal entry lands."""
        ws = wired_system
        thread_count = 12
        results: list[object] = [None] * thread_count
        barrier = threading.Barrier(thread_count)

        def run(index: int) -> None:
            request = WriteRequest(
                tenant_id=DEFAULT_TENANT_ID,
                item_id=f"adv-distinct-item-{index}",
                source_zone=ZoneId.WORKING,
                source_type_raw="tool_output",
                caller_identity="adv-caller",
                retrieval_context_hash=_hash(f"adv-distinct-{index}".encode()),
                idempotency_key=f"adv-distinct-key-{index}",
            )
            barrier.wait(timeout=5)
            results[index] = ws.write_gate.submit_write(request, lambda: None)

        threads = [threading.Thread(target=run, args=(i,)) for i in range(thread_count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert all(isinstance(r, WriteAccepted) for r in results), results
        write_ids = {r.write_id for r in results}
        assert len(write_ids) == thread_count, (
            "every distinct idempotency_key must mint its own write_id -- "
            f"got {len(write_ids)} distinct ids for {thread_count} threads"
        )
        assert len(ws.provenance_journal.appended) == thread_count

    def test_concurrent_writes_reusing_the_same_key_string_across_tenants_do_not_collide(
        self, wired_system: WiredSystem
    ) -> None:
        """Cross-tenant probe on the write path: two tenants race to submit
        a write using the IDENTICAL idempotency_key literal at the same
        instant. The lock and the journal are both keyed by
        (tenant_id, idempotency_key), so this must behave exactly like two
        unrelated keys -- neither tenant's write may be silently treated as
        a replay of the other's, and neither may block the other's result
        on the other's cached write_id."""
        ws = wired_system
        shared_key = "adv-shared-key-across-tenants"
        tenants = ["adv-tenant-A", "adv-tenant-B"]
        results: dict[str, object] = {}
        barrier = threading.Barrier(len(tenants))

        def run(tenant_id: str) -> None:
            request = WriteRequest(
                tenant_id=tenant_id,
                item_id=f"adv-item-for-{tenant_id}",
                source_zone=ZoneId.WORKING,
                source_type_raw="tool_output",
                caller_identity="adv-caller",
                retrieval_context_hash=_hash(f"adv-{tenant_id}".encode()),
                idempotency_key=shared_key,
            )
            barrier.wait(timeout=5)
            results[tenant_id] = ws.write_gate.submit_write(request, lambda: None)

        threads = [threading.Thread(target=run, args=(t,)) for t in tenants]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        for tenant_id in tenants:
            assert isinstance(results[tenant_id], WriteAccepted), results

        write_ids = {results[t].write_id for t in tenants}
        assert len(write_ids) == 2, (
            "two different tenants sharing the same idempotency_key literal "
            f"must each mint their OWN write_id -- got {write_ids}"
        )
        for tenant_id in tenants:
            entry = ws.provenance_journal.find_by_idempotency_key(tenant_id, shared_key)
            assert entry is not None
            assert entry.item_id == f"adv-item-for-{tenant_id}"
            assert entry.tenant_id == tenant_id

    def test_reentrant_persist_fact_same_key_returns_cached_result_no_deadlock(
        self, wired_system: WiredSystem
    ) -> None:
        """Exercises the class docstring's own documented RLock rationale:
        'a caller whose own persist_fact callback re-enters submit_write on
        the same thread for the same key ... does not deadlock against
        itself.' By the time persist_fact runs, the journal append has
        already happened, so the reentrant call is a genuine verbatim
        replay and must return the SAME write_id, not deadlock and not
        mint a second one."""
        ws = wired_system
        item_id = "adv-reentrant-item"
        key = "adv-reentrant-key"
        retrieval_context_hash = _hash(b"adv-reentrant")
        outcomes: list[object] = []

        def make_request() -> WriteRequest:
            return WriteRequest(
                tenant_id=DEFAULT_TENANT_ID,
                item_id=item_id,
                source_zone=ZoneId.WORKING,
                source_type_raw="tool_output",
                caller_identity="adv-caller",
                retrieval_context_hash=retrieval_context_hash,
                idempotency_key=key,
            )

        def outer_persist_fact() -> None:
            inner_result = ws.write_gate.submit_write(make_request(), lambda: None)
            outcomes.append(inner_result)

        outer_result = ws.write_gate.submit_write(make_request(), outer_persist_fact)

        assert isinstance(outer_result, WriteAccepted)
        assert len(outcomes) == 1
        assert isinstance(outcomes[0], WriteAccepted)
        assert outcomes[0].write_id == outer_result.write_id, (
            "reentrant call on the same key must return the SAME cached "
            "write_id as a verbatim replay, not mint a second write_id"
        )
        assert len(ws.provenance_journal.appended) == 1

    def test_concurrent_gated_writes_to_the_same_item_id_via_different_keys_do_not_corrupt_zone1(
        self, wired_system: WiredSystem
    ) -> None:
        """Direct probe of the SAME item_id under concurrent load -- as
        distinct from Scenario 6, which races the SAME idempotency_key.
        Here each thread uses its OWN idempotency_key (a legitimate
        pattern: N independent logical writes that all happen to target
        the same item_id, e.g. rapid corrections), so `ProvenanceWriteGate`
        itself does not serialize them against each other at all (module
        docstring: locks are per-key). Each accepted write's `persist_fact`
        then calls the REAL `WorkingMemoryLRURepository.put` for the SAME
        `(tenant_id, session_id, item_id)`. `working_memory_lru_repository
        .py`'s own class docstring self-documents this as an unmitigated
        gap ("ADR-018 ... is not implemented here"): confirm whether that
        gap is latent-but-harmless or produces an actual corrupted/crashed
        outcome under real concurrent load through the real, wired gate."""
        ws = wired_system
        item_id = "adv-same-item-race"
        session_id = "adv-same-item-race-session"
        thread_count = 16
        results: list[object] = [None] * thread_count
        errors: list[BaseException] = []
        errors_guard = threading.Lock()
        barrier = threading.Barrier(thread_count)

        def persist_fact_for(index: int):
            def _persist() -> None:
                ws.working_memory_repository.put(
                    tenant_id=DEFAULT_TENANT_ID,
                    session_id=session_id,
                    item_id=item_id,
                    payload=f"{PLACEHOLDER_PAYLOAD}-{index}",
                    token_count=5,
                )

            return _persist

        def run(index: int) -> None:
            request = WriteRequest(
                tenant_id=DEFAULT_TENANT_ID,
                item_id=item_id,
                source_zone=ZoneId.WORKING,
                source_type_raw="tool_output",
                caller_identity="adv-caller",
                retrieval_context_hash=_hash(f"adv-same-item-{index}".encode()),
                idempotency_key=f"adv-same-item-key-{index}",
            )
            barrier.wait(timeout=5)
            try:
                results[index] = ws.write_gate.submit_write(
                    request, persist_fact_for(index)
                )
            except BaseException as exc:  # noqa: BLE001 -- adversarial probe must
                # capture EVERY exception type a real corrupted-dict access
                # could raise (RuntimeError on size-changed-during-iteration,
                # KeyError, etc.), not just the ones anticipated in advance.
                with errors_guard:
                    errors.append(exc)

        threads = [threading.Thread(target=run, args=(i,)) for i in range(thread_count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert errors == [], (
            "concurrent gated writes to the SAME item_id (distinct "
            "idempotency_keys, so the write gate does not serialize them) "
            f"raised {len(errors)} unhandled exception(s) inside "
            f"WorkingMemoryLRURepository.put: {errors!r} -- this is the "
            "concurrency gap the adapter's own class docstring flags as "
            "unmitigated (ADR-018 not implemented), reproduced live"
        )
        assert all(isinstance(r, WriteAccepted) for r in results), results
        final = ws.working_memory_repository.get(DEFAULT_TENANT_ID, session_id, item_id)
        assert final is not None, (
            "the item must still be readable after the race, whichever "
            "thread's write ended up as the final state"
        )


# ---------------------------------------------------------------------------
# C. A zone adapter raising mid-assembly (partial-failure handling)
# ---------------------------------------------------------------------------


class _AlwaysZoneRepositoryError:
    """A real, non-mock ZoneRepository that always raises ZoneRepositoryError."""

    def fetch(self, query: ZoneQuery) -> list:
        raise ZoneRepositoryError(zone="adv-poison-zone", reason="adversarial probe")


class _AlwaysGenericException:
    """A real, non-mock ZoneRepository that raises an UNDOCUMENTED exception
    type -- not ZoneRepositoryError -- to test the documented "never a bare
    Exception" catch boundary in MemoryOrchestrator._fetch_zone."""

    def fetch(self, query: ZoneQuery) -> list:
        raise RuntimeError("adversarial: a bug in a zone adapter, not a declared failure")


class TestPartialZoneFailureIsolation:
    def test_one_zone_raising_zonerepositoryerror_does_not_lose_other_real_zones_items(
        self, wired_system: WiredSystem
    ) -> None:
        """Register a genuinely failing zone ALONGSIDE the two already-real,
        working zones (Working, RetrievalIndex) on the SAME orchestrator
        instance, and confirm the failure of one zone neither corrupts nor
        suppresses the other zones' real items -- true partial-failure
        isolation, not just "the failed zone is reported"."""
        ws = wired_system
        session_id = "adv-partial-failure-session"
        ws.working_memory_repository.put(
            tenant_id=DEFAULT_TENANT_ID,
            session_id=session_id,
            item_id="adv-partial-working-item",
            payload=PLACEHOLDER_PAYLOAD,
            token_count=5,
        )
        ws.memory_orchestrator._zone_repositories[ZoneId.SEMANTIC] = (
            _AlwaysZoneRepositoryError()
        )
        try:
            result = ws.memory_orchestrator.assemble_context(
                ContextAssemblyRequest(
                    tenant_id=DEFAULT_TENANT_ID,
                    session_id=session_id,
                    task="adversarial partial failure probe",
                    token_budget=1000,
                    zones=[ZoneId.WORKING, ZoneId.SEMANTIC],
                )
            )
        finally:
            del ws.memory_orchestrator._zone_repositories[ZoneId.SEMANTIC]

        assert result.degraded is True
        assert result.zones_unavailable == [ZoneId.SEMANTIC]
        item_ids = {item.item_id for item in result.items}
        assert "adv-partial-working-item" in item_ids, (
            "a real, working zone's items must survive a SIBLING zone's "
            "failure on the same assembly call"
        )

    def test_undeclared_exception_from_a_zone_adapter_propagates_not_silently_swallowed(
        self, wired_system: WiredSystem
    ) -> None:
        """Adversarial probe on the documented AC-009-SUPP-1 contract:
        MemoryOrchestrator._fetch_zone catches ZoneRepositoryError
        SPECIFICALLY (error-handling-patterns: never a bare `except
        Exception`). Confirm a genuine bug in a zone adapter (an
        undeclared RuntimeError, e.g. a null-pointer-equivalent bug) is
        NOT silently converted into a degraded-but-successful assembly --
        it must propagate, so a real adapter bug fails loudly instead of
        being reported to the host as 'zone unavailable'."""
        ws = wired_system
        ws.memory_orchestrator._zone_repositories[ZoneId.SEMANTIC] = (
            _AlwaysGenericException()
        )
        try:
            with pytest.raises(RuntimeError, match="adversarial: a bug"):
                ws.memory_orchestrator.assemble_context(
                    ContextAssemblyRequest(
                        tenant_id=DEFAULT_TENANT_ID,
                        session_id="adv-undeclared-exception-session",
                        task="adversarial undeclared exception probe",
                        token_budget=1000,
                        zones=[ZoneId.WORKING, ZoneId.SEMANTIC],
                    )
                )
        finally:
            del ws.memory_orchestrator._zone_repositories[ZoneId.SEMANTIC]

    def test_duplicate_zone_in_request_does_not_double_count_the_same_items(
        self, wired_system: WiredSystem
    ) -> None:
        """Fuzz input: `ContextAssemblyRequest.zones` is caller-supplied and
        `__post_init__` does not deduplicate it. Requesting the SAME zone
        twice must not silently double-fetch and double-add the same
        underlying items into the assembled result (would corrupt
        token-budget accounting and hand the host duplicate items)."""
        ws = wired_system
        session_id = "adv-duplicate-zone-session"
        ws.working_memory_repository.put(
            tenant_id=DEFAULT_TENANT_ID,
            session_id=session_id,
            item_id="adv-dup-zone-item",
            payload=PLACEHOLDER_PAYLOAD,
            token_count=5,
        )

        result = ws.memory_orchestrator.assemble_context(
            ContextAssemblyRequest(
                tenant_id=DEFAULT_TENANT_ID,
                session_id=session_id,
                task="adversarial duplicate zone probe",
                token_budget=1000,
                zones=[ZoneId.WORKING, ZoneId.WORKING],
            )
        )

        matching = [item for item in result.items if item.item_id == "adv-dup-zone-item"]
        assert len(matching) == 1, (
            "a duplicate zone entry in the request must not cause the same "
            f"item to appear twice in the assembled result -- got {len(matching)} copies"
        )


# ---------------------------------------------------------------------------
# D. Cross-tenant probing against the fully-assembled multi-zone system
# ---------------------------------------------------------------------------


class TestCrossTenantProbingFullMultiZoneSystem:
    def test_sql_injection_style_tenant_id_is_bound_as_a_parameter_not_concatenated(
        self, wired_system: WiredSystem
    ) -> None:
        """Cross-tenant probe against the real SQL adapter (Zone 2): a
        `tenant_id` value crafted to look like a SQL injection payload must
        reach the database ONLY as a bound parameter -- never concatenated
        into the executed SQL text, which would otherwise let a malicious
        or buggy upstream caller widen the WHERE clause beyond that single
        tenant."""
        ws = wired_system
        malicious_tenant_id = "tenant-A' OR '1'='1"
        ws.episodic_connection.cursor_obj.executed.clear()

        result = ws.memory_orchestrator.assemble_context(
            ContextAssemblyRequest(
                tenant_id=malicious_tenant_id,
                session_id="adv-sqli-session",
                task="adversarial sqli probe",
                token_budget=1000,
                zones=[ZoneId.EPISODIC],
            )
        )

        assert result.degraded is False
        executed_sql, executed_params = ws.episodic_connection.cursor_obj.executed[-1]
        assert "OR '1'='1'" not in executed_sql.upper().replace(" ", "")
        assert malicious_tenant_id not in executed_sql, (
            "the tenant_id value must never be concatenated into the SQL "
            f"text itself -- found it embedded in: {executed_sql!r}"
        )
        assert executed_params[0] == malicious_tenant_id, (
            "the tenant_id value must still be passed as the bound "
            "parameter, exactly as given, not silently dropped or altered"
        )

    def test_two_tenants_simultaneously_registered_working_and_retrieval_zones_never_cross_leak_under_concurrent_reads(
        self, wired_system: WiredSystem
    ) -> None:
        """Beyond Scenario 5's sequential per-tenant reads: race BOTH
        tenants' `assemble_context` calls against the SAME orchestrator
        instance and the SAME underlying Zone 1 / Zone 6 stores
        CONCURRENTLY, to probe for a read-time cross-tenant leak that only
        a sequential test would miss (e.g. shared mutable iteration state)."""
        ws = wired_system
        tenant_a, tenant_b = "adv-race-tenant-A", "adv-race-tenant-B"

        ws.working_memory_repository.put(
            tenant_id=tenant_a,
            session_id="adv-race-session-a",
            item_id="adv-race-working-a",
            payload=PLACEHOLDER_PAYLOAD,
            token_count=5,
        )
        ws.working_memory_repository.put(
            tenant_id=tenant_b,
            session_id="adv-race-session-b",
            item_id="adv-race-working-b",
            payload=PLACEHOLDER_PAYLOAD,
            token_count=5,
        )
        ws.retrieval_index_repository.index_item(
            tenant_id=tenant_a,
            item_id="adv-race-index-a",
            source_zone=ZoneId.EPISODIC,
            text="race probe shared terms",
            vector=(1.0, 0.0),
            model_id="test-model",
        )
        ws.retrieval_index_repository.index_item(
            tenant_id=tenant_b,
            item_id="adv-race-index-b",
            source_zone=ZoneId.EPISODIC,
            text="race probe shared terms",
            vector=(1.0, 0.0),
            model_id="test-model",
        )

        results: dict[str, object] = {}
        barrier = threading.Barrier(2)

        def run(tenant_id: str) -> None:
            barrier.wait(timeout=5)
            for _ in range(50):
                results[tenant_id] = ws.memory_orchestrator.assemble_context(
                    ContextAssemblyRequest(
                        tenant_id=tenant_id,
                        session_id="adv-race-check-session",
                        task="race probe shared terms",
                        token_budget=1000,
                        query_embedding=[1.0, 0.0],
                        zones=[ZoneId.WORKING, ZoneId.RETRIEVAL_INDEX],
                    )
                )
                own_ids = {"adv-race-working-" + tenant_id[-1].lower(), "adv-race-index-" + tenant_id[-1].lower()}
                other_ids = ({"adv-race-working-a", "adv-race-index-a", "adv-race-working-b", "adv-race-index-b"} - own_ids)
                item_ids = {item.item_id for item in results[tenant_id].items}
                assert not (other_ids & item_ids), (
                    f"tenant {tenant_id!r} observed another tenant's items "
                    f"under concurrent load: {other_ids & item_ids}"
                )

        threads = [
            threading.Thread(target=run, args=(t,)) for t in (tenant_a, tenant_b)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
