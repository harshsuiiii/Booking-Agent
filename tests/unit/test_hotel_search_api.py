"""Hotel search API tests."""

import asyncio
import json

import fakeredis.aioredis
from httpx import ASGITransport, AsyncClient

from booking_agent.api.app import create_app

HEADERS = {"X-API-Key": "dev-key"}
HOTEL_BODY = {
    "location": "Mumbai",
    "checkin_date": "2027-09-01",
    "checkout_date": "2027-09-05",
}


def _make_app(**kwargs):
    from booking_agent.connectors.fake import FakeConnector

    connectors = kwargs.pop(
        "connectors",
        [
            FakeConnector(
                supplier_id="fake",
                max_retries=1,
                retry_min_wait=0.0,
                retry_max_wait=0.0,
            )
        ],
    )
    redis = kwargs.pop("redis", fakeredis.aioredis.FakeRedis())
    return create_app(connectors=connectors, redis=redis, **kwargs)


class TestHotelSearchStart:
    async def test_returns_202_with_search_id(self):
        app = _make_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/v1/hotels/search", json=HOTEL_BODY, headers=HEADERS)
        assert r.status_code == 202
        body = r.json()
        assert "search_id" in body
        assert len(body["search_id"]) > 0

    async def test_requires_auth(self):
        app = _make_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/v1/hotels/search", json=HOTEL_BODY)
        assert r.status_code == 401

    async def test_past_checkin_rejected(self):
        app = _make_app()
        bad = {**HOTEL_BODY, "checkin_date": "2020-01-01", "checkout_date": "2020-01-05"}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/v1/hotels/search", json=bad, headers=HEADERS)
        assert r.status_code == 422

    async def test_checkout_before_checkin_rejected(self):
        app = _make_app()
        bad = {**HOTEL_BODY, "checkin_date": "2027-09-05", "checkout_date": "2027-09-01"}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/v1/hotels/search", json=bad, headers=HEADERS)
        assert r.status_code == 422

    async def test_missing_location_rejected(self):
        app = _make_app()
        bad = {"checkin_date": "2027-09-01", "checkout_date": "2027-09-05"}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/v1/hotels/search", json=bad, headers=HEADERS)
        assert r.status_code == 422


class TestHotelSearchStream:
    async def test_stream_delivers_events(self):
        app = _make_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/v1/hotels/search", json=HOTEL_BODY, headers=HEADERS)
            assert r.status_code == 202
            search_id = r.json()["search_id"]

            await asyncio.sleep(0.1)

            lines = []
            async with c.stream(
                "GET", f"/v1/hotels/search/{search_id}/stream", headers=HEADERS
            ) as resp:
                assert resp.status_code == 200
                assert "text/event-stream" in resp.headers["content-type"]
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        payload = line[6:]
                        lines.append(payload)
                        if payload == "[DONE]":
                            break

        assert "[DONE]" in lines
        event_types = [json.loads(line).get("type") for line in lines if line != "[DONE]"]
        assert "source_started" in event_types
        assert "search_complete" in event_types

    async def test_stream_404_for_unknown_id(self):
        app = _make_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/hotels/search/bad-id/stream", headers=HEADERS)
        assert r.status_code == 404

    async def test_stream_tenant_isolation(self):
        """Tenant A cannot stream Tenant B's hotel search."""
        import hashlib

        app = _make_app()
        from booking_agent.api import middleware as mw_mod

        tenant_b_key = "hotel-tenant-b-secret"
        key_hash = hashlib.sha256(tenant_b_key.encode()).hexdigest()
        mw_mod._REGISTRY[key_hash] = "tenant_b_hotel"

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/v1/hotels/search", json=HOTEL_BODY, headers=HEADERS)
            search_id = r.json()["search_id"]
            r2 = await c.get(
                f"/v1/hotels/search/{search_id}/stream", headers={"X-API-Key": tenant_b_key}
            )
        assert r2.status_code == 404
        del mw_mod._REGISTRY[key_hash]


class TestHotelSearchSnapshot:
    async def test_snapshot_after_completion(self):
        app = _make_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/v1/hotels/search", json=HOTEL_BODY, headers=HEADERS)
            search_id = r.json()["search_id"]

            await asyncio.sleep(0.1)

            snap = await c.get(f"/v1/hotels/search/{search_id}", headers=HEADERS)
        assert snap.status_code == 200
        body = snap.json()
        assert body["search_id"] == search_id
        assert "is_complete" in body
        assert "groups" in body

    async def test_snapshot_404_for_unknown(self):
        app = _make_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/v1/hotels/search/unknown-id", headers=HEADERS)
        assert r.status_code == 404

    async def test_snapshot_has_hotel_groups(self):
        app = _make_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/v1/hotels/search", json=HOTEL_BODY, headers=HEADERS)
            search_id = r.json()["search_id"]
            await asyncio.sleep(0.15)
            snap = await c.get(f"/v1/hotels/search/{search_id}", headers=HEADERS)
        body = snap.json()
        if body["is_complete"]:
            assert len(body["groups"]) > 0
            group = body["groups"][0]
            assert "primary" in group
            assert "property_name" in group["primary"]
            assert "total_inr" in group["primary"]
            assert "cancellation_policy" in group["primary"]
