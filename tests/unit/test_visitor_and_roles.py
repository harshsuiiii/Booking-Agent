"""
No login: the built-in visitor, role rules and the bookings/dashboard endpoints.

The key guarantee: no response the visitor (an Agent) can receive contains net fare or
markup fields (checked recursively with find_forbidden_keys); an Admin's (API key) does.
"""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import fakeredis.aioredis
import pytest
from httpx import ASGITransport, AsyncClient

from booking_agent.api import middleware as mw_mod
from booking_agent.api.app import create_app
from booking_agent.api.views import find_forbidden_keys
from booking_agent.dashboard.demo import seed_demo_bookings
from booking_agent.simulated.connector import SimulatedConnector
from booking_agent.simulated.pricing import PROFILES

ADMIN_KEY = "admin-test-key-demo-agency"
DAY = (date.today() + timedelta(days=21)).isoformat()


@dataclass
class _Text:
    text: str
    type: str = "text"


@dataclass
class _ToolUse:
    id: str
    name: str
    input: dict[str, Any]
    type: str = "tool_use"


@dataclass
class _Msg:
    content: list[Any]
    stop_reason: str = "end_turn"


class _ScriptedMessages:
    def __init__(self) -> None:
        self.turn = 0

    async def create(self, **kwargs: Any) -> _Msg:
        self.turn += 1
        if self.turn % 2 == 1:
            return _Msg(
                [
                    _ToolUse(
                        id=f"t{self.turn}",
                        name="search_flights",
                        input={"origin": "DEL", "destination": "BOM", "departure_date": DAY},
                    )
                ],
                "tool_use",
            )
        return _Msg([_Text("Here are the options.")])


class _Client:
    def __init__(self) -> None:
        self.messages = _ScriptedMessages()


@pytest.fixture
async def demo_app(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    """App in demo mode with seeded bookings (in-memory stores)."""
    monkeypatch.setenv("DEMO_MODE", "true")
    # An Admin API key for the demo agency (scripts are the only way to be Admin).
    monkeypatch.setitem(mw_mod._REGISTRY, mw_mod._hash_key(ADMIN_KEY), "demo-agency")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    app = create_app(
        connectors=[
            SimulatedConnector(p, latency_s=(0, 0), failure_rate=0, price_change_rate=0)
            for p in PROFILES.values()
        ],
        redis=fakeredis.aioredis.FakeRedis(),
        anthropic_client=_Client(),
    )
    # httpx's ASGI transport doesn't run lifespan, so seed like startup would.
    await seed_demo_bookings(app.state.booking_service.repository)
    yield app


@asynccontextmanager
async def _client(app: Any, role: str | None) -> AsyncIterator[AsyncClient]:
    """A client for `app`: the visitor (role "agent" or None), or Admin via the API key."""
    headers = {"X-API-Key": ADMIN_KEY} if role == "admin" else {}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers=headers
    ) as c:
        yield c


async def _search_stream(c: AsyncClient) -> list[dict[str, Any]]:
    r = await c.post(
        "/v1/search", json={"origin": "DEL", "destination": "BOM", "departure_date": DAY}
    )
    sid = r.json()["search_id"]
    r = await c.get(f"/v1/search/{sid}/stream")
    return [
        json.loads(line[6:])
        for line in r.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]


# ── no login ──────────────────────────────────────────────────────────────────


class TestNoLogin:
    @pytest.mark.parametrize(
        "path", ["/v1/bookings", "/v1/dashboard/summary", "/v1/dashboard/charts"]
    )
    async def test_visitor_needs_no_credentials(self, demo_app: Any, path: str) -> None:
        async with _client(demo_app, None) as c:
            r = await c.get(path)
        assert r.status_code == 200, r.text

    @pytest.mark.parametrize(
        "method,path",
        [
            ("POST", "/v1/auth/login"),
            ("POST", "/v1/auth/demo"),
            ("POST", "/v1/auth/logout"),
            ("GET", "/v1/auth/me"),
            ("GET", "/v1/auth/config"),
        ],
    )
    async def test_login_endpoints_are_gone(self, demo_app: Any, method: str, path: str) -> None:
        async with _client(demo_app, None) as c:
            r = await c.request(method, path)
        assert r.status_code in (404, 405)

    async def test_no_cookie_is_ever_set(self, demo_app: Any) -> None:
        async with _client(demo_app, None) as c:
            responses = [await c.get("/v1/bookings"), await c.get("/v1/dashboard/summary")]
        assert all("set-cookie" not in r.headers for r in responses)

    async def test_wrong_api_key_is_still_rejected(self, demo_app: Any) -> None:
        async with _client(demo_app, None) as c:
            r = await c.get("/v1/bookings", headers={"X-API-Key": "wrong"})
        assert r.status_code == 401 and r.json()["code"] == "invalid_api_key"

    async def test_no_default_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("API_KEY", raising=False)
        assert mw_mod._build_registry() == {}

    async def test_rate_limit_is_per_visitor_ip(self) -> None:
        limiter = mw_mod.RateLimiter(max_requests=2, window_seconds=60)
        app = create_app(
            connectors=[SimulatedConnector(PROFILES["sim_gds"], latency_s=(0, 0), failure_rate=0)],
            redis=fakeredis.aioredis.FakeRedis(),
            rate_limiter=limiter,
        )
        transport_a = ASGITransport(app=app, client=("10.0.0.1", 1111))
        transport_b = ASGITransport(app=app, client=("10.0.0.2", 2222))
        async with (
            AsyncClient(transport=transport_a, base_url="http://test") as a,
            AsyncClient(transport=transport_b, base_url="http://test") as b,
        ):
            codes_a = [(await a.get("/v1/bookings")).status_code for _ in range(3)]
            code_b = (await b.get("/v1/bookings")).status_code
        assert codes_a == [200, 200, 429]
        assert code_b == 200  # another visitor isn't throttled by the first one


