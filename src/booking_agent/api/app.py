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
from contextlib import asynccontextmanager, suppress
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy import text

from booking_agent.agent.runner import AgentRunner
from booking_agent.agent.session import AgentSession
from booking_agent.api.middleware import AuthMiddleware, RateLimiter, RateLimitMiddleware
from booking_agent.api.routes.admin import router as admin_router
from booking_agent.api.routes.agent import router as agent_router
from booking_agent.api.routes.airports import router as airports_router
from booking_agent.api.routes.booking import router as booking_router
from booking_agent.api.routes.dashboard import router as dashboard_router
from booking_agent.api.routes.hotel_search import router as hotel_search_router
from booking_agent.api.routes.price import router as price_router
from booking_agent.api.routes.search import router as search_router
from booking_agent.api.session import SearchSession
from booking_agent.booking.encryption import check_encryption_key
from booking_agent.booking.payment_gateway import FakePaymentGateway
from booking_agent.booking.repository import (
    BookingRepository,
    InMemoryBookingRepository,
    SqlBookingRepository,
)
from booking_agent.booking.service import BookingService
from booking_agent.cache.search_cache import SearchCache
from booking_agent.connectors.fake import FakeConnector
from booking_agent.connectors.fake_hotel import FakeHotelConnector
from booking_agent.connectors.hotel_protocol import HotelConnector
from booking_agent.connectors.protocol import Connector
from booking_agent.dashboard.demo import demo_markup_rules, demo_mode, seed_demo_bookings
from booking_agent.dashboard.recent_searches import (
    InMemoryRecentSearchStore,
    RecentSearchStore,
    SqlRecentSearchStore,
)
from booking_agent.db.session import AsyncSessionFactory, create_engine
from booking_agent.observability.middleware import RequestLoggingMiddleware
from booking_agent.observability.redaction import install_log_redaction
from booking_agent.observability.setup import configure_tracing
from booking_agent.orchestrator.hotel_search import HotelSearchOrchestrator
from booking_agent.orchestrator.search import SearchOrchestrator

log = structlog.get_logger(__name__)


ALL_SUPPLIERS = ("duffel", "sim_gds", "sim_consolidator", "sim_ota")


# Local dev UI and the deployed web app (Vercel). CORS_ORIGINS replaces this list.
DEFAULT_CORS_ORIGINS = "http://localhost:3000,https://booking-agent-lemon.vercel.app"


def cors_origins() -> list[str]:
    """Allowed browser origins from CORS_ORIGINS (comma list).

    Browsers send Origin without a trailing slash, so "https://x.app/" would never match;
    strip slashes and blanks.
    """
    raw = os.getenv("CORS_ORIGINS") or DEFAULT_CORS_ORIGINS
    return [o.strip().rstrip("/") for o in raw.split(",") if o.strip()]


def enabled_suppliers() -> list[str]:
    """SUPPLIERS_ENABLED (comma list); unset or "all" = every supplier we know."""
    raw = os.getenv("SUPPLIERS_ENABLED", "all").strip().lower()
    if raw in ("", "all"):
        return list(ALL_SUPPLIERS)
    return [s.strip() for s in raw.split(",") if s.strip()]


