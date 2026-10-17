"""
Minimal Offer factory for unit tests.
All times are UTC. All prices are Decimal. Keeps domain invariants valid.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from booking_agent.domain._types import UNKNOWN, Unknown
from booking_agent.domain.common import CabinClass, Carrier
from booking_agent.domain.itinerary import LayoverInfo, Segment, Slice
from booking_agent.domain.offer import (
    BaggageAllowance,
    FareRulePolicy,
    FareRules,
    Offer,
    PriceBreakdown,
)

_BASE_DEP = datetime(2025, 9, 1, 6, 0, tzinfo=timezone.utc)


def make_segment(
    origin: str = "DEL",
    destination: str = "BOM",
    iata_code: str = "AI",
    flight_number: str = "101",
    duration_minutes: int = 150,
    departure_at: datetime | None = None,
    booking_class: str = "Y",
) -> Segment:
    dep = departure_at or _BASE_DEP
    arr = dep + timedelta(minutes=duration_minutes)
    return Segment(
        marketing_carrier=Carrier(iata_code=iata_code, name=iata_code),
        flight_number=flight_number,
        origin=origin,
        destination=destination,
        departure_at=dep,
        arrival_at=arr,
        cabin_class=CabinClass.ECONOMY,
        booking_class=booking_class,
        duration_minutes=duration_minutes,
    )


def make_offer(
    *,
    total_inr: Decimal = Decimal("3000"),
    supplier_id: str = "amadeus",
    iata_code: str = "AI",
    flight_number: str = "101",
    fare_basis: str = "YOWCHIN",
    cabin_class: CabinClass = CabinClass.ECONOMY,
    checked_pieces: int | None | Unknown = 1,
    refundable: bool | Unknown = UNKNOWN,
    changeable: bool | Unknown = UNKNOWN,
    duration_minutes: int = 150,
    departure_at: datetime | None = None,
    origin: str = "DEL",
    destination: str = "BOM",
    n_segments: int = 1,
    layover_minutes: int = 90,
    extra_iata: str = "BLR",
) -> Offer:
    """
    Build a minimal valid Offer. Set n_segments > 1 for connecting flights.
    layover_minutes is the gap between segments in a multi-segment slice.
    """
    dep = departure_at or _BASE_DEP
    seg_duration = duration_minutes // n_segments

    segments: list[Segment] = []
    airports = [origin] + [extra_iata] * (n_segments - 1) + [destination]
    cur_dep = dep
    for i in range(n_segments):
        seg = make_segment(
            origin=airports[i],
            destination=airports[i + 1],
            iata_code=iata_code,
            flight_number=f"{flight_number}{i or ''}".rstrip("0") if i > 0 else flight_number,
            duration_minutes=seg_duration,
            departure_at=cur_dep,
        )
        segments.append(seg)
        cur_dep = seg.arrival_at + timedelta(minutes=layover_minutes)

    sl = Slice(segments=segments)

    return Offer(
        supplier_id=supplier_id,
        supplier_offer_ref=f"ref-{supplier_id}-{flight_number}",
        slices=[sl],
        fare_basis=fare_basis,
        cabin_class=cabin_class,
        baggage=BaggageAllowance(checked_pieces=checked_pieces),
        fare_rules=FareRules(
            refund=FareRulePolicy(allowed=refundable),
            change=FareRulePolicy(allowed=changeable),
        ),
        price=PriceBreakdown(base_fare=total_inr, currency="INR"),
        total_inr=total_inr,
        expires_at=dep + timedelta(hours=1),
    )


def clone_offer(offer: Offer, **overrides: Any) -> Offer:
    """Return a copy of offer with specific fields overridden."""
    data = offer.model_dump()
    data.update(overrides)
    return Offer.model_validate(data)
