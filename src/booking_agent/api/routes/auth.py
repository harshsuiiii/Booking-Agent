"""
Demo-grade login.

POST /v1/auth/login   {email, password} → sets the session cookie, returns the user
POST /v1/auth/demo    signs in as the demo Agent (never Admin); only when DEMO_MODE=true
POST /v1/auth/logout  clears the cookie
GET  /v1/auth/me      the signed-in user
GET  /v1/auth/config  public: which demo accounts exist (emails and roles, never passwords)

No sign-up, password reset or social login.
"""

import os
import time
from collections import deque
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel

from booking_agent.auth.passwords import DUMMY_HASH, verify_password
from booking_agent.auth.principal import Principal, get_principal
from booking_agent.auth.sessions import (
    SESSION_COOKIE,
    auth_secret,
    issue_session_token,
    session_hours,
)
from booking_agent.auth.users import DEMO_ACCOUNTS, UserRepository, demo_mode

router = APIRouter(prefix="/v1/auth", tags=["auth"])

_LOGIN_ATTEMPTS_PER_MINUTE = 10


class LoginRequest(BaseModel):
    email: str
    password: str


class UserOut(BaseModel):
    email: str
    name: str
    role: Literal["agent", "admin"]
    tenant_id: str


class DemoAccountOut(BaseModel):
    email: str
    role: Literal["agent", "admin"]
    available: bool


class AuthConfig(BaseModel):
    login_enabled: bool
    demo_mode: bool
    demo_accounts: list[DemoAccountOut]


def _user_out(p: Principal) -> UserOut:
    return UserOut(email=p.email, name=p.name, role=p.role, tenant_id=p.tenant_id)


def _users(request: Request) -> UserRepository:
    repo: UserRepository = request.app.state.users
    return repo


def _throttle(request: Request) -> None:
    """At most N login attempts per client IP per minute (brute-force guard)."""
    attempts: dict[str, deque[float]] = request.app.state.login_attempts
    ip = request.client.host if request.client else "unknown"
    window = attempts.setdefault(ip, deque())
    now = time.monotonic()
    while window and window[0] < now - 60:
        window.popleft()
    if len(window) >= _LOGIN_ATTEMPTS_PER_MINUTE:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many sign-in attempts. Wait a minute and try again.",
        )
    window.append(now)


def _start_session(response: Response, principal: Principal) -> None:
    secret = auth_secret()
    if secret is None:
        raise HTTPException(status_code=503, detail="Login is not configured (AUTH_SECRET).")
    response.set_cookie(
        SESSION_COOKIE,
        issue_session_token(principal, secret),
        max_age=int(session_hours() * 3600),
        httponly=True,
        samesite="lax",
        secure=os.getenv("COOKIE_SECURE", "false").lower() == "true",
        path="/",
    )


@router.get("/config", response_model=AuthConfig)
async def auth_config(request: Request) -> AuthConfig:
    users = _users(request)
    accounts = [
        DemoAccountOut(
            email=a.email,
            role=a.role,
            available=await users.get_by_email(a.email) is not None,
        )
        for a in DEMO_ACCOUNTS
    ]
    return AuthConfig(
        login_enabled=auth_secret() is not None,
        demo_mode=demo_mode(),
        demo_accounts=accounts if demo_mode() else [],
    )


@router.post("/login", response_model=UserOut)
async def login(body: LoginRequest, request: Request, response: Response) -> UserOut:
    _throttle(request)
    user = await _users(request).get_by_email(body.email)
    # Always run one hash check, so unknown emails take as long as wrong passwords.
    ok = verify_password(body.password, user.password_hash if user else DUMMY_HASH)
    if user is None or not ok:
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    principal = user.principal()
    _start_session(response, principal)
    return _user_out(principal)


@router.post("/demo", response_model=UserOut)
async def demo_login(request: Request, response: Response) -> UserOut:
    if not demo_mode():
        raise HTTPException(status_code=404, detail="Demo login is disabled.")
    agent = next(a for a in DEMO_ACCOUNTS if a.role == "agent")
    user = await _users(request).get_by_email(agent.email)
    if user is None:
        raise HTTPException(
            status_code=503, detail="Demo agent is not set up (DEMO_AGENT_PASSWORD)."
        )
    principal = user.principal()
    _start_session(response, principal)
    return _user_out(principal)


@router.post("/logout", status_code=204)
async def logout(response: Response) -> Response:
    response.status_code = 204
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.get("/me", response_model=UserOut)
async def me(request: Request) -> UserOut:
    return _user_out(get_principal(request))
