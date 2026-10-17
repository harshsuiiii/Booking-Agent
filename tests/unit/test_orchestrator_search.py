"""
Tests for SearchOrchestrator using FakeConnector.
Covers: fan-out, partial failure, timeouts, circuit breaker, cancellation,
streaming order, cross-source dedupe, tenant filtering, and metrics.
"""

import asyncio
from datetime import date

import pytest

from booking_agent.connectors.errors import SupplierBadResponse, SupplierAuthError
from booking_agent.connectors.fake import FakeConnector
from booking_agent.domain.search import SearchQuery
from booking_agent.orchestrator.circuit_breaker import CircuitBreaker, CircuitState
from booking_agent.orchestrator.events import (
    OffersPartial,
    SearchComplete,
    SourceFailed,
    SourceSkipped,
    SourceStarted,
    SourceTimedOut,
)
from booking_agent.orchestrator.search import SearchOrchestrator, TenantContext


def _query() -> SearchQuery:
    return SearchQuery(origin="DEL", destination="BOM", departure_date=date(2025, 9, 1))


def _fake(supplier_id: str = "fake", **kwargs) -> FakeConnector:
    return FakeConnector(
        supplier_id=supplier_id,
        max_retries=1,
        retry_min_wait=0.0,
        retry_max_wait=0.0,
        **kwargs,
    )


async def _collect(orch: SearchOrchestrator, query: SearchQuery | None = None):
    return [e async for e in orch.search(query or _query())]


# ── basic fan-out ─────────────────────────────────────────────────────────────

class TestFanOut:
    async def test_single_source_success(self):
        async with SearchOrchestrator([_fake("a")]) as orch:
            events = await _collect(orch)

        assert any(isinstance(e, SourceStarted) for e in events)
        assert any(isinstance(e, OffersPartial) for e in events)
        complete = next(e for e in events if isinstance(e, SearchComplete))
        assert "a" in complete.suppliers_succeeded

    async def test_two_sources_both_succeed(self):
        async with SearchOrchestrator([_fake("a"), _fake("b")]) as orch:
            events = await _collect(orch)

        complete = next(e for e in events if isinstance(e, SearchComplete))
        assert set(complete.suppliers_succeeded) == {"a", "b"}
        assert complete.suppliers_failed == []

    async def test_search_complete_always_last(self):
        async with SearchOrchestrator([_fake("a"), _fake("b")]) as orch:
            events = await _collect(orch)
        assert isinstance(events[-1], SearchComplete)

    async def test_source_started_before_partial(self):
        async with SearchOrchestrator([_fake("a")]) as orch:
            events = await _collect(orch)
        types = [type(e).__name__ for e in events]
        assert types.index("SourceStarted") < types.index("OffersPartial")

    async def test_offers_in_complete_are_deduped(self):
        # Two suppliers returning the same flight → 1 group
        a = _fake("a", base_price=__import__("decimal").Decimal("3000"))
        b = _fake("b", base_price=__import__("decimal").Decimal("3200"))
        async with SearchOrchestrator([a, b]) as orch:
            events = await _collect(orch)
        complete = next(e for e in events if isinstance(e, SearchComplete))
        # Both connectors return the identical FX001 itinerary → 1 dedupe group
        assert len(complete.groups) == 1
        assert complete.groups[0].primary.supplier_id == "a"  # cheaper is primary


# ── partial failure ───────────────────────────────────────────────────────────

class TestPartialFailure:
    async def test_one_fails_other_succeeds(self):
        bad = _fake("bad", search_errors=[SupplierBadResponse("bad", 500, "err", "search")])
        good = _fake("good")
        async with SearchOrchestrator([bad, good]) as orch:
            events = await _collect(orch)

        complete = next(e for e in events if isinstance(e, SearchComplete))
        assert "good" in complete.suppliers_succeeded
        assert "bad" in complete.suppliers_failed
        assert any(isinstance(e, SourceFailed) for e in events)

    async def test_source_failed_has_reason(self):
        bad = _fake("bad", search_errors=[SupplierAuthError("bad")])
        async with SearchOrchestrator([bad]) as orch:
            events = await _collect(orch)
        failed = next(e for e in events if isinstance(e, SourceFailed))
        assert failed.reason == "auth_error"

    async def test_all_fail_returns_empty_complete(self):
        a = _fake("a", search_errors=[SupplierBadResponse("a", 500, "x", "search")])
        b = _fake("b", search_errors=[SupplierBadResponse("b", 500, "x", "search")])
        async with SearchOrchestrator([a, b]) as orch:
            events = await _collect(orch)
        complete = next(e for e in events if isinstance(e, SearchComplete))
        assert complete.groups == []
        assert set(complete.suppliers_failed) == {"a", "b"}


