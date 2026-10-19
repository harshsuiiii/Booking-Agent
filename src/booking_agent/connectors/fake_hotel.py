"""
FakeHotelConnector: deterministic in-process hotel connector for testing.

Returns synthetic hotel offers from a fixed catalogue with configurable
latency and error injection. Mirrors the FakeConnector pattern for flights.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from booking_agent.connectors.hotel_protocol import HotelSupplierCapabilities
from booking_agent.connectors.payment import PaymentInstruction
from booking_agent.domain.booking import BookingState, Passenger
from booking_agent.domain.hotel import (
    CancellationPolicy,
    HotelBooking,
    HotelOffer,
    HotelSearchQuery,
    PricedHotelOffer,
    RoomInfo,
)
from booking_agent.domain.offer import PriceBreakdown, TaxLine

_CATALOGUE: list[dict[str, Any]] = [
    {
        "property_id": "prop_0001",
        "name": "The Grand Hotel",
        "stars": 4.0,
        "price_per_night": Decimal("5000"),
        "room_type": "STANDARD",
        "bed_type": "DOUBLE",
        "meal_plan": None,
        "cancellation": "free",
        "amenities": ["wifi", "ac", "parking"],
    },
    {
        "property_id": "prop_0002",
        "name": "Luxury Palace",
        "stars": 5.0,
        "price_per_night": Decimal("8000"),
        "room_type": "DELUXE",
        "bed_type": "KING",
        "meal_plan": "BB",
        "cancellation": "free",
        "amenities": ["wifi", "ac", "pool", "gym", "spa"],
    },
    {
        "property_id": "prop_0003",
        "name": "Budget Stay Inn",
        "stars": 3.0,
        "price_per_night": Decimal("2500"),
        "room_type": "STANDARD",
        "bed_type": "TWIN",
        "meal_plan": None,
        "cancellation": "non_refundable",
        "amenities": ["wifi"],
    },
]


def _build_offer(entry: dict[str, Any], query: HotelSearchQuery, supplier_id: str) -> HotelOffer:
    now = datetime.now(UTC)
    nights = query.nights
    base = entry["price_per_night"] * nights * query.room_count
    gst = (base * Decimal("0.18")).quantize(Decimal("0.01"))
    total = base + gst
    cancellation_before = now + timedelta(days=2) if entry["cancellation"] == "free" else None
    return HotelOffer(
        offer_id=f"fh_{entry['property_id']}_{uuid.uuid4().hex[:8]}",
        supplier_id=supplier_id,
        supplier_offer_ref=f"FH-{uuid.uuid4().hex[:8].upper()}",
        property_id=entry["property_id"],
        property_name=entry["name"],
        star_rating=entry["stars"],
        location=query.location,
        checkin_date=query.checkin_date,
        checkout_date=query.checkout_date,
        nights=nights,
        room=RoomInfo(
            room_type=entry["room_type"],
            bed_type=entry["bed_type"],
            max_occupancy=2,
            room_count=query.room_count,
            amenities=list(entry["amenities"]),
        ),
        price=PriceBreakdown(
            base_fare=base,
            taxes=[TaxLine(code="GST", description="Goods and Services Tax", amount=gst)],
            currency="INR",
        ),
        total_inr=total,
        cancellation_policy=CancellationPolicy(
            type=entry["cancellation"],
            free_cancellation_before=cancellation_before,
        ),
        meal_plan=entry["meal_plan"],
        expires_at=now + timedelta(minutes=30),
        is_test_data=True,
    )


class FakeHotelConnector:
    """Deterministic hotel connector for tests. Implements HotelConnector protocol."""

    def __init__(
        self,
        supplier_id: str = "fake_hotel",
        delay_seconds: float = 0.0,
        search_error: Exception | None = None,
        price_drift_factor: Decimal = Decimal("1"),
        max_retries: int = 3,
        retry_min_wait: float = 0.0,
        retry_max_wait: float = 0.0,
    ) -> None:
        self.supplier_id = supplier_id
        self._delay = delay_seconds
        self._search_error = search_error
        self._price_drift_factor = price_drift_factor

    @property
    def capabilities(self) -> HotelSupplierCapabilities:
        return HotelSupplierCapabilities(
            supplier_id=self.supplier_id,
            supports_instant_confirmation=True,
            max_rooms=5,
            meal_plans=["RO", "BB"],
        )

    async def search(self, query: HotelSearchQuery) -> list[HotelOffer]:
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._search_error:
            raise self._search_error
        return [_build_offer(e, query, self.supplier_id) for e in _CATALOGUE]

    async def price(self, offer: HotelOffer) -> PricedHotelOffer:
        if self._delay:
            await asyncio.sleep(self._delay)
        now = datetime.now(UTC)
        drifted_total = (offer.total_inr * self._price_drift_factor).quantize(Decimal("0.01"))
        data = offer.model_dump()
        data["total_inr"] = drifted_total
        data["priced_at"] = now
        data["valid_until"] = now + timedelta(minutes=10)
        return PricedHotelOffer(**data)

    async def book(
        self,
        priced_offer: PricedHotelOffer,
        guests: list[Passenger],
        idempotency_key: str,
        payment: PaymentInstruction,
    ) -> HotelBooking:
        booking = HotelBooking(
            tenant_id="",
            idempotency_key=idempotency_key,
            priced_offer=priced_offer,
            guests=guests,
            state=BookingState.DRAFT,
            supplier_booking_ref=f"FH_BK_{uuid.uuid4().hex[:8].upper()}",
            confirmation_number=f"CONF{uuid.uuid4().hex[:6].upper()}",
        )
        return booking.transition_to(BookingState.PRICED, "fake_hotel")

    async def __aenter__(self) -> "FakeHotelConnector":
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass
