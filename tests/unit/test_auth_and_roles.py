"""
Demo login, sessions, role rules and the bookings/dashboard endpoints.

The key guarantee: no response an Agent can receive contains net fare or markup
fields (checked recursively with find_forbidden_keys); an Admin's does.
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
from booking_agent.auth.passwords import hash_password, verify_password
from booking_agent.auth.principal import Principal
from booking_agent.auth.sessions import (
    SESSION_COOKIE,
    SessionExpired,
    SessionInvalid,
    decode_session_token,
    issue_session_token,
)
from booking_agent.auth.users import seed_demo_users
from booking_agent.dashboard.demo import seed_demo_bookings
from booking_agent.simulated.connector import SimulatedConnector
from booking_agent.simulated.pricing import PROFILES

AGENT_PW = "agent-test-pw"
ADMIN_PW = "admin-test-pw"
SECRET = "test-secret-not-for-production-0123456789"
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
    """App in demo mode with seeded users and bookings (in-memory stores)."""
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("DEMO_AGENT_PASSWORD", AGENT_PW)
    monkeypatch.setenv("DEMO_ADMIN_PASSWORD", ADMIN_PW)
    monkeypatch.setenv("AUTH_SECRET", SECRET)
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
    await seed_demo_users(app.state.users)
    await seed_demo_bookings(app.state.booking_service.repository)
    yield app


@asynccontextmanager
async def _client(app: Any, role: str | None) -> AsyncIterator[AsyncClient]:
    """A client for `app`, signed in as `role` (or signed out when None)."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        if role:
            pw = AGENT_PW if role == "agent" else ADMIN_PW
            r = await c.post("/v1/auth/login", json={"email": f"{role}@demo.test", "password": pw})
            assert r.status_code == 200, r.text
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


# ── passwords and tokens ──────────────────────────────────────────────────────


class TestPasswordsAndTokens:
    def test_hash_verifies_and_is_salted(self) -> None:
        h1, h2 = hash_password("s3cret"), hash_password("s3cret")
        assert h1 != h2 and h1.startswith("scrypt$")
        assert verify_password("s3cret", h1) and not verify_password("nope", h1)
        assert not verify_password("s3cret", "garbage")

    def test_token_round_trip_expiry_and_tamper(self) -> None:
        p = Principal("u1", "a@b.c", "A", "agent", "t1", "session")
        assert decode_session_token(issue_session_token(p, SECRET), SECRET) == p
        with pytest.raises(SessionExpired):
            decode_session_token(issue_session_token(p, SECRET, hours=-1), SECRET)
        with pytest.raises(SessionInvalid):
            decode_session_token(
                issue_session_token(p, SECRET), "another-secret-that-is-long-enough-123"
            )
        with pytest.raises(SessionInvalid):
            decode_session_token("not.a.token", SECRET)


# ── login ─────────────────────────────────────────────────────────────────────


