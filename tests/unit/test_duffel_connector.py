"""
DuffelConnector tests using respx to mock HTTP calls.
All fixtures are synthetic — not from the live Duffel API.
"""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

from booking_agent.connectors.duffel import DuffelConnector
from booking_agent.connectors.duffel_fx import FixedFxProvider
from booking_agent.connectors.errors import (
    BookingDisabled,
    FareExpired,
    PriceChanged,
    SupplierAuthError,
    SupplierBadResponse,
    SupplierRateLimited,
    SupplierTimeout,
)
from booking_agent.connectors.payment import DuffelBalancePayment
from booking_agent.connectors.secrets import EnvSecretsProvider
from booking_agent.domain.booking import Passenger, PassengerType
from booking_agent.domain.offer import Offer
from booking_agent.domain.search import SearchQuery

FIXTURES = Path(__file__).parent.parent / "fixtures" / "duffel"
BASE = "https://api.duffel.com"
OFFER_REQUESTS_URL = f"{BASE}/air/offer_requests"
OFFERS_URL = f"{BASE}/air/offers"
ORDERS_URL = f"{BASE}/air/orders"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def make_connector(monkeypatch, base_url: str = BASE) -> DuffelConnector:
    monkeypatch.setenv("DUFFEL_ACCESS_TOKEN", "duffel_test_xyz123")
    return DuffelConnector(
        secrets=EnvSecretsProvider(),
        fx_provider=FixedFxProvider(
            {
                "GBP": Decimal("105"),
                "USD": Decimal("83"),
                "EUR": Decimal("90"),
            }
        ),
        base_url=base_url,
        max_retries=3,
        retry_min_wait=0.0,
        retry_max_wait=0.0,
    )


def make_query() -> SearchQuery:
    return SearchQuery(
        origin="BOM",
        destination="DEL",
        departure_date=date(2027, 1, 15),
    )


def make_passenger() -> Passenger:
    return Passenger(
        pax_type=PassengerType.ADULT,
        given_name="Test",
        family_name="Passenger",
        date_of_birth=date(1990, 1, 1),
    )


# ── init validation ───────────────────────────────────────────────────────────


class TestDuffelConnectorInit:
    def test_rejects_live_token(self, monkeypatch):
        monkeypatch.setenv("DUFFEL_ACCESS_TOKEN", "duffel_live_xyz123")
        with pytest.raises(ValueError, match="duffel_test_"):
            DuffelConnector(
                secrets=EnvSecretsProvider(),
                fx_provider=FixedFxProvider({}),
            )

    def test_accepts_test_token(self, monkeypatch):
        monkeypatch.setenv("DUFFEL_ACCESS_TOKEN", "duffel_test_xyz123")
        c = DuffelConnector(secrets=EnvSecretsProvider(), fx_provider=FixedFxProvider({}))
        assert c.capabilities.supplier_id == "duffel"

    def test_supports_hold_is_false(self, monkeypatch):
        monkeypatch.setenv("DUFFEL_ACCESS_TOKEN", "duffel_test_xyz123")
        c = DuffelConnector(secrets=EnvSecretsProvider(), fx_provider=FixedFxProvider({}))
        assert c.capabilities.supports_hold is False


# ── search ────────────────────────────────────────────────────────────────────


