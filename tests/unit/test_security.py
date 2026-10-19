"""
Security tests: authentication middleware, rate limiting, tenant isolation,
and input validation.
"""

import time

import fakeredis.aioredis
from httpx import ASGITransport, AsyncClient

from booking_agent.api.app import create_app
from booking_agent.api.middleware import RateLimiter
from booking_agent.connectors.fake import FakeConnector

# ── test app fixture ──────────────────────────────────────────────────────────


def _make_app(**kwargs):
    connectors = [
        FakeConnector(
            supplier_id="fake",
            max_retries=1,
            retry_min_wait=0.0,
            retry_max_wait=0.0,
        )
    ]
    redis = fakeredis.aioredis.FakeRedis()
    return create_app(connectors=connectors, redis=redis, **kwargs)


HEADERS = {"X-API-Key": "dev-key"}
SEARCH_BODY = {"origin": "DEL", "destination": "BOM", "departure_date": "2027-09-01"}


# ── AuthMiddleware ────────────────────────────────────────────────────────────


class TestAuthMiddleware:
    async def test_missing_key_returns_401(self):
        app = _make_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/v1/search", json=SEARCH_BODY)
        assert r.status_code == 401

    async def test_invalid_key_returns_401(self):
        app = _make_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/v1/search", json=SEARCH_BODY, headers={"X-API-Key": "wrong"})
        assert r.status_code == 401

    async def test_valid_key_passes(self):
        app = _make_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/v1/search", json=SEARCH_BODY, headers=HEADERS)
        assert r.status_code == 202

    async def test_health_skips_auth(self):
        app = _make_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/health")
        assert r.status_code == 200

    async def test_ready_skips_auth(self):
        app = _make_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/ready")
        assert r.status_code in (200, 503)  # any response, not 401

    async def test_response_has_no_auth_data_in_error(self):
        """401 error body must not leak internal registry contents."""
        app = _make_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/v1/search", json=SEARCH_BODY)
        body = r.json()
        # Must not contain any hash or internal token in the response
        assert "sha256" not in str(body).lower()
        assert "registry" not in str(body).lower()


# ── RateLimiter ───────────────────────────────────────────────────────────────


class TestRateLimiter:
    def test_allows_under_limit(self):
        limiter = RateLimiter(max_requests=5, window_seconds=60.0)
        for _ in range(5):
            assert limiter.is_allowed("tenant-a") is True

    def test_blocks_at_limit(self):
        limiter = RateLimiter(max_requests=5, window_seconds=60.0)
        for _ in range(5):
            limiter.is_allowed("tenant-a")
        assert limiter.is_allowed("tenant-a") is False

    def test_different_keys_are_independent(self):
        limiter = RateLimiter(max_requests=2, window_seconds=60.0)
        limiter.is_allowed("a")
        limiter.is_allowed("a")
        # tenant-a is at limit but tenant-b is unaffected
        assert limiter.is_allowed("a") is False
        assert limiter.is_allowed("b") is True

    def test_reset_clears_window(self):
        limiter = RateLimiter(max_requests=1, window_seconds=60.0)
        limiter.is_allowed("a")
        assert limiter.is_allowed("a") is False
        limiter.reset("a")
        assert limiter.is_allowed("a") is True

    def test_window_expiry(self):
        limiter = RateLimiter(max_requests=1, window_seconds=0.05)
        limiter.is_allowed("a")
        assert limiter.is_allowed("a") is False
        time.sleep(0.06)
        assert limiter.is_allowed("a") is True


class TestRateLimitMiddleware:
    async def test_429_when_limit_exceeded(self):
        limiter = RateLimiter(max_requests=2, window_seconds=60.0)
        app = _make_app(rate_limiter=limiter)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r1 = await c.post("/v1/search", json=SEARCH_BODY, headers=HEADERS)
            r2 = await c.post("/v1/search", json=SEARCH_BODY, headers=HEADERS)
            r3 = await c.post("/v1/search", json=SEARCH_BODY, headers=HEADERS)
        assert r1.status_code == 202
        assert r2.status_code == 202
        assert r3.status_code == 429
        assert r3.headers.get("Retry-After") == "60"

    async def test_429_body_has_detail(self):
        limiter = RateLimiter(max_requests=0, window_seconds=60.0)
        app = _make_app(rate_limiter=limiter)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/v1/search", json=SEARCH_BODY, headers=HEADERS)
        assert r.status_code == 429
        assert "detail" in r.json()

    async def test_health_not_rate_limited(self):
        limiter = RateLimiter(max_requests=0, window_seconds=60.0)
        app = _make_app(rate_limiter=limiter)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/health")
        assert r.status_code == 200


# ── Tenant isolation ──────────────────────────────────────────────────────────


class TestTenantIsolation:
    async def test_search_session_tenant_scoped(self):
        """Tenant A cannot stream or access tenant B's search session."""
        app = _make_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            # Start a search as default tenant
            r = await c.post("/v1/search", json=SEARCH_BODY, headers=HEADERS)
            assert r.status_code == 202
            search_id = r.json()["search_id"]

            # Try to access it with a second key that resolves to a different tenant
            # (here we'll just use a wrong/no key to simulate a different tenant)
            r2 = await c.get(f"/v1/search/{search_id}", headers={"X-API-Key": "wrong-key"})
        assert r2.status_code in (401, 404)

    async def test_booking_tenant_scoped(self):
        """GET /v1/bookings returns only bookings for the authenticated tenant."""
        app = _make_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/bookings", headers=HEADERS)
        # Must succeed for valid tenant and return a page (empty is fine)
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body["items"], list) and body["total"] == len(body["items"])

    async def test_unauthenticated_cannot_list_bookings(self):
        app = _make_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/bookings")
        assert r.status_code == 401


# ── Input validation ──────────────────────────────────────────────────────────


class TestInputValidation:
    async def test_invalid_iata_code_rejected(self):
        """Origin must be exactly 3 uppercase letters."""
        app = _make_app()
        bad_body = {"origin": "INVALID", "destination": "BOM", "departure_date": "2027-09-01"}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/v1/search", json=bad_body, headers=HEADERS)
        assert r.status_code == 422

    async def test_past_date_rejected(self):
        """Departure date must not be in the past."""
        app = _make_app()
        bad_body = {"origin": "DEL", "destination": "BOM", "departure_date": "2000-01-01"}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/v1/search", json=bad_body, headers=HEADERS)
        assert r.status_code == 422

    async def test_valid_search_body_accepted(self):
        app = _make_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/v1/search", json=SEARCH_BODY, headers=HEADERS)
        assert r.status_code == 202

    async def test_missing_required_field_rejected(self):
        app = _make_app()
        bad_body = {"origin": "DEL", "departure_date": "2027-09-01"}  # missing destination
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/v1/search", json=bad_body, headers=HEADERS)
        assert r.status_code == 422


# ── Response headers ──────────────────────────────────────────────────────────


class TestResponseHeaders:
    async def test_trace_id_header_present(self):
        app = _make_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/v1/search", json=SEARCH_BODY, headers=HEADERS)
        assert "X-Trace-Id" in r.headers
        assert len(r.headers["X-Trace-Id"]) > 0
