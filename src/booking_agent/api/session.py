"""
In-memory search session: buffers serialized SSE events and notifies waiting clients.
"""

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncGenerator

from booking_agent.orchestrator.dedupe import DedupeGroup


def _serialize_group(group: DedupeGroup) -> dict[str, Any]:
    return {
        "dedupe_key": group.dedupe_key,
        "primary": group.primary.model_dump(mode="json"),
        "alternates": [
            {
                "supplier_id": a.supplier_id,
                "offer_id": a.offer_id,
                "total_inr": str(a.total_inr),
                "supplier_offer_ref": a.supplier_offer_ref,
            }
            for a in group.alternates
        ],
    }


class SearchSession:
    """
    Buffers JSON-serialized SSE event strings for one search.

    Multiple concurrent SSE subscribers can attach via `event_stream()`.
    """

    def __init__(self, search_id: str, tenant_id: str) -> None:
        self.search_id = search_id
        self.tenant_id = tenant_id
        self.events: list[str] = []  # JSON lines (one per event)
        self.is_complete = False
        self.groups: list[DedupeGroup] = []
        self.suppliers_succeeded: list[str] = []
        self.suppliers_failed: list[str] = []
        self.suppliers_timed_out: list[str] = []
        self.created_at = datetime.now(timezone.utc)
        self._notify = asyncio.Event()

    def push(self, data: dict[str, Any]) -> None:
        self.events.append(json.dumps(data, default=str))
        self._notify.set()
        self._notify = asyncio.Event()

    def complete(self, groups: list[DedupeGroup], complete_event_data: dict[str, Any]) -> None:
        self.groups = groups
        self.events.append(json.dumps(complete_event_data, default=str))
        self.is_complete = True
        self._notify.set()

    async def event_stream(self) -> AsyncGenerator[str, None]:
        """Async generator of raw JSON strings, one per event."""
        sent = 0
        while True:
            while sent < len(self.events):
                yield self.events[sent]
                sent += 1
            if self.is_complete:
                return
            await self._notify.wait()
