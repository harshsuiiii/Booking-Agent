"""
Booking HTTP routes.

  POST /v1/bookings/prepare
  POST /v1/bookings/{booking_id}/confirm   ← human-only; agent cannot reach this
  GET  /v1/bookings/{booking_id}
  POST /v1/bookings/{booking_id}/cancel
  GET  /v1/bookings
"""

from datetime import date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel

from booking_agent.api.views import money_view, offer_view, sell_inr
from booking_agent.auth.principal import get_principal
from booking_agent.booking.errors import (
    BookingConflict,
    BookingNotFound,
    IdempotentReplay,
    InvalidToken,
    PriceChangedError,
    TokenExpired,
)
from booking_agent.booking.repository import BookingQuery
from booking_agent.booking.service import BookingService
from booking_agent.domain.booking import (
    Booking,
    BookingState,
    DocumentType,
    Passenger,
    PassengerType,
    TravelDocument,
    passenger_mix_errors,
)
from booking_agent.domain.offer import Offer
from booking_agent.suppliers import supplier_label

router = APIRouter(prefix="/v1/bookings", tags=["booking"])


# ── auth helpers ──────────────────────────────────────────────────────────────


def _require_tenant(request: Request) -> str:
    tenant: str | None = getattr(request.state, "tenant_id", None)
    if not tenant:
        raise HTTPException(status_code=401, detail="Authentication required")
    return tenant


def _actor(request: Request, tenant_id: str) -> str:
    return getattr(request.state, "actor", tenant_id)


def _service(request: Request) -> BookingService:
    svc: BookingService | None = getattr(request.app.state, "booking_service", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="Booking service not configured")
    return svc


# ── request / response schemas ────────────────────────────────────────────────


class TravelDocumentIn(BaseModel):
    doc_type: DocumentType
    number: str
    issuing_country: str
    expiry_date: str  # ISO date
    nationality: str


class PassengerIn(BaseModel):
    pax_type: PassengerType
    given_name: str
    family_name: str
    date_of_birth: str  # ISO date
    title: str | None = None
    document: TravelDocumentIn | None = None


class PrepareRequest(BaseModel):
    offer_id: str
    passengers: list[PassengerIn]
    idempotency_key: str


class PrepareResponse(BaseModel):
    booking_id: str
    state: str
    confirmation_token: str  # UI stores this; never passed to agent
    confirmation_token_expires_at: datetime
    customer_price_inr: str
    offer_summary: dict[str, Any]


class ConfirmRequest(BaseModel):
    confirmation_token: str


class BookingResponse(BaseModel):
    booking_id: str
    tenant_id: str
    state: str
    pnr: str | None = None
    supplier_booking_ref: str | None = None
    passenger_count: int
    created_at: datetime
    updated_at: datetime
    transitions: list[dict[str, Any]]
    reference: str = ""
    is_demo: bool = False


class BookingDetail(BookingResponse):
    """One booking for the detail page. `offer` and `price` depend on the caller's role."""

    supplier_id: str
    supplier_label: str
    passengers: list[dict[str, str]]
    offer: dict[str, Any]  # itinerary, baggage, fare rules (role view)
    price: dict[str, Any]  # agent: sell only; admin: full breakdown
    error_detail: str | None = None


class BookingListItem(BaseModel):
    booking_id: str
    reference: str
    passenger: str
    origin: str
    destination: str
    travel_date: date
    supplier_id: str
    supplier_label: str
    state: str
    pnr: str | None
    is_demo: bool
    created_at: datetime
    price: dict[str, str]  # sell_inr (+ net_inr, markup_inr for admins)


class BookingPage(BaseModel):
    items: list[BookingListItem]
    total: int
    page: int
    page_size: int


def _booking_response(b: Booking) -> BookingResponse:
    return BookingResponse(
        booking_id=b.booking_id,
        tenant_id=b.tenant_id,
        state=b.state.value,
        pnr=b.pnr,
        supplier_booking_ref=b.supplier_booking_ref,
        passenger_count=len(b.passengers),
        created_at=b.created_at,
        updated_at=b.updated_at,
        reference=b.reference,
        is_demo=b.is_demo,
        transitions=[
            {
                "from": t.from_state.value,
                "to": t.to_state.value,
                "actor": t.actor,
                "occurred_at": t.occurred_at.isoformat(),
            }
            for t in b.transitions
        ],
    )


