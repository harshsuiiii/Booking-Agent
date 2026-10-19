"""
Hotel search orchestrator: fans out to hotel connectors in parallel.

Reuses circuit breaker and metrics from the flight orchestrator.
"""

import asyncio
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from booking_agent.connectors.errors import (
    SupplierAuthError,
    SupplierBadResponse,
    SupplierRateLimited,
    SupplierTimeout,
)
from booking_agent.connectors.hotel_protocol import HotelConnector
from booking_agent.domain.hotel import HotelOffer, HotelSearchQuery
from booking_agent.orchestrator.circuit_breaker import CircuitBreaker
from booking_agent.orchestrator.metrics import MetricsCollector
from booking_agent.orchestrator.search import TenantContext


@dataclass
class HotelAlternateSource:
    supplier_id: str
    offer_id: str
    total_inr: Decimal


@dataclass
class HotelDedupeGroup:
    dedupe_key: str
    primary: HotelOffer
    alternates: list[HotelAlternateSource] = field(default_factory=list)


@dataclass
class HotelSourceStarted:
    supplier_id: str
    searched_at: datetime


@dataclass
class HotelOffersPartial:
    supplier_id: str
    offers_count: int
    groups: list[HotelDedupeGroup]


@dataclass
class HotelSourceFailed:
    supplier_id: str
    reason: str
    detail: str = ""


@dataclass
class HotelSourceTimedOut:
    supplier_id: str
    timeout_seconds: float


@dataclass
class HotelSourceSkipped:
    supplier_id: str
    reason: str


@dataclass
class HotelSearchComplete:
    groups: list[HotelDedupeGroup]
    suppliers_succeeded: list[str]
    suppliers_failed: list[str]
    suppliers_timed_out: list[str]
    suppliers_skipped: list[str]
    elapsed_seconds: float


HotelSearchEvent = (
    HotelSourceStarted
    | HotelOffersPartial
    | HotelSourceFailed
    | HotelSourceTimedOut
    | HotelSourceSkipped
    | HotelSearchComplete
)


def dedupe_hotels(offers: list[HotelOffer]) -> list[HotelDedupeGroup]:
    """Group by dedupe_key; cheapest offer (by total_inr) becomes the primary."""
    groups: dict[str, HotelDedupeGroup] = {}
    for offer in sorted(offers, key=lambda o: o.total_inr):
        key = offer.dedupe_key
        if key not in groups:
            groups[key] = HotelDedupeGroup(dedupe_key=key, primary=offer)
        else:
            groups[key].alternates.append(
                HotelAlternateSource(
                    supplier_id=offer.supplier_id,
                    offer_id=offer.offer_id,
                    total_inr=offer.total_inr,
                )
            )
    return list(groups.values())


def rank_hotel_groups(groups: list[HotelDedupeGroup]) -> list[HotelDedupeGroup]:
    """Sort by total_inr ascending; higher star rating as tie-breaker."""
    return sorted(
        groups,
        key=lambda g: (g.primary.total_inr, -g.primary.star_rating, g.primary.offer_id),
    )


def _classify(exc: BaseException) -> str:
    if isinstance(exc, (asyncio.TimeoutError, SupplierTimeout)):
        return "timeout"
    if isinstance(exc, SupplierAuthError):
        return "auth_error"
    if isinstance(exc, SupplierRateLimited):
        return "rate_limited"
    if isinstance(exc, SupplierBadResponse):
        return "bad_response"
    return "error"


class HotelSearchOrchestrator:
    """
    Fans out a HotelSearchQuery to all active hotel connectors in parallel.

    Reuses CircuitBreaker and MetricsCollector from the flight orchestrator.
    """

    def __init__(
        self,
        connectors: list[HotelConnector],
        supplier_timeout: float = 7.0,
        circuit_breakers: dict[str, CircuitBreaker] | None = None,
        semaphores: dict[str, asyncio.Semaphore] | None = None,
        max_concurrent_per_supplier: int = 2,
        metrics: MetricsCollector | None = None,
    ) -> None:
        self._connectors = list(connectors)
        self._supplier_timeout = supplier_timeout
        self._circuit_breakers: dict[str, CircuitBreaker] = dict(circuit_breakers or {})
        self._semaphores: dict[str, asyncio.Semaphore] = dict(semaphores or {})
        self._max_concurrent = max_concurrent_per_supplier
        self._metrics = metrics or MetricsCollector()

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

    def _active_connectors(self, tenant_ctx: TenantContext | None) -> list[HotelConnector]:
        if not tenant_ctx or not tenant_ctx.enabled_suppliers:
            return self._connectors
        enabled = set(tenant_ctx.enabled_suppliers)
        return [c for c in self._connectors if c.supplier_id in enabled]

    async def _call_supplier(
        self, connector: HotelConnector, query: HotelSearchQuery
    ) -> list[HotelOffer]:
        sid = connector.supplier_id
        async with self._semaphore(sid):
            return await asyncio.wait_for(connector.search(query), timeout=self._supplier_timeout)

    async def search(
        self,
        query: HotelSearchQuery,
        tenant_ctx: TenantContext | None = None,
    ) -> AsyncGenerator[HotelSearchEvent, None]:
        wall_start = time.monotonic()
        active = self._active_connectors(tenant_ctx)
        accumulated: list[HotelOffer] = []
        succeeded: list[str] = []
        failed: list[str] = []
        timed_out: list[str] = []
        skipped: list[str] = []
        tasks: dict[asyncio.Task[list[HotelOffer]], tuple[str, float]] = {}

        try:
            for connector in active:
                sid = connector.supplier_id
                if not self._breaker(sid).is_allowed():
                    skipped.append(sid)
                    yield HotelSourceSkipped(supplier_id=sid, reason="circuit_open")
                    continue
                self._metrics.record_search_started(sid)
                task: asyncio.Task[list[HotelOffer]] = asyncio.create_task(
                    self._call_supplier(connector, query)
                )
                tasks[task] = (sid, time.monotonic())
                yield HotelSourceStarted(
                    supplier_id=sid,
                    searched_at=datetime.now(UTC),
                )

            pending = set(tasks)
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    sid, t0 = tasks[task]
                    latency_ms = (time.monotonic() - t0) * 1000
                    exc = task.exception()

                    if exc is None:
                        offers = task.result()
                        accumulated.extend(offers)
                        succeeded.append(sid)
                        self._breaker(sid).record_success()
                        self._metrics.record_success(sid, latency_ms, len(offers))
                        groups = rank_hotel_groups(dedupe_hotels(accumulated))
                        yield HotelOffersPartial(
                            supplier_id=sid,
                            offers_count=len(offers),
                            groups=groups,
                        )
                    elif isinstance(exc, asyncio.TimeoutError):
                        timed_out.append(sid)
                        self._breaker(sid).record_failure()
                        self._metrics.record_timeout(sid)
                        yield HotelSourceTimedOut(
                            supplier_id=sid,
                            timeout_seconds=self._supplier_timeout,
                        )
                    else:
                        failed.append(sid)
                        self._breaker(sid).record_failure()
                        self._metrics.record_error(sid)
                        yield HotelSourceFailed(
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

        final_groups = rank_hotel_groups(dedupe_hotels(accumulated))
        if final_groups:
            self._metrics.record_win(final_groups[0].primary.supplier_id)

        yield HotelSearchComplete(
            groups=final_groups,
            suppliers_succeeded=succeeded,
            suppliers_failed=failed,
            suppliers_timed_out=timed_out,
            suppliers_skipped=skipped,
            elapsed_seconds=round(time.monotonic() - wall_start, 3),
        )
