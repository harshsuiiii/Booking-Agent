"""
AgentRunner: orchestrates one conversation turn.

Flow per turn:
  1. Append the user message to session history.
  2. Call the LLM with tool definitions.
  3. If the response contains tool_use blocks, execute each tool, append the
     tool_result message, and loop. At most `max_tool_rounds` tool rounds; after
     that the model must answer with what it has (tool_choice none).
  4. When the response is pure text, run the post-check and return the reply.

chat_events() yields progress events for streaming; chat() is the same turn without
them. The whole turn has one deadline (AGENT_TURN_TIMEOUT_S); every model and tool
call gets only the time that is left, so the turn fails with AgentTurnTimeoutError instead
of hanging.
"""

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator, Awaitable
from datetime import date
from pathlib import Path
from typing import Any, TypeVar

import structlog

from booking_agent.agent.post_check import check_reply
from booking_agent.agent.session import AgentSession
from booking_agent.agent.tools import TOOL_DEFINITIONS, ToolExecutor, offer_summary
from booking_agent.cache.search_cache import SearchCache
from booking_agent.dashboard.recent_searches import RecentSearchStore
from booking_agent.domain.offer import Offer
from booking_agent.domain.ranking import RankingPreferences
from booking_agent.orchestrator.search import SearchOrchestrator
from booking_agent.rules.model import Rule
from booking_agent.suppliers import supplier_label

log = structlog.get_logger(__name__)

_SYSTEM_PROMPT = (Path(__file__).parent / "prompts" / "system_v1.md").read_text(encoding="utf-8")

_T = TypeVar("_T")

# Progress text shown in the UI while a tool runs.
_TOOL_STATUS = {
    "search_flights": "Searching suppliers…",
    "get_offer_details": "Looking up fare details…",
    "compare_offers": "Comparing offers…",
    "explain_ranking": "Checking the ranking…",
}


class AgentTurnTimeoutError(Exception):
    """The whole turn (all model and tool calls) exceeded its time budget."""

    def __init__(self, timeout_s: float) -> None:
        super().__init__(f"The assistant did not finish within {timeout_s:g}s.")
        self.timeout_s = timeout_s


def agent_turn_timeout_s() -> float:
    return float(os.getenv("AGENT_TURN_TIMEOUT_S", "45"))


def agent_max_tool_rounds() -> int:
    return int(os.getenv("AGENT_MAX_TOOL_ROUNDS", "3"))