def _to_passengers(inputs: list[PassengerIn]) -> list[Passenger]:
    passengers = []
    for p in inputs:
        doc = None
        if p.document:
            from datetime import date

            doc = TravelDocument(
                doc_type=p.document.doc_type,
                number=p.document.number,
                issuing_country=p.document.issuing_country,
                expiry_date=date.fromisoformat(p.document.expiry_date),
                nationality=p.document.nationality,
            )
        from datetime import date

        passengers.append(
            Passenger(
                pax_type=p.pax_type,
                given_name=p.given_name,
                family_name=p.family_name,
                date_of_birth=date.fromisoformat(p.date_of_birth),
                title=p.title,
                document=doc,
            )
        )
    return passengers


# ── endpoints ─────────────────────────────────────────────────────────────────


@router.post("/prepare", response_model=PrepareResponse, status_code=status.HTTP_201_CREATED)
async def prepare_booking(body: PrepareRequest, request: Request) -> PrepareResponse:
    """
    Live re-price the offer and create a PRICED booking.

    Returns a confirmation_token that the UI must present to /confirm.
    The token is NOT returned to the agent.
    """
    tenant_id = _require_tenant(request)
    svc = _service(request)
    actor = _actor(request, tenant_id)

    # Look up the offer from the current search session
    sessions = getattr(request.app.state, "agent_sessions", {})
    offer: Offer | None = None
    for session in sessions.values():
        if session.tenant_id == tenant_id and body.offer_id in session.stored_offers:
            offer = session.stored_offers[body.offer_id]
            break

    if offer is None:
        raise HTTPException(status_code=404, detail="Offer not found. Run a search first.")

    if len(body.passengers) != offer.pax_count:
        raise HTTPException(
            status_code=422,
            detail=f"Offer is priced for {offer.pax_count} passenger(s), "
            f"got {len(body.passengers)}.",
        )

    passengers = _to_passengers(body.passengers)
    problems = passenger_mix_errors(
        [(p.pax_type, p.date_of_birth) for p in passengers],
        offer.pax_counts,
        offer.slices[0].departure_at.date(),
        offer.slices[-1].departure_at.date(),
    )
    if problems:
        raise HTTPException(status_code=422, detail=" ".join(problems))

    try:
        booking, token = await svc.prepare(
            tenant_id=tenant_id,
            offer=offer,
            passengers=passengers,
            idempotency_key=body.idempotency_key,
            actor=actor,
        )
    except IdempotentReplay as exc:
        raise HTTPException(
            status_code=409, detail=f"Booking already exists: {exc.booking_id}"
        ) from exc
    except PriceChangedError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "price_changed",
                "old_price_inr": str(exc.old_price),
                "new_price_inr": str(exc.new_price),
                "message": str(exc),
            },
        ) from exc

    customer_price = booking.priced_offer.total_inr + booking.priced_offer.price.agency_markup
    offer_summary = {
        "supplier_id": booking.priced_offer.supplier_id,
        "route": f"{booking.priced_offer.slices[0].origin} → {booking.priced_offer.slices[-1].destination}",
        "departure": booking.priced_offer.slices[0].departure_at.isoformat(),
        "cabin": booking.priced_offer.cabin_class.value,
        "fare_basis": booking.priced_offer.fare_basis,
        "valid_until": booking.priced_offer.valid_until.isoformat(),
    }

    return PrepareResponse(
        booking_id=booking.booking_id,
        state=booking.state.value,
        confirmation_token=token,
        confirmation_token_expires_at=booking.confirmation_token_expires_at,
        customer_price_inr=str(customer_price),
        offer_summary=offer_summary,
    )


