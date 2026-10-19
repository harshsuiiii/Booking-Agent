from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_engine(db_url: str) -> AsyncEngine:
    return create_async_engine(db_url, echo=False, pool_pre_ping=True)


def AsyncSessionFactory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:  # noqa: N802
    return async_sessionmaker(engine, expire_on_commit=False)
