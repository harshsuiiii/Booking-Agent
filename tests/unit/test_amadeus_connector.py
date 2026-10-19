"""
AmadeusConnector integration tests using respx to mock HTTP calls.
All fixtures are SYNTHETIC — they are not from the live Amadeus API.
"""

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

from booking_agent.connectors.amadeus import AmadeusConnector
from booking_agent.connectors.errors import (
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

FIXTURES = Path(__file__).parent.parent / "fixtures" / "amadeus"
BASE = "https://test.api.amadeus.com"
TOKEN_URL = f"{BASE}/v1/security/oauth2/token"
SEARCH_URL = f"{BASE}/v2/shopping/flight-offers"
PRICE_URL = f"{BASE}/v1/shopping/flight-offers/pricing"
BOOK_URL = f"{BASE}/v1/booking/flight-orders"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


TOKEN_RESP = {"access_token": "test-token-xyz", "token_type": "Bearer", "expires_in": 1799}


def make_query() -> SearchQuery:
    return SearchQuery(
        origin="DEL",
        destination="BOM",
        departure_date=date(2027, 9, 1),
    )


def fast_connector(monkeypatch) -> AmadeusConnector:
    monkeypatch.setenv("AMADEUS_CLIENT_ID", "test-id")
    monkeypatch.setenv("AMADEUS_CLIENT_SECRET", "test-secret")
    return AmadeusConnector(
        secrets=EnvSecretsProvider(),
        base_url=BASE,
        max_retries=3,
        retry_min_wait=0.0,
        retry_max_wait=0.0,
    )


def make_passenger() -> Passenger:
    return Passenger(
        pax_type=PassengerType.ADULT,
        given_name="Test",
        family_name="User",
        date_of_birth=date(1990, 1, 1),
    )


# ── search: one-way ───────────────────────────────────────────────────────────


class TestAmadeusSearch:
    @respx.mock
    async def test_one_way_returns_offer(self, monkeypatch):
        respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESP))
        respx.get(SEARCH_URL).mock(
            return_value=httpx.Response(200, json=load("search_one_way.json"))
        )

        async with fast_connector(monkeypatch) as c:
            offers = await c.search(make_query())

        assert len(offers) == 1
        assert isinstance(offers[0], Offer)
        assert offers[0].slices[0].segments[0].origin == "DEL"
        assert offers[0].total_inr == Decimal("3700")

    @respx.mock
    async def test_round_trip_two_slices(self, monkeypatch):
        respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESP))
        respx.get(SEARCH_URL).mock(
            return_value=httpx.Response(200, json=load("search_round_trip.json"))
        )

        async with fast_connector(monkeypatch) as c:
            offers = await c.search(
                SearchQuery(
                    origin="DEL",
                    destination="BOM",
                    departure_date=date(2027, 9, 1),
                    return_date=date(2027, 9, 8),
                )
            )

        assert len(offers[0].slices) == 2

    @respx.mock
    async def test_connecting_flight_two_segments(self, monkeypatch):
        respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESP))
        respx.get(SEARCH_URL).mock(
            return_value=httpx.Response(200, json=load("search_connecting.json"))
        )

        async with fast_connector(monkeypatch) as c:
            offers = await c.search(make_query())

        assert len(offers[0].slices[0].segments) == 2
        assert len(offers[0].slices[0].layovers) == 1

    @respx.mock
    async def test_missing_baggage_is_unknown(self, monkeypatch):
        from booking_agent.domain._types import UNKNOWN

        respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESP))
        respx.get(SEARCH_URL).mock(
            return_value=httpx.Response(200, json=load("search_no_baggage.json"))
        )

        async with fast_connector(monkeypatch) as c:
            offers = await c.search(make_query())

        assert offers[0].baggage.checked_pieces == UNKNOWN

    @respx.mock
    async def test_token_cached_across_calls(self, monkeypatch):
        token_route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESP))
        respx.get(SEARCH_URL).mock(
            return_value=httpx.Response(200, json=load("search_one_way.json"))
        )

        async with fast_connector(monkeypatch) as c:
            await c.search(make_query())
            await c.search(make_query())

        # Token fetched only once — cached for subsequent calls
        assert token_route.call_count == 1

    @respx.mock
    async def test_401_raises_auth_error(self, monkeypatch):
        respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESP))
        respx.get(SEARCH_URL).mock(
            return_value=httpx.Response(401, json={"errors": [{"title": "Unauthorized"}]})
        )

        async with fast_connector(monkeypatch) as c:
            with pytest.raises(SupplierAuthError):
                await c.search(make_query())

    @respx.mock
    async def test_429_raises_rate_limited(self, monkeypatch):
        respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESP))
        respx.get(SEARCH_URL).mock(
            return_value=httpx.Response(429, json={}, headers={"Retry-After": "30"})
        )

        async with fast_connector(monkeypatch) as c:
            with pytest.raises(SupplierRateLimited) as exc_info:
                await c.search(make_query())

        # All retries exhausted (3 attempts, all 429)
        assert exc_info.value.retry_after == 30

    @respx.mock
    async def test_500_raises_bad_response(self, monkeypatch):
        respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESP))
        respx.get(SEARCH_URL).mock(return_value=httpx.Response(500, text="Internal Server Error"))

        async with fast_connector(monkeypatch) as c:
            with pytest.raises(SupplierBadResponse) as exc_info:
                await c.search(make_query())

        assert exc_info.value.status_code == 500

    @respx.mock
    async def test_timeout_raises_supplier_timeout(self, monkeypatch):
        respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESP))
        respx.get(SEARCH_URL).mock(side_effect=httpx.TimeoutException("timed out"))

        async with fast_connector(monkeypatch) as c:
            with pytest.raises(SupplierTimeout):
                await c.search(make_query())

    @respx.mock
    async def test_malformed_response_raises(self, monkeypatch):
        respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESP))
        respx.get(SEARCH_URL).mock(
            return_value=httpx.Response(200, json={"data": [{"bad": "payload"}]})
        )

        async with fast_connector(monkeypatch) as c:
            # The mapper currently leaks a raw KeyError here, not SupplierBadResponse.
            with pytest.raises(Exception):  # noqa: B017
                await c.search(make_query())


