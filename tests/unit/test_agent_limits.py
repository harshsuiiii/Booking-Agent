"""
Agent reliability: round-trip cap, timeouts, rate limits, retries, streaming, health,
and small tool results. Stubbed LLM clients and the simulated supplier; no real calls.
"""

import asyncio
import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import fakeredis.aioredis
import httpx
import pytest
import respx
from httpx import ASGITransport, AsyncClient

from booking_agent.agent.llm_adapters import (
    GroqClientAdapter,
    LLMRateLimitError,
    LLMTimeoutError,
)
from booking_agent.agent.runner import AgentRunner, AgentTurnTimeoutError
from booking_agent.agent.session import AgentSession
from booking_agent.agent.tools import MODEL_TOP_N
from booking_agent.api.app import create_app
from booking_agent.orchestrator.search import SearchOrchestrator
from booking_agent.simulated.connector import SimulatedConnector
from booking_agent.simulated.pricing import PROFILES

HEADERS = {"X-API-Key": "dev-key"}
DAY = (date.today() + timedelta(days=21)).isoformat()
SEARCH = {"origin": "DEL", "destination": "BOM", "departure_date": DAY, "adults": 1}
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


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
class _Messages:
    """Scripted turns; a callable turn is awaited (to simulate slowness)."""

    turns: list[Any]
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def create(self, **kwargs: Any) -> _Msg:
        self.calls.append(kwargs)
        turn = self.turns.pop(0) if self.turns else _Msg([_Text("done")])
        if callable(turn):
            return await turn()
        if isinstance(turn, Exception):
            raise turn
        return turn


class _Client:
    def __init__(self, turns: list[Any]) -> None:
        self.messages = _Messages(turns=list(turns))


def _search_turn(i: int = 1) -> _Msg:
    return _Msg([_ToolUse(id=f"tu_{i}", name="search_flights", input=SEARCH)], "tool_use")


def _sim_orchestrator() -> SearchOrchestrator:
    return SearchOrchestrator(
        connectors=[
            SimulatedConnector(p, latency_s=(0, 0), failure_rate=0) for p in PROFILES.values()
        ]
    )


def _runner(turns: list[Any], **kw: Any) -> tuple[AgentRunner, _Client]:
    client = _Client(turns)
    return AgentRunner(client=client, orchestrator=_sim_orchestrator(), **kw), client


def _app(turns: list[Any]):
    client = _Client(turns)
    app = create_app(
        connectors=[
            SimulatedConnector(p, latency_s=(0, 0), failure_rate=0) for p in PROFILES.values()
        ],
        redis=fakeredis.aioredis.FakeRedis(),
        anthropic_client=client,
    )
    return app, client


async def _post(app, path: str, body: dict[str, Any]) -> httpx.Response:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.post(path, json=body, headers=HEADERS)


class TestRoundTripCap:
    async def test_model_that_keeps_calling_tools_is_stopped(self) -> None:
        runner, client = _runner([_search_turn(i) for i in range(10)], max_tool_rounds=3)
        reply = await runner.chat(AgentSession(tenant_id="t"), "loop forever")
        # 3 tool rounds + 1 forced final answer
        assert len(client.messages.calls) == 4
        assert client.messages.calls[-1]["tool_choice"] == {"type": "none"}
        assert all("tool_choice" not in c for c in client.messages.calls[:-1])
        assert "step limit" in reply

    async def test_normal_search_uses_two_rounds(self) -> None:
        runner, client = _runner([_search_turn(), _Msg([_Text("Here you go.")])])
        assert await runner.chat(AgentSession(tenant_id="t"), "DEL BOM") == "Here you go."
        assert len(client.messages.calls) == 2


class TestSmallToolResults:
    async def test_search_result_sent_to_model_is_top_n_and_slim(self) -> None:
        runner, client = _runner([_search_turn(), _Msg([_Text("ok")])])
        await runner.chat(AgentSession(tenant_id="t"), "DEL BOM")
        # history: [user, assistant tool_use, user tool_result, assistant reply]
        tool_msg = client.messages.calls[1]["messages"][2]["content"][0]["content"]
        result = json.loads(tool_msg)
        assert result["total_results"] > MODEL_TOP_N
        assert len(result["offers"]) == MODEL_TOP_N
        assert result["simulated_data"] is True
        assert "fare_basis" not in result["offers"][0]
        assert len(tool_msg) < 4000

    async def test_identical_search_is_served_from_cache(self) -> None:
        from booking_agent.cache.search_cache import SearchCache

        cache = SearchCache(fakeredis.aioredis.FakeRedis(), ttl_seconds=60)
        runner, _ = _runner(
            [_search_turn(1), _Msg([_Text("a")]), _search_turn(2), _Msg([_Text("b")])],
            cache=cache,
        )
        session = AgentSession(tenant_id="t")
        statuses: list[str] = []
        for msg in ("first", "again"):
            async for e in runner.chat_events(session, msg):
                if e["type"] == "tool_done":
                    statuses.append(e["text"])
        assert "cached" not in statuses[0] and "cached" in statuses[1]


