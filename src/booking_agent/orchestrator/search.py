"""
Search orchestrator: fans out to all enabled connectors in parallel, streams
normalized+deduped results as they arrive, and handles timeouts/failures cleanly.
"""

import asyncio
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from datetime import datetime, timezone

from booking_agent.connectors.errors import (
    SupplierAuthError,
    SupplierBadResponse,
    SupplierError,
    SupplierRateLimited,
    SupplierTimeout,
)
from booking_agent.connectors.protocol import Connector
from booking_agent.domain.offer import Offer
from booking_agent.domain.ranking import RankingPreferences
from booking_agent.domain.search import SearchQuery
from booking_agent.orchestrator.circuit_breaker import CircuitBreaker
from booking_agent.orchestrator.dedupe import DedupeGroup, dedupe
from booking_agent.orchestrator.events import (
    OffersPartial,
    SearchComplete,
    SearchEvent,
    SourceFailed,
    SourceSkipped,
    SourceStarted,
    SourceTimedOut,
)
from booking_agent.orchestrator.metrics import MetricsCollector
from booking_agent.orchestrator.ranking import rank_groups


@dataclass
class TenantContext:
    tenant_id: str = "default"
    enabled_suppliers: list[str] = field(default_factory=list)  # empty = all enabled


def _classify(exc: BaseException) -> str:
    if isinstance(exc, asyncio.TimeoutError):
        return "timeout"
    if isinstance(exc, SupplierAuthError):
        return "auth_error"
    if isinstance(exc, SupplierRateLimited):
        return "rate_limited"
    if isinstance(exc, SupplierBadResponse):
        return "bad_response"
    if isinstance(exc, SupplierTimeout):
        return "timeout"
    return "error"


class SearchOrchestrator:
    """
    Fans out a SearchQuery to all active connectors in parallel.

    Usage::

        async with SearchOrchestrator(connectors) as orch:
            async for event in orch.search(query):
                ...

    The orchestrator enters/exits each connector's HTTP lifecycle on enter/exit.
    """

    def __init__(
        self,
        connectors: list[Connector],
        ranking_preferences: RankingPreferences | None = None,
        supplier_timeout: float = 7.0,
        circuit_breakers: dict[str, CircuitBreaker] | None = None,
        semaphores: dict[str, asyncio.Semaphore] | None = None,
        max_concurrent_per_supplier: int = 2,
        metrics: MetricsCollector | None = None,
    ) -> None:
        self._connectors = list(connectors)
        self._prefs = ranking_preferences or RankingPreferences()
        self._supplier_timeout = supplier_timeout
        self._circuit_breakers: dict[str, CircuitBreaker] = dict(circuit_breakers or {})
        self._semaphores: dict[str, asyncio.Semaphore] = dict(semaphores or {})
        self._max_concurrent = max_concurrent_per_supplier
        self._metrics = metrics or MetricsCollector()

    async def __aenter__(self) -> "SearchOrchestrator":
        for c in self._connectors:
            await c.__aenter__()
        return self

    async def __aexit__(self, *args: object) -> None:
        for c in self._connectors:
            try:
                await c.__aexit__(*args)
            except Exception:
                pass

    @property
    def metrics(self) -> MetricsCollector:
        return self._metrics

    def _breaker(self, supplier_id: str) -> CircuitBreaker:
        if supplier_id not in self._circuit_breakers:
            self._circuit_breakers[supplier_id] = CircuitBreaker(supplier_id)
        return self._circuit_breakers[supplier_id]

    def _semaphore(self, supplier_id: str) -> asyncio.Semaphore:
        if supplier_id not in self._semaphores:
            self._semaphores[supplier_id] = asyncio.Semaphore(self._max_concurrent)
        return self._semaphores[supplier_id]

    def _active_connectors(self, tenant_ctx: TenantContext | None) -> list[Connector]:
        if not tenant_ctx or not tenant_ctx.enabled_suppliers:
            return self._connectors
        enabled = set(tenant_ctx.enabled_suppliers)
        return [c for c in self._connectors if c.capabilities.supplier_id in enabled]

    async def _call_supplier(self, connector: Connector, query: SearchQuery) -> list[Offer]:
        sid = connector.capabilities.supplier_id
        async with self._semaphore(sid):
            return await asyncio.wait_for(connector.search(query), timeout=self._supplier_timeout)

    async def search(
        self,
        query: SearchQuery,
        tenant_ctx: TenantContext | None = None,
    ) -> AsyncGenerator[SearchEvent, None]:
        active = self._active_connectors(tenant_ctx)
        accumulated: list[Offer] = []
        succeeded: list[str] = []
        failed: list[str] = []
        timed_out: list[str] = []
        skipped: list[str] = []
        # task → (supplier_id, start_time_monotonic)
        tasks: dict[asyncio.Task[list[Offer]], tuple[str, float]] = {}

        try:
            for connector in active:
                sid = connector.capabilities.supplier_id
                if not self._breaker(sid).is_allowed():
                    skipped.append(sid)
                    yield SourceSkipped(supplier_id=sid, reason="circuit_open")
                    continue

                # Create the task BEFORE yielding so it exists if the caller closes
                # the generator immediately after receiving SourceStarted.
                self._metrics.record_search_started(sid)
                task: asyncio.Task[list[Offer]] = asyncio.create_task(
                    self._call_supplier(connector, query)
                )
                tasks[task] = (sid, time.monotonic())
                yield SourceStarted(
                    supplier_id=sid,
                    searched_at=datetime.now(timezone.utc),
                )

            pending = set(tasks)
            while pending:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    sid, t0 = tasks[task]
                    latency_ms = (time.monotonic() - t0) * 1000
                    exc = task.exception()

                    if exc is None:
                        offers: list[Offer] = task.result()
                        accumulated.extend(offers)
                        succeeded.append(sid)
                        self._breaker(sid).record_success()
                        self._metrics.record_success(sid, latency_ms, len(offers))
                        groups = rank_groups(dedupe(accumulated), self._prefs)
                        yield OffersPartial(
                            supplier_id=sid,
                            offers_count=len(offers),
                            groups=groups,
                        )
                    elif isinstance(exc, asyncio.TimeoutError):
                        timed_out.append(sid)
                        self._breaker(sid).record_failure()
                        self._metrics.record_timeout(sid)
                        yield SourceTimedOut(
                            supplier_id=sid,
                            timeout_seconds=self._supplier_timeout,
                        )
                    else:
                        failed.append(sid)
                        self._breaker(sid).record_failure()
                        self._metrics.record_error(sid)
                        yield SourceFailed(
                            supplier_id=sid,
                            reason=_classify(exc),
                            detail=str(exc)[:300],
                        )
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks.keys(), return_exceptions=True)

        final_groups = rank_groups(dedupe(accumulated), self._prefs)
        if final_groups:
            self._metrics.record_win(final_groups[0].primary.supplier_id)

        yield SearchComplete(
            groups=final_groups,
            suppliers_succeeded=succeeded,
            suppliers_failed=failed,
            suppliers_timed_out=timed_out,
            suppliers_skipped=skipped,
            elapsed_seconds=time.monotonic(),
        )
