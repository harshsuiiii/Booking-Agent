"""
In-memory search session: buffers serialized SSE events and notifies waiting clients.
"""

import asyncio
import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

from booking_agent.auth.principal import Role
from booking_agent.orchestrator.dedupe import DedupeGroup


class SearchSession:
    """
    Buffers JSON-serialized SSE event strings for one search.

    Multiple concurrent SSE subscribers can attach via `event_stream()`.
    """

    def __init__(
        self,
        search_id: str,
        tenant_id: str,
        user_id: str = "",
        role: Role = "agent",
    ) -> None:
        self.search_id = search_id
        self.tenant_id = tenant_id
        # Events are serialized once, for the role of the user who started the search,
        # so only that user may read them back.
        self.user_id = user_id
        self.role: Role = role
        self.events: list[str] = []  # JSON lines (one per event)
        self.is_complete = False
        self.groups: list[DedupeGroup] = []
        self.suppliers_succeeded: list[str] = []
        self.suppliers_failed: list[str] = []
        self.suppliers_timed_out: list[str] = []
        self.created_at = datetime.now(UTC)
        self._notify = asyncio.Event()

    def push(self, data: dict[str, Any]) -> None:
        self.events.append(json.dumps(data, default=str))
        self._notify.set()
        self._notify = asyncio.Event()

    def complete(self, groups: list[DedupeGroup], complete_event_data: dict[str, Any]) -> None:
        self.groups = groups
        self.suppliers_succeeded = list(complete_event_data.get("suppliers_succeeded", []))
        self.suppliers_failed = list(complete_event_data.get("suppliers_failed", []))
        self.suppliers_timed_out = list(complete_event_data.get("suppliers_timed_out", []))
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