class AgentRunner:
    def __init__(
        self,
        client: Any,  # anthropic.AsyncAnthropic, one of our adapters, or a test mock
        orchestrator: SearchOrchestrator,
        rules: list[Rule] | None = None,
        preferences: RankingPreferences | None = None,
        model: str = "openai/gpt-oss-120b",
        max_tool_calls_per_turn: int = 5,
        max_tool_rounds: int | None = None,
        timeout_seconds: float | None = None,
        cache: SearchCache | None = None,
        recent_searches: RecentSearchStore | None = None,
    ) -> None:
        self._client = client
        self._recent = recent_searches
        self._orchestrator = orchestrator
        self._rules = rules or []
        self._prefs = preferences or RankingPreferences()
        self._model = model
        self._max_tool_calls = max_tool_calls_per_turn
        self._max_tool_rounds = max_tool_rounds or agent_max_tool_rounds()
        self._timeout = timeout_seconds or agent_turn_timeout_s()
        self._cache = cache

    @property
    def model(self) -> str:
        return self._model

    @property
    def client(self) -> Any:
        return self._client

    async def chat(self, session: AgentSession, user_message: str) -> str:
        reply = ""
        async for event in self.chat_events(session, user_message):
            if event["type"] == "reply":
                reply = event["text"]
        return reply

    async def chat_events(
        self, session: AgentSession, user_message: str
    ) -> AsyncIterator[dict[str, Any]]:
        session.history.append({"role": "user", "content": user_message})
        executor = ToolExecutor(
            session=session,
            orchestrator=self._orchestrator,
            rules=self._rules,
            preferences=self._prefs,
            cache=self._cache,
            recent_searches=self._recent,
        )
        history_len = len(session.history) - 1
        started = time.monotonic()
        deadline = started + self._timeout
        try:
            async for event in self._run_loop(session, executor, deadline):
                yield event
            log.info("agent.turn", outcome="ok", total_ms=_ms_since(started))
        except BaseException as exc:
            log.warning(
                "agent.turn",
                outcome=type(exc).__name__,
                total_ms=_ms_since(started),
                timeout_s=self._timeout,
            )
            # Drop the failed turn so the next message doesn't follow a dangling
            # user message or half-finished tool exchange.
            del session.history[history_len:]
            raise

    def offer_cards(self, offers: list[Offer]) -> list[dict[str, Any]]:
        """Offer summaries for the UI. Prices come from code, same as the model sees."""
        return [
            {
                **offer_summary(o, i, self._prefs),
                "pax_count": o.pax_count,
                "pax_counts": o.pax_counts.model_dump() if o.pax_counts else None,
                "supplier_label": supplier_label(o.supplier_id),
            }
            for i, o in enumerate(offers, 1)
        ]

    async def _bounded(self, aw: Awaitable[_T], deadline: float) -> _T:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AgentTurnTimeoutError(self._timeout)
        try:
            return await asyncio.wait_for(aw, remaining)
        except TimeoutError as exc:
            raise AgentTurnTimeoutError(self._timeout) from exc

    async def _run_loop(
        self, session: AgentSession, executor: ToolExecutor, deadline: float
    ) -> AsyncIterator[dict[str, Any]]:
        tool_calls_used = 0
        tool_rounds = 0
        round_no = 0

        while True:
            round_no += 1
            final_round = tool_rounds >= self._max_tool_rounds
            yield {
                "type": "status",
                "stage": "thinking" if round_no == 1 else "writing",
                "text": "Thinking…" if round_no == 1 else "Writing the answer…",
            }
            kwargs: dict[str, Any] = {}
            if final_round:
                # Round budget spent: the model must answer from what it already has.
                kwargs["tool_choice"] = {"type": "none"}
            t0 = time.monotonic()
            response = await self._bounded(
                self._client.messages.create(
                    model=self._model,
                    max_tokens=2048,
                    system=_system_prompt(),
                    tools=TOOL_DEFINITIONS,
                    messages=session.history,
                    **kwargs,
                ),
                deadline,
            )
            log.info(
                "agent.llm_call",
                round=round_no,
                ms=_ms_since(t0),
                request_chars=len(json.dumps(session.history, default=str)),
                stop_reason=response.stop_reason,
                final_round=final_round,
                prompt_tokens=_usage(self._client, "prompt_tokens"),
                completion_tokens=_usage(self._client, "completion_tokens"),
            )

            if response.stop_reason != "tool_use" or final_round:
                text = "".join(
                    b.text for b in response.content if getattr(b, "type", None) == "text"
                )
                if not text:
                    text = (
                        "I couldn't finish that within my step limit. "
                        "Please narrow the request (route, date, passengers) and try again."
                    )
                check_reply(text, session.allowed_prices)
                session.history.append({"role": "assistant", "content": text})
                yield {"type": "reply", "text": text}
                return

            # Execute tool calls
            tool_rounds += 1
            assistant_content = list(response.content)
            tool_results: list[dict[str, Any]] = []

            for block in response.content:
                if getattr(block, "type", None) != "tool_use":
                    continue
                tool_calls_used += 1
                if tool_calls_used > self._max_tool_calls:
                    result_content = json.dumps(
                        {"error": "Tool call budget exceeded for this conversation turn."}
                    )
                else:
                    yield {
                        "type": "status",
                        "stage": "tool",
                        "tool": block.name,
                        "text": _TOOL_STATUS.get(block.name, "Working…"),
                    }
                    t0 = time.monotonic()
                    result_content = await self._bounded(
                        executor.execute(block.name, block.input), deadline
                    )
                    ms = _ms_since(t0)
                    log.info(
                        "agent.tool_call",
                        round=round_no,
                        tool=block.name,
                        ms=ms,
                        result_chars=len(result_content),
                    )
                    yield {
                        "type": "tool_done",
                        "tool": block.name,
                        "ms": ms,
                        "text": executor.last_status or "Done.",
                    }

                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_content,
                    }
                )

            session.history.append({"role": "assistant", "content": assistant_content})
            session.history.append({"role": "user", "content": tool_results})


def _usage(client: Any, key: str) -> int | None:
    usage = getattr(client, "last_usage", None)
    value = usage.get(key) if isinstance(usage, dict) else None
    return value if isinstance(value, int) else None


def _system_prompt() -> str:
    today = date.today()
    return f"{_SYSTEM_PROMPT}\n\nToday's date: {today.isoformat()} ({today:%A})."


def _ms_since(t0: float) -> int:
    return round((time.monotonic() - t0) * 1000)
