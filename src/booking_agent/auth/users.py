"""
Users: storage (in-memory or SQL) and demo-account seeding.

Demo accounts exist only when their password is set in the environment
(DEMO_AGENT_PASSWORD / DEMO_ADMIN_PASSWORD); passwords are never hard-coded.
"""

import os
import uuid
from dataclasses import dataclass, replace
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from booking_agent.auth.passwords import hash_password, verify_password
from booking_agent.auth.principal import Principal, Role
from booking_agent.db.models import UserRecord

DEMO_TENANT_ID = "demo-agency"


@dataclass(frozen=True)
class User:
    id: str
    email: str
    name: str
    role: Role
    tenant_id: str
    password_hash: str
    is_demo: bool = False

    def principal(self) -> Principal:
        return Principal(
            user_id=self.id,
            email=self.email,
            name=self.name,
            role=self.role,
            tenant_id=self.tenant_id,
            via="session",
        )


@dataclass(frozen=True)
class DemoAccount:
    email: str
    name: str
    role: Role
    password_env: str


DEMO_ACCOUNTS = (
    DemoAccount("agent@demo.test", "Demo Agent", "agent", "DEMO_AGENT_PASSWORD"),
    DemoAccount("admin@demo.test", "Demo Admin", "admin", "DEMO_ADMIN_PASSWORD"),
)


def demo_mode() -> bool:
    return os.getenv("DEMO_MODE", "false").lower() == "true"


class UserRepository(Protocol):
    async def get_by_email(self, email: str) -> User | None: ...
    async def upsert(self, user: User) -> User: ...


class InMemoryUserRepository:
    def __init__(self) -> None:
        self._by_email: dict[str, User] = {}

    async def get_by_email(self, email: str) -> User | None:
        return self._by_email.get(email.strip().lower())

    async def upsert(self, user: User) -> User:
        existing = self._by_email.get(user.email)
        stored = replace(user, id=existing.id) if existing else user
        self._by_email[user.email] = stored
        return stored


class SqlUserRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def get_by_email(self, email: str) -> User | None:
        async with self._sessions() as db:
            row = (
                await db.execute(
                    select(UserRecord).where(UserRecord.email == email.strip().lower())
                )
            ).scalar_one_or_none()
            return _to_user(row) if row else None

    async def upsert(self, user: User) -> User:
        async with self._sessions() as db, db.begin():
            row = (
                await db.execute(select(UserRecord).where(UserRecord.email == user.email))
            ).scalar_one_or_none()
            if row is None:
                row = UserRecord(id=user.id, email=user.email)
                db.add(row)
            row.name = user.name
            row.role = user.role
            row.tenant_id = user.tenant_id
            row.password_hash = user.password_hash
            row.is_demo = user.is_demo
        return replace(user, id=row.id)


def _to_user(row: UserRecord) -> User:
    role: Role = "admin" if row.role == "admin" else "agent"
    return User(
        id=row.id,
        email=row.email,
        name=row.name,
        role=role,
        tenant_id=row.tenant_id,
        password_hash=row.password_hash,
        is_demo=row.is_demo,
    )


async def seed_demo_users(repo: UserRepository) -> list[str]:
    """Create or update demo accounts whose password env var is set. Returns their emails."""
    seeded: list[str] = []
    for account in DEMO_ACCOUNTS:
        password = os.getenv(account.password_env)
        if not password:
            continue
        existing = await repo.get_by_email(account.email)
        # Re-hash only when the password changed, so restarts don't churn the table.
        if existing and verify_password(password, existing.password_hash):
            password_hash = existing.password_hash
        else:
            password_hash = hash_password(password)
        await repo.upsert(
            User(
                id=existing.id if existing else str(uuid.uuid4()),
                email=account.email,
                name=account.name,
                role=account.role,
                tenant_id=DEMO_TENANT_ID,
                password_hash=password_hash,
                is_demo=True,
            )
        )
        seeded.append(account.email)
    return seeded