class TestDuffelSearch:
    @respx.mock
    async def test_one_way_returns_offer(self, monkeypatch):
        respx.post(OFFER_REQUESTS_URL).mock(
            return_value=httpx.Response(200, json=load("offer_request_response.json"))
        )
        async with make_connector(monkeypatch) as c:
            offers = await c.search(make_query())

        assert len(offers) == 1
        assert isinstance(offers[0], Offer)
        assert offers[0].total_inr == Decimal("5240.00")

    @respx.mock
    async def test_search_sends_correct_payload(self, monkeypatch):
        route = respx.post(OFFER_REQUESTS_URL).mock(
            return_value=httpx.Response(200, json=load("offer_request_response.json"))
        )
        async with make_connector(monkeypatch) as c:
            await c.search(make_query())

        body = json.loads(route.calls[0].request.content)
        assert body["data"]["slices"][0]["origin"] == "BOM"
        assert body["data"]["slices"][0]["destination"] == "DEL"
        assert body["data"]["passengers"] == [{"type": "adult"}]

    @respx.mock
    async def test_round_trip_sends_two_slices(self, monkeypatch):
        route = respx.post(OFFER_REQUESTS_URL).mock(
            return_value=httpx.Response(200, json=load("offer_request_response.json"))
        )
        async with make_connector(monkeypatch) as c:
            await c.search(
                SearchQuery(
                    origin="BOM",
                    destination="DEL",
                    departure_date=date(2027, 1, 15),
                    return_date=date(2027, 1, 22),
                )
            )

        body = json.loads(route.calls[0].request.content)
        assert len(body["data"]["slices"]) == 2

    @respx.mock
    async def test_zz_offer_is_test_data(self, monkeypatch):
        respx.post(OFFER_REQUESTS_URL).mock(
            return_value=httpx.Response(200, json=load("offer_request_response_zz.json"))
        )
        async with make_connector(monkeypatch) as c:
            offers = await c.search(make_query())

        assert offers[0].is_test_data is True

    @respx.mock
    async def test_empty_offers_returns_empty_list(self, monkeypatch):
        respx.post(OFFER_REQUESTS_URL).mock(
            return_value=httpx.Response(200, json={"data": {"id": "orr_x", "offers": []}})
        )
        async with make_connector(monkeypatch) as c:
            offers = await c.search(make_query())

        assert offers == []

    @respx.mock
    async def test_duffel_version_header_sent(self, monkeypatch):
        route = respx.post(OFFER_REQUESTS_URL).mock(
            return_value=httpx.Response(200, json=load("offer_request_response.json"))
        )
        async with make_connector(monkeypatch) as c:
            await c.search(make_query())

        assert route.calls[0].request.headers.get("Duffel-Version") == "v2"

    @respx.mock
    async def test_401_raises_auth_error(self, monkeypatch):
        respx.post(OFFER_REQUESTS_URL).mock(
            return_value=httpx.Response(
                401, json={"errors": [{"title": "Unauthorized", "code": "unauthenticated"}]}
            )
        )
        async with make_connector(monkeypatch) as c:
            with pytest.raises(SupplierAuthError):
                await c.search(make_query())

    @respx.mock
    async def test_429_raises_rate_limited(self, monkeypatch):
        respx.post(OFFER_REQUESTS_URL).mock(
            return_value=httpx.Response(429, json={}, headers={"Retry-After": "30"})
        )
        async with make_connector(monkeypatch) as c:
            with pytest.raises(SupplierRateLimited) as exc_info:
                await c.search(make_query())

        assert exc_info.value.retry_after == 30

    @respx.mock
    async def test_500_raises_bad_response(self, monkeypatch):
        respx.post(OFFER_REQUESTS_URL).mock(
            return_value=httpx.Response(500, text="Internal Server Error")
        )
        async with make_connector(monkeypatch) as c:
            with pytest.raises(SupplierBadResponse) as exc_info:
                await c.search(make_query())

        assert exc_info.value.status_code == 500

    @respx.mock
    async def test_timeout_raises_supplier_timeout(self, monkeypatch):
        respx.post(OFFER_REQUESTS_URL).mock(side_effect=httpx.TimeoutException("timed out"))
        async with make_connector(monkeypatch) as c:
            with pytest.raises(SupplierTimeout):
                await c.search(make_query())


# ── price ─────────────────────────────────────────────────────────────────────


class TestDuffelPrice:
    @respx.mock
    async def test_price_returns_priced_offer(self, monkeypatch):
        from booking_agent.domain.offer import PricedOffer

        respx.post(OFFER_REQUESTS_URL).mock(
            return_value=httpx.Response(200, json=load("offer_request_response.json"))
        )
        respx.get(f"{OFFERS_URL}/off_0000AuzFnPbflPXiD0QJME").mock(
            return_value=httpx.Response(200, json=load("offer_response.json"))
        )
        async with make_connector(monkeypatch) as c:
            offers = await c.search(make_query())
            priced = await c.price(offers[0])

        assert isinstance(priced, PricedOffer)
        assert priced.priced_at is not None

    @respx.mock
    async def test_price_detects_price_change(self, monkeypatch):
        respx.post(OFFER_REQUESTS_URL).mock(
            return_value=httpx.Response(200, json=load("offer_request_response.json"))
        )
        respx.get(f"{OFFERS_URL}/off_0000AuzFnPbflPXiD0QJME").mock(
            return_value=httpx.Response(200, json=load("offer_response_price_changed.json"))
        )
        async with make_connector(monkeypatch) as c:
            offers = await c.search(make_query())
            with pytest.raises(PriceChanged) as exc_info:
                await c.price(offers[0])

        e = exc_info.value
        assert e.old_price == Decimal("5240.00")
        assert e.new_price == Decimal("5900.00")

    @respx.mock
    async def test_422_offer_no_longer_available_raises_fare_expired(self, monkeypatch):
        respx.post(OFFER_REQUESTS_URL).mock(
            return_value=httpx.Response(200, json=load("offer_request_response.json"))
        )
        respx.get(f"{OFFERS_URL}/off_0000AuzFnPbflPXiD0QJME").mock(
            return_value=httpx.Response(
                422,
                json={"errors": [{"code": "offer_no_longer_available", "title": "Offer expired"}]},
            )
        )
        async with make_connector(monkeypatch) as c:
            offers = await c.search(make_query())
            with pytest.raises(FareExpired):
                await c.price(offers[0])


# ── book ──────────────────────────────────────────────────────────────────────


