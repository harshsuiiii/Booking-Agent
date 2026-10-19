"""
API security middleware: authentication and rate limiting.

Middleware order in app.py (last added = outermost = runs first):
  1. RequestLoggingMiddleware  (innermost — wraps route, sees response status)
  2. RateLimitMiddleware       (middle — keyed by the authenticated user)
  3. AuthMiddleware            (validates the caller first)
  4. CORSMiddleware            (outermost — answers browser preflights)

Two ways in:
  - Web app: signed session cookie (see booking_agent.auth.sessions), set by /v1/auth/login.
  - Scripts: X-API-Key header, only when API_KEY is configured. Acts as Admin in its tenant.
"""

import hashlib
import hmac
import os
import time
from collections import deque
from collections.abc import Awaitable, Callable

from fastapi import status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from booking_agent.auth.principal import Principal
from booking_agent.auth.sessions import (
    SESSION_COOKIE,
    SessionExpired,
    SessionInvalid,
    auth_secret,
    decode_session_token,
)
from booking_agent.auth.users import DEMO_TENANT_ID

# Paths that skip authentication entirely.
_AUTH_EXCLUDED: frozenset[str] = frozenset(
    {
        "/health",
        "/ready",
        "/docs",
        "/redoc",
        "/openapi.json",
        "/v1/auth/login",
        "/v1/auth/demo",
        "/v1/auth/logout",
        "/v1/auth/config",
    }
)


# ── API keys (scripts) ────────────────────────────────────────────────────────


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _build_registry() -> dict[str, str]:
    """Key hash → tenant_id. Empty unless API_KEY is set: there is no default key."""
    key = os.getenv("API_KEY")
    if not key:
        return {}
    return {_hash_key(key): os.getenv("API_KEY_TENANT", DEMO_TENANT_ID)}


# Filled on first use, not at import: the package can be imported before .env is loaded
# (booking_agent.api.main imports booking_agent.api, then loads .env). Tests add keys here.
_REGISTRY: dict[str, str] = {}
_env_keys_loaded = False


def _registry() -> dict[str, str]:
    global _env_keys_loaded
    if not _env_keys_loaded:
        for hashed, tenant_id in _build_registry().items():
            _REGISTRY.setdefault(hashed, tenant_id)
        _env_keys_loaded = True
    return _REGISTRY


def _api_key_principal(api_key: str) -> Principal | None:
    hashed = _hash_key(api_key)
    for known, tenant_id in _registry().items():
        if hmac.compare_digest(hashed, known):
            return Principal(
                user_id=f"api-key:{tenant_id}",
                email="api-key",
                name="API key",
                role="admin",
                tenant_id=tenant_id,
                via="api_key",
            )
    return None


def _unauthorized(detail: str, code: str, clear_cookie: bool = False) -> JSONResponse:
    response = JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED, content={"detail": detail, "code": code}
    )
    if clear_cookie:
        response.delete_cookie(SESSION_COOKIE, path="/")
    return response


# ── auth middleware ───────────────────────────────────────────────────────────


class AuthMiddleware(BaseHTTPMiddleware):
    """
    Resolves the caller into request.state.principal (and tenant_id / actor).

    401 responses carry a machine-readable `code` so the UI can tell
    "please sign in" (auth_required) from "your session expired" (session_expired).
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.url.path in _AUTH_EXCLUDED or request.url.path.startswith("/openapi"):
            return await call_next(request)

        principal: Principal | None = None
        api_key = request.headers.get("X-API-Key", "")
        token = request.cookies.get(SESSION_COOKIE)

        if api_key:
            principal = _api_key_principal(api_key)
            if principal is None:
                return _unauthorized("Invalid API key", "invalid_api_key")
        elif token:
            secret = auth_secret()
            if secret is None:
                return _unauthorized("Login is not configured", "auth_required", True)
            try:
                principal = decode_session_token(token, secret)
            except SessionExpired:
                return _unauthorized(
                    "Your session has expired. Please sign in again.", "session_expired", True
                )
            except SessionInvalid:
                return _unauthorized("Please sign in again.", "session_invalid", True)
        else:
            return _unauthorized("Authentication required", "auth_required")

        request.state.principal = principal
        request.state.tenant_id = principal.tenant_id
        request.state.actor = principal.email
        return await call_next(request)


# ── rate limiter ──────────────────────────────────────────────────────────────


class RateLimiter:
    """
    Sliding-window rate limiter (in-process).

    For production, swap the in-memory dict for a Redis atomic counter
    (INCR + EXPIRE) to work correctly across multiple app instances.
    """

    def __init__(self, max_requests: int = 60, window_seconds: float = 60.0) -> None:
        self._max = max_requests
        self._window = window_seconds
        self._windows: dict[str, deque[float]] = {}

    def is_allowed(self, key: str) -> bool:
        """Return True and record the request, or return False if rate exceeded."""
        now = time.monotonic()
        cutoff = now - self._window

        if key not in self._windows:
            self._windows[key] = deque()
        window = self._windows[key]

        # Evict expired entries
        while window and window[0] < cutoff:
            window.popleft()

        if len(window) >= self._max:
            return False
        window.append(now)
        return True

    def reset(self, key: str) -> None:
        """Clear the window for a key (useful in tests)."""
        self._windows.pop(key, None)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Enforces per-tenant request rate limits.

    Skips auth-excluded paths. Uses tenant_id from request.state (set by
    AuthMiddleware which runs first). Falls back to remote IP if not set.
    """

    def __init__(self, app: object, rate_limiter: RateLimiter) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._limiter = rate_limiter

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.url.path in _AUTH_EXCLUDED:
            return await call_next(request)

        principal = getattr(request.state, "principal", None)
        key = principal.user_id if principal is not None else None
        if key is None:
            key = request.client.host if request.client else "unknown"

        if not self._limiter.is_allowed(key):
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={"detail": "Rate limit exceeded. Retry after 60 seconds."},
                headers={"Retry-After": "60"},
            )
        return await call_next(request)
