"""
GET /v1/dashboard/summary  cards: bookings and sales this month, upcoming trips, recent
                           searches (markup earned for admins)
GET /v1/dashboard/charts   bookings over the last 30 days and by supplier (admins also get
                           net, markup and supplier win rate)
"""

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request

from booking_agent.auth.principal import get_principal
from booking_agent.booking.service import BookingService
from booking_agent.dashboard import service as figures
from booking_agent.dashboard.recent_searches import RecentSearchStore
from booking_agent.orchestrator.search import SearchOrchestrator

router = APIRouter(prefix="/v1/dashboard", tags=["dashboard"])


@router.get("/summary")
async def dashboard_summary(request: Request) -> dict[str, Any]:
    principal = get_principal(request)
    svc: BookingService = request.app.state.booking_service
    recent_store: RecentSearchStore = request.app.state.recent_searches
    rows = await svc.summaries(principal.tenant_id)
    recent = await recent_store.latest(principal.tenant_id, principal.user_id)
    return figures.summary(rows, principal.role, datetime.now(UTC), recent)


@router.get("/charts")
async def dashboard_charts(request: Request) -> dict[str, Any]:
    principal = get_principal(request)
    svc: BookingService = request.app.state.booking_service
    orch: SearchOrchestrator = request.app.state.orchestrator
    rows = await svc.summaries(principal.tenant_id)
    return figures.charts(rows, principal.role, datetime.now(UTC), orch.metrics.snapshot())
