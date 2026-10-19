import asyncio
import uuid
from collections.abc import AsyncGenerator
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from booking_agent.api.auth import Tenant, get_tenant
from booking_agent.api.models import (
    SearchRequest,
    SearchSnapshot,
    SearchStarted,
)
from booking_agent.api.session import SearchSession
from booking_agent.api.views import group_view
from booking_agent.auth.principal import Principal, Role, get_principal
from booking_agent.dashboard.recent_searches import RecentSearch, RecentSearchStore
from booking_agent.orchestrator.events import (
    OffersPartial,
    SearchComplete,
    SourceFailed,
    SourceSkipped,
    SourceStarted,
    SourceTimedOut,
)
from booking_agent.orchestrator.search import TenantContext

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1/search", tags=["search"])


# ── event serialization ───────────────────────────────────────────────────────


def _serialize_event(event: Any, role: Role) -> dict[str, Any]:
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
            "groups": [group_view(g, role) for g in event.groups],
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
            "groups": [group_view(g, role) for g in event.groups],
            "suppliers_succeeded": event.suppliers_succeeded,
            "suppliers_failed": event.suppliers_failed,
            "suppliers_timed_out": event.suppliers_timed_out,
            "suppliers_skipped": event.suppliers_skipped,
            "elapsed_seconds": event.elapsed_seconds,
        }
    return {"type": "unknown"}


async def _remember(request: Request, session: SearchSession, query: Any, offers: int) -> None:
    store: RecentSearchStore | None = getattr(request.app.state, "recent_searches", None)
    if store is None or not session.user_id:
        return
    try:
        await store.add(
            RecentSearch.from_query(query, session.tenant_id, session.user_id, "search", offers)
        )
    except Exception:  # history is best-effort; never fail a search over it
        log.warning("recent_search_save_failed", search_id=session.search_id)


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
        from booking_agent.domain.ranking import RankingPreferences
        from booking_agent.orchestrator.dedupe import dedupe
        from booking_agent.orchestrator.ranking import rank_groups

        groups = rank_groups(dedupe(cached), RankingPreferences())
        await _remember(request, session, query_obj, len(groups))
        session.push({"type": "cache_hit", "offers_count": len(cached)})
        session.push(
            {
                "type": "offers_partial",
                "supplier_id": "cache",
                "offers_count": len(cached),
                "groups": [group_view(g, session.role) for g in groups],
            }
        )
        complete_data = {
            "type": "search_complete",
            "groups": [group_view(g, session.role) for g in groups],
            "suppliers_succeeded": ["cache"],
            "suppliers_failed": [],
            "suppliers_timed_out": [],
            "suppliers_skipped": [],
            "elapsed_seconds": 0.0,
        }
        session.complete(groups, complete_data)
        return

    # Live fan-out
    try:
        async for event in orch.search(query_obj, tenant_ctx):
            if isinstance(event, SearchComplete):
                # Store in cache (best-effort; never blocks the stream).
                # Groups are the final deduped snapshot, so each offer appears once.
                primaries = [g.primary for g in event.groups]
                if primaries:
                    try:
                        await cache.set(cache_key, primaries)
                    except Exception:
                        log.warning("search_cache_set_failed", search_id=session.search_id)
                await _remember(request, session, query_obj, len(event.groups))
                # complete() emits the event; pushing it as well would send it twice.
                session.complete(event.groups, _serialize_event(event, session.role))
                return
            session.push(_serialize_event(event, session.role))
    except Exception:
        log.exception("search_failed", search_id=session.search_id)
    session.complete([], {"type": "search_complete", "groups": [], "error": "internal"})


def _own_session(request: Request, search_id: str, tenant: Tenant) -> SearchSession:
    """The search, if the caller started it (results are serialized for their role)."""
    principal: Principal = get_principal(request)
    registry: dict[str, SearchSession] = request.app.state.search_registry
    session = registry.get(search_id)
    if (
        session is None
        or session.tenant_id != tenant.tenant_id
        or session.user_id != principal.user_id
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Search not found")
    return session


# ── routes ────────────────────────────────────────────────────────────────────


@router.post("", response_model=SearchStarted, status_code=status.HTTP_202_ACCEPTED)
async def start_search(
    body: SearchRequest,
    request: Request,
    tenant: Annotated[Tenant, Depends(get_tenant)],
) -> SearchStarted:
    principal = get_principal(request)
    search_id = str(uuid.uuid4())
    session = SearchSession(search_id, tenant.tenant_id, principal.user_id, principal.role)
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
    session = _own_session(request, search_id, tenant)

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
    session = _own_session(request, search_id, tenant)

    return SearchSnapshot(
        search_id=search_id,
        tenant_id=session.tenant_id,
        is_complete=session.is_complete,
        groups=[group_view(g, session.role) for g in session.groups],
        suppliers_succeeded=session.suppliers_succeeded,
        suppliers_failed=session.suppliers_failed,
        suppliers_timed_out=session.suppliers_timed_out,
    )
