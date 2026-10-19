"""
FastAPI/Starlette middleware: request logging with trace-ID injection.

Adds X-Trace-Id to every response and emits a structured log event for each
HTTP request including method, path, status code, and latency.
"""

import time
import uuid
from collections.abc import Awaitable, Callable

import structlog
from opentelemetry import trace
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

_log = structlog.get_logger(__name__)


def _trace_id() -> str:
    """Return the current OTel trace ID, or generate a random one."""
    ctx = trace.get_current_span().get_span_context()
    if ctx.is_valid:
        return format(ctx.trace_id, "032x")
    return uuid.uuid4().hex


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """
    Logs every HTTP request with method, path, status, latency, and trace_id.

    Should be innermost middleware so it can capture the actual response status.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        start = time.monotonic()
        trace_id = _trace_id()
        request.state.trace_id = trace_id

        response = await call_next(request)

        latency_ms = round((time.monotonic() - start) * 1000, 2)
        _log.info(
            "http.request",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            latency_ms=latency_ms,
            trace_id=trace_id,
            tenant_id=getattr(request.state, "tenant_id", None),
        )
        response.headers["X-Trace-Id"] = trace_id
        return response
