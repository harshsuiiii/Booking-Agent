"""Hotel search API routes."""

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from booking_agent.api.auth import Tenant, get_tenant
from booking_agent.api.models_hotel import (
    CancellationPolicyOut,
    HotelAlternateSourceOut,
    HotelDedupeGroupOut,
    HotelOfferOut,
    HotelSearchRequest,
    HotelSearchSnapshot,
    HotelSearchStarted,
    RoomInfoOut,
)
from booking_agent.auth.principal import require_admin
from booking_agent.orchestrator.hotel_search import (
    HotelDedupeGroup,
    HotelOffersPartial,
    HotelSearchComplete,
    HotelSourceFailed,
    HotelSourceSkipped,
    HotelSourceStarted,
    HotelSourceTimedOut,
)
from booking_agent.orchestrator.search import TenantContext

# Hotel prices are supplier (net) prices with no agency markup yet, so Agents may not
# see them until hotel sell prices exist (Step 13). Admin and API-key callers only.
router = APIRouter(
    prefix="/v1/hotels/search", tags=["hotels"], dependencies=[Depends(require_admin)]
)


# -- session ------------------------------------------------------------------


class HotelSearchSession:
    def __init__(self, search_id: str, tenant_id: str) -> None:
        self.search_id = search_id
        self.tenant_id = tenant_id
        self.is_complete = False
        self.groups: list[HotelDedupeGroup] = []
        self.suppliers_succeeded: list[str] = []
        self.suppliers_failed: list[str] = []
        self.suppliers_timed_out: list[str] = []
        self._events: asyncio.Queue[str] = asyncio.Queue()

    def push(self, event: dict[str, Any]) -> None:
        self._events.put_nowait(json.dumps(event))

    def complete(self, groups: list[HotelDedupeGroup], event_data: dict[str, Any]) -> None:
        self.groups = groups
        self.suppliers_succeeded = event_data.get("suppliers_succeeded", [])
        self.suppliers_failed = event_data.get("suppliers_failed", [])
        self.suppliers_timed_out = event_data.get("suppliers_timed_out", [])
        self.is_complete = True
        self._events.put_nowait("[DONE]")

    async def event_stream(self) -> AsyncGenerator[str, None]:
        while True:
            item = await self._events.get()
            yield item
            if item == "[DONE]":
                break


# -- serialization -------------------------------------------------------------


def _serialize_group(g: HotelDedupeGroup) -> dict[str, Any]:
    p = g.primary
    return {
        "dedupe_key": g.dedupe_key,
        "primary": {
            "offer_id": p.offer_id,
            "supplier_id": p.supplier_id,
            "property_id": p.property_id,
            "property_name": p.property_name,
            "star_rating": p.star_rating,
            "location": p.location,
            "address": p.address,
            "checkin_date": p.checkin_date.isoformat(),
            "checkout_date": p.checkout_date.isoformat(),
            "nights": p.nights,
            "room": {
                "room_type": p.room.room_type,
                "bed_type": p.room.bed_type,
                "max_occupancy": p.room.max_occupancy,
                "room_count": p.room.room_count,
                "amenities": p.room.amenities,
            },
            "total_inr": str(p.total_inr),
            "cancellation_policy": {
                "type": p.cancellation_policy.type,
                "free_cancellation_before": p.cancellation_policy.free_cancellation_before.isoformat()
                if p.cancellation_policy.free_cancellation_before
                else None,
                "penalty_inr": str(p.cancellation_policy.penalty_inr)
                if p.cancellation_policy.penalty_inr is not None
                else None,
            },
            "meal_plan": p.meal_plan,
            "expires_at": p.expires_at.isoformat(),
            "is_test_data": p.is_test_data,
        },
        "alternates": [
            {
                "supplier_id": a.supplier_id,
                "offer_id": a.offer_id,
                "total_inr": str(a.total_inr),
            }
            for a in g.alternates
        ],
    }


def _serialize_event(event: Any) -> dict[str, Any]:
    if isinstance(event, HotelSourceStarted):
        return {
            "type": "source_started",
            "supplier_id": event.supplier_id,
            "searched_at": event.searched_at.isoformat(),
        }
    if isinstance(event, HotelOffersPartial):
        return {
            "type": "offers_partial",
            "supplier_id": event.supplier_id,
            "offers_count": event.offers_count,
            "groups": [_serialize_group(g) for g in event.groups],
        }
    if isinstance(event, HotelSourceFailed):
        return {"type": "source_failed", "supplier_id": event.supplier_id, "reason": event.reason}
    if isinstance(event, HotelSourceTimedOut):
        return {
            "type": "source_timed_out",
            "supplier_id": event.supplier_id,
            "timeout_seconds": event.timeout_seconds,
        }
    if isinstance(event, HotelSourceSkipped):
        return {
            "type": "source_skipped",
            "supplier_id": event.supplier_id,
            "reason": event.reason,
        }
    if isinstance(event, HotelSearchComplete):
        return {
            "type": "search_complete",
            "groups": [_serialize_group(g) for g in event.groups],
            "suppliers_succeeded": event.suppliers_succeeded,
            "suppliers_failed": event.suppliers_failed,
            "suppliers_timed_out": event.suppliers_timed_out,
            "elapsed_seconds": event.elapsed_seconds,
        }
    return {"type": "unknown"}


