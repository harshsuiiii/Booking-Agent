"""
BookingRepository contract tests, run against both the in-memory store and Postgres.

Postgres tests use TEST_DATABASE_URL (default: the docker-compose Postgres, database
booking_agent_test, created and migrated on first use). They are skipped when no
Postgres is reachable, e.g. `docker compose up -d postgres` has not been run.
"""

import asyncio
import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import asyncpg
import fakeredis.aioredis
import pytest
from sqlalchemy import text

from booking_agent.api.app import create_app
from booking_agent.booking.errors import BookingConflict, IdempotentReplay
from booking_agent.booking.payment_gateway import FakePaymentGateway
from booking_agent.booking.repository import (
    BookingQuery,
    BookingRepository,
    InMemoryBookingRepository,
    SqlBookingRepository,
)
from booking_agent.booking.service import BookingService
from booking_agent.connectors.fake import FakeConnector
from booking_agent.db.session import AsyncSessionFactory, create_engine
from booking_agent.domain.booking import Booking, BookingState
from booking_agent.domain.offer import PricedOffer
from tests.unit.test_booking_service import _make_offer, _pax

PG_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://booking:booking@localhost:5432/booking_agent_test",
)
ROOT = Path(__file__).parent.parent.parent


async def _ensure_database(url: str) -> None:
    server, _, db_name = url.replace("postgresql+asyncpg://", "postgresql://").rpartition("/")
    conn = await asyncpg.connect(f"{server}/postgres", timeout=3)
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", db_name)
        if not exists:
            await conn.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def pg_url() -> str:
    try:
        # Own thread: this fixture can be first requested from inside pytest's event loop.
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(asyncio.run, _ensure_database(PG_URL)).result()
    except (OSError, asyncpg.PostgresError, TimeoutError) as exc:
        pytest.skip(f"Postgres not reachable for repository tests: {exc}")
    # Real migrations, in a subprocess so Alembic's logging config stays out of pytest.
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": PG_URL},
        check=True,
        capture_output=True,
    )
    return PG_URL


@pytest.fixture(scope="session")
def sqlite_url(tmp_path_factory: pytest.TempPathFactory) -> str:
    """A migrated SQLite file (the demo setup when Postgres isn't running)."""
    db = tmp_path_factory.mktemp("sqlite") / "bookings.db"
    url = f"sqlite+aiosqlite:///{db.as_posix()}"
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": url},
        check=True,
        capture_output=True,
    )
    return url


@pytest.fixture(params=["memory", "sqlite", "postgres"])
async def repo(request: pytest.FixtureRequest) -> AsyncIterator[BookingRepository]:
    if request.param == "memory":
        yield InMemoryBookingRepository()
        return
    if request.param == "sqlite":
        engine = create_engine(request.getfixturevalue("sqlite_url"))
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM booking_state_transitions"))
            await conn.execute(text("DELETE FROM bookings"))
        yield SqlBookingRepository(AsyncSessionFactory(engine))
        await engine.dispose()
        return
    engine = create_engine(request.getfixturevalue("pg_url"))
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE booking_state_transitions, bookings"))
    yield SqlBookingRepository(AsyncSessionFactory(engine))
    await engine.dispose()


def _booking(key: str = "ik-1", tenant: str = "t1") -> Booking:
    offer = _make_offer()
    priced = PricedOffer(
        **offer.model_dump(), priced_at=offer.searched_at, valid_until=offer.expires_at
    )
    return Booking(
        tenant_id=tenant,
        idempotency_key=key,
        priced_offer=priced,
        passengers=_pax(),
        state=BookingState.PRICED,
        confirmation_token_hash="abc",
    )


