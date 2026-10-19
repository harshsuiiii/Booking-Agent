"""
LLM adapters: present the same duck-typed interface as anthropic.AsyncAnthropic
so AgentRunner works with Groq, Cerebras or Gemini via pure httpx REST calls.

All adapters expose:  adapter.messages.create(model, max_tokens, system, tools, messages)
and return mock _Response / _TextBlock / _ToolUseBlock objects that AgentRunner
already knows how to read (same duck-typed interface as Anthropic SDK objects).

Each adapter owns ONE httpx client for its lifetime (creating a client per call cost
6-7 s on Windows). Call `await adapter.aclose()` on shutdown.

Retries: one retry with backoff on 429, 5xx and timeouts. No retry when the provider
asks to wait longer than _MAX_RETRY_WAIT_S (quota exhausted): that surfaces at once.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from booking_agent.httpclient import ssl_context

log = structlog.get_logger(__name__)

_MAX_RETRY_WAIT_S = 20.0
_RETRY_STATUS = {429, 500, 502, 503, 504}


class LLMProviderError(RuntimeError):
    """The LLM provider rejected the call (bad model, auth, quota, outage)."""

    def __init__(self, provider: str, status_code: int, detail: str) -> None:
        super().__init__(f"{provider} returned HTTP {status_code}: {detail}")
        self.provider = provider
        self.status_code = status_code
        self.detail = detail


class LLMRateLimitError(LLMProviderError):
    """429 from the provider. retry_after_s is the provider's requested wait, if given."""

    def __init__(self, provider: str, detail: str, retry_after_s: float | None) -> None:
        super().__init__(provider, 429, detail)
        self.retry_after_s = retry_after_s


class LLMTimeoutError(LLMProviderError):
    """The provider did not answer within the per-call timeout."""

    def __init__(self, provider: str, timeout_s: float) -> None:
        super().__init__(provider, 0, f"no response within {timeout_s:g}s")
        self.timeout_s = timeout_s


def _error_detail(r: httpx.Response) -> str:
    try:
        body = r.json()
        err = body.get("error", body)
        detail = err.get("message", str(err)) if isinstance(err, dict) else str(err)
    except Exception:
        detail = r.text
    # Never echo request headers (API key); only the provider's error message.
    return str(detail)[:300]


def _retry_after(r: httpx.Response) -> float | None:
    raw = r.headers.get("retry-after")
    try:
        return float(raw) if raw is not None else None
    except ValueError:
        return None


def _raise_for_provider(provider: str, r: httpx.Response) -> None:
    if r.is_success:
        return
    if r.status_code == 429:
        raise LLMRateLimitError(provider, _error_detail(r), _retry_after(r))
    raise LLMProviderError(provider, r.status_code, _error_detail(r))


class _HttpCaller:
    """One shared client per adapter, explicit timeouts, one retry."""

    def __init__(self, provider: str, timeout_s: float) -> None:
        self.provider = provider
        self.timeout_s = timeout_s
        self._client: httpx.AsyncClient | None = None
        self.last_ratelimit: dict[str, str] = {}
        self.last_usage: dict[str, Any] = {}

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout_s, connect=5.0),
                verify=ssl_context(),
            )
        return self._client

    async def post(self, url: str, **kwargs: Any) -> dict[str, Any]:
        for attempt in (1, 2):
            try:
                r = await self._get_client().post(url, **kwargs)
            except httpx.TimeoutException as exc:
                if attempt == 2:
                    raise LLMTimeoutError(self.provider, self.timeout_s) from exc
                log.warning("llm.retry", provider=self.provider, reason="timeout")
                await asyncio.sleep(1.0)
                continue
            self.last_ratelimit = {
                k: v for k, v in r.headers.items() if k.startswith("x-ratelimit-")
            }
            if r.status_code in _RETRY_STATUS and attempt == 1:
                wait = _retry_after(r) or 1.0
                if wait <= _MAX_RETRY_WAIT_S:
                    log.warning(
                        "llm.retry", provider=self.provider, status=r.status_code, wait_s=wait
                    )
                    await asyncio.sleep(wait)
                    continue
            _raise_for_provider(self.provider, r)
            data: dict[str, Any] = r.json()
            usage = data.get("usage") or data.get("usageMetadata") or {}
            self.last_usage = usage if isinstance(usage, dict) else {}
            return data
        raise AssertionError("unreachable")

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _forbids_tools(tool_choice: Any) -> bool:
    """True for Anthropic-style {"type": "none"} or the plain string "none"."""
    if isinstance(tool_choice, dict):
        return bool(tool_choice.get("type") == "none")
    return bool(tool_choice == "none")


def llm_timeout_s() -> float:
    return float(os.getenv("LLM_TIMEOUT_S", "20"))


