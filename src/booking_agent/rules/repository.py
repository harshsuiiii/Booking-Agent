"""
RulesRepository: abstract interface and in-memory implementation for tests.

The Postgres implementation lives in api/routes/admin.py and uses SQLAlchemy sessions
directly; this module provides the Protocol + a test double.
"""

import uuid
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from booking_agent.rules.model import Rule


@runtime_checkable
class RulesRepository(Protocol):
    async def list_active(self, tenant_id: str) -> list[Rule]: ...
    async def get(self, rule_id: str, tenant_id: str) -> Rule: ...
    async def create(self, rule: Rule, actor: str) -> Rule: ...
    async def update(self, rule: Rule, actor: str) -> Rule: ...
    async def delete(self, rule_id: str, tenant_id: str, actor: str) -> None: ...


class InMemoryRulesRepository:
    """Thread-unsafe in-memory store for unit tests."""

    def __init__(self) -> None:
        self._rules: dict[str, Rule] = {}

    async def list_active(self, tenant_id: str) -> list[Rule]:
        return [r for r in self._rules.values() if r.tenant_id == tenant_id and r.is_active]

    async def get(self, rule_id: str, tenant_id: str) -> Rule:
        rule = self._rules.get(rule_id)
        if rule is None or rule.tenant_id != tenant_id:
            raise KeyError(f"Rule {rule_id!r} not found for tenant {tenant_id!r}")
        return rule

    async def create(self, rule: Rule, actor: str) -> Rule:  # noqa: ARG002
        stored = rule.model_copy(
            update={
                "rule_id": rule.rule_id or str(uuid.uuid4()),
                "version": 1,
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
            }
        )
        self._rules[stored.rule_id] = stored
        return stored

    async def update(self, rule: Rule, actor: str) -> Rule:  # noqa: ARG002
        existing = await self.get(rule.rule_id, rule.tenant_id)
        updated = rule.model_copy(
            update={
                "version": existing.version + 1,
                "updated_at": datetime.now(UTC),
            }
        )
        self._rules[updated.rule_id] = updated
        return updated

    async def delete(self, rule_id: str, tenant_id: str, actor: str) -> None:  # noqa: ARG002
        rule = await self.get(rule_id, tenant_id)
        self._rules[rule_id] = rule.model_copy(update={"is_active": False})
