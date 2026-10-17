from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.asyncio import AsyncEngine


def create_engine(db_url: str) -> AsyncEngine:
    return create_async_engine(db_url, echo=False, pool_pre_ping=True)


def AsyncSessionFactory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