class TestRoundTrip:
    async def test_save_and_get(self, repo: BookingRepository) -> None:
        saved = await repo.save(_booking())
        assert saved.version == 1

        loaded = await repo.get(saved.booking_id)
        assert loaded is not None
        assert loaded.version == 1
        assert loaded.state == BookingState.PRICED
        assert loaded.passengers == saved.passengers
        assert loaded.priced_offer.total_inr == saved.priced_offer.total_inr
        assert loaded.priced_offer.fare_rules == saved.priced_offer.fare_rules

    async def test_transitions_are_appended_in_order(self, repo: BookingRepository) -> None:
        b = await repo.save(_booking())
        b = await repo.save(b.transition_to(BookingState.CONFIRMED_BY_USER, "user"))
        b = b.model_copy(update={"pnr": "PNR1"})
        b = await repo.save(b.transition_to(BookingState.PNR_CREATED, "system"))

        loaded = await repo.get(b.booking_id)
        assert loaded is not None
        assert loaded.version == 3
        assert loaded.pnr == "PNR1"
        assert [(t.from_state, t.to_state) for t in loaded.transitions] == [
            (BookingState.PRICED, BookingState.CONFIRMED_BY_USER),
            (BookingState.CONFIRMED_BY_USER, BookingState.PNR_CREATED),
        ]

    async def test_get_unknown_is_none(self, repo: BookingRepository) -> None:
        assert await repo.get("nope") is None

    async def test_timestamps_come_back_timezone_aware(self, repo: BookingRepository) -> None:
        # SQLite drops the zone; the repository must restore UTC everywhere.
        b = await repo.save(_booking())
        b = await repo.save(b.transition_to(BookingState.CONFIRMED_BY_USER, "user"))
        loaded = await repo.get(b.booking_id)
        assert loaded is not None
        assert loaded.created_at.tzinfo is not None
        assert loaded.transitions[0].occurred_at.tzinfo is not None
        assert loaded.transitions[0].occurred_at == b.transitions[0].occurred_at
        [summary] = await repo.summaries(b.tenant_id)
        assert summary.created_at.tzinfo is not None


class TestSummariesAndQuery:
    async def _seed(self, repo: BookingRepository) -> None:
        a = await repo.save(_booking(key="a"))
        await repo.save(a.transition_to(BookingState.CONFIRMED_BY_USER, "user"))
        await repo.save(_booking(key="b"))
        await repo.save(_booking(key="c", tenant="other"))

    async def test_summary_has_list_fields_and_prices(self, repo: BookingRepository) -> None:
        saved = await repo.save(_booking())
        [s] = await repo.summaries("t1")
        assert s.reference == saved.reference and s.reference.startswith("BA-")
        assert s.passenger_display == "J. Doe"
        assert (s.origin, s.destination) == ("DEL", "BOM")
        assert s.sell_inr == s.net_inr + s.markup_inr

    async def test_query_filters_and_pages(self, repo: BookingRepository) -> None:
        await self._seed(repo)
        rows, total = await repo.query("t1", BookingQuery())
        assert total == 2 and len(rows) == 2  # other tenant excluded
        rows, total = await repo.query("t1", BookingQuery(status=BookingState.CONFIRMED_BY_USER))
        assert total == 1 and rows[0].state == BookingState.CONFIRMED_BY_USER
        rows, total = await repo.query("t1", BookingQuery(q="doe"))
        assert total == 2
        rows, total = await repo.query("t1", BookingQuery(q="zzz"))
        assert total == 0
        travel = rows[0].travel_date if rows else (await repo.summaries("t1"))[0].travel_date
        _, total = await repo.query("t1", BookingQuery(travel_from=travel, travel_to=travel))
        assert total == 2
        _, total = await repo.query("t1", BookingQuery(travel_from=travel + timedelta(days=1)))
        assert total == 0
        rows, total = await repo.query("t1", BookingQuery(page=2, page_size=1))
        assert total == 2 and len(rows) == 1


