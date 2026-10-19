"""
Signed session tokens (JWT, HS256) carried in an httpOnly cookie.

The token holds the user's id, email, name, role and tenant, so requests need no
database lookup. Rotating AUTH_SECRET signs everyone out.
"""

import os
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from booking_agent.auth.principal import Principal

SESSION_COOKIE = "ba_session"
_ALGORITHM = "HS256"


class SessionExpired(Exception):
    """The session token was valid but its lifetime has passed."""


class SessionInvalid(Exception):
    """The session token is malformed, tampered with, or signed with another key."""


def auth_secret() -> str | None:
    return os.getenv("AUTH_SECRET") or None


def session_hours() -> float:
    return float(os.getenv("AUTH_SESSION_HOURS", "8"))


def issue_session_token(principal: Principal, secret: str, hours: float | None = None) -> str:
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "sub": principal.user_id,
        "email": principal.email,
        "name": principal.name,
        "role": principal.role,
        "tenant": principal.tenant_id,
        "iat": now,
        "exp": now + timedelta(hours=hours if hours is not None else session_hours()),
        "typ": "session",
    }
    return jwt.encode(claims, secret, algorithm=_ALGORITHM)


def decode_session_token(token: str, secret: str) -> Principal:
    try:
        claims = jwt.decode(
            token, secret, algorithms=[_ALGORITHM], options={"require": ["exp", "sub"]}
        )
    except jwt.ExpiredSignatureError as exc:
        raise SessionExpired from exc
    except jwt.PyJWTError as exc:
        raise SessionInvalid from exc
    role = claims.get("role")
    if claims.get("typ") != "session" or role not in ("agent", "admin"):
        raise SessionInvalid
    return Principal(
        user_id=str(claims["sub"]),
        email=str(claims.get("email", "")),
        name=str(claims.get("name", "")),
        role=role,
        tenant_id=str(claims.get("tenant", "")),
        via="session",
    )
