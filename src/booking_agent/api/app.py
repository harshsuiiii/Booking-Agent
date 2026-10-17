"""
FastAPI application factory.

Production usage::

    app = create_app()
    uvicorn.run(app, host="0.0.0.0", port=8000)

Test usage::

    app = create_app(connectors=[FakeConnector()], redis=FakeRedis())
"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from redis.asyncio import Redis

from booking_agent.api.routes.price import router as price_router
from booking_agent.api.routes.search import router as search_router
from booking_agent.api.session import SearchSession
from booking_agent.cache.search_cache import SearchCache
from booking_agent.connectors.fake import FakeConnector
from booking_agent.connectors.protocol import Connector
from booking_agent.orchestrator.search import SearchOrchestrator


def _build_connectors() -> list[Connector]:
    """Build production connectors from env vars. Extend as suppliers are added."""
    # Stub: only FakeConnector for now; AmadeusConnector added when credentials present
    connectors: list[Connector] = []
    if os.getenv("AMADEUS_CLIENT_ID") and os.getenv("AMADEUS_CLIENT_SECRET"):
        from booking_agent.connectors.amadeus import AmadeusConnector
        from booking_agent.connectors.secrets import EnvSecretsProvider
        connectors.append(AmadeusConnector(secrets=EnvSecretsProvider()))
    if not connectors:
        connectors.append(FakeConnector(supplier_id="demo"))
    return connectors


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Enter all connectors
    for c in app.state.connectors.values():
        await c.__aenter__()
    yield
    # Exit all connectors
    for c in app.state.connectors.values():
        try:
            await c.__aexit__(None, None, None)
        except Exception:
            pass


def create_app(
    connectors: list[Connector] | None = None,
    redis: Redis | None = None,
    cache_ttl: int = 300,
    supplier_timeout: float = 7.0,
    db_url: str | None = None,
) -> FastAPI:
    if connectors is None:
        connectors = _build_connectors()

    if redis is None:
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        redis = Redis.from_url(redis_url)

    app = FastAPI(title="Booking Agent API", version="0.1.0", lifespan=_lifespan)

    # Inject state before lifespan so it's accessible in _lifespan
    connectors_by_id: dict[str, Connector] = {
        c.capabilities.supplier_id: c for c in connectors
    }
    app.state.connectors = connectors_by_id
    app.state.redis = redis
    app.state.cache = SearchCache(redis, ttl_seconds=cache_ttl)
    app.state.search_registry: dict[str, SearchSession] = {}
    app.state.orchestrator = SearchOrchestrator(
        connectors=connectors,
        supplier_timeout=supplier_timeout,
    )

    app.include_router(search_router)
    app.include_router(price_router)

    @app.get("/health", tags=["ops"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready", tags=["ops"])
    async def ready(request: Request) -> JSONResponse:
        redis_ok = False
        try:
            await request.app.state.redis.ping()
            redis_ok = True
        except Exception:
            pass
        ok = redis_ok
        return JSONResponse(
            status_code=200 if ok else 503,
            content={"status": "ok" if ok else "degraded", "redis": redis_ok},
        )

    return app