def _build_connectors(status: dict[str, str] | None = None) -> list[Connector]:
    """
    Build connectors from env. `status` records why each supplier is on or off (for /ready).

    Duffel is skipped, never fatal, when it's disabled, has no token, or the token isn't a
    test token: search keeps working with the other suppliers.
    """
    # AmadeusConnector: enterprise-only (shut down July 2026 for self-service); not wired.
    status = status if status is not None else {}
    wanted = enabled_suppliers()
    connectors: list[Connector] = []
    for unknown in [w for w in wanted if w not in ALL_SUPPLIERS]:
        # A typo here silently disables a supplier, so say so loudly.
        log.warning("supplier.unknown_id", supplier_id=unknown, known=list(ALL_SUPPLIERS))
        status[unknown] = f"unknown supplier id (known: {', '.join(ALL_SUPPLIERS)})"

    if "duffel" not in wanted:
        status["duffel"] = "off (SUPPLIERS_ENABLED)"
    elif not os.getenv("DUFFEL_ACCESS_TOKEN"):
        status["duffel"] = "off (no DUFFEL_ACCESS_TOKEN)"
    else:
        from booking_agent.connectors.duffel import DuffelConnector
        from booking_agent.connectors.duffel.connector import DuffelTokenRefused
        from booking_agent.connectors.duffel_fx import FrankfurterFxProvider
        from booking_agent.connectors.secrets import EnvSecretsProvider

        try:
            connectors.append(
                DuffelConnector(secrets=EnvSecretsProvider(), fx_provider=FrankfurterFxProvider())
            )
            status["duffel"] = "on (sandbox)"
        except DuffelTokenRefused as exc:
            # Refuse to run Duffel with a live token; keep the app (and other suppliers) up.
            log.error("supplier.refused", supplier_id="duffel", reason=str(exc))
            status["duffel"] = "refused (token is not duffel_test_)"

    from booking_agent.simulated.connector import simulated_connectors_from_env

    sims = simulated_connectors_from_env([s for s in wanted if s.startswith("sim_")])
    connectors.extend(sims)
    for sid in ALL_SUPPLIERS[1:]:
        status[sid] = "on (simulated)" if sid in wanted else "off (SUPPLIERS_ENABLED)"

    if not connectors:
        connectors.append(FakeConnector(supplier_id="demo"))
        status["demo"] = "on (no other supplier enabled)"
    return connectors


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Enter flight connectors
    for c in app.state.connectors.values():
        await c.__aenter__()
    # Enter hotel connectors
    for c in app.state.hotel_connectors:
        await c.__aenter__()
    if demo_mode():
        bookings = await seed_demo_bookings(app.state.booking_service.repository)
        log.info("demo.ready", bookings_created=bookings)
    yield
    # Exit flight connectors
    for c in app.state.connectors.values():
        with suppress(Exception):
            await c.__aexit__(None, None, None)
    # Exit hotel connectors
    for c in app.state.hotel_connectors:
        with suppress(Exception):
            await c.__aexit__(None, None, None)
    if app.state.db_engine is not None:
        await app.state.db_engine.dispose()
    runner = app.state.agent_runner
    if runner is not None and hasattr(runner.client, "aclose"):
        await runner.client.aclose()