# ── timeout ───────────────────────────────────────────────────────────────────

class TestTimeout:
    async def test_slow_source_times_out(self):
        slow = _fake("slow", delay_seconds=5.0)
        async with SearchOrchestrator([slow], supplier_timeout=0.05) as orch:
            events = await _collect(orch)

        assert any(isinstance(e, SourceTimedOut) for e in events)
        complete = next(e for e in events if isinstance(e, SearchComplete))
        assert "slow" in complete.suppliers_timed_out

    async def test_slow_source_does_not_block_fast(self):
        fast = _fake("fast", delay_seconds=0.0)
        slow = _fake("slow", delay_seconds=0.2)
        async with SearchOrchestrator([fast, slow], supplier_timeout=5.0) as orch:
            events = await _collect(orch)

        partial_events = [e for e in events if isinstance(e, OffersPartial)]
        assert len(partial_events) == 2
        assert partial_events[0].supplier_id == "fast"
        assert partial_events[1].supplier_id == "slow"

    async def test_timeout_fires_at_configured_limit(self):
        slow = _fake("slow", delay_seconds=2.0)
        fast = _fake("fast", delay_seconds=0.0)
        async with SearchOrchestrator([slow, fast], supplier_timeout=0.05) as orch:
            events = await _collect(orch)

        complete = next(e for e in events if isinstance(e, SearchComplete))
        assert "slow" in complete.suppliers_timed_out
        assert "fast" in complete.suppliers_succeeded


# ── circuit breaker ───────────────────────────────────────────────────────────

class TestCircuitBreaker:
    async def test_breaker_opens_after_n_failures(self):
        connector = _fake("a", search_errors=[
            SupplierBadResponse("a", 500, "err", "search") for _ in range(5)
        ])
        breaker = CircuitBreaker("a", failure_threshold=3, recovery_timeout=60.0)
        orch = SearchOrchestrator([connector], circuit_breakers={"a": breaker})

        async with orch:
            for _ in range(3):
                await _collect(orch)

        assert breaker.state == CircuitState.OPEN

    async def test_open_breaker_emits_skipped(self):
        breaker = CircuitBreaker("a", failure_threshold=1, recovery_timeout=60.0)
        breaker.record_failure()  # trip immediately
        assert breaker.state == CircuitState.OPEN

        connector = _fake("a")
        orch = SearchOrchestrator([connector], circuit_breakers={"a": breaker})
        async with orch:
            events = await _collect(orch)

        assert any(isinstance(e, SourceSkipped) for e in events)
        skipped = next(e for e in events if isinstance(e, SourceSkipped))
        assert skipped.reason == "circuit_open"
        assert connector.search_call_count == 0

    async def test_breaker_recovers_to_half_open(self):
        breaker = CircuitBreaker("a", failure_threshold=1, recovery_timeout=0.05)
        breaker.record_failure()
        assert breaker.state == CircuitState.OPEN

        await asyncio.sleep(0.1)
        assert breaker.state == CircuitState.HALF_OPEN

    async def test_half_open_success_closes_breaker(self):
        breaker = CircuitBreaker("a", failure_threshold=1, recovery_timeout=0.05)
        breaker.record_failure()
        await asyncio.sleep(0.1)

        connector = _fake("a")
        orch = SearchOrchestrator([connector], circuit_breakers={"a": breaker})
        async with orch:
            await _collect(orch)

        assert breaker.state == CircuitState.CLOSED


# ── cancellation ─────────────────────────────────────────────────────────────

