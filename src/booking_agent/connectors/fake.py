"""
FakeConnector: deterministic, in-process connector for testing.

Behavioral switches allow tests to simulate: slowness, auth failure, timeouts,
rate limits, bad responses, price drift, and sold-out scenarios without any
real network calls.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from booking_agent.connectors.base import BaseConnector
from booking_agent.connectors.errors import SupplierError
from booking_agent.connectors.payment import PaymentInstruction
from booking_agent.connectors.protocol import SupplierCapabilities
from booking_agent.connectors.secrets import EnvSecretsProvider, SecretsProvider
from booking_agent.domain.booking import Booking, BookingState, Passenger
from booking_agent.domain.common import CabinClass, Carrier
from booking_agent.domain.itinerary import Segment, Slice
from booking_agent.domain.offer import (
    BaggageAllowance,
    FareRulePolicy,
    FareRules,
    Offer,
    PriceBreakdown,
    PricedOffer,
    TaxLine,
)
from booking_agent.domain.search import SearchQuery

_FAKE_CARRIER = Carrier(iata_code="FX", name="Fake Airways")


def _build_offer(query: SearchQuery, supplier_id: str, price_base: Decimal) -> Offer:
    now = datetime.now(UTC)
    dep = datetime.combine(query.departure_date, datetime.min.time()).replace(tzinfo=UTC, hour=6)
    arr = dep + timedelta(hours=2, minutes=30)
    seg = Segment(
        marketing_carrier=_FAKE_CARRIER,
        flight_number="FX001",
        origin=query.origin,
        destination=query.destination,
        departure_at=dep,
        arrival_at=arr,
        cabin_class=query.cabin,
        booking_class="Y",
        duration_minutes=150,
    )
    taxes = [TaxLine(code="YQ", amount=Decimal("500")), TaxLine(code="IN", amount=Decimal("200"))]
    tax_sum = sum(t.amount for t in taxes)
    return Offer(
        supplier_id=supplier_id,
        supplier_offer_ref=f"{supplier_id.upper()}-{uuid.uuid4().hex[:8].upper()}",
        slices=[Slice(segments=[seg])],
        fare_basis="YOWFAKE",
        fare_family="ECONOMY_SAVER",
        cabin_class=CabinClass.ECONOMY,
        baggage=BaggageAllowance(cabin_pieces=1, checked_pieces=1),
        fare_rules=FareRules(
            refund=FareRulePolicy(allowed=True, fee=None),
            change=FareRulePolicy(allowed=True, fee=None),
        ),
        price=PriceBreakdown(base_fare=price_base, taxes=taxes, currency="INR"),
        total_inr=price_base + tax_sum,
        expires_at=now + timedelta(minutes=15),
        searched_at=now,
        pax_count=query.pax_counts.total,
        pax_counts=query.pax_counts,
    )


class FakeConnector(BaseConnector):
    """
    In-process connector with configurable failure injection for testing.

    search_errors / price_errors: errors raised in sequence before eventual success.
    book_error: if set, every book() call raises this error (simulates sold-out, etc.).
    price_drift: added to total_inr on every price() call (simulates price change).
    delay_seconds: asyncio.sleep() injected before each operation.
    """

    SUPPLIER_ID = "fake"

    def __init__(
        self,
        secrets: SecretsProvider | None = None,
        timeout_seconds: float = 8.0,
        max_retries: int = 3,
        retry_min_wait: float = 0.0,
        retry_max_wait: float = 0.0,
        # --- behavioral switches ---
        search_errors: list[SupplierError] | None = None,
        price_errors: list[SupplierError] | None = None,
        book_error: SupplierError | None = None,
        price_drift: Decimal = Decimal("0"),
        delay_seconds: float = 0.0,
        base_price: Decimal = Decimal("3000"),
        supplier_id: str = "fake",
    ) -> None:
        # Set before super().__init__ so capabilities.supplier_id is correct
        # when BaseConnector binds the logger.
        self.SUPPLIER_ID = supplier_id
        super().__init__(
            secrets=secrets or EnvSecretsProvider(),
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            retry_min_wait=retry_min_wait,
            retry_max_wait=retry_max_wait,
        )
        self._search_errors: list[SupplierError] = list(search_errors or [])
        self._price_errors: list[SupplierError] = list(price_errors or [])
        self._book_error = book_error
        self._price_drift = price_drift
        self._delay_seconds = delay_seconds
        self._base_price = base_price

        # call counters for test assertions
        self.search_call_count = 0
        self.price_call_count = 0
        self.book_call_count = 0

    @property
    def capabilities(self) -> SupplierCapabilities:
        return SupplierCapabilities(
            supplier_id=self.SUPPLIER_ID,
            supports_round_trip=True,
            max_pax=9,
        )

    # FakeConnector makes no real HTTP calls — skip httpx client lifecycle.
    async def __aenter__(self) -> "FakeConnector":
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass

    # ── abstract implementations ───────────────────────────────────────────

    async def _do_search(self, query: SearchQuery) -> list[Offer]:
        self.search_call_count += 1
        if self._delay_seconds:
            await asyncio.sleep(self._delay_seconds)
        if self._search_errors:
            raise self._search_errors.pop(0)
        return [_build_offer(query, self.SUPPLIER_ID, self._base_price)]

    async def _do_price(self, offer: Offer) -> PricedOffer:
        self.price_call_count += 1
        if self._delay_seconds:
            await asyncio.sleep(self._delay_seconds)
        if self._price_errors:
            raise self._price_errors.pop(0)
        now = datetime.now(UTC)
        drifted_total = offer.total_inr + self._price_drift
        drifted_base = offer.price.base_fare + self._price_drift
        new_price = PriceBreakdown(
            base_fare=drifted_base,
            taxes=offer.price.taxes,
            fuel_surcharge=offer.price.fuel_surcharge,
            supplier_fee=offer.price.supplier_fee,
            agency_markup=offer.price.agency_markup,
            currency=offer.price.currency,
        )
        return PricedOffer(
            **{
                **offer.model_dump(),
                "price": new_price,
                "total_inr": drifted_total,
                "priced_at": now,
                "valid_until": now + timedelta(minutes=10),
            }
        )

    async def _do_book(
        self,
        priced_offer: PricedOffer,
        passengers: list[Passenger],
        idempotency_key: str,
        payment: PaymentInstruction,
    ) -> Booking:
        self.book_call_count += 1
        if self._book_error is not None:
            raise self._book_error
        return Booking(
            idempotency_key=idempotency_key,
            priced_offer=priced_offer,
            passengers=passengers,
            state=BookingState.PNR_CREATED,
            pnr=f"PNR{uuid.uuid4().hex[:6].upper()}",
            supplier_booking_ref=f"fake_ord_{uuid.uuid4().hex[:8]}",
        )
