"""
API integration tests: SSE contract, re-price flow, tenant isolation.
Uses FakeConnector + fakeredis (no real network or Redis).
"""

import asyncio
import json
from decimal import Decimal

import fakeredis.aioredis
import pytest
from httpx import ASGITransport, AsyncClient

from booking_agent.api.app import create_app
from booking_agent.connectors.errors import PriceChanged
from booking_agent.connectors.fake import FakeConnector

# ── test app fixture ──────────────────────────────────────────────────────────


def _make_app(connectors=None, **kwargs):
    if connectors is None:
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


@pytest.fixture
async def client():
    app = _make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


HEADERS = {"X-API-Key": "dev-key"}
SEARCH_BODY = {
    "origin": "DEL",
    "destination": "BOM",
    "departure_date": "2027-09-01",
}


# ── health / ready ────────────────────────────────────────────────────────────


class TestOps:
    async def test_health(self, client):
        r = await client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    async def test_ready_degraded_without_redis(self):
        # Use a Redis that always fails ping
        import unittest.mock as mock

        app = _make_app()
        app.state.redis = mock.AsyncMock()
        app.state.redis.ping.side_effect = ConnectionError("no redis")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/ready")
        assert r.status_code == 503
        assert r.json()["redis"] is False


# ── POST /v1/search ───────────────────────────────────────────────────────────


class TestStartSearch:
    async def test_returns_search_id(self, client):
        r = await client.post("/v1/search", json=SEARCH_BODY, headers=HEADERS)
        assert r.status_code == 202
        body = r.json()
        assert "search_id" in body
        assert len(body["search_id"]) > 0

    async def test_no_key_is_a_visitor(self, client):
        r = await client.post("/v1/search", json=SEARCH_BODY)
        assert r.status_code == 202  # no login: anyone may search, as an Agent

    async def test_invalid_api_key(self, client):
        r = await client.post("/v1/search", json=SEARCH_BODY, headers={"X-API-Key": "wrong"})
        assert r.status_code == 401

    async def test_invalid_iata_rejected(self, client):
        bad = {**SEARCH_BODY, "origin": "del"}  # lowercase
        r = await client.post("/v1/search", json=bad, headers=HEADERS)
        assert r.status_code == 422


# ── GET /v1/search/{id}/stream ────────────────────────────────────────────────


class TestSSEStream:
    async def test_stream_delivers_events(self):
        connector = FakeConnector(
            supplier_id="fake",
            max_retries=1,
            retry_min_wait=0.0,
            retry_max_wait=0.0,
        )
        app = _make_app(connectors=[connector])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            # Start search
            r = await c.post("/v1/search", json=SEARCH_BODY, headers=HEADERS)
            search_id = r.json()["search_id"]

            # Give background task a moment to run
            await asyncio.sleep(0.05)

            # Collect SSE stream
            lines = []
            async with c.stream("GET", f"/v1/search/{search_id}/stream", headers=HEADERS) as resp:
                assert resp.status_code == 200
                assert "text/event-stream" in resp.headers["content-type"]
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        payload = line[6:]
                        lines.append(payload)
                        if payload == "[DONE]":
                            break

        assert "[DONE]" in lines
        event_types = []
        for line in lines:
            if line != "[DONE]":
                event_types.append(json.loads(line).get("type"))
        assert "source_started" in event_types
        assert "search_complete" in event_types

    async def test_stream_404_for_unknown_id(self, client):
        r = await client.get("/v1/search/bad-id/stream", headers=HEADERS)
        assert r.status_code == 404

    async def test_stream_isolates_tenants(self):
        """Tenant A cannot stream Tenant B's search."""
        import hashlib

        app = _make_app()
        from booking_agent.api import middleware as mw_mod

        tenant_b_key = "tenant-b-secret"
        key_hash = hashlib.sha256(tenant_b_key.encode()).hexdigest()
        mw_mod._REGISTRY[key_hash] = "tenant_b"

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            # Tenant A starts a search
            r = await c.post("/v1/search", json=SEARCH_BODY, headers={"X-API-Key": "dev-key"})
            search_id = r.json()["search_id"]

            # Tenant B tries to access it — auth passes but search belongs to tenant A
            r2 = await c.get(f"/v1/search/{search_id}/stream", headers={"X-API-Key": tenant_b_key})
            assert r2.status_code == 404

        # Clean up
        del mw_mod._REGISTRY[key_hash]