class TestCancellation:
    async def test_cancellation_leaves_no_dangling_tasks(self):
        """Closing the generator mid-stream cancels outstanding supplier tasks."""
        started = asyncio.Event()
        cancelled = asyncio.Event()

        connector = _fake("slow")

        async def slow_search(query):
            started.set()
            try:
                await asyncio.sleep(10.0)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return []

        connector._do_search = slow_search

        orch = SearchOrchestrator([connector], supplier_timeout=30.0)
        async with orch:
            gen = orch.search(_query())
            event = await gen.__anext__()
            assert isinstance(event, SourceStarted)

            await asyncio.wait_for(started.wait(), timeout=1.0)
            await gen.aclose()

        await asyncio.wait_for(cancelled.wait(), timeout=1.0)

    async def test_no_events_after_aclose(self):
        connector = _fake("a", delay_seconds=0.0)
        orch = SearchOrchestrator([connector], supplier_timeout=5.0)
        collected = []

        async with orch:
            gen = orch.search(_query())
            async for event in gen:
                collected.append(event)
                if isinstance(event, SourceStarted):
                    await gen.aclose()
                    break

        # No OffersPartial or SearchComplete should have been collected
        assert not any(isinstance(e, (OffersPartial, SearchComplete)) for e in collected)


# ── streaming order and dedupe ────────────────────────────────────────────────

class TestStreamingDedupe:
    async def test_offers_partial_grows_over_time(self):
        """Each OffersPartial snapshot is a superset of the previous one."""
        a = _fake("a", delay_seconds=0.0)
        b = _fake("b", delay_seconds=0.1)
        async with SearchOrchestrator([a, b], supplier_timeout=5.0) as orch:
            events = await _collect(orch)

        partials = [e for e in events if isinstance(e, OffersPartial)]
        assert len(partials) == 2
        # second snapshot has at least as many groups as first
        assert len(partials[1].groups) >= len(partials[0].groups)

    async def test_cross_source_dedupe_in_partial(self):
        """Same flight from two sources → only one group after both arrive."""
        a = _fake("a")
        b = _fake("b")
        async with SearchOrchestrator([a, b], supplier_timeout=5.0) as orch:
            events = await _collect(orch)

        complete = next(e for e in events if isinstance(e, SearchComplete))
        assert len(complete.groups) == 1  # same FX001 itinerary deduped

    async def test_partial_event_supplier_id_matches(self):
        a = _fake("a")
        async with SearchOrchestrator([a]) as orch:
            events = await _collect(orch)
        partial = next(e for e in events if isinstance(e, OffersPartial))
        assert partial.supplier_id == "a"


# ── tenant context ────────────────────────────────────────────────────────────

class TestTenantContext:
    async def test_only_enabled_suppliers_called(self):
        a = _fake("a")
        b = _fake("b")
        ctx = TenantContext(tenant_id="t1", enabled_suppliers=["a"])

        async with SearchOrchestrator([a, b]) as orch:
            events = [e async for e in orch.search(_query(), tenant_ctx=ctx)]

        started = [e.supplier_id for e in events if isinstance(e, SourceStarted)]
        assert started == ["a"]
        assert b.search_call_count == 0

    async def test_empty_enabled_suppliers_calls_all(self):
        a = _fake("a")
        b = _fake("b")
        ctx = TenantContext(tenant_id="t1", enabled_suppliers=[])

        async with SearchOrchestrator([a, b]) as orch:
            events = [e async for e in orch.search(_query(), tenant_ctx=ctx)]

        started = {e.supplier_id for e in events if isinstance(e, SourceStarted)}
        assert started == {"a", "b"}


# ── metrics ───────────────────────────────────────────────────────────────────

class TestMetrics:
    async def test_success_recorded(self):
        connector = _fake("a")
        async with SearchOrchestrator([connector]) as orch:
            await _collect(orch)

        snap = orch.metrics.snapshot()
        assert "a" in snap
        assert snap["a"].success_count == 1
        assert snap["a"].search_count == 1
        assert snap["a"].total_offers_returned == 1
        assert snap["a"].latencies_ms[0] >= 0

    async def test_error_recorded(self):
        connector = _fake("a", search_errors=[SupplierBadResponse("a", 500, "x", "search")])
        async with SearchOrchestrator([connector]) as orch:
            await _collect(orch)

        snap = orch.metrics.snapshot()
        assert snap["a"].error_count == 1
        assert snap["a"].error_rate == 1.0

    async def test_timeout_recorded(self):
        connector = _fake("a", delay_seconds=5.0)
        async with SearchOrchestrator([connector], supplier_timeout=0.05) as orch:
            await _collect(orch)

        snap = orch.metrics.snapshot()
        assert snap["a"].timeout_count == 1
        assert snap["a"].timeout_rate == 1.0

    async def test_win_recorded_for_best_ranked(self):
        a = _fake("a")
        async with SearchOrchestrator([a]) as orch:
            await _collect(orch)

        snap = orch.metrics.snapshot()
        assert snap["a"].win_count == 1