# ── role rules ────────────────────────────────────────────────────────────────


async def _everything_a_user_sees(c: AsyncClient) -> dict[str, Any]:
    page = (await c.get("/v1/bookings")).json()
    first = page["items"][0]["booking_id"]
    events = await _search_stream(c)
    complete = next(e for e in events if e["type"] == "search_complete")
    offer_id = complete["groups"][0]["primary"]["offer_id"]
    chat = await c.post("/v1/agent/chat/stream", json={"message": "DEL to BOM"})
    return {
        "bookings": page,
        "detail": (await c.get(f"/v1/bookings/{first}")).json(),
        "summary": (await c.get("/v1/dashboard/summary")).json(),
        "charts": (await c.get("/v1/dashboard/charts")).json(),
        "search_stream": events,
        "reprice": (await c.post(f"/v1/offers/{offer_id}/price")).json(),
        "assistant": [json.loads(x[6:]) for x in chat.text.splitlines() if x.startswith("data: ")],
    }


class TestRoleRules:
    async def test_visitor_never_receives_net_or_markup(self, demo_app: Any) -> None:
        async with _client(demo_app, None) as c:
            seen = await _everything_a_user_sees(c)
        leaks = find_forbidden_keys(seen)
        assert leaks == [], f"net/markup leaked to the visitor: {leaks[:10]}"
        # ...but they still get what they need: sell prices.
        assert seen["bookings"]["items"][0]["price"]["sell_inr"]
        assert seen["detail"]["price"]["sell_inr"] and seen["detail"]["price"]["taxes_total"]

    async def test_admin_receives_net_markup_and_supplier_performance(self, demo_app: Any) -> None:
        async with _client(demo_app, "admin") as c:
            seen = await _everything_a_user_sees(c)
        item = seen["bookings"]["items"][0]["price"]
        assert {"net_inr", "markup_inr", "sell_inr"} <= set(item)
        assert "markup_this_month_inr" in seen["summary"]
        assert "supplier_performance" in seen["charts"]["by_supplier"][0]
        assert {"base_fare", "supplier_fee", "net_inr", "markup_inr"} <= set(
            seen["detail"]["price"]
        )
        offer = next(e for e in seen["search_stream"] if e["type"] == "search_complete")
        assert "net_inr" in offer["groups"][0]["primary"]

    async def test_markup_rules_apply_to_searches(self, demo_app: Any) -> None:
        async with _client(demo_app, "admin") as c:
            events = await _search_stream(c)
        offer = next(e for e in events if e["type"] == "search_complete")["groups"][0]["primary"]
        assert float(offer["markup_inr"]) > 0
        assert float(offer["sell_inr"]) == pytest.approx(
            float(offer["net_inr"]) + float(offer["markup_inr"])
        )

    async def test_visitor_cannot_read_admins_search(self, demo_app: Any) -> None:
        async with _client(demo_app, "admin") as admin:
            sid = (
                await admin.post(
                    "/v1/search",
                    json={"origin": "DEL", "destination": "BOM", "departure_date": DAY},
                )
            ).json()["search_id"]
        async with _client(demo_app, None) as visitor:
            assert (await visitor.get(f"/v1/search/{sid}/stream")).status_code == 404
            assert (await visitor.get(f"/v1/search/{sid}")).status_code == 404

    @pytest.mark.parametrize(
        "method,path,body",
        [
            ("GET", "/v1/admin/rules", None),
            ("GET", "/v1/admin/rules/any-id", None),
            ("POST", "/v1/admin/rules", {}),
            ("PUT", "/v1/admin/rules/any-id", {}),
            ("DELETE", "/v1/admin/rules/any-id", None),
        ],
    )
    async def test_visitor_cannot_read_or_change_markup_rules(
        self, demo_app: Any, method: str, path: str, body: dict[str, Any] | None
    ) -> None:
        async with _client(demo_app, None) as c:
            r = await c.request(method, path, json=body)
        assert r.status_code == 403

    async def test_admin_key_reaches_markup_rules(self, demo_app: Any) -> None:
        async with _client(demo_app, "admin") as c:
            r = await c.get("/v1/admin/rules")
        assert r.status_code != 403

    async def test_visitor_blocked_from_hotel_net_prices(self, demo_app: Any) -> None:
        async with _client(demo_app, None) as c:
            r = await c.post("/v1/hotels/search", json={})
        assert r.status_code == 403