# -- background task -----------------------------------------------------------


async def _run_hotel_search(
    session: HotelSearchSession,
    request: Request,
    query: Any,
    tenant_ctx: TenantContext,
) -> None:
    orch = request.app.state.hotel_orchestrator
    try:
        async for event in orch.search(query, tenant_ctx):
            session.push(_serialize_event(event))
            if isinstance(event, HotelSearchComplete):
                session.complete(event.groups, _serialize_event(event))
                return
    except Exception:
        pass
    session.complete(
        [],
        {
            "type": "search_complete",
            "groups": [],
            "suppliers_succeeded": [],
            "suppliers_failed": [],
            "suppliers_timed_out": [],
        },
    )


# -- routes --------------------------------------------------------------------


@router.post("", response_model=HotelSearchStarted, status_code=status.HTTP_202_ACCEPTED)
async def start_hotel_search(
    body: HotelSearchRequest,
    request: Request,
    tenant: Annotated[Tenant, Depends(get_tenant)],
) -> HotelSearchStarted:
    search_id = str(uuid.uuid4())
    session = HotelSearchSession(search_id, tenant.tenant_id)
    request.app.state.hotel_search_registry[search_id] = session
    query = body.to_query()
    tenant_ctx = TenantContext(
        tenant_id=tenant.tenant_id,
        enabled_suppliers=list(tenant.enabled_suppliers),
    )
    asyncio.create_task(_run_hotel_search(session, request, query, tenant_ctx))
    return HotelSearchStarted(search_id=search_id)


@router.get("/{search_id}/stream")
async def stream_hotel_search(
    search_id: str,
    request: Request,
    tenant: Annotated[Tenant, Depends(get_tenant)],
) -> StreamingResponse:
    registry: dict[str, HotelSearchSession] = request.app.state.hotel_search_registry
    session = registry.get(search_id)
    if session is None or session.tenant_id != tenant.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Search not found")

    async def _sse_gen() -> AsyncGenerator[str, None]:
        async for line in session.event_stream():
            yield f"data: {line}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(_sse_gen(), media_type="text/event-stream")


@router.get("/{search_id}", response_model=HotelSearchSnapshot)
async def get_hotel_snapshot(
    search_id: str,
    request: Request,
    tenant: Annotated[Tenant, Depends(get_tenant)],
) -> HotelSearchSnapshot:
    registry: dict[str, HotelSearchSession] = request.app.state.hotel_search_registry
    session = registry.get(search_id)
    if session is None or session.tenant_id != tenant.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Search not found")

    groups_out = [
        HotelDedupeGroupOut(
            dedupe_key=g.dedupe_key,
            primary=HotelOfferOut(
                offer_id=g.primary.offer_id,
                supplier_id=g.primary.supplier_id,
                property_id=g.primary.property_id,
                property_name=g.primary.property_name,
                star_rating=g.primary.star_rating,
                location=g.primary.location,
                address=g.primary.address,
                checkin_date=g.primary.checkin_date,
                checkout_date=g.primary.checkout_date,
                nights=g.primary.nights,
                room=RoomInfoOut(
                    room_type=g.primary.room.room_type,
                    bed_type=g.primary.room.bed_type,
                    max_occupancy=g.primary.room.max_occupancy,
                    room_count=g.primary.room.room_count,
                    amenities=g.primary.room.amenities,
                ),
                total_inr=str(g.primary.total_inr),
                cancellation_policy=CancellationPolicyOut(
                    type=g.primary.cancellation_policy.type,
                    free_cancellation_before=g.primary.cancellation_policy.free_cancellation_before,
                    penalty_inr=str(g.primary.cancellation_policy.penalty_inr)
                    if g.primary.cancellation_policy.penalty_inr is not None
                    else None,
                ),
                meal_plan=g.primary.meal_plan,
                expires_at=g.primary.expires_at,
                is_test_data=g.primary.is_test_data,
            ),
            alternates=[
                HotelAlternateSourceOut(
                    supplier_id=a.supplier_id,
                    offer_id=a.offer_id,
                    total_inr=str(a.total_inr),
                )
                for a in g.alternates
            ],
        )
        for g in session.groups
    ]

    return HotelSearchSnapshot(
        search_id=search_id,
        tenant_id=session.tenant_id,
        is_complete=session.is_complete,
        groups=groups_out,
        suppliers_succeeded=session.suppliers_succeeded,
        suppliers_failed=session.suppliers_failed,
        suppliers_timed_out=session.suppliers_timed_out,
    )
