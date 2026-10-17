import asyncio
import json
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from booking_agent.api.auth import Tenant, get_tenant
from booking_agent.api.models import (
    SearchRequest,
    SearchSnapshot,
    SearchStarted,
)
from booking_agent.api.session import SearchSession, _serialize_group
from booking_agent.connectors.errors import SupplierError
from booking_agent.orchestrator.events import (
    OffersPartial,
    SearchComplete,
    SourceFailed,
    SourceSkipped,
    SourceStarted,
    SourceTimedOut,
)
from booking_agent.orchestrator.search import TenantContext

router = APIRouter(prefix="/v1/search", tags=["search"])


# ── event serialization ───────────────────────────────────────────────────────

def _serialize_event(event: Any) -> dict[str, Any]:
    if isinstance(event, SourceStarted):
        return {
            "type": "source_started",
            "supplier_id": event.supplier_id,
            "searched_at": event.searched_at.isoformat(),
        }
    if isinstance(event, OffersPartial):
        return {
            "type": "offers_partial",
            "supplier_id": event.supplier_id,
            "offers_count": event.offers_count,
            "groups": [_serialize_group(g) for g in event.groups],
        }
    if isinstance(event, SourceFailed):
        return {"type": "source_failed", "supplier_id": event.supplier_id, "reason": event.reason}
    if isinstance(event, SourceTimedOut):
        return {
            "type": "source_timed_out",
            "supplier_id": event.supplier_id,
            "timeout_seconds": event.timeout_seconds,
        }
    if isinstance(event, SourceSkipped):
        return {
            "type": "source_skipped",
            "supplier_id": event.supplier_id,
            "reason": event.reason,
        }
    if isinstance(event, SearchComplete):
        return {
            "type": "search_complete",
            "groups": [_serialize_group(g) for g in event.groups],
            "suppliers_succeeded": event.suppliers_succeeded,
            "suppliers_failed": event.suppliers_failed,
            "suppliers_timed_out": event.suppliers_timed_out,
            "suppliers_skipped": event.suppliers_skipped,
            "elapsed_seconds": event.elapsed_seconds,
        }
    return {"type": "unknown"}


# ── background search task ────────────────────────────────────────────────────

async def _run_search(
    session: SearchSession,
    request: Request,
    query_obj: Any,
    tenant_ctx: TenantContext,
) -> None:
    orch = request.app.state.orchestrator
    cache = request.app.state.cache
    cache_key = cache.make_key(query_obj, tenant_ctx.tenant_id, tenant_ctx.enabled_suppliers)

    # Check cache first — serve a synthetic stream of one OffersPartial + SearchComplete
    cached = await cache.get(cache_key)
    if cached:
        from booking_agent.orchestrator.dedupe import dedupe
        from booking_agent.orchestrator.ranking import rank_groups
        from booking_agent.domain.ranking import RankingPreferences
        groups = rank_groups(dedupe(cached), RankingPreferences())
        session.push({"type": "cache_hit", "offers_count": len(cached)})
        session.push({
            "type": "offers_partial",
            "supplier_id": "cache",
            "offers_count": len(cached),
            "groups": [_serialize_group(g) for g in groups],
        })
        complete_data = {
            "type": "search_complete",
            "groups": [_serialize_group(g) for g in groups],
            "suppliers_succeeded": ["cache"],
            "suppliers_failed": [],
            "suppliers_timed_out": [],
            "suppliers_skipped": [],
            "elapsed_seconds": 0.0,
        }
        session.complete(groups, complete_data)
        return

    # Live fan-out
    accumulated_offers = []
    try:
        async for event in orch.search(query_obj, tenant_ctx):
            session.push(_serialize_event(event))
            if isinstance(event, OffersPartial):
                for group in event.groups:
                    accumulated_offers.extend([group.primary] + [])
            elif isinstance(event, SearchComplete):
                # Store in cache (best-effort; never blocks the stream)
                if accumulated_offers:
                    try:
                        await cache.set(cache_key, accumulated_offers)
                    except Exception:
                        pass
                complete_data = _serialize_event(event)
                session.complete(event.groups, complete_data)
                return
    except Exception:
        pass
    session.complete([], {"type": "search_complete", "groups": [], "error": "internal"})


# ── routes ────────────────────────────────────────────────────────────────────

@router.post("", response_model=SearchStarted, status_code=status.HTTP_202_ACCEPTED)
async def start_search(
    body: SearchRequest,
    request: Request,
    tenant: Annotated[Tenant, Depends(get_tenant)],
) -> SearchStarted:
    search_id = str(uuid.uuid4())
    session = SearchSession(search_id, tenant.tenant_id)
    request.app.state.search_registry[search_id] = session

    query_obj = body.to_query()
    tenant_ctx = TenantContext(
        tenant_id=tenant.tenant_id,
        enabled_suppliers=list(tenant.enabled_suppliers),
    )
    asyncio.create_task(_run_search(session, request, query_obj, tenant_ctx))
    return SearchStarted(search_id=search_id)


@router.get("/{search_id}/stream")
async def stream_search(
    search_id: str,
    request: Request,
    tenant: Annotated[Tenant, Depends(get_tenant)],
) -> StreamingResponse:
    registry: dict[str, SearchSession] = request.app.state.search_registry
    session = registry.get(search_id)
    if session is None or session.tenant_id != tenant.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Search not found")

    async def _sse_gen() -> AsyncGenerator[str, None]:
        async for line in session.event_stream():
            yield f"data: {line}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(_sse_gen(), media_type="text/event-stream")


@router.get("/{search_id}", response_model=SearchSnapshot)
async def get_snapshot(
    search_id: str,
    request: Request,
    tenant: Annotated[Tenant, Depends(get_tenant)],
) -> SearchSnapshot:
    registry: dict[str, SearchSession] = request.app.state.search_registry
    session = registry.get(search_id)
    if session is None or session.tenant_id != tenant.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Search not found")

    from booking_agent.api.models import AlternateSourceOut, DedupeGroupOut, OfferOut

    groups_out = [
        DedupeGroupOut(
            dedupe_key=g.dedupe_key,
            primary=OfferOut(
                offer_id=g.primary.offer_id,
                supplier_id=g.primary.supplier_id,
                total_inr=str(g.primary.total_inr),
                fare_basis=g.primary.fare_basis,
                fare_family=g.primary.fare_family,
                expires_at=g.primary.expires_at,
                raw=g.primary.model_dump(mode="json"),
            ),
            alternates=[
                AlternateSourceOut(
                    supplier_id=a.supplier_id,
                    offer_id=a.offer_id,
                    total_inr=str(a.total_inr),
                )
                for a in g.alternates
            ],
        )
        for g in session.groups
    ]

    return SearchSnapshot(
        search_id=search_id,
        tenant_id=session.tenant_id,
        is_complete=session.is_complete,
        groups=groups_out,
        suppliers_succeeded=session.suppliers_succeeded,
        suppliers_failed=session.suppliers_failed,
        suppliers_timed_out=session.suppliers_timed_out,
    )