class TestConflicts:
    async def test_stale_save_raises_conflict(self, repo: BookingRepository) -> None:
        b = await repo.save(_booking())
        await repo.save(b.transition_to(BookingState.CONFIRMED_BY_USER, "user"))
        with pytest.raises(BookingConflict):
            await repo.save(b.transition_to(BookingState.CANCELLED, "user"))

    async def test_concurrent_saves_from_same_version_one_wins(
        self, repo: BookingRepository
    ) -> None:
        b = await repo.save(_booking())
        results = await asyncio.gather(
            repo.save(b.transition_to(BookingState.CONFIRMED_BY_USER, "user-a")),
            repo.save(b.transition_to(BookingState.CONFIRMED_BY_USER, "user-b")),
            return_exceptions=True,
        )
        assert sum(isinstance(r, Booking) for r in results) == 1
        assert sum(isinstance(r, BookingConflict) for r in results) == 1
        loaded = await repo.get(b.booking_id)
        assert loaded is not None and len(loaded.transitions) == 1

    async def test_duplicate_idempotency_key_same_tenant(self, repo: BookingRepository) -> None:
        first = await repo.save(_booking(key="dup"))
        with pytest.raises(IdempotentReplay) as exc:
            await repo.save(_booking(key="dup"))
        assert exc.value.booking_id == first.booking_id

    async def test_same_key_other_tenant_is_separate(self, repo: BookingRepository) -> None:
        await repo.save(_booking(key="k", tenant="t1"))
        await repo.save(_booking(key="k", tenant="t2"))
        found = await repo.get_by_idempotency_key("k", "t2")
        assert found is not None and found.tenant_id == "t2"

    async def test_list_is_tenant_scoped(self, repo: BookingRepository) -> None:
        await repo.save(_booking(key="a", tenant="t1"))
        await repo.save(_booking(key="b", tenant="t2"))
        assert [b.idempotency_key for b in await repo.list_by_tenant("t1")] == ["a"]


class TestServiceDoubleConfirm:
    async def test_two_confirms_at_once_book_once(self, repo: BookingRepository) -> None:
        connector = FakeConnector(supplier_id="fake", retry_min_wait=0.0, retry_max_wait=0.0)
        svc = BookingService(
            repository=repo,
            connectors={"fake": connector},
            payment_gateway=FakePaymentGateway(),
        )
        async with connector:
            booking, token = await svc.prepare(
                tenant_id="t1",
                offer=_make_offer(),
                passengers=_pax(),
                idempotency_key="dc-1",
                actor="agent",
            )
            results = await asyncio.gather(
                *(
                    svc.confirm(
                        booking_id=booking.booking_id,
                        tenant_id="t1",
                        confirmation_token=token,
                        actor="user",
                    )
                    for _ in range(2)
                ),
                return_exceptions=True,
            )
        winners = [r for r in results if isinstance(r, Booking)]
        assert len(winners) == 1 and winners[0].state == BookingState.TICKETED
        assert all(
            isinstance(r, (BookingConflict, ValueError)) for r in results if r not in winners
        )
        assert connector.book_call_count == 1


class TestEncryptionAtRest:
    async def test_passenger_names_not_stored_in_plain_text(self, pg_url: str) -> None:
        engine = create_engine(pg_url)
        try:
            repo = SqlBookingRepository(AsyncSessionFactory(engine))
            saved = await repo.save(_booking(key=f"enc-{uuid.uuid4()}"))
            async with engine.connect() as conn:
                row = (
                    (
                        await conn.execute(
                            text("SELECT * FROM bookings WHERE id = :id"), {"id": saved.booking_id}
                        )
                    )
                    .mappings()
                    .one()
                )
            stored = " ".join(str(v) for v in row.values())
            for p in saved.passengers:
                assert p.given_name not in stored
                assert p.family_name not in stored
        finally:
            await engine.dispose()


class TestAppWiring:
    def test_database_without_encryption_key_refuses_to_start(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("BOOKING_ENCRYPTION_KEY", raising=False)
        with pytest.raises(RuntimeError, match="BOOKING_ENCRYPTION_KEY"):
            create_app(
                connectors=[FakeConnector(supplier_id="fake")],
                redis=fakeredis.aioredis.FakeRedis(),
                db_url=PG_URL,
            )

    def test_invalid_encryption_key_refuses_to_start(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BOOKING_ENCRYPTION_KEY", "not-a-fernet-key")
        with pytest.raises(RuntimeError, match="not a valid Fernet key"):
            create_app(
                connectors=[FakeConnector(supplier_id="fake")],
                redis=fakeredis.aioredis.FakeRedis(),
                db_url=PG_URL,
            )
