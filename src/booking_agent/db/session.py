"""
Database engine and sessions.

DATABASE_URL may be given the way hosts hand it out (Supabase, Render, Neon):
postgres://... or postgresql://..., with or without ?sslmode=. It is converted for the
async driver (postgresql+asyncpg://). SQLite URLs (sqlite+aiosqlite:///...) pass through.

Supabase: use a pooler URL (*.pooler.supabase.com); the direct db.*.supabase.co host is
IPv6-only. The Session pooler (port 5432) works as is; the Transaction pooler (port 6543)
can't keep prepared statements between transactions, so statement caching is turned off.
"""

import uuid
from typing import Any

from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

TRANSACTION_POOLER_PORT = 6543


def normalize_db_url(raw: str) -> str:
    """The URL in the form SQLAlchemy's async drivers expect."""
    url = raw.strip()
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            url = "postgresql+asyncpg://" + url[len(prefix) :]
            break
    parsed = make_url(url)
    if parsed.get_backend_name() != "postgresql":
        return url
    query = dict(parsed.query)
    # libpq's sslmode isn't an asyncpg argument; asyncpg takes the same values as `ssl`.
    sslmode = query.pop("sslmode", None)
    if sslmode and "ssl" not in query:
        query["ssl"] = sslmode
    query.pop("pgbouncer", None)  # Prisma-style flag; meaningless here
    return parsed.set(query=query).render_as_string(hide_password=False)


def _is_transaction_pooler(url: URL) -> bool:
    return url.port == TRANSACTION_POOLER_PORT


def engine_options(db_url: str) -> dict[str, Any]:
    """create_async_engine keyword arguments for this database."""
    url = make_url(db_url)
    if url.get_backend_name() != "postgresql":
        return {"pool_pre_ping": True}
    options: dict[str, Any] = {
        "pool_pre_ping": True,
        # Hosted Postgres and its poolers drop idle connections; recycle before they do.
        "pool_recycle": 300,
        "pool_size": 5,
        "max_overflow": 5,
    }
    if _is_transaction_pooler(url):
        options["connect_args"] = {
            "statement_cache_size": 0,
            "prepared_statement_name_func": lambda: f"__asyncpg_{uuid.uuid4()}__",
        }
    return options


def create_engine(db_url: str) -> AsyncEngine:
    url = normalize_db_url(db_url)
    return create_async_engine(url, echo=False, **engine_options(url))


def AsyncSessionFactory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:  # noqa: N802
    return async_sessionmaker(engine, expire_on_commit=False)
