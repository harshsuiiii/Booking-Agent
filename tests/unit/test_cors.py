"""CORS origins: the deployed web app is allowed, and env values are normalised."""

import fakeredis.aioredis
import pytest
from fastapi.testclient import TestClient

from booking_agent.api.app import cors_origins, create_app
from booking_agent.connectors.fake import FakeConnector

VERCEL = "https://booking-agent-lemon.vercel.app"


def test_default_allows_local_dev_and_vercel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    assert cors_origins() == ["http://localhost:3000", VERCEL]


def test_env_strips_trailing_slashes_and_blanks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORS_ORIGINS", f" {VERCEL}/ , ,http://localhost:3000")
    assert cors_origins() == [VERCEL, "http://localhost:3000"]


def test_preflight_from_vercel_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORS_ORIGINS", f"{VERCEL}/")
    client = TestClient(
        create_app(connectors=[FakeConnector()], redis=fakeredis.aioredis.FakeRedis())
    )
    res = client.options(
        "/v1/bookings",
        headers={"Origin": VERCEL, "Access-Control-Request-Method": "GET"},
    )
    assert res.status_code == 200
    assert res.headers["access-control-allow-origin"] == VERCEL
    assert res.headers["access-control-allow-credentials"] == "true"
