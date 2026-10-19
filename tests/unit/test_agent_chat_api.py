"""
Agent chat API: offer cards in the response, UI-only booking, provider errors.
Uses a scripted LLM client, FakeConnector and fakeredis (no network).
"""

import json
from dataclasses import dataclass, field
from typing import Any

import fakeredis.aioredis
from httpx import ASGITransport, AsyncClient

from booking_agent.agent.llm_adapters import LLMProviderError
from booking_agent.api.app import create_app
from booking_agent.connectors.fake import FakeConnector

HEADERS = {"X-API-Key": "dev-key"}

SEARCH_INPUT = {
    "origin": "DEL",
    "destination": "BOM",
    "departure_date": "2027-09-01",
    "adults": 2,
}


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


@dataclass
class _ScriptedMessages:
    turns: list[Any] = field(default_factory=list)  # _Msg or Exception
    calls: list[list[dict[str, Any]]] = field(default_factory=list)

    async def create(self, **kwargs: Any) -> _Msg:
        self.calls.append(list(kwargs["messages"]))
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        return turn


class _ScriptedClient:
    def __init__(self, turns: list[Any]) -> None:
        self.messages = _ScriptedMessages(turns=list(turns))


def _search_turn() -> _Msg:
    return _Msg(
        content=[_ToolUse(id="tu_1", name="search_flights", input=SEARCH_INPUT)],
        stop_reason="tool_use",
    )


def _app(turns: list[Any]):
    client = _ScriptedClient(turns)
    app = create_app(
        connectors=[
            FakeConnector(
                supplier_id="fake",
                max_retries=1,
                retry_min_wait=0.0,
                retry_max_wait=0.0,
            )
        ],
        redis=fakeredis.aioredis.FakeRedis(),
        anthropic_client=client,
    )
    return app, client


async def _post(app, path: str, body: dict[str, Any]):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.post(path, json=body, headers=HEADERS)


def _adult(given: str) -> dict[str, Any]:
    return {
        "pax_type": "ADULT",
        "given_name": given,
        "family_name": "Sharma",
        "date_of_birth": "1990-01-01",
    }


class TestChatOffers:
    async def test_search_turn_returns_offer_cards(self):
        app, _ = _app([_search_turn(), _Msg(content=[_Text("Here are your options.")])])
        r = await _post(app, "/v1/agent/chat", {"message": "DEL to BOM, 2 adults"})
        assert r.status_code == 200
        data = r.json()
        assert data["reply"] == "Here are your options."
        assert len(data["offers"]) >= 1
        card = data["offers"][0]
        assert card["rank"] == 1
        assert card["pax_count"] == 2
        assert card["route"] == "DEL → BOM"
        assert card["customer_price_inr"]
        assert "confirmation_token" not in json.dumps(data)

    async def test_turn_without_search_returns_no_offers(self):
        app, _ = _app(
            [
                _search_turn(),
                _Msg(content=[_Text("Found some.")]),
                _Msg(content=[_Text("The first one is cheapest.")]),
            ]
        )
        first = (await _post(app, "/v1/agent/chat", {"message": "search"})).json()
        r = await _post(
            app,
            "/v1/agent/chat",
            {"message": "which is cheapest?", "session_id": first["session_id"]},
        )
        assert r.status_code == 200
        assert r.json()["offers"] == []


class TestUiBookingFlow:
    async def test_book_offer_from_chat_through_ui_endpoints(self):
        app, client = _app([_search_turn(), _Msg(content=[_Text("Options above.")])])
        chat = (await _post(app, "/v1/agent/chat", {"message": "search"})).json()
        offer_id = chat["offers"][0]["offer_id"]

        prep = await _post(
            app,
            "/v1/bookings/prepare",
            {
                "offer_id": offer_id,
                "idempotency_key": "ui-test-1",
                "passengers": [_adult("Asha"), _adult("Ravi")],
            },
        )
        assert prep.status_code == 201, prep.text
        token = prep.json()["confirmation_token"]

        conf = await _post(
            app,
            f"/v1/bookings/{prep.json()['booking_id']}/confirm",
            {"confirmation_token": token},
        )
        assert conf.status_code == 200, conf.text
        assert conf.json()["state"] == "TICKETED"
        assert conf.json()["pnr"]

        # Passenger names never went to the LLM.
        sent_to_llm = json.dumps(client.messages.calls, default=str)
        assert "Asha" not in sent_to_llm and "Ravi" not in sent_to_llm

    async def test_prepare_rejects_wrong_passenger_count(self):
        app, _ = _app([_search_turn(), _Msg(content=[_Text("Options above.")])])
        chat = (await _post(app, "/v1/agent/chat", {"message": "search"})).json()

        prep = await _post(
            app,
            "/v1/bookings/prepare",
            {
                "offer_id": chat["offers"][0]["offer_id"],
                "idempotency_key": "ui-test-2",
                "passengers": [_adult("Asha")],
            },
        )
        assert prep.status_code == 422
        assert "2 passenger" in prep.json()["detail"]

    async def test_prepare_rejects_wrong_passenger_types(self):
        app, _ = _app([_search_turn(), _Msg(content=[_Text("Options above.")])])
        chat = (await _post(app, "/v1/agent/chat", {"message": "search"})).json()
        assert chat["offers"][0]["pax_counts"] == {"adults": 2, "children": 0, "infants": 0}
        child = {**_adult("Mira"), "pax_type": "CHILD", "date_of_birth": "2019-05-01"}

        prep = await _post(
            app,
            "/v1/bookings/prepare",
            {
                "offer_id": chat["offers"][0]["offer_id"],
                "idempotency_key": "ui-test-3",
                "passengers": [_adult("Asha"), child],
            },
        )
        assert prep.status_code == 422
        assert "priced for 2 adults, got 1 adult, 1 child" in prep.json()["detail"]


class TestProviderErrors:
    async def test_provider_error_is_502_and_turn_is_rolled_back(self):
        app, client = _app(
            [
                LLMProviderError("Groq", 404, "model not found"),
                _Msg(content=[_Text("Hello again.")]),
            ]
        )
        r = await _post(app, "/v1/agent/chat", {"message": "first"})
        assert r.status_code == 502
        assert "model not found" in r.json()["detail"]

        session_id = next(iter(app.state.agent_sessions))
        r2 = await _post(app, "/v1/agent/chat", {"message": "second", "session_id": session_id})
        assert r2.status_code == 200
        # The failed "first" message was dropped, so history has no dangling user turn.
        assert [m["content"] for m in client.messages.calls[-1]] == ["second"]