class TestLogin:
    async def test_login_success_sets_http_only_cookie(self, demo_app: Any) -> None:
        async with _client(demo_app, None) as c:
            r = await c.post(
                "/v1/auth/login", json={"email": "agent@demo.test", "password": AGENT_PW}
            )
            assert r.status_code == 200 and r.json()["role"] == "agent"
            cookie = r.headers["set-cookie"]
            assert SESSION_COOKIE in cookie and "HttpOnly" in cookie
            assert "samesite=lax" in cookie.lower()
            me = await c.get("/v1/auth/me")
            assert me.json() == {
                "email": "agent@demo.test",
                "name": "Demo Agent",
                "role": "agent",
                "tenant_id": "demo-agency",
            }

    @pytest.mark.parametrize(
        "email,password",
        [
            ("agent@demo.test", "wrong"),
            ("nobody@demo.test", AGENT_PW),
        ],
    )
    async def test_login_failure_is_generic_401(
        self, demo_app: Any, email: str, password: str
    ) -> None:
        async with _client(demo_app, None) as c:
            r = await c.post("/v1/auth/login", json={"email": email, "password": password})
            assert r.status_code == 401
            assert r.json()["detail"] == "Invalid email or password."
            assert "set-cookie" not in r.headers

    async def test_login_is_throttled(self, demo_app: Any) -> None:
        async with _client(demo_app, None) as c:
            codes = [
                (
                    await c.post(
                        "/v1/auth/login", json={"email": "agent@demo.test", "password": "x"}
                    )
                ).status_code
                for _ in range(11)
            ]
        assert codes[:10] == [401] * 10 and codes[10] == 429

    async def test_demo_button_signs_in_as_agent_only(self, demo_app: Any) -> None:
        async with _client(demo_app, None) as c:
            r = await c.post("/v1/auth/demo")
            assert r.status_code == 200 and r.json()["role"] == "agent"

    async def test_demo_button_disabled_outside_demo_mode(
        self, demo_app: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DEMO_MODE", "false")
        async with _client(demo_app, None) as c:
            assert (await c.post("/v1/auth/demo")).status_code == 404

    async def test_config_lists_accounts_without_passwords(self, demo_app: Any) -> None:
        async with _client(demo_app, None) as c:
            body = (await c.get("/v1/auth/config")).json()
        assert [a["email"] for a in body["demo_accounts"]] == ["agent@demo.test", "admin@demo.test"]
        assert AGENT_PW not in json.dumps(body) and ADMIN_PW not in json.dumps(body)

    async def test_logout_ends_the_session(self, demo_app: Any) -> None:
        async with _client(demo_app, "agent") as c:
            assert (await c.post("/v1/auth/logout")).status_code == 204
            assert (await c.get("/v1/auth/me")).status_code == 401


class TestProtection:
    @pytest.mark.parametrize(
        "path", ["/v1/bookings", "/v1/dashboard/summary", "/v1/dashboard/charts", "/v1/auth/me"]
    )
    async def test_signed_out_gets_auth_required(self, demo_app: Any, path: str) -> None:
        async with _client(demo_app, None) as c:
            r = await c.get(path)
        assert r.status_code == 401 and r.json()["code"] == "auth_required"

    async def test_expired_session_is_reported_as_expired(self, demo_app: Any) -> None:
        p = Principal("u1", "agent@demo.test", "A", "agent", "demo-agency", "session")
        async with _client(demo_app, None) as c:
            c.cookies.set(SESSION_COOKIE, issue_session_token(p, SECRET, hours=-1))
            r = await c.get("/v1/bookings")
        assert r.status_code == 401 and r.json()["code"] == "session_expired"

    async def test_forged_session_is_rejected(self, demo_app: Any) -> None:
        p = Principal("u1", "x", "X", "admin", "demo-agency", "session")
        async with _client(demo_app, None) as c:
            c.cookies.set(
                SESSION_COOKIE, issue_session_token(p, "attacker-secret-0123456789abcdef")
            )
            r = await c.get("/v1/bookings")
        assert r.status_code == 401 and r.json()["code"] == "session_invalid"

    async def test_no_default_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("API_KEY", raising=False)
        assert mw_mod._build_registry() == {}


# ── role rules ────────────────────────────────────────────────────────────────


async def _everything_a_user_sees(c: AsyncClient) -> dict[str, Any]:
    page = (await c.get("/v1/bookings")).json()
    first = page["items"][0]["booking_id"]
    events = await _search_stream(c)
    complete = next(e for e in events if e["type"] == "search_complete")
    offer_id = complete["groups"][0]["primary"]["offer_id"]
    chat = await c.post("/v1/agent/chat/stream", json={"message": "DEL to BOM"})
    return {
        "me": (await c.get("/v1/auth/me")).json(),
        "bookings": page,
        "detail": (await c.get(f"/v1/bookings/{first}")).json(),
        "summary": (await c.get("/v1/dashboard/summary")).json(),
        "charts": (await c.get("/v1/dashboard/charts")).json(),
        "search_stream": events,
        "reprice": (await c.post(f"/v1/offers/{offer_id}/price")).json(),
        "assistant": [json.loads(x[6:]) for x in chat.text.splitlines() if x.startswith("data: ")],
    }


class TestRoleRules:
    async def test_agent_never_receives_net_or_markup(self, demo_app: Any) -> None:
        async with _client(demo_app, "agent") as c:
            seen = await _everything_a_user_sees(c)
        leaks = find_forbidden_keys(seen)
        assert leaks == [], f"net/markup leaked to an Agent: {leaks[:10]}"
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

    async def test_agent_cannot_read_admins_search(self, demo_app: Any) -> None:
        async with _client(demo_app, "admin") as admin:
            sid = (
                await admin.post(
                    "/v1/search",
                    json={"origin": "DEL", "destination": "BOM", "departure_date": DAY},
                )
            ).json()["search_id"]
        async with _client(demo_app, "agent") as agent:
            assert (await agent.get(f"/v1/search/{sid}/stream")).status_code == 404
            assert (await agent.get(f"/v1/search/{sid}")).status_code == 404

    async def test_agent_blocked_from_hotel_net_prices(self, demo_app: Any) -> None:
        async with _client(demo_app, "agent") as c:
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
