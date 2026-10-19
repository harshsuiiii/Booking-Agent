"""
Duffel sandbox connector, replaying a REAL test-mode response recorded on 2026-10-18
(LHR→JFK, 1 adult, economy; trimmed to 6 offers; token and account keys redacted).
respx intercepts every request, so these tests never touch the network.
"""

import copy
import json
import re
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import fakeredis.aioredis
import httpx
import pytest
import respx

from booking_agent.booking.errors import PriceChangedError
from booking_agent.booking.payment_gateway import FakePaymentGateway
from booking_agent.booking.repository import InMemoryBookingRepository
from booking_agent.booking.service import BookingService
from booking_agent.connectors.duffel import DuffelConnector
from booking_agent.connectors.duffel.connector import DuffelTokenRefused
from booking_agent.connectors.duffel_fx import FixedFxProvider
from booking_agent.connectors.errors import (
    BookingDisabled,
    PriceChanged,
    SupplierAuthError,
    SupplierBadResponse,
    SupplierRateLimited,
    SupplierTimeout,
)
from booking_agent.connectors.payment import DuffelBalancePayment
from booking_agent.connectors.secrets import EnvSecretsProvider
from booking_agent.domain.booking import Passenger, PassengerType
from booking_agent.domain.search import SearchQuery
from booking_agent.orchestrator.search import SearchComplete, SearchOrchestrator
from booking_agent.simulated.connector import SimulatedConnector
from booking_agent.simulated.pricing import PROFILES

FIXTURE = (
    Path(__file__).parent.parent / "fixtures" / "duffel" / "offer_request_test_mode_lhr_jfk.json"
)
BASE = "https://api.duffel.com"
OFFER_REQUESTS = f"{BASE}/air/offer_requests"
TOKEN = "duffel_test_unit_tests_only_not_a_real_token"
USD_INR = Decimal("84.10")


def recorded() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def connector(monkeypatch: pytest.MonkeyPatch, token: str = TOKEN, **kw: Any) -> DuffelConnector:
    monkeypatch.setenv("DUFFEL_ACCESS_TOKEN", token)
    return DuffelConnector(
        secrets=EnvSecretsProvider(),
        fx_provider=FixedFxProvider({"USD": USD_INR}),
        base_url=BASE,
        max_retries=1,
        retry_min_wait=0.0,
        retry_max_wait=0.0,
        **kw,
    )


def query() -> SearchQuery:
    return SearchQuery(
        origin="LHR", destination="JFK", departure_date=date.today() + timedelta(days=30)
    )


class TestFixtureIsSafe:
    def test_no_tokens_or_personal_data(self) -> None:
        text = FIXTURE.read_text(encoding="utf-8")
        assert not re.search(r"duffel_(test|live)_(?!REDACTED)[A-Za-z0-9]", text)
        assert not re.search(r"(?i)bearer\s+\w", text)
        assert not re.search(r"[\w.%+\-]+@[\w.\-]+\.[A-Za-z]{2,}", text)
        assert recorded()["data"]["client_key"] == "REDACTED"