@router.post(
    "/{booking_id}/confirm",
    response_model=BookingResponse,
    summary="Human-authenticated confirmation — not accessible from the agent",
)
async def confirm_booking(
    booking_id: str, body: ConfirmRequest, request: Request
) -> BookingResponse:
    """
    Complete the booking. Requires a valid confirmation token from /prepare.

    This endpoint is intentionally not wired into any agent tool.
    """
    tenant_id = _require_tenant(request)
    svc = _service(request)
    actor = _actor(request, tenant_id)

    try:
        booking = await svc.confirm(
            booking_id=booking_id,
            tenant_id=tenant_id,
            confirmation_token=body.confirmation_token,
            actor=actor,
        )
    except BookingNotFound:
        raise HTTPException(status_code=404, detail="Booking not found") from None
    except BookingConflict as exc:
        # Another confirm for this booking is already in progress or done.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        # Not PRICED any more (already confirmed, cancelled, expired).
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (InvalidToken, TokenExpired) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Supplier error: {exc}") from exc

    return _booking_response(booking)


@router.get("/{booking_id}", response_model=BookingDetail)
async def get_booking(booking_id: str, request: Request) -> BookingDetail:
    tenant_id = _require_tenant(request)
    role = get_principal(request).role
    svc = _service(request)
    try:
        booking = await svc.get(booking_id, tenant_id)
    except BookingNotFound:
        raise HTTPException(status_code=404, detail="Booking not found") from None
    offer = booking.priced_offer
    price = offer.price
    breakdown: dict[str, Any] = money_view(
        role, sell=sell_inr(offer), net=offer.total_inr, markup=price.agency_markup
    )
    breakdown["taxes_total"] = str(price.taxes_total)
    if role == "admin":
        breakdown.update(
            base_fare=str(price.base_fare),
            fuel_surcharge=str(price.fuel_surcharge),
            supplier_fee=str(price.supplier_fee),
            taxes=[t.model_dump(mode="json") for t in price.taxes],
        )
    return BookingDetail(
        **_booking_response(booking).model_dump(),
        supplier_id=offer.supplier_id,
        supplier_label=supplier_label(offer.supplier_id),
        passengers=[
            {
                "name": " ".join(filter(None, [p.title, p.given_name, p.family_name])),
                "type": p.pax_type.value,
            }
            for p in booking.passengers
        ],
        offer=offer_view(offer, role),
        price=breakdown,
        error_detail=booking.error_detail,
    )


@router.post("/{booking_id}/cancel", response_model=BookingResponse)
async def cancel_booking(booking_id: str, request: Request) -> BookingResponse:
    tenant_id = _require_tenant(request)
    svc = _service(request)
    actor = _actor(request, tenant_id)
    try:
        booking = await svc.cancel(booking_id=booking_id, tenant_id=tenant_id, actor=actor)
    except BookingNotFound:
        raise HTTPException(status_code=404, detail="Booking not found") from None
    except BookingConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _booking_response(booking)


@router.get("", response_model=BookingPage)
async def list_bookings(
    request: Request,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    travel_from: Annotated[date | None, Query(alias="from")] = None,
    travel_to: Annotated[date | None, Query(alias="to")] = None,
    q: Annotated[str | None, Query(max_length=60)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> BookingPage:
    tenant_id = _require_tenant(request)
    role = get_principal(request).role
    state: BookingState | None = None
    if status_filter:
        try:
            state = BookingState(status_filter.upper())
        except ValueError:
            raise HTTPException(
                status_code=422, detail=f"Unknown status: {status_filter!r}"
            ) from None
    rows, total = await _service(request).query(
        tenant_id,
        BookingQuery(
            status=state,
            travel_from=travel_from,
            travel_to=travel_to,
            q=q,
            page=page,
            page_size=page_size,
        ),
    )
    return BookingPage(
        items=[
            BookingListItem(
                booking_id=r.booking_id,
                reference=r.reference,
                passenger=r.passenger_display,
                origin=r.origin,
                destination=r.destination,
                travel_date=r.travel_date,
                supplier_id=r.supplier_id,
                supplier_label=supplier_label(r.supplier_id),
                state=r.state.value,
                pnr=r.pnr,
                is_demo=r.is_demo,
                created_at=r.created_at,
                price=money_view(role, sell=r.sell_inr, net=r.net_inr, markup=r.markup_inr),
            )
            for r in rows
        ],
        total=total,
        page=page,
        page_size=page_size,
    )