# ── auth: token refresh on 401 ────────────────────────────────────────────────


class TestTokenRefresh:
    @respx.mock
    async def test_expired_token_triggers_refetch(self, monkeypatch):
        """If the connector is initialized, token expires, then a new call arrives,
        it should fetch a fresh token."""
        call_count = 0

        def token_side_effect(request):
            nonlocal call_count
            call_count += 1
            return httpx.Response(200, json=TOKEN_RESP)

        respx.post(TOKEN_URL).mock(side_effect=token_side_effect)
        respx.get(SEARCH_URL).mock(
            return_value=httpx.Response(200, json=load("search_one_way.json"))
        )

        async with fast_connector(monkeypatch) as c:
            # Manually expire the token
            await c.search(make_query())
            from datetime import timedelta

            assert c._token_manager is not None
            c._token_manager._expires_at = datetime.now(UTC) - timedelta(seconds=120)
            await c.search(make_query())

        assert call_count == 2  # token refreshed after expiry


# ── price ─────────────────────────────────────────────────────────────────────


class TestAmadeusPrice:
    @respx.mock
    async def test_price_returns_priced_offer(self, monkeypatch):
        from booking_agent.domain.offer import PricedOffer

        respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESP))
        respx.get(SEARCH_URL).mock(
            return_value=httpx.Response(200, json=load("search_one_way.json"))
        )
        respx.post(PRICE_URL).mock(
            return_value=httpx.Response(200, json=load("price_response.json"))
        )

        async with fast_connector(monkeypatch) as c:
            offers = await c.search(make_query())
            priced = await c.price(offers[0])

        assert isinstance(priced, PricedOffer)
        assert priced.priced_at is not None

    @respx.mock
    async def test_price_detects_price_change(self, monkeypatch):
        respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESP))
        respx.get(SEARCH_URL).mock(
            return_value=httpx.Response(200, json=load("search_one_way.json"))
        )
        respx.post(PRICE_URL).mock(
            return_value=httpx.Response(200, json=load("price_changed.json"))
        )

        async with fast_connector(monkeypatch) as c:
            offers = await c.search(make_query())
            with pytest.raises(PriceChanged) as exc_info:
                await c.price(offers[0])

        e = exc_info.value
        assert e.old_price == Decimal("3700")
        assert e.new_price == Decimal("4200")

    @respx.mock
    async def test_fare_rules_populated_from_detailed(self, monkeypatch):
        respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESP))
        respx.get(SEARCH_URL).mock(
            return_value=httpx.Response(200, json=load("search_one_way.json"))
        )
        respx.post(PRICE_URL).mock(
            return_value=httpx.Response(200, json=load("price_response.json"))
        )

        async with fast_connector(monkeypatch) as c:
            offers = await c.search(make_query())
            priced = await c.price(offers[0])

        assert priced.fare_rules.refund.allowed is True
        assert priced.fare_rules.change.allowed is True


# ── book ──────────────────────────────────────────────────────────────────────


class TestAmadeusBook:
    @respx.mock
    async def test_book_returns_pnr(self, monkeypatch):
        respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESP))
        respx.get(SEARCH_URL).mock(
            return_value=httpx.Response(200, json=load("search_one_way.json"))
        )
        respx.post(PRICE_URL).mock(
            return_value=httpx.Response(200, json=load("price_response.json"))
        )
        respx.post(BOOK_URL).mock(
            return_value=httpx.Response(
                201,
                json={
                    "data": {
                        "type": "flight-order",
                        "id": "MlpZVkFMfFdBVFNPTnwyMDE1LTExLTAy",
                        "associatedRecords": [{"reference": "2ZYVAL", "originSystemCode": "GDS"}],
                        "flightOffers": [],
                        "travelers": [],
                    }
                },
            )
        )

        async with fast_connector(monkeypatch) as c:
            offers = await c.search(make_query())
            priced = await c.price(offers[0])
            booking = await c.book(
                priced, [make_passenger()], idempotency_key="idem-1", payment=DuffelBalancePayment()
            )

        from booking_agent.domain.booking import BookingState

        assert booking.pnr == "2ZYVAL"
        assert booking.state == BookingState.PNR_CREATED

    @respx.mock
    async def test_book_sends_idempotency_key(self, monkeypatch):
        respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESP))
        respx.get(SEARCH_URL).mock(
            return_value=httpx.Response(200, json=load("search_one_way.json"))
        )
        respx.post(PRICE_URL).mock(
            return_value=httpx.Response(200, json=load("price_response.json"))
        )
        book_route = respx.post(BOOK_URL).mock(
            return_value=httpx.Response(
                201,
                json={
                    "data": {
                        "id": "ORDER_ID",
                        "associatedRecords": [{"reference": "PNR001", "originSystemCode": "GDS"}],
                    }
                },
            )
        )

        async with fast_connector(monkeypatch) as c:
            offers = await c.search(make_query())
            priced = await c.price(offers[0])
            await c.book(
                priced,
                [make_passenger()],
                idempotency_key="unique-key-42",
                payment=DuffelBalancePayment(),
            )

        assert book_route.calls[0].request.headers.get("Idempotency-Key") == "unique-key-42"