class TestDuffelBook:
    @respx.mock
    async def test_book_raises_booking_disabled_by_default(self, monkeypatch):
        respx.post(OFFER_REQUESTS_URL).mock(
            return_value=httpx.Response(200, json=load("offer_request_response.json"))
        )
        respx.get(f"{OFFERS_URL}/off_0000AuzFnPbflPXiD0QJME").mock(
            return_value=httpx.Response(200, json=load("offer_response.json"))
        )
        async with make_connector(monkeypatch) as c:
            offers = await c.search(make_query())
            priced = await c.price(offers[0])
            with pytest.raises(BookingDisabled):
                await c.book(
                    priced,
                    [make_passenger()],
                    idempotency_key="idem-1",
                    payment=DuffelBalancePayment(),
                )

    @respx.mock
    async def test_book_succeeds_when_live_enabled(self, monkeypatch):
        from booking_agent.domain.booking import Booking, BookingState

        monkeypatch.setenv("BOOKING_LIVE_ENABLED", "true")
        respx.post(OFFER_REQUESTS_URL).mock(
            return_value=httpx.Response(200, json=load("offer_request_response.json"))
        )
        respx.get(f"{OFFERS_URL}/off_0000AuzFnPbflPXiD0QJME").mock(
            return_value=httpx.Response(200, json=load("offer_response.json"))
        )
        respx.post(ORDERS_URL).mock(
            return_value=httpx.Response(201, json=load("order_response.json"))
        )
        async with make_connector(monkeypatch) as c:
            offers = await c.search(make_query())
            priced = await c.price(offers[0])
            booking = await c.book(
                priced,
                [make_passenger()],
                idempotency_key="idem-live-1",
                payment=DuffelBalancePayment(),
            )

        assert isinstance(booking, Booking)
        assert booking.pnr == "RZPNX8"
        assert booking.supplier_booking_ref == "ord_0000AuzFnPbflPXiD0QJMK"
        assert booking.state == BookingState.PNR_CREATED

    @respx.mock
    async def test_book_sends_idempotency_key(self, monkeypatch):
        monkeypatch.setenv("BOOKING_LIVE_ENABLED", "true")
        respx.post(OFFER_REQUESTS_URL).mock(
            return_value=httpx.Response(200, json=load("offer_request_response.json"))
        )
        respx.get(f"{OFFERS_URL}/off_0000AuzFnPbflPXiD0QJME").mock(
            return_value=httpx.Response(200, json=load("offer_response.json"))
        )
        book_route = respx.post(ORDERS_URL).mock(
            return_value=httpx.Response(201, json=load("order_response.json"))
        )
        async with make_connector(monkeypatch) as c:
            offers = await c.search(make_query())
            priced = await c.price(offers[0])
            await c.book(
                priced,
                [make_passenger()],
                idempotency_key="unique-key-99",
                payment=DuffelBalancePayment(),
            )

        assert book_route.calls[0].request.headers.get("Idempotency-Key") == "unique-key-99"

    @respx.mock
    async def test_book_sends_balance_payment(self, monkeypatch):
        monkeypatch.setenv("BOOKING_LIVE_ENABLED", "true")
        respx.post(OFFER_REQUESTS_URL).mock(
            return_value=httpx.Response(200, json=load("offer_request_response.json"))
        )
        respx.get(f"{OFFERS_URL}/off_0000AuzFnPbflPXiD0QJME").mock(
            return_value=httpx.Response(200, json=load("offer_response.json"))
        )
        book_route = respx.post(ORDERS_URL).mock(
            return_value=httpx.Response(201, json=load("order_response.json"))
        )
        async with make_connector(monkeypatch) as c:
            offers = await c.search(make_query())
            priced = await c.price(offers[0])
            await c.book(
                priced,
                [make_passenger()],
                idempotency_key="idem-pay",
                payment=DuffelBalancePayment(),
            )

        body = json.loads(book_route.calls[0].request.content)
        payments = body["data"]["payments"]
        assert len(payments) == 1
        assert payments[0]["type"] == "balance"
        assert payments[0]["currency"] == "INR"

    @respx.mock
    async def test_book_not_retried_on_timeout(self, monkeypatch):
        monkeypatch.setenv("BOOKING_LIVE_ENABLED", "true")
        respx.post(OFFER_REQUESTS_URL).mock(
            return_value=httpx.Response(200, json=load("offer_request_response.json"))
        )
        respx.get(f"{OFFERS_URL}/off_0000AuzFnPbflPXiD0QJME").mock(
            return_value=httpx.Response(200, json=load("offer_response.json"))
        )
        book_route = respx.post(ORDERS_URL).mock(side_effect=httpx.TimeoutException("timeout"))
        async with make_connector(monkeypatch) as c:
            offers = await c.search(make_query())
            priced = await c.price(offers[0])
            with pytest.raises(SupplierTimeout):
                await c.book(
                    priced,
                    [make_passenger()],
                    idempotency_key="idem-t",
                    payment=DuffelBalancePayment(),
                )

        # book is never retried — exactly one attempt even with max_retries=3
        assert book_route.call_count == 1