def create_app(
    connectors: list[Connector] | None = None,
    redis: Redis | None = None,
    cache_ttl: int = 300,
    supplier_timeout: float = 7.0,
    db_url: str | None = None,
    anthropic_client: Any = None,
    booking_service: BookingService | None = None,
    rate_limiter: RateLimiter | None = None,
    enable_tracing: bool = False,
) -> FastAPI:
    supplier_status: dict[str, str] = {}
    if connectors is None:
        connectors = _build_connectors(supplier_status)

    if redis is None:
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        if redis_url == "memory://":
            # Local dev without a Redis server: in-process fake (dev dependency).
            from fakeredis import FakeAsyncRedis

            redis = FakeAsyncRedis()
        else:
            redis = Redis.from_url(redis_url)

    install_log_redaction()

    if enable_tracing:
        configure_tracing(
            service_name="booking-agent",
            otlp_endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"),
        )

    app = FastAPI(title="Booking Agent API", version="0.1.0", lifespan=_lifespan)

    # Inject state before lifespan so it's accessible in _lifespan
    connectors_by_id: dict[str, Connector] = {c.capabilities.supplier_id: c for c in connectors}
    app.state.connectors = connectors_by_id
    app.state.supplier_status = supplier_status or {sid: "on" for sid in connectors_by_id}
    app.state.redis = redis
    app.state.cache = SearchCache(redis, ttl_seconds=cache_ttl)
    search_registry: dict[str, SearchSession] = {}
    app.state.search_registry = search_registry
    orch = SearchOrchestrator(
        connectors=connectors,
        supplier_timeout=supplier_timeout,
        # Demo agency pricing (4% + ₹199) so sell price, markup and net differ.
        default_rules=demo_markup_rules() if demo_mode() else None,
    )
    app.state.orchestrator = orch
    agent_sessions: dict[str, AgentSession] = {}
    app.state.agent_sessions = agent_sessions

    # Storage: SQL (Postgres or SQLite) when DATABASE_URL is set, else in memory.
    app.state.db_engine = None
    database_url = db_url or os.getenv("DATABASE_URL")
    session_factory = None
    if database_url:
        # Persistent passengers need a stable key, or a restart makes them unreadable.
        check_encryption_key()
        engine = create_engine(database_url)
        app.state.db_engine = engine
        session_factory = AsyncSessionFactory(engine)

    recent: RecentSearchStore = (
        SqlRecentSearchStore(session_factory) if session_factory else InMemoryRecentSearchStore()
    )
    app.state.recent_searches = recent

    if booking_service is None:
        repository: BookingRepository = (
            SqlBookingRepository(session_factory)
            if session_factory
            else InMemoryBookingRepository()
        )
        booking_service = BookingService(
            repository=repository,
            connectors=connectors_by_id,
            payment_gateway=FakePaymentGateway(),
        )
    app.state.booking_service = booking_service

    # Agent runner: Groq → Cerebras → Gemini → Anthropic; injected client in tests.
    _client = anthropic_client
    _model = "openai/gpt-oss-120b"
    _provider = "test" if anthropic_client is not None else ""

    if _client is None:
        from booking_agent.agent.llm_adapters import (
            CerebrasClientAdapter,
            GeminiClientAdapter,
            GroqClientAdapter,
        )

        groq_key = os.getenv("GROQ_API_KEY")
        cerebras_key = os.getenv("CEREBRAS_API_KEY")
        gemini_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        anthropic_key = os.getenv("ANTHROPIC_API_KEY")

        if groq_key:
            _client = GroqClientAdapter(api_key=groq_key)
            _model = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
            _provider = "Groq"
        elif cerebras_key:
            _client = CerebrasClientAdapter(api_key=cerebras_key)
            _model = os.getenv("CEREBRAS_MODEL", "gpt-oss-120b")
            _provider = "Cerebras"
        elif gemini_key:
            _client = GeminiClientAdapter(api_key=gemini_key)
            _model = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
            _provider = "Gemini"
        elif anthropic_key and os.getenv("ANTHROPIC_MODEL"):
            import anthropic  # optional dependency

            _client = anthropic.AsyncAnthropic()
            _model = os.environ["ANTHROPIC_MODEL"]
            _provider = "Anthropic"

    app.state.llm_provider = _provider
    if _client is not None:
        app.state.agent_runner = AgentRunner(
            client=_client,
            orchestrator=orch,
            model=_model,
            cache=app.state.cache,
            recent_searches=app.state.recent_searches,
        )
    else:
        app.state.agent_runner = None

    # Hotel vertical
    hotel_connectors: list[HotelConnector] = [FakeHotelConnector(supplier_id="fake_hotel")]
    app.state.hotel_connectors = hotel_connectors
    hotel_orch = HotelSearchOrchestrator(connectors=hotel_connectors)
    app.state.hotel_orchestrator = hotel_orch
    hotel_search_registry: dict[str, Any] = {}
    app.state.hotel_search_registry = hotel_search_registry

    app.include_router(dashboard_router)
    app.include_router(search_router)
    app.include_router(airports_router)
    app.include_router(price_router)
    app.include_router(admin_router)
    app.include_router(agent_router)
    app.include_router(booking_router)
    app.include_router(hotel_search_router)

    # Middleware order: last added = outermost = runs first on request.
    # Execution order on request: AuthMiddleware → RateLimitMiddleware → RequestLoggingMiddleware
    app.add_middleware(RequestLoggingMiddleware)
    _limiter = rate_limiter or RateLimiter(
        max_requests=int(os.getenv("RATE_LIMIT_MAX_REQUESTS", "300")),
        window_seconds=float(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60")),
    )
    app.add_middleware(RateLimitMiddleware, rate_limiter=_limiter)
    app.add_middleware(AuthMiddleware)
    # Outermost: answer browser preflight (OPTIONS) before auth rejects it.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

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
        db_ok: bool | None = None  # None: no database configured (in-memory storage)
        engine = request.app.state.db_engine
        if engine is not None:
            try:
                async with engine.connect() as conn:
                    await conn.execute(text("SELECT 1"))
                db_ok = True
            except Exception:
                db_ok = False
        ok = redis_ok and db_ok is not False
        return JSONResponse(
            status_code=200 if ok else 503,
            content={
                "status": "ok" if ok else "degraded",
                "redis": redis_ok,
                "db": db_ok,
                "suppliers": request.app.state.supplier_status,
            },
        )

    return app