class TestSearch:
    @respx.mock
    async def test_success_maps_the_real_response(self, monkeypatch: pytest.MonkeyPatch) -> None:
        route = respx.post(OFFER_REQUESTS).mock(return_value=httpx.Response(201, json=recorded()))
        async with connector(monkeypatch) as c:
            offers = await c.search(query())

        assert len(offers) == 6
        owners = {o.slices[0].segments[0].marketing_carrier.iata_code for o in offers}
        expected = {
            o["slices"][0]["segments"][0]["marketing_carrier"]["iata_code"]
            for o in recorded()["data"]["offers"]
        }
        assert owners == expected and "ZZ" in owners  # Duffel Airways plus airline sandboxes
        assert all(o.supplier_id == "duffel" and o.is_test_data for o in offers)

        sent = route.calls.last.request
        assert sent.headers["Duffel-Version"] == "v2"
        assert sent.headers["Authorization"] == f"Bearer {TOKEN}"
        assert sent.url.params["supplier_timeout"] == "5000"
        assert json.loads(sent.content)["data"]["slices"][0]["origin"] == "LHR"

    @respx.mock
    async def test_times_are_airport_local(self, monkeypatch: pytest.MonkeyPatch) -> None:
        respx.post(OFFER_REQUESTS).mock(return_value=httpx.Response(201, json=recorded()))
        async with connector(monkeypatch) as c:
            offers = await c.search(query())
        for offer in offers:
            for sl in offer.slices:
                seg = sl.segments[0]
                assert seg.departure_at.tzinfo is not None
                # Elapsed time between local timestamps equals the flight's own duration.
                if len(sl.segments) == 1:
                    assert sl.journey_minutes == seg.duration_minutes
        lhr = offers[0].slices[0].segments[0]
        assert lhr.origin == "LHR" and lhr.departure_at.utcoffset() == timedelta(0)  # GMT in Nov

    @respx.mock
    async def test_prices_are_in_inr_with_fx_audit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        raw = recorded()["data"]["offers"][0]
        respx.post(OFFER_REQUESTS).mock(return_value=httpx.Response(201, json=recorded()))
        async with connector(monkeypatch) as c:
            offer = (await c.search(query()))[0]
        assert offer.fx_audit is not None
        assert offer.fx_audit.original_amount == Decimal(raw["total_amount"])
        assert offer.fx_audit.original_currency == "USD" and offer.fx_audit.fx_rate == USD_INR
        assert offer.total_inr == (Decimal(raw["total_amount"]) * USD_INR).quantize(Decimal("0.01"))
        assert offer.price.currency == "INR" and offer.price.net_total == offer.total_inr

    @respx.mock
    async def test_baggage_weight_unknown_not_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        respx.post(OFFER_REQUESTS).mock(return_value=httpx.Response(201, json=recorded()))
        async with connector(monkeypatch) as c:
            offers = await c.search(query())
        assert all(o.baggage.checked_kg == "UNKNOWN" for o in offers)  # Duffel gives pieces only

    @respx.mock
    async def test_empty_result(self, monkeypatch: pytest.MonkeyPatch) -> None:
        body = recorded()
        body["data"]["offers"] = []
        respx.post(OFFER_REQUESTS).mock(return_value=httpx.Response(201, json=body))
        async with connector(monkeypatch) as c:
            assert await c.search(query()) == []

    @respx.mock
    async def test_401(self, monkeypatch: pytest.MonkeyPatch) -> None:
        respx.post(OFFER_REQUESTS).mock(
            return_value=httpx.Response(
                401, json={"errors": [{"code": "unauthorized", "title": "Invalid access token"}]}
            )
        )
        async with connector(monkeypatch) as c:
            with pytest.raises(SupplierAuthError):
                await c.search(query())

    @respx.mock
    async def test_429(self, monkeypatch: pytest.MonkeyPatch) -> None:
        respx.post(OFFER_REQUESTS).mock(
            return_value=httpx.Response(
                429,
                headers={"Retry-After": "3"},
                json={"errors": [{"code": "rate_limit_exceeded"}]},
            )
        )
        async with connector(monkeypatch) as c:
            with pytest.raises(SupplierRateLimited) as exc:
                await c.search(query())
        assert exc.value.retry_after == 3

    @respx.mock
    async def test_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        respx.post(OFFER_REQUESTS).mock(side_effect=httpx.ReadTimeout("slow"))
        async with connector(monkeypatch) as c:
            with pytest.raises(SupplierTimeout):
                await c.search(query())

    @pytest.mark.parametrize(
        "response",
        [
            httpx.Response(201, text="<html>gateway hiccup</html>"),
            httpx.Response(201, json={"unexpected": True}),
            httpx.Response(201, json={"data": {"offers": "nope"}}),
        ],
    )
    @respx.mock
    async def test_malformed_response(
        self, monkeypatch: pytest.MonkeyPatch, response: httpx.Response
    ) -> None:
        respx.post(OFFER_REQUESTS).mock(return_value=response)
        async with connector(monkeypatch) as c:
            with pytest.raises(SupplierBadResponse):
                await c.search(query())

    @respx.mock
    async def test_one_bad_offer_is_skipped_not_fatal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        body = recorded()
        del body["data"]["offers"][0]["slices"]
        respx.post(OFFER_REQUESTS).mock(return_value=httpx.Response(201, json=body))
        async with connector(monkeypatch) as c:
            assert len(await c.search(query())) == 5

    @respx.mock
    async def test_error_bodies_are_redacted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        respx.post(OFFER_REQUESTS).mock(
            return_value=httpx.Response(500, text=f"upstream echoed Authorization: Bearer {TOKEN}")
        )
        async with connector(monkeypatch) as c:
            with pytest.raises(SupplierBadResponse) as exc:
                await c.search(query())
        assert TOKEN not in str(exc.value) and "REDACTED" in str(exc.value)


