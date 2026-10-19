"""
Agent chat endpoints.

POST /v1/agent/chat
  body: {"session_id": "<existing or omit to create>", "message": "..."}
  response: {"session_id": "...", "reply": "...", "offers": [<offer card>, ...]}

POST /v1/agent/chat/stream
  same body; Server-Sent Events, one JSON object per `data:` line:
    {"type": "session", "session_id": ...}
    {"type": "status", "stage": "thinking|tool|writing", "text": ...}   (repeats)
    {"type": "tool_done", "tool": ..., "ms": ..., "text": ...}
    {"type": "reply", "text": ...}
    {"type": "offers", "offers": [...]}
    {"type": "error", "status": <http-like code>, "message": ...}
    {"type": "done"}

GET /v1/agent/health
  one tiny model call; reports provider, model, latency and rate-limit headroom.

The agent is read-only. Booking goes through /v1/bookings in the UI, so passenger
details never reach the LLM.

Sessions are stored in app.state.agent_sessions (in-memory; use Redis in prod).
"""

import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from booking_agent.agent.llm_adapters import LLMProviderError, LLMRateLimitError, LLMTimeoutError
from booking_agent.agent.runner import AgentRunner, AgentTurnTimeoutError
from booking_agent.agent.session import AgentSession
from booking_agent.auth.principal import get_principal

router = APIRouter(prefix="/v1/agent", tags=["agent"])

_MAX_OFFER_CARDS = 5


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    # Offers from a search run during this turn (empty if the turn ran no new search).
    # Rendered as cards with a Book button; booking happens outside the agent.
    offers: list[dict[str, Any]] = []


class AgentHealth(BaseModel):
    ok: bool
    provider: str
    model: str
    latency_ms: int | None = None
    error: str | None = None
    ratelimit: dict[str, str] = {}


def _require_tenant(request: Request) -> str:
    tenant: str | None = getattr(request.state, "tenant_id", None)
    if not tenant:
        raise HTTPException(status_code=401, detail="Authentication required")
    return tenant


def _runner(request: Request) -> AgentRunner:
    runner: AgentRunner | None = getattr(request.app.state, "agent_runner", None)
    if runner is None:
        raise HTTPException(
            status_code=503,
            detail="Agent not configured: set GROQ_API_KEY (or CEREBRAS_API_KEY / GEMINI_API_KEY).",
        )
    return runner


def _session(request: Request, tenant_id: str, session_id: str | None) -> AgentSession:
    user_id = get_principal(request).user_id
    sessions: dict[str, AgentSession] = request.app.state.agent_sessions
    if session_id and session_id in sessions:
        session = sessions[session_id]
        if session.tenant_id != tenant_id or session.user_id != user_id:
            raise HTTPException(status_code=403, detail="This conversation belongs to someone else")
        return session
    session = AgentSession(tenant_id=tenant_id, user_id=user_id)
    sessions[session.session_id] = session
    return session


def _error_status(exc: BaseException) -> tuple[int, str] | None:
    """Map a failed turn to (status, message the agent user can act on)."""
    if isinstance(exc, AgentTurnTimeoutError):
        return 504, (
            f"The assistant took longer than {exc.timeout_s:g}s and was stopped. "
            "Try again, or ask a narrower question."
        )
    if isinstance(exc, LLMTimeoutError):
        return 504, f"AI provider ({exc.provider}) did not respond within {exc.timeout_s:g}s."
    if isinstance(exc, LLMRateLimitError):
        wait = f" Try again in about {exc.retry_after_s:g}s." if exc.retry_after_s else ""
        # Provider messages can include account identifiers; keep the first sentence only.
        reason = exc.detail.split(" in organization")[0].split(". ")[0].strip()
        return 429, f"AI provider ({exc.provider}) rate limit or quota reached.{wait} ({reason})"
    if isinstance(exc, LLMProviderError):
        return 502, f"AI provider error ({exc.provider}): {exc.detail}"
    if isinstance(exc, httpx.HTTPError):
        return 502, "Could not reach the AI provider."
    if isinstance(exc, ValueError):
        return 422, str(exc)
    return None


def _offer_cards(runner: AgentRunner, session: AgentSession, before: list[Any]) -> list[Any]:
    if session.ranked_offers is before:
        return []
    return runner.offer_cards(session.ranked_offers[:_MAX_OFFER_CARDS])


@router.post("/chat", response_model=ChatResponse)
async def chat(body: ChatRequest, request: Request) -> ChatResponse:
    tenant_id = _require_tenant(request)
    runner = _runner(request)
    session = _session(request, tenant_id, body.session_id)

    offers_before = session.ranked_offers
    try:
        reply = await runner.chat(session, body.message)
    except Exception as exc:
        mapped = _error_status(exc)
        if mapped is None:
            raise
        raise HTTPException(status_code=mapped[0], detail=mapped[1]) from exc

    return ChatResponse(
        session_id=session.session_id,
        reply=reply,
        offers=_offer_cards(runner, session, offers_before),
    )


@router.post("/chat/stream")
async def chat_stream(body: ChatRequest, request: Request) -> StreamingResponse:
    tenant_id = _require_tenant(request)
    runner = _runner(request)
    session = _session(request, tenant_id, body.session_id)
    offers_before = session.ranked_offers

    async def events() -> AsyncIterator[str]:
        def sse(data: dict[str, Any]) -> str:
            return f"data: {json.dumps(data, default=str)}\n\n"

        yield sse({"type": "session", "session_id": session.session_id})
        try:
            async for event in runner.chat_events(session, body.message):
                yield sse(event)
        except Exception as exc:
            mapped = _error_status(exc) or (500, "The assistant failed unexpectedly.")
            yield sse({"type": "error", "status": mapped[0], "message": mapped[1]})
            yield sse({"type": "done"})
            return
        yield sse({"type": "offers", "offers": _offer_cards(runner, session, offers_before)})
        yield sse({"type": "done"})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/health", response_model=AgentHealth)
async def agent_health(request: Request) -> AgentHealth:
    _require_tenant(request)
    runner: AgentRunner | None = getattr(request.app.state, "agent_runner", None)
    provider = getattr(request.app.state, "llm_provider", "") or "none"
    if runner is None:
        return AgentHealth(
            ok=False, provider=provider, model="", error="No LLM API key configured."
        )
    t0 = time.monotonic()
    try:
        await runner.client.messages.create(
            model=runner.model,
            max_tokens=16,
            system="Reply with the single word OK.",
            tools=[],
            messages=[{"role": "user", "content": "ping"}],
        )
    except Exception as exc:
        mapped = _error_status(exc)
        return AgentHealth(
            ok=False,
            provider=provider,
            model=runner.model,
            latency_ms=round((time.monotonic() - t0) * 1000),
            error=mapped[1] if mapped else f"{type(exc).__name__}: {exc}"[:300],
        )
    return AgentHealth(
        ok=True,
        provider=provider,
        model=runner.model,
        latency_ms=round((time.monotonic() - t0) * 1000),
        ratelimit=dict(getattr(runner.client, "last_ratelimit", {}) or {}),
    )