# ── GET /v1/search/{id} snapshot ─────────────────────────────────────────────


class TestSnapshot:
    async def test_snapshot_returns_groups_after_complete(self):
        connector = FakeConnector(
            supplier_id="fake",
            max_retries=1,
            retry_min_wait=0.0,
            retry_max_wait=0.0,
        )
        app = _make_app(connectors=[connector])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/v1/search", json=SEARCH_BODY, headers=HEADERS)
            search_id = r.json()["search_id"]

            # Wait for completion
            await asyncio.sleep(0.1)

            snap = await c.get(f"/v1/search/{search_id}", headers=HEADERS)
        assert snap.status_code == 200
        body = snap.json()
        assert body["search_id"] == search_id
        assert "is_complete" in body
        assert "groups" in body


# ── POST /v1/offers/{id}/price ────────────────────────────────────────────────


class TestRePrice:
    async def test_reprice_returns_priced_offer(self):
        connector = FakeConnector(
            supplier_id="fake",
            max_retries=1,
            retry_min_wait=0.0,
            retry_max_wait=0.0,
        )
        app = _make_app(connectors=[connector])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            # Start search and wait for completion
            r = await c.post("/v1/search", json=SEARCH_BODY, headers=HEADERS)
            search_id = r.json()["search_id"]
            await asyncio.sleep(0.15)

            # Get the snapshot to find an offer_id
            snap = await c.get(f"/v1/search/{search_id}", headers=HEADERS)
            groups = snap.json().get("groups", [])
            if not groups:
                pytest.skip("no offers returned")
            offer_id = groups[0]["primary"]["offer_id"]

            # Re-price it
            price_resp = await c.post(f"/v1/offers/{offer_id}/price", headers=HEADERS)
        assert price_resp.status_code == 200
        body = price_resp.json()
        assert "priced_offer" in body

    async def test_reprice_detects_price_change(self):
        """FakeConnector with price drift triggers PriceChanged response."""
        connector = FakeConnector(
            supplier_id="fake",
            price_drift=Decimal("500"),
            max_retries=1,
            retry_min_wait=0.0,
            retry_max_wait=0.0,
        )

        # Patch price() to raise PriceChanged to test the error path
        async def raising_price(offer):
            raise PriceChanged(
                supplier_id="fake",
                offer_ref=offer.supplier_offer_ref,
                old_price=offer.total_inr,
                new_price=offer.total_inr + Decimal("500"),
            )

        connector._do_price = raising_price

        app = _make_app(connectors=[connector])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/v1/search", json=SEARCH_BODY, headers=HEADERS)
            search_id = r.json()["search_id"]
            await asyncio.sleep(0.15)

            snap = await c.get(f"/v1/search/{search_id}", headers=HEADERS)
            groups = snap.json().get("groups", [])
            if not groups:
                pytest.skip("no offers returned")
            offer_id = groups[0]["primary"]["offer_id"]

            price_resp = await c.post(f"/v1/offers/{offer_id}/price", headers=HEADERS)

        assert price_resp.status_code == 200
        body = price_resp.json()
        assert body["type"] == "price_changed"
        assert "old_price_inr" in body
        assert "new_price_inr" in body

    async def test_reprice_404_unknown_offer(self, client):
        r = await client.post("/v1/offers/nonexistent-offer-id/price", headers=HEADERS)
        assert r.status_code == 404


# ── cache integration ─────────────────────────────────────────────────────────


class TestCacheIntegration:
    async def test_second_search_uses_cache(self):
        """Second identical search gets a cache_hit event, connector called once."""
        connector = FakeConnector(
            supplier_id="fake",
            max_retries=1,
            retry_min_wait=0.0,
            retry_max_wait=0.0,
        )
        app = _make_app(connectors=[connector], cache_ttl=300)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            # First search
            r1 = await c.post("/v1/search", json=SEARCH_BODY, headers=HEADERS)
            assert "search_id" in r1.json()
            await asyncio.sleep(0.15)  # let it complete + cache

            first_count = connector.search_call_count

            # Second identical search
            r2 = await c.post("/v1/search", json=SEARCH_BODY, headers=HEADERS)
            assert "search_id" in r2.json()
            await asyncio.sleep(0.1)

        # Connector should not have been called again
        assert connector.search_call_count == first_count
