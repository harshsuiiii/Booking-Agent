"""Each user's recent flight searches (search page and assistant), newest first."""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Literal, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from booking_agent.db.models import RecentSearchRecord
from booking_agent.domain.search import JourneyType, SearchQuery

Source = Literal["search", "assistant"]


@dataclass(frozen=True)
class RecentSearch:
    tenant_id: str
    user_id: str
    origin: str
    destination: str
    departure_date: date
    passengers: int
    cabin: str
    source: Source
    offer_count: int = 0
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def from_query(
        cls, query: SearchQuery, tenant_id: str, user_id: str, source: Source, offers: int = 0
    ) -> "RecentSearch":
        return cls(
            tenant_id=tenant_id,
            user_id=user_id,
            origin=query.origin,
            destination=query.legs[-1].destination
            if query.journey_type == JourneyType.MULTI_CITY
            else query.destination,
            departure_date=query.departure_date,
            passengers=query.pax_counts.total,
            cabin=query.cabin.value,
            source=source,
            offer_count=offers,
        )


class RecentSearchStore(Protocol):
    async def add(self, entry: RecentSearch) -> None: ...
    async def latest(self, tenant_id: str, user_id: str, limit: int = 8) -> list[RecentSearch]: ...


class InMemoryRecentSearchStore:
    def __init__(self) -> None:
        self._entries: list[RecentSearch] = []

    async def add(self, entry: RecentSearch) -> None:
        self._entries.append(entry)
        del self._entries[:-500]  # bounded

    async def latest(self, tenant_id: str, user_id: str, limit: int = 8) -> list[RecentSearch]:
        mine = [e for e in self._entries if e.tenant_id == tenant_id and e.user_id == user_id]
        return sorted(mine, key=lambda e: e.created_at, reverse=True)[:limit]


class SqlRecentSearchStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def add(self, entry: RecentSearch) -> None:
        async with self._sessions() as db, db.begin():
            db.add(
                RecentSearchRecord(
                    id=entry.id,
                    tenant_id=entry.tenant_id,
                    user_id=entry.user_id,
                    origin=entry.origin,
                    destination=entry.destination,
                    departure_date=entry.departure_date,
                    passengers=entry.passengers,
                    cabin=entry.cabin,
                    source=entry.source,
                    offer_count=entry.offer_count,
                    created_at=entry.created_at,
                )
            )

    async def latest(self, tenant_id: str, user_id: str, limit: int = 8) -> list[RecentSearch]:
        async with self._sessions() as db:
            rows = (
                (
                    await db.execute(
                        select(RecentSearchRecord)
                        .where(
                            RecentSearchRecord.tenant_id == tenant_id,
                            RecentSearchRecord.user_id == user_id,
                        )
                        .order_by(RecentSearchRecord.created_at.desc())
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
        return [
            RecentSearch(
                id=r.id,
                tenant_id=r.tenant_id,
                user_id=r.user_id,
                origin=r.origin,
                destination=r.destination,
                departure_date=r.departure_date,
                passengers=r.passengers,
                cabin=r.cabin,
                source="assistant" if r.source == "assistant" else "search",
                offer_count=r.offer_count,
                created_at=r.created_at
                if r.created_at.tzinfo
                else r.created_at.replace(tzinfo=UTC),
            )
            for r in rows
        ]
