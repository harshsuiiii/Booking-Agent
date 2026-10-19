"""
Tests for SearchCache: hit/miss/expiry and coalescing.
Uses fakeredis so no real Redis is needed.
"""

import asyncio
from datetime import date
from decimal import Decimal

import fakeredis.aioredis
import pytest

from booking_agent.cache.search_cache import SearchCache
from booking_agent.domain.search import SearchQuery
from tests.unit.offer_factory import make_offer


def _query(origin: str = "DEL", dest: str = "BOM") -> SearchQuery:
    return SearchQuery(origin=origin, destination=dest, departure_date=date(2027, 9, 1))


def _fake_redis() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis()


# ── key generation ────────────────────────────────────────────────────────────


class TestCacheKey:
    def test_same_inputs_same_key(self):
        cache = SearchCache(_fake_redis())
        q = _query()
        k1 = cache.make_key(q, "t1", ["amadeus"])
        k2 = cache.make_key(q, "t1", ["amadeus"])
        assert k1 == k2

    def test_different_query_different_key(self):
        cache = SearchCache(_fake_redis())
        k1 = cache.make_key(_query("DEL", "BOM"), "t1", [])
        k2 = cache.make_key(_query("BOM", "DEL"), "t1", [])
        assert k1 != k2

    def test_different_tenant_different_key(self):
        cache = SearchCache(_fake_redis())
        q = _query()
        k1 = cache.make_key(q, "tenant_a", [])
        k2 = cache.make_key(q, "tenant_b", [])
        assert k1 != k2

    def test_supplier_order_does_not_affect_key(self):
        cache = SearchCache(_fake_redis())
        q = _query()
        k1 = cache.make_key(q, "t1", ["amadeus", "sabre"])
        k2 = cache.make_key(q, "t1", ["sabre", "amadeus"])
        assert k1 == k2

    def test_key_starts_with_prefix(self):
        cache = SearchCache(_fake_redis())
        k = cache.make_key(_query(), "t1", [])
        assert k.startswith("search:")


# ── get / set / miss ──────────────────────────────────────────────────────────


class TestCacheGetSet:
    async def test_miss_returns_none(self):
        cache = SearchCache(_fake_redis())
        result = await cache.get("nonexistent-key")
        assert result is None

    async def test_set_then_get_returns_offers(self):
        redis = _fake_redis()
        cache = SearchCache(redis)
        offers = [make_offer(total_inr=Decimal("3700"))]
        key = cache.make_key(_query(), "t1", [])
        await cache.set(key, offers)
        retrieved = await cache.get(key)
        assert retrieved is not None
        assert len(retrieved) == 1
        assert retrieved[0].total_inr == Decimal("3700")

    async def test_offers_survive_round_trip(self):
        redis = _fake_redis()
        cache = SearchCache(redis)
        offers = [
            make_offer(total_inr=Decimal("3000"), flight_number="101"),
            make_offer(total_inr=Decimal("4500"), flight_number="202"),
        ]
        key = "roundtrip-key"
        await cache.set(key, offers)
        back = await cache.get(key)
        assert back is not None
        assert len(back) == 2
        prices = {o.total_inr for o in back}
        assert prices == {Decimal("3000"), Decimal("4500")}

    async def test_expiry_respects_ttl(self):
        redis = _fake_redis()
        cache = SearchCache(redis, ttl_seconds=1)
        offers = [make_offer()]
        key = "expiry-key"
        await cache.set(key, offers)
        # TTL is set — key should exist
        assert await cache.get(key) is not None
        # Manually expire via fakeredis
        await redis.expire(key, 0)
        assert await cache.get(key) is None


# ── get_or_search ─────────────────────────────────────────────────────────────


class TestGetOrSearch:
    async def test_miss_calls_factory(self):
        cache = SearchCache(_fake_redis())
        calls = 0

        async def search_fn():
            nonlocal calls
            calls += 1
            return [make_offer()]

        offers, from_cache = await cache.get_or_search("key-a", search_fn)
        assert calls == 1
        assert from_cache is False
        assert len(offers) == 1

    async def test_hit_skips_factory(self):
        redis = _fake_redis()
        cache = SearchCache(redis)
        key = "key-b"
        await cache.set(key, [make_offer()])
        calls = 0

        async def search_fn():
            nonlocal calls
            calls += 1
            return []

        offers, from_cache = await cache.get_or_search(key, search_fn)
        assert calls == 0
        assert from_cache is True

    async def test_result_cached_after_search(self):
        redis = _fake_redis()
        cache = SearchCache(redis)
        key = "key-c"

        async def search_fn():
            return [make_offer(total_inr=Decimal("5000"))]

        await cache.get_or_search(key, search_fn)
        # Second call should hit cache
        offers, from_cache = await cache.get_or_search(key, search_fn)
        assert from_cache is True
        assert offers[0].total_inr == Decimal("5000")


# ── coalescing ────────────────────────────────────────────────────────────────


class TestCoalescing:
    async def test_concurrent_searches_share_one_fan_out(self):
        """Two concurrent get_or_search calls with the same key run search_fn once."""
        cache = SearchCache(_fake_redis())
        key = "coalesce-key"
        call_count = 0
        started = asyncio.Event()
        proceed = asyncio.Event()

        async def slow_search():
            nonlocal call_count
            call_count += 1
            started.set()
            await proceed.wait()
            return [make_offer()]

        # Start first search
        task1 = asyncio.create_task(cache.get_or_search(key, slow_search))
        # Wait for it to start
        await asyncio.wait_for(started.wait(), timeout=1.0)

        # Start second search (should coalesce)
        task2 = asyncio.create_task(cache.get_or_search(key, slow_search))

        # Let first search complete
        proceed.set()
        r1, from_cache1 = await task1
        r2, from_cache2 = await task2

        assert call_count == 1  # only one fan-out
        assert len(r1) == 1
        assert len(r2) == 1
        assert from_cache1 is False
        assert from_cache2 is True  # second got it from cache

    async def test_failed_primary_allows_retry(self):
        """If the primary search fails, the waiting coalescer can try its own search."""
        cache = SearchCache(_fake_redis())
        key = "fail-coalesce-key"
        call_count = 0
        started = asyncio.Event()
        proceed = asyncio.Event()

        async def failing_search():
            nonlocal call_count
            call_count += 1
            started.set()
            await proceed.wait()
            raise RuntimeError("search failed")

        async def fallback_search():
            nonlocal call_count
            call_count += 1
            return [make_offer()]

        task1 = asyncio.create_task(cache.get_or_search(key, failing_search))
        await asyncio.wait_for(started.wait(), timeout=1.0)
        task2 = asyncio.create_task(cache.get_or_search(key, fallback_search))

        proceed.set()
        with pytest.raises(RuntimeError):
            await task1
        # task2 should run its own fallback_search since primary failed
        r2, from_cache2 = await task2
        assert len(r2) == 1
        assert from_cache2 is False
