"""
Admin API: CRUD endpoints for pricing rules.

Admin only (API key): markup and commission rules reveal how sell prices relate to net
fares, so the visitor (an Agent) can neither read nor change them.
Rules are tenant-scoped. The authenticated tenant may only view and manage its own rules.
Versioning: every update bumps the rule's version; deletes soft-delete (is_active=False).
An immutable audit log is written for every mutation.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from booking_agent.auth.principal import require_admin
from booking_agent.db.models import RuleAuditEntry, RuleRecord
from booking_agent.rules.model import Rule, RuleAction, RuleScope

router = APIRouter(prefix="/v1/admin/rules", tags=["admin"], dependencies=[Depends(require_admin)])


# ── request / response schemas ────────────────────────────────────────────────


class RuleCreateRequest(BaseModel):
    name: str
    description: str = ""
    scope: RuleScope = Field(default_factory=RuleScope)
    action: RuleAction
    priority: int = 100


class RuleUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    scope: RuleScope | None = None
    action: RuleAction | None = None
    priority: int | None = None
    is_active: bool | None = None


def _record_to_domain(rec: RuleRecord) -> Rule:
    return Rule(
        rule_id=rec.id,
        tenant_id=rec.tenant_id,
        name=rec.name,
        description=rec.description,
        scope=RuleScope.model_validate(rec.scope_json),
        action=RuleAction.model_validate(rec.action_json),
        priority=rec.priority,
        is_active=rec.is_active,
        version=rec.version,
        created_at=rec.created_at,
        updated_at=rec.updated_at,
    )


async def _get_db(request: Request) -> AsyncSession:
    db: AsyncSession | None = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="Database not configured")
    return db


async def _require_tenant(request: Request) -> str:
    tenant: str | None = getattr(request.state, "tenant_id", None)
    if not tenant:
        raise HTTPException(status_code=401, detail="Authentication required")
    return tenant


# ── endpoints ─────────────────────────────────────────────────────────────────


@router.get("", response_model=list[Rule])
async def list_rules(
    request: Request,
    include_inactive: bool = False,
    tenant_id: str = Depends(_require_tenant),
    db: AsyncSession = Depends(_get_db),
) -> list[Rule]:
    stmt = select(RuleRecord).where(RuleRecord.tenant_id == tenant_id)
    if not include_inactive:
        stmt = stmt.where(RuleRecord.is_active.is_(True))
    stmt = stmt.order_by(RuleRecord.priority, RuleRecord.created_at)
    result = await db.execute(stmt)
    return [_record_to_domain(r) for r in result.scalars().all()]


@router.get("/{rule_id}", response_model=Rule)
async def get_rule(
    rule_id: str,
    tenant_id: str = Depends(_require_tenant),
    db: AsyncSession = Depends(_get_db),
) -> Rule:
    rec = await db.get(RuleRecord, rule_id)
    if rec is None or rec.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Rule not found")
    return _record_to_domain(rec)


@router.post("", response_model=Rule, status_code=status.HTTP_201_CREATED)
async def create_rule(
    body: RuleCreateRequest,
    request: Request,
    tenant_id: str = Depends(_require_tenant),
    db: AsyncSession = Depends(_get_db),
) -> Rule:
    actor = getattr(request.state, "actor", tenant_id)
    rec = RuleRecord(
        tenant_id=tenant_id,
        name=body.name,
        description=body.description,
        scope_json=body.scope.model_dump(mode="json"),
        action_json=body.action.model_dump(mode="json"),
        priority=body.priority,
        is_active=True,
        version=1,
    )
    db.add(rec)
    await db.flush()  # get the generated id
    audit = RuleAuditEntry(
        rule_id=rec.id,
        tenant_id=tenant_id,
        actor=actor,
        action="created",
        before_json=None,
        after_json=body.model_dump(mode="json"),
    )
    db.add(audit)
    await db.commit()
    await db.refresh(rec)
    return _record_to_domain(rec)


@router.put("/{rule_id}", response_model=Rule)
async def update_rule(
    rule_id: str,
    body: RuleUpdateRequest,
    request: Request,
    tenant_id: str = Depends(_require_tenant),
    db: AsyncSession = Depends(_get_db),
) -> Rule:
    rec = await db.get(RuleRecord, rule_id)
    if rec is None or rec.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Rule not found")

    before = _record_to_domain(rec).model_dump(mode="json")
    if body.name is not None:
        rec.name = body.name
    if body.description is not None:
        rec.description = body.description
    if body.scope is not None:
        rec.scope_json = body.scope.model_dump(mode="json")
    if body.action is not None:
        rec.action_json = body.action.model_dump(mode="json")
    if body.priority is not None:
        rec.priority = body.priority
    if body.is_active is not None:
        rec.is_active = body.is_active
    rec.version += 1
    rec.updated_at = datetime.now(UTC)

    actor = getattr(request.state, "actor", tenant_id)
    audit = RuleAuditEntry(
        rule_id=rule_id,
        tenant_id=tenant_id,
        actor=actor,
        action="updated",
        before_json=before,
        after_json=_record_to_domain(rec).model_dump(mode="json"),
    )
    db.add(audit)
    await db.commit()
    await db.refresh(rec)
    return _record_to_domain(rec)


@router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rule(
    rule_id: str,
    request: Request,
    tenant_id: str = Depends(_require_tenant),
    db: AsyncSession = Depends(_get_db),
) -> None:
    rec = await db.get(RuleRecord, rule_id)
    if rec is None or rec.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Rule not found")

    before = _record_to_domain(rec).model_dump(mode="json")
    rec.is_active = False
    rec.updated_at = datetime.now(UTC)

    actor = getattr(request.state, "actor", tenant_id)
    audit = RuleAuditEntry(
        rule_id=rule_id,
        tenant_id=tenant_id,
        actor=actor,
        action="deleted",
        before_json=before,
        after_json={"is_active": False},
    )
    db.add(audit)
    await db.commit()
