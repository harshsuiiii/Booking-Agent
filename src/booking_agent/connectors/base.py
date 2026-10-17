"""
BaseConnector: shared infrastructure for every supplier adapter.

Provides:
- httpx.AsyncClient lifecycle (context manager)
- Per-request timeout passed to httpx
- Tenacity retries for search/price (NEVER for book)
- Structured logging via structlog (supplier_id bound at init)
- OpenTelemetry spans per public operation
- Optional request/response recorder hook (for fixture capture)
- Credential loading via injected SecretsProvider
"""

import abc
from typing import Any, Awaitable, Callable, Optional, TypeVar

import httpx
import structlog
from opentelemetry import trace
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from booking_agent.connectors.errors import SupplierBadResponse, SupplierRateLimited, SupplierTimeout
from booking_agent.connectors.protocol import SupplierCapabilities
from booking_agent.connectors.secrets import SecretsProvider
from booking_agent.domain.booking import Booking, Passenger
from booking_agent.domain.offer import Offer, PricedOffer
from booking_agent.domain.search import SearchQuery

T = TypeVar("T")
tracer = trace.get_tracer(__name__)

# Only these errors trigger a retry; auth errors and bad data are not retriable.
_RETRYABLE = (SupplierTimeout, SupplierRateLimited)


class RecorderHook(abc.ABC):
    """
    Hook called after every HTTP exchange (sanitized — no auth headers).
    Implement to write fixtures or audit logs.
    """

    @abc.abstractmethod
    async def record(
        self,
        supplier_id: str,
        operation: str,
        request: dict[str, Any],
        response: dict[str, Any],
    ) -> None: ...


class BaseConnector(abc.ABC):
    def __init__(
        self,
        secrets: SecretsProvider,
        timeout_seconds: float = 8.0,
        max_retries: int = 3,
        retry_min_wait: float = 1.0,
        retry_max_wait: float = 10.0,
        recorder: Optional[RecorderHook] = None,
    ) -> None:
        self._secrets = secrets
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._retry_min_wait = retry_min_wait
        self._retry_max_wait = retry_max_wait
        self._recorder = recorder
        self._client: Optional[httpx.AsyncClient] = None
        self._log = structlog.get_logger(__name__).bind(
            supplier_id=self.capabilities.supplier_id
        )

    @property
    @abc.abstractmethod
    def capabilities(self) -> SupplierCapabilities: ...

    # ── async context manager ──────────────────────────────────────────────

    async def __aenter__(self) -> "BaseConnector":
        self._client = httpx.AsyncClient(timeout=self._timeout_seconds)
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._client is not None:
            await self._client.__aexit__(*args)
            self._client = None

    # ── public API ─────────────────────────────────────────────────────────

    async def search(self, query: SearchQuery) -> list[Offer]:
        with tracer.start_as_current_span(f"{self.capabilities.supplier_id}.search"):
            self._log.info(
                "search.start",
                origin=query.origin,
                destination=query.destination,
                departure_date=str(query.departure_date),
            )
            try:
                results = await self._with_retry(self._do_search, query)
                self._log.info("search.complete", offer_count=len(results))
                return results
            except Exception as exc:
                self._log.error("search.failed", error_type=type(exc).__name__)
                raise

    async def price(self, offer: Offer) -> PricedOffer:
        with tracer.start_as_current_span(f"{self.capabilities.supplier_id}.price"):
            self._log.info("price.start", offer_ref=offer.supplier_offer_ref)
            try:
                result = await self._with_retry(self._do_price, offer)
                self._log.info("price.complete")
                return result
            except Exception as exc:
                self._log.error("price.failed", error_type=type(exc).__name__)
                raise

    async def book(
        self,
        priced_offer: PricedOffer,
        passengers: list[Passenger],
        idempotency_key: str,
    ) -> Booking:
        # Book is intentionally NOT retried at the connector layer.
        # GDS book calls create PNRs; retrying would create duplicate bookings.
        # Idempotency at our layer is handled via idempotency_key in Postgres.
        with tracer.start_as_current_span(f"{self.capabilities.supplier_id}.book"):
            self._log.info(
                "book.start",
                offer_ref=priced_offer.supplier_offer_ref,
                idempotency_key=idempotency_key,
            )
            try:
                result = await self._do_book(priced_offer, passengers, idempotency_key)
                self._log.info("book.complete", booking_id=result.booking_id)
                return result
            except Exception as exc:
                self._log.error("book.failed", error_type=type(exc).__name__)
                raise

    # ── HTTP helper ────────────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        url: str,
        operation: str,
        **kwargs: Any,
    ) -> httpx.Response:
        """
        Thin wrapper around httpx that:
        - Raises SupplierTimeout on httpx.TimeoutException
        - Calls the recorder hook with sanitized data (auth headers stripped)
        """
        if self._client is None:
            raise RuntimeError(
                f"{type(self).__name__} must be used as an async context manager"
            )
        try:
            response = await self._client.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            raise SupplierTimeout(
                supplier_id=self.capabilities.supplier_id, operation=operation
            ) from exc

        if self._recorder is not None:
            # Strip authorization headers before recording — never log secrets.
            safe_headers = {
                k: v
                for k, v in (kwargs.get("headers") or {}).items()
                if k.lower() not in {"authorization", "x-api-key"}
            }
            await self._recorder.record(
                supplier_id=self.capabilities.supplier_id,
                operation=operation,
                request={"method": method, "url": url, "headers": safe_headers},
                response={
                    "status_code": response.status_code,
                    "body": response.text[:8192],
                },
            )
        return response

    # ── retry helper ───────────────────────────────────────────────────────

    async def _with_retry(
        self,
        fn: Callable[..., Awaitable[T]],
        *args: Any,
        **kwargs: Any,
    ) -> T:
        """Wrap *fn* with tenacity retries for SupplierTimeout / SupplierRateLimited."""
        async for attempt in AsyncRetrying(
            retry=retry_if_exception_type(_RETRYABLE),
            stop=stop_after_attempt(self._max_retries),
            wait=wait_exponential(
                multiplier=1,
                min=self._retry_min_wait,
                max=self._retry_max_wait,
            ),
            reraise=True,
        ):
            with attempt:
                return await fn(*args, **kwargs)
        raise AssertionError("unreachable")  # reraise=True always re-raises

    # ── abstract hooks ─────────────────────────────────────────────────────

    @abc.abstractmethod
    async def _do_search(self, query: SearchQuery) -> list[Offer]: ...

    @abc.abstractmethod
    async def _do_price(self, offer: Offer) -> PricedOffer: ...

    @abc.abstractmethod
    async def _do_book(
        self,
        priced_offer: PricedOffer,
        passengers: list[Passenger],
        idempotency_key: str,
    ) -> Booking: ...