# ── duck-typed response objects ───────────────────────────────────────────────


@dataclass
class _TextBlock:
    text: str
    type: str = "text"


@dataclass
class _ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]
    type: str = "tool_use"


@dataclass
class _Response:
    stop_reason: str  # "tool_use" | "end_turn"
    content: list[Any]


# ── shared helpers ────────────────────────────────────────────────────────────


def _get(block: Any, attr: str, default: Any = None) -> Any:
    """Read attribute from either a mock object or a plain dict."""
    if isinstance(block, dict):
        return block.get(attr, default)
    return getattr(block, attr, default)


def _tools_to_openai(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t["input_schema"],
            },
        }
        for t in tools
    ]


def _messages_to_openai(system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Anthropic-style history (possibly containing mock objects) → OpenAI format."""
    result: list[dict[str, Any]] = [{"role": "system", "content": system}]

    for msg in messages:
        role = msg["role"]
        content = msg["content"]

        if role == "user":
            if isinstance(content, str):
                result.append({"role": "user", "content": content})
            elif isinstance(content, list):
                for block in content:
                    if _get(block, "type") == "tool_result":
                        result.append(
                            {
                                "role": "tool",
                                "tool_call_id": _get(block, "tool_use_id", ""),
                                "content": str(_get(block, "content", "")),
                            }
                        )

        elif role == "assistant":
            if isinstance(content, str):
                result.append({"role": "assistant", "content": content})
            elif isinstance(content, list):
                text_parts: list[str] = []
                tool_calls: list[dict[str, Any]] = []
                for block in content:
                    bt = _get(block, "type")
                    if bt == "text":
                        t = _get(block, "text", "")
                        if t:
                            text_parts.append(t)
                    elif bt == "tool_use":
                        inp = _get(block, "input", {})
                        tool_calls.append(
                            {
                                "id": _get(block, "id", str(uuid.uuid4())),
                                "type": "function",
                                "function": {
                                    "name": _get(block, "name", ""),
                                    "arguments": json.dumps(inp)
                                    if isinstance(inp, dict)
                                    else str(inp),
                                },
                            }
                        )
                am: dict[str, Any] = {
                    "role": "assistant",
                    "content": " ".join(text_parts) if text_parts else None,
                }
                if tool_calls:
                    am["tool_calls"] = tool_calls
                result.append(am)

    return result


def _parse_openai_response(data: dict[str, Any]) -> _Response:
    choice = data["choices"][0]
    message = choice["message"]
    content: list[Any] = []

    if message.get("content"):
        content.append(_TextBlock(text=message["content"]))

    for tc in message.get("tool_calls") or []:
        try:
            inp = json.loads(tc["function"]["arguments"])
        except Exception:
            inp = {}
        content.append(
            _ToolUseBlock(
                id=tc["id"],
                name=tc["function"]["name"],
                input=inp,
            )
        )

    finish = choice.get("finish_reason", "stop")
    stop_reason = "tool_use" if finish == "tool_calls" else "end_turn"
    return _Response(stop_reason=stop_reason, content=content)


# ── OpenAI-compatible adapters (Groq, Cerebras) ──────────────────────────────


class _OpenAICompatMessages:
    def __init__(
        self,
        provider: str,
        url: str,
        api_key: str,
        timeout_s: float,
        reasoning_effort: str | None,
    ) -> None:
        self._url = url
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._reasoning_effort = reasoning_effort
        self.http = _HttpCaller(provider, timeout_s)

    async def create(
        self,
        *,
        model: str,
        max_tokens: int,
        system: str,
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        tool_choice: Any = None,
    ) -> _Response:
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": _messages_to_openai(system, messages),
        }
        if tools:
            payload["tools"] = _tools_to_openai(tools)
            payload["tool_choice"] = "none" if _forbids_tools(tool_choice) else "auto"
        # gpt-oss models are reasoning models; "low" measured ~35% faster on Groq.
        if self._reasoning_effort and "gpt-oss" in model:
            payload["reasoning_effort"] = self._reasoning_effort
        data = await self.http.post(self._url, headers=self._headers, json=payload)
        return _parse_openai_response(data)


class _OpenAICompatAdapter:
    provider = ""
    url = ""

    def __init__(
        self,
        api_key: str,
        timeout_s: float | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        effort = reasoning_effort or os.getenv("LLM_REASONING_EFFORT", "low")
        self.messages = _OpenAICompatMessages(
            self.provider, self.url, api_key, timeout_s or llm_timeout_s(), effort or None
        )

    @property
    def last_ratelimit(self) -> dict[str, str]:
        return self.messages.http.last_ratelimit

    @property
    def last_usage(self) -> dict[str, Any]:
        return self.messages.http.last_usage

    async def aclose(self) -> None:
        await self.messages.http.aclose()


class GroqClientAdapter(_OpenAICompatAdapter):
    """Groq REST API behind the anthropic.AsyncAnthropic().messages interface."""

    provider = "Groq"
    url = "https://api.groq.com/openai/v1/chat/completions"


class CerebrasClientAdapter(_OpenAICompatAdapter):
    """Cerebras REST API (OpenAI-compatible) behind the same interface."""

    provider = "Cerebras"
    url = "https://api.cerebras.ai/v1/chat/completions"


# ── Gemini adapter (REST) ─────────────────────────────────────────────────────


def _tools_to_gemini(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "name": t["name"],
            "description": t.get("description", ""),
            "parameters": t["input_schema"],
        }
        for t in tools
    ]


def _build_id_to_name(messages: list[dict[str, Any]]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for msg in messages:
        if msg["role"] == "assistant" and isinstance(msg["content"], list):
            for block in msg["content"]:
                if _get(block, "type") == "tool_use":
                    tid = _get(block, "id", "")
                    if tid:
                        mapping[tid] = _get(block, "name", tid)
    return mapping


def _messages_to_gemini(system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    id_to_name = _build_id_to_name(messages)
    contents: list[dict[str, Any]] = []

    for msg in messages:
        role = msg["role"]
        content = msg["content"]

        if role == "user":
            if isinstance(content, str):
                contents.append({"role": "user", "parts": [{"text": content}]})
            elif isinstance(content, list):
                parts = []
                for block in content:
                    if _get(block, "type") == "tool_result":
                        tid = _get(block, "tool_use_id", "")
                        func_name = id_to_name.get(tid, tid or "tool")
                        parts.append(
                            {
                                "functionResponse": {
                                    "name": func_name,
                                    "response": {"result": str(_get(block, "content", ""))},
                                }
                            }
                        )
                if parts:
                    contents.append({"role": "user", "parts": parts})

        elif role == "assistant":
            if isinstance(content, str):
                contents.append({"role": "model", "parts": [{"text": content}]})
            elif isinstance(content, list):
                parts = []
                for block in content:
                    bt = _get(block, "type")
                    if bt == "text":
                        t = _get(block, "text", "")
                        if t:
                            parts.append({"text": t})
                    elif bt == "tool_use":
                        inp = _get(block, "input", {})
                        parts.append(
                            {
                                "functionCall": {
                                    "name": _get(block, "name", ""),
                                    "args": inp if isinstance(inp, dict) else {},
                                }
                            }
                        )
                if parts:
                    contents.append({"role": "model", "parts": parts})

    return contents


def _parse_gemini_response(data: dict[str, Any]) -> _Response:
    candidate = data["candidates"][0]
    content: list[Any] = []

    for part in candidate.get("content", {}).get("parts", []):
        if "text" in part and part["text"]:
            content.append(_TextBlock(text=part["text"]))
        elif "functionCall" in part:
            fc = part["functionCall"]
            content.append(
                _ToolUseBlock(
                    id=str(uuid.uuid4()),
                    name=fc["name"],
                    input=fc.get("args", {}),
                )
            )

    has_tool = any(isinstance(b, _ToolUseBlock) for b in content)
    finish = candidate.get("finishReason", "STOP")
    stop_reason = "tool_use" if (has_tool or finish == "TOOL_CALLS") else "end_turn"
    return _Response(stop_reason=stop_reason, content=content)


class _GeminiMessages:
    _BASE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    def __init__(self, api_key: str, timeout_s: float) -> None:
        self._headers = {"x-goog-api-key": api_key}
        self.http = _HttpCaller("Gemini", timeout_s)

    async def create(
        self,
        *,
        model: str,
        max_tokens: int,
        system: str,
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        tool_choice: Any = None,
    ) -> _Response:
        payload: dict[str, Any] = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": _messages_to_gemini(system, messages),
            "generationConfig": {"maxOutputTokens": max_tokens},
        }
        if tools:
            payload["tools"] = [{"function_declarations": _tools_to_gemini(tools)}]
            if _forbids_tools(tool_choice):
                payload["toolConfig"] = {"functionCallingConfig": {"mode": "NONE"}}
        data = await self.http.post(
            self._BASE.format(model=model), headers=self._headers, json=payload
        )
        return _parse_gemini_response(data)


class GeminiClientAdapter:
    """Gemini REST API behind the anthropic.AsyncAnthropic().messages interface."""

    provider = "Gemini"

    def __init__(self, api_key: str, timeout_s: float | None = None) -> None:
        self.messages = _GeminiMessages(api_key, timeout_s or llm_timeout_s())

    @property
    def last_ratelimit(self) -> dict[str, str]:
        return self.messages.http.last_ratelimit

    async def aclose(self) -> None:
        await self.messages.http.aclose()