class TestRepriceAndBook:
    @respx.mock
    async def test_price_change_on_reprice(self, monkeypatch: pytest.MonkeyPatch) -> None:
        respx.post(OFFER_REQUESTS).mock(return_value=httpx.Response(201, json=recorded()))
        async with connector(monkeypatch) as c:
            offer = (await c.search(query()))[0]
            raw = copy.deepcopy(recorded()["data"]["offers"][0])
            raw["total_amount"] = str(Decimal(raw["total_amount"]) + Decimal("25.00"))
            respx.get(f"{BASE}/air/offers/{offer.supplier_offer_ref}").mock(
                return_value=httpx.Response(200, json={"data": raw})
            )
            with pytest.raises(PriceChanged) as exc:
                await c.price(offer)
        assert exc.value.new_price > exc.value.old_price

    @respx.mock
    async def test_price_change_reaches_booking_as_customer_prices(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        respx.post(OFFER_REQUESTS).mock(return_value=httpx.Response(201, json=recorded()))
        async with connector(monkeypatch) as c:
            offer = (await c.search(query()))[0]
            price = offer.price.model_copy(update={"agency_markup": Decimal("199")})
            offer = offer.model_copy(update={"price": price})
            raw = copy.deepcopy(recorded()["data"]["offers"][0])
            raw["total_amount"] = str(Decimal(raw["total_amount"]) + Decimal("25.00"))
            respx.get(f"{BASE}/air/offers/{offer.supplier_offer_ref}").mock(
                return_value=httpx.Response(200, json={"data": raw})
            )
            svc = BookingService(InMemoryBookingRepository(), {"duffel": c}, FakePaymentGateway())
            with pytest.raises(PriceChangedError) as exc:
                await svc.prepare(
                    tenant_id="t",
                    offer=offer,
                    idempotency_key="k",
                    actor="a",
                    passengers=[
                        Passenger(
                            pax_type=PassengerType.ADULT,
                            given_name="A",
                            family_name="B",
                            date_of_birth=date(1990, 1, 1),
                        )
                    ],
                )
        assert exc.value.old_price == offer.total_inr + Decimal("199")

    async def test_non_test_token_refused_without_echoing_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        live = "duffel_live_should_never_be_printed_1234"
        with pytest.raises(DuffelTokenRefused) as exc:
            connector(monkeypatch, token=live)
        assert live not in str(exc.value)

    @respx.mock
    async def test_booking_blocked_until_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BOOKING_LIVE_ENABLED", raising=False)
        respx.post(OFFER_REQUESTS).mock(return_value=httpx.Response(201, json=recorded()))
        async with connector(monkeypatch) as c:
            offer = (await c.search(query()))[0]
            priced = await self._priced(c, offer)
            with pytest.raises(BookingDisabled):
                await c.book(priced, [self._pax()], "idem-1", DuffelBalancePayment())

    @respx.mock
    async def test_balance_payment_in_airline_currency(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BOOKING_LIVE_ENABLED", "true")
        respx.post(OFFER_REQUESTS).mock(return_value=httpx.Response(201, json=recorded()))
        order = respx.post(f"{BASE}/air/orders").mock(
            return_value=httpx.Response(
                201, json={"data": {"id": "ord_test", "booking_reference": "ZZTEST"}}
            )
        )
        async with connector(monkeypatch) as c:
            offer = (await c.search(query()))[0]
            priced = await self._priced(c, offer)
            booking = await c.book(priced, [self._pax()], "idem-2", DuffelBalancePayment())
        sent = json.loads(order.calls.last.request.content)["data"]
        assert sent["payments"] == [
            {
                "type": "balance",
                "amount": recorded()["data"]["offers"][0]["total_amount"],
                "currency": "USD",
            }
        ]
        assert order.calls.last.request.headers["Idempotency-Key"] == "idem-2"
        assert booking.supplier_booking_ref == "ord_test"

    @staticmethod
    def _pax() -> Passenger:
        return Passenger(
            pax_type=PassengerType.ADULT,
            given_name="Test",
            family_name="Pax",
            date_of_birth=date(1990, 1, 1),
        )

    @staticmethod
    async def _priced(c: DuffelConnector, offer: Any) -> Any:
        raw = recorded()["data"]["offers"][0]
        respx.get(f"{BASE}/air/offers/{offer.supplier_offer_ref}").mock(
            return_value=httpx.Response(200, json={"data": raw})
        )
        return await c.price(offer)


class TestAlongsideOtherSuppliers:
    @respx.mock
    async def test_unreachable_duffel_still_returns_other_suppliers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        respx.post(OFFER_REQUESTS).mock(side_effect=httpx.ConnectError("no route to host"))
        sims = [SimulatedConnector(p, latency_s=(0, 0), failure_rate=0) for p in PROFILES.values()]
        duffel = connector(monkeypatch)
        orch = SearchOrchestrator(connectors=[duffel, *sims])
        async with duffel:
            q = SearchQuery(
                origin="DEL", destination="BOM", departure_date=date.today() + timedelta(days=30)
            )
            done = [e async for e in orch.search(q) if isinstance(e, SearchComplete)][0]
        assert "duffel" in done.suppliers_failed
        assert set(done.suppliers_succeeded) == set(PROFILES)
        assert done.groups

    def test_suppliers_enabled_switch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from booking_agent.api.app import _build_connectors

        monkeypatch.setenv("DUFFEL_ACCESS_TOKEN", TOKEN)
        monkeypatch.setenv("SUPPLIERS_ENABLED", "sim_gds")
        status: dict[str, str] = {}
        ids = [c.capabilities.supplier_id for c in _build_connectors(status)]
        assert ids == ["sim_gds"] and status["duffel"].startswith("off")

        monkeypatch.setenv("SUPPLIERS_ENABLED", "all")
        monkeypatch.delenv("DUFFEL_ACCESS_TOKEN")
        status = {}
        ids = [c.capabilities.supplier_id for c in _build_connectors(status)]
        assert "duffel" not in ids and set(PROFILES) <= set(ids)
        assert status["duffel"] == "off (no DUFFEL_ACCESS_TOKEN)"

        monkeypatch.setenv("SUPPLIERS_ENABLED", "simulated_a,sim_ota")
        status = {}
        ids = [c.capabilities.supplier_id for c in _build_connectors(status)]
        assert ids == ["sim_ota"] and status["simulated_a"].startswith("unknown supplier id")

    @respx.mock
    async def test_api_labels_duffel_offers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from httpx import ASGITransport, AsyncClient

        from booking_agent.api.app import create_app

        respx.post(OFFER_REQUESTS).mock(return_value=httpx.Response(201, json=recorded()))
        respx.route(host="test").pass_through()
        app = create_app(connectors=[connector(monkeypatch)], redis=fakeredis.aioredis.FakeRedis())
        async with (
            app.state.connectors["duffel"],
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c,
        ):
            h = {"X-API-Key": "dev-key"}
            sid = (
                await c.post(
                    "/v1/search",
                    headers=h,
                    json={
                        "origin": "LHR",
                        "destination": "JFK",
                        "departure_date": (date.today() + timedelta(days=30)).isoformat(),
                    },
                )
            ).json()["search_id"]
            text = (await c.get(f"/v1/search/{sid}/stream", headers=h)).text
        done = next(
            json.loads(line[6:])
            for line in text.splitlines()
            if line.startswith("data: {") and '"search_complete"' in line
        )
        primary = done["groups"][0]["primary"]
        assert primary["supplier_label"] == "Duffel (sandbox)" and primary["is_test_data"] is True


class TestFx:
    @respx.mock
    async def test_frankfurter_rate_is_fetched_once_and_cached(self) -> None:
        from booking_agent.connectors.duffel_fx import FrankfurterFxProvider

        route = respx.get(FrankfurterFxProvider.URL).mock(
            return_value=httpx.Response(200, json={"base": "USD", "rates": {"INR": 94.96}})
        )
        fx = FrankfurterFxProvider()
        async with httpx.AsyncClient() as c:
            rate, _ = await fx.get_rate_to_inr("USD", c)
            again, _ = await fx.get_rate_to_inr("USD", c)
        assert rate == again == Decimal("94.96") and route.call_count == 1
        assert route.calls.last.request.url.params["base"] == "USD"

    @respx.mock
    async def test_fx_failure_is_a_clear_supplier_error(self) -> None:
        from booking_agent.connectors.duffel_fx import FrankfurterFxProvider

        respx.get(FrankfurterFxProvider.URL).mock(return_value=httpx.Response(301))
        async with httpx.AsyncClient() as c:
            with pytest.raises(SupplierBadResponse, match="no USD->INR rate"):
                await FrankfurterFxProvider().get_rate_to_inr("USD", c)
