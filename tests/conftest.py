"""Shared fixtures for all test suites."""

import os

# Test-only auth settings, set before any app module reads them. Real values live in .env.
os.environ.setdefault("API_KEY", "dev-key")
os.environ.setdefault("API_KEY_TENANT", "default")
os.environ["DEMO_MODE"] = "false"

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from booking_agent.domain.common import CabinClass, Carrier
from booking_agent.domain.itinerary import Segment, Slice
from booking_agent.domain.money import Money
from booking_agent.domain.offer import (
    BaggageAllowance,
    FareRulePolicy,
    FareRules,
    Offer,
    PriceBreakdown,
    TaxLine,
)

T0 = datetime(2027, 6, 1, 6, 0, tzinfo=UTC)
T1 = datetime(2027, 6, 1, 8, 30, tzinfo=UTC)
T_EXPIRE = datetime(2027, 6, 1, 7, 0, tzinfo=UTC)

AI = Carrier(iata_code="AI", name="Air India")


@pytest.fixture
def base_segment() -> Segment:
    return Segment(
        marketing_carrier=AI,
        flight_number="AI101",
        origin="DEL",
        destination="BOM",
        departure_at=T0,
        arrival_at=T1,
        cabin_class=CabinClass.ECONOMY,
        booking_class="Y",
        duration_minutes=150,
    )


@pytest.fixture
def base_slice(base_segment: Segment) -> Slice:
    return Slice(segments=[base_segment])


@pytest.fixture
def base_price() -> PriceBreakdown:
    return PriceBreakdown(
        base_fare=Decimal("3000"),
        taxes=[
            TaxLine(code="YQ", amount=Decimal("500")),
            TaxLine(code="IN", amount=Decimal("200")),
        ],
        fuel_surcharge=Decimal("150"),
        currency="INR",
    )


@pytest.fixture
def base_offer(base_slice: Slice, base_price: PriceBreakdown) -> Offer:
    return Offer(
        supplier_id="amadeus",
        supplier_offer_ref="AMX123",
        slices=[base_slice],
        fare_basis="YOWCHIN",
        cabin_class=CabinClass.ECONOMY,
        baggage=BaggageAllowance(cabin_pieces=1, checked_pieces=1),
        fare_rules=FareRules(
            refund=FareRulePolicy(allowed=True, fee=Money(amount=Decimal("0"), currency="INR")),
            change=FareRulePolicy(allowed=True, fee=Money(amount=Decimal("500"), currency="INR")),
        ),
        price=base_price,
        total_inr=Decimal("3850"),
        expires_at=T_EXPIRE,
    )