# ── bookings and dashboard ────────────────────────────────────────────────────


class TestBookingsAndDashboard:
    async def test_seeded_demo_bookings_cover_every_status(self, demo_app: Any) -> None:
        async with _client(demo_app, "agent") as c:
            page = (await c.get("/v1/bookings", params={"page_size": 100})).json()
        assert page["total"] == 30
        assert {i["state"] for i in page["items"]} == {
            "DRAFT",
            "PRICED",
            "CONFIRMED_BY_USER",
            "PNR_CREATED",
            "PAYMENT_PENDING",
            "PAID",
            "TICKETED",
            "FAILED",
            "CANCELLED",
            "EXPIRED",
        }
        assert all(i["is_demo"] for i in page["items"])

    async def test_filters_and_pagination(self, demo_app: Any) -> None:
        async with _client(demo_app, "agent") as c:
            ticketed = (
                await c.get("/v1/bookings", params={"status": "ticketed", "page_size": 100})
            ).json()
            assert ticketed["total"] == 13
            assert {i["state"] for i in ticketed["items"]} == {"TICKETED"}

            ref = ticketed["items"][0]["reference"]
            by_ref = (await c.get("/v1/bookings", params={"q": ref.lower()})).json()
            assert [i["reference"] for i in by_ref["items"]] == [ref]

            lhr = (await c.get("/v1/bookings", params={"q": "LHR", "page_size": 100})).json()
            assert lhr["total"] > 0
            assert all("LHR" in (i["origin"], i["destination"]) for i in lhr["items"])

            d = ticketed["items"][0]["travel_date"]
            window = (
                await c.get("/v1/bookings", params={"from": d, "to": d, "page_size": 100})
            ).json()
            assert window["total"] >= 1 and all(i["travel_date"] == d for i in window["items"])

            p1 = (await c.get("/v1/bookings", params={"page": 1, "page_size": 7})).json()
            p5 = (await c.get("/v1/bookings", params={"page": 5, "page_size": 7})).json()
            assert len(p1["items"]) == 7 and len(p5["items"]) == 2 and p5["total"] == 30

            assert (await c.get("/v1/bookings", params={"status": "nope"})).status_code == 422

    async def test_other_tenants_bookings_are_invisible(self, demo_app: Any) -> None:
        async with AsyncClient(transport=ASGITransport(app=demo_app), base_url="http://test") as c:
            # API key belongs to tenant "default" in tests, not the demo agency.
            page = (await c.get("/v1/bookings", headers={"X-API-Key": "dev-key"})).json()
        assert page["total"] == 0

    async def test_booking_made_in_the_app_appears_immediately(self, demo_app: Any) -> None:
        async with _client(demo_app, "agent") as c:
            chat = await c.post("/v1/agent/chat/stream", json={"message": "DEL to BOM"})
            events = [json.loads(x[6:]) for x in chat.text.splitlines() if x.startswith("data: ")]
            offer = next(e for e in events if e["type"] == "offers")["offers"][0]
            prep = await c.post(
                "/v1/bookings/prepare",
                json={
                    "offer_id": offer["offer_id"],
                    "idempotency_key": "ui-dashboard-1",
                    "passengers": [
                        {
                            "pax_type": "ADULT",
                            "given_name": "Asha",
                            "family_name": "Verma",
                            "date_of_birth": "1990-01-01",
                        }
                    ],
                },
            )
            assert prep.status_code == 201, prep.text
            page = (await c.get("/v1/bookings", params={"q": "verma"})).json()
        assert page["total"] == 1
        item = page["items"][0]
        assert item["state"] == "PRICED" and not item["is_demo"]
        assert item["passenger"] == "A. Verma"
        assert float(item["price"]["sell_inr"]) == float(prep.json()["customer_price_inr"])

    async def test_dashboard_summary_and_charts(self, demo_app: Any) -> None:
        async with _client(demo_app, "agent") as c:
            await _search_stream(c)  # becomes a recent search
            s = (await c.get("/v1/dashboard/summary")).json()
            ch = (await c.get("/v1/dashboard/charts")).json()
        assert s["bookings_this_month"] >= 1 and float(s["sales_this_month_inr"]) > 0
        assert s["upcoming_trips"] == len(s["next_trips"]) or s["upcoming_trips"] > 5
        assert s["recent_searches"][0]["origin"] == "DEL"
        assert len(ch["bookings_over_time"]) == 30
        assert {x["supplier_id"] for x in ch["by_supplier"]} <= set(PROFILES)
