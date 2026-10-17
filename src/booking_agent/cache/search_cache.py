"""
Redis-backed search result cache with request coalescing.

Key  = SHA-256( normalized SearchQuery JSON + tenant_id + sorted enabled_suppliers )
Value = JSON array of serialized Offer objects (display hints only — never used for booking)
TTL  = configurable (default 5 minutes)

Coalescing: if two identical searches arrive simultaneously, only one fan-out is
performed. The second waiter blocks on an asyncio.Event until the first completes,
then reads the cache.
"""

import asyncio
import hashlib
import json
from typing import Any, Awaitable, Callable

from redis.asyncio import Redis

from booking_agent.domain.offer import Offer
from booking_agent.domain.search import SearchQuery

_KEY_PREFIX = "search:"


def _make_key(
    query: SearchQuery, tenant_id: str, enabled_suppliers: list[str]
) -> str:
    payload = {
        "query": query.model_dump(mode="json"),
        "tenant_id": tenant_id,
        "suppliers": sorted(enabled_suppliers),
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()
    return f"{_KEY_PREFIX}{digest}"


class SearchCache:
    def __init__(self, redis: Redis, ttl_seconds: int = 300) -> None:
        self._redis = redis
        self._ttl = ttl_seconds
        # in-flight keys → asyncio.Event that is set when the search completes
        self._in_flight: dict[str, asyncio.Event] = {}

    def make_key(
        self, query: SearchQuery, tenant_id: str, enabled_suppliers: list[str]
    ) -> str:
        return _make_key(query, tenant_id, enabled_suppliers)

    async def get(self, key: str) -> list[Offer] | None:
        raw = await self._redis.get(key)
        if raw is None:
            return None
        data: list[dict[str, Any]] = json.loads(raw)
        return [Offer.model_validate(o) for o in data]

    async def set(self, key: str, offers: list[Offer]) -> None:
        payload = json.dumps(
            [o.model_dump(mode="json") for o in offers], default=str
        )
        await self._redis.set(key, payload, ex=self._ttl)

    async def get_or_search(
        self,
        key: str,
        search_fn: Callable[[], Awaitable[list[Offer]]],
    ) -> tuple[list[Offer], bool]:
        """
        Returns (offers, from_cache).

        Coalescing: if another coroutine is already running search_fn for this key,
        wait for it to finish and return from cache rather than launching a second
        fan-out.
        """
        # Fast path: cache hit
        cached = await self.get(key)
        if cached is not None:
            return cached, True

        # Coalesce: wait if another call is already in flight for this key
        if key in self._in_flight:
            await self._in_flight[key].wait()
            cached = await self.get(key)
            if cached is not None:
                return cached, True
            # Primary search failed — fall through and try ourselves

        event = asyncio.Event()
        self._in_flight[key] = event
        try:
            offers = await search_fn()
            await self.set(key, offers)
            return offers, False
        finally:
            event.set()
            self._in_flight.pop(key, None)