class TestTimeouts:
    async def test_slow_model_hits_turn_timeout(self) -> None:
        async def slow() -> _Msg:
            await asyncio.sleep(5)
            return _Msg([_Text("too late")])

        runner, _ = _runner([slow], timeout_seconds=0.2)
        session = AgentSession(tenant_id="t")
        with pytest.raises(AgentTurnTimeoutError):
            await runner.chat(session, "hi")
        assert session.history == []  # failed turn rolled back

    async def test_turn_timeout_is_a_clear_504(self) -> None:
        async def slow() -> _Msg:
            await asyncio.sleep(5)
            return _Msg([_Text("too late")])

        app, _ = _app([slow])
        app.state.agent_runner._timeout = 0.2
        r = await _post(app, "/v1/agent/chat", {"message": "hi"})
        assert r.status_code == 504
        assert "took longer than 0.2s" in r.json()["detail"]

    async def test_llm_call_timeout_is_a_clear_504(self) -> None:
        app, _ = _app([LLMTimeoutError("Groq", 20)])
        r = await _post(app, "/v1/agent/chat", {"message": "hi"})
        assert r.status_code == 504
        assert "did not respond within 20s" in r.json()["detail"]

    async def test_quota_is_a_clear_429_without_account_details(self) -> None:
        exc = LLMRateLimitError(
            "Groq",
            "Rate limit reached for model `x` in organization `org_123` service tier on "
            "tokens per minute (TPM): Limit 8000. Please try again in 12s.",
            12.0,
        )
        app, _ = _app([exc])
        r = await _post(app, "/v1/agent/chat", {"message": "hi"})
        assert r.status_code == 429
        assert "Try again in about 12s" in r.json()["detail"]
        assert "org_123" not in r.json()["detail"]


class TestAdapterRetry:
    @respx.mock
    async def test_retries_once_on_short_429(self) -> None:
        route = respx.post(GROQ_URL).mock(
            side_effect=[
                httpx.Response(
                    429, headers={"retry-after": "0.01"}, json={"error": {"message": "slow"}}
                ),
                httpx.Response(
                    200, json={"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]}
                ),
            ]
        )
        adapter = GroqClientAdapter(api_key="k", timeout_s=5)
        resp = await adapter.messages.create(
            model="m",
            max_tokens=10,
            system="s",
            tools=[],
            messages=[{"role": "user", "content": "x"}],
        )
        assert resp.content[0].text == "hi" and route.call_count == 2
        await adapter.aclose()

    @respx.mock
    async def test_long_wait_is_not_retried(self) -> None:
        route = respx.post(GROQ_URL).mock(
            return_value=httpx.Response(
                429, headers={"retry-after": "120"}, json={"error": {"message": "daily quota"}}
            )
        )
        adapter = GroqClientAdapter(api_key="k", timeout_s=5)
        with pytest.raises(LLMRateLimitError) as exc:
            await adapter.messages.create(
                model="m", max_tokens=10, system="s", tools=[], messages=[]
            )
        assert exc.value.retry_after_s == 120 and route.call_count == 1
        await adapter.aclose()

    @respx.mock
    async def test_two_timeouts_raise_llm_timeout(self) -> None:
        route = respx.post(GROQ_URL).mock(side_effect=httpx.ReadTimeout("slow"))
        adapter = GroqClientAdapter(api_key="k", timeout_s=5)
        with pytest.raises(LLMTimeoutError):
            await adapter.messages.create(
                model="m", max_tokens=10, system="s", tools=[], messages=[]
            )
        assert route.call_count == 2
        await adapter.aclose()

    @respx.mock
    async def test_one_client_is_reused_across_calls(self) -> None:
        respx.post(GROQ_URL).mock(
            return_value=httpx.Response(
                200, json={"choices": [{"message": {"content": "x"}, "finish_reason": "stop"}]}
            )
        )
        adapter = GroqClientAdapter(api_key="k", timeout_s=5)
        for _ in range(2):
            await adapter.messages.create(
                model="m", max_tokens=5, system="s", tools=[], messages=[]
            )
        first = adapter.messages.http._client
        await adapter.messages.create(model="m", max_tokens=5, system="s", tools=[], messages=[])
        assert adapter.messages.http._client is first
        await adapter.aclose()


class TestStreamingAndHealth:
    async def test_stream_event_order(self) -> None:
        app, _ = _app([_search_turn(), _Msg([_Text("Cheapest is #1.")])])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/v1/agent/chat/stream", json={"message": "DEL BOM"}, headers=HEADERS)
        events = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]
        types = [e["type"] for e in events]
        assert types[0] == "session" and types[-1] == "done"
        assert types.index("status") < types.index("tool_done") < types.index("reply")
        assert types.index("reply") < types.index("offers")
        offers = next(e for e in events if e["type"] == "offers")["offers"]
        assert offers and all(o["source"] == "simulated" for o in offers)

    async def test_stream_reports_errors_as_events(self) -> None:
        app, _ = _app([LLMTimeoutError("Groq", 20)])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/v1/agent/chat/stream", json={"message": "hi"}, headers=HEADERS)
        events = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]
        err = next(e for e in events if e["type"] == "error")
        assert err["status"] == 504 and events[-1]["type"] == "done"

    async def test_health_reports_latency(self) -> None:
        app, _ = _app([_Msg([_Text("OK")])])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/agent/health", headers=HEADERS)
        body = r.json()
        assert body["ok"] is True and body["latency_ms"] is not None

    async def test_health_reports_failure(self) -> None:
        app, _ = _app([LLMTimeoutError("Groq", 20)])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/agent/health", headers=HEADERS)
        assert r.json()["ok"] is False and "20s" in r.json()["error"]
