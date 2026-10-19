"""
Eval harness: scripted mock Anthropic client + scenario runner.

Each EvalScenario scripts the LLM's turn-by-turn responses so evals are
deterministic and require no API key. The runner tests the framework
(tool execution, post-check, budget limiting, session management) rather
than LLM behaviour, which is exercised via the real API separately.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any

from booking_agent.agent.runner import AgentRunner
from booking_agent.agent.session import AgentSession
from booking_agent.connectors.fake import FakeConnector
from booking_agent.orchestrator.search import SearchOrchestrator

# ── mock Anthropic types ──────────────────────────────────────────────────────


@dataclass
class _TextBlock:
    type: str = "text"
    text: str = ""


@dataclass
class _ToolUseBlock:
    type: str = "tool_use"
    id: str = ""
    name: str = ""
    input: dict = field(default_factory=dict)


@dataclass
class _MockMessage:
    id: str = "msg_mock"
    type: str = "message"
    role: str = "assistant"
    content: list = field(default_factory=list)
    stop_reason: str = "end_turn"
    model: str = "mock-model"


@dataclass
class EvalTurn:
    """
    One scripted LLM response.

    Set tool_calls to a non-empty list to produce a tool_use response.
    Set text for the final text response (no tool calls).
    """

    tool_calls: list[tuple[str, dict]] = field(default_factory=list)
    text: str = ""


class _ScriptedMessages:
    """Mimics anthropic.AsyncAnthropic().messages interface."""

    def __init__(self, turns: list[EvalTurn]) -> None:
        self._turns = turns
        self._pos = 0

    async def create(self, **kwargs: Any) -> _MockMessage:
        if self._pos >= len(self._turns):
            return _MockMessage(
                content=[_TextBlock(text="(no more scripted turns)")],
                stop_reason="end_turn",
            )
        turn = self._turns[self._pos]
        self._pos += 1

        if turn.tool_calls:
            content = [
                _ToolUseBlock(
                    id=f"tu_{uuid.uuid4().hex[:8]}_{i}",
                    name=name,
                    input=inp,
                )
                for i, (name, inp) in enumerate(turn.tool_calls)
            ]
            return _MockMessage(content=content, stop_reason="tool_use")
        return _MockMessage(
            content=[_TextBlock(text=turn.text)],
            stop_reason="end_turn",
        )


class _ScriptedClient:
    def __init__(self, turns: list[EvalTurn]) -> None:
        self.messages = _ScriptedMessages(turns)


# ── scenario definition ───────────────────────────────────────────────────────


@dataclass
class EvalScenario:
    name: str
    user_message: str
    turns: list[EvalTurn]
    # Assertions on the final reply text
    reply_contains: list[str] = field(default_factory=list)
    reply_not_contains: list[str] = field(default_factory=list)
    # Assertions on tool calls made
    tools_called: list[str] = field(default_factory=list)
    tools_not_called: list[str] = field(default_factory=list)
    # Special flags
    expect_post_check_fail: bool = False  # True if the reply should be rejected


# ── runner ────────────────────────────────────────────────────────────────────


@dataclass
class EvalResult:
    scenario: str
    passed: bool
    error: str = ""
    reply: str = ""


async def run_scenario(scenario: EvalScenario) -> EvalResult:
    connector = FakeConnector(supplier_id="demo")
    async with connector:
        orchestrator = SearchOrchestrator(connectors=[connector])
        client = _ScriptedClient(scenario.turns)
        runner = AgentRunner(client=client, orchestrator=orchestrator)
        session = AgentSession(tenant_id="eval_tenant")

        try:
            reply = await runner.chat(session, scenario.user_message)
        except ValueError as exc:
            if scenario.expect_post_check_fail:
                return EvalResult(scenario=scenario.name, passed=True, reply=str(exc))
            return EvalResult(scenario=scenario.name, passed=False, error=str(exc))
        except Exception as exc:
            if scenario.expect_post_check_fail:
                return EvalResult(scenario=scenario.name, passed=True, reply=str(exc))
            return EvalResult(scenario=scenario.name, passed=False, error=f"Unexpected: {exc}")

        if scenario.expect_post_check_fail:
            return EvalResult(
                scenario=scenario.name,
                passed=False,
                error="Expected post-check to fail but reply was accepted",
                reply=reply,
            )

        # Check reply content assertions
        for expected in scenario.reply_contains:
            if expected.lower() not in reply.lower():
                return EvalResult(
                    scenario=scenario.name,
                    passed=False,
                    error=f"Reply missing expected text: {expected!r}",
                    reply=reply,
                )
        for forbidden in scenario.reply_not_contains:
            if forbidden.lower() in reply.lower():
                return EvalResult(
                    scenario=scenario.name,
                    passed=False,
                    error=f"Reply contains forbidden text: {forbidden!r}",
                    reply=reply,
                )

        # Check tool call assertions
        called_tools: list[str] = []
        for msg in session.history:
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if getattr(block, "type", None) == "tool_use":
                        called_tools.append(block.name)

        for required_tool in scenario.tools_called:
            if required_tool not in called_tools:
                return EvalResult(
                    scenario=scenario.name,
                    passed=False,
                    error=f"Expected tool {required_tool!r} was not called",
                    reply=reply,
                )
        for forbidden_tool in scenario.tools_not_called:
            if forbidden_tool in called_tools:
                return EvalResult(
                    scenario=scenario.name,
                    passed=False,
                    error=f"Tool {forbidden_tool!r} was called but should not have been",
                    reply=reply,
                )

        return EvalResult(scenario=scenario.name, passed=True, reply=reply)


def run_all(scenarios: list[EvalScenario]) -> list[EvalResult]:
    return asyncio.get_event_loop().run_until_complete(
        asyncio.gather(*[run_scenario(s) for s in scenarios])
    )
