"""
SimulatedConnector: a realistic, deterministic stand-in supplier for development and demos.

No free source offers live bookable fares, so this generates them: real airports and
airlines, plausible schedules (network.py) and a distance-based price model with
per-supplier differences (pricing.py). Every offer has source="simulated" and the UI
labels it; these are never real fares and booking them issues no real ticket.

Configured from env by simulated_connectors_from_env():
  (which suppliers run is SUPPLIERS_ENABLED, read by the app)
  SIM_LATENCY_MIN_S/MAX_S  per-search delay, default 0.2 / 1.5
  SIM_FAILURE_RATE       chance a search fails (partial-failure UI), default 0.08
  SIM_PRICE_CHANGE_RATE  chance a live re-price comes back higher, default 0.05
"""

import asyncio
import os
import random
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from booking_agent.connectors.base import BaseConnector
from booking_agent.connectors.errors import SoldOut, SupplierBadResponse
from booking_agent.connectors.payment import PaymentInstruction
from booking_agent.connectors.protocol import SupplierCapabilities
from booking_agent.connectors.secrets import EnvSecretsProvider
from booking_agent.domain.booking import Booking, BookingState, Passenger
from booking_agent.domain.common import CabinClass, Carrier
from booking_agent.domain.itinerary import Segment, Slice
from booking_agent.domain.offer import Offer, PricedOffer
from booking_agent.domain.search import JourneyType, PaxCounts, SearchLeg, SearchQuery
from booking_agent.simulated.airports import distance_km, get_airport
from booking_agent.simulated.network import Itinerary, build_itineraries, seeded_rng
from booking_agent.simulated.pricing import (
    PROFILES,
    FareFamily,
    SupplierProfile,
    baggage_and_rules,
    economy_fare_per_pax,
    families_for,
    price_breakdown,
)

_REF_PREFIX = "SIM2"
_MIN_CONNECT = timedelta(hours=2)  # between two flights of the journey


class SimulatedConnector(BaseConnector):
    def __init__(
        self,
        profile: SupplierProfile,
        latency_s: tuple[float, float] = (0.2, 1.5),
        failure_rate: float = 0.08,
        price_change_rate: float = 0.05,
        max_offers: int = 30,
        today: Callable[[], date] = date.today,
        rand: random.Random | None = None,
    ) -> None:
        # Set before super().__init__, which reads capabilities to bind its logger.
        self.SUPPLIER_ID = profile.supplier_id
        self.profile = profile
        super().__init__(secrets=EnvSecretsProvider(), max_retries=1)
        self._latency = latency_s
        self._failure_rate = failure_rate
        self._price_change_rate = price_change_rate
        self._max_offers = max_offers
        self._today = today
        # Unseeded on purpose: latency, outages and price drift vary per call.
        self._rand = rand or random.Random()

    @property
    def capabilities(self) -> SupplierCapabilities:
        return SupplierCapabilities(
            supplier_id=self.profile.supplier_id,
            supports_round_trip=True,
            supports_multi_city=True,
            supports_ticketing=True,
            max_pax=9,
            currencies=["INR"],
        )

    # No HTTP: skip the client lifecycle.
    async def __aenter__(self) -> "SimulatedConnector":
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass

    # ── search ────────────────────────────────────────────────────────────────

    async def _simulate_network(self, operation: str) -> None:
        lo, hi = self._latency
        if hi > 0:
            await asyncio.sleep(self._rand.uniform(lo, hi))
        if self._rand.random() < self._failure_rate:
            raise SupplierBadResponse(self.SUPPLIER_ID, 503, "simulated supplier outage", operation)

    async def _do_search(self, query: SearchQuery) -> list[Offer]:
        await self._simulate_network("search")
        return self.generate_offers(query)

    def generate_offers(self, query: SearchQuery) -> list[Offer]:
        """Deterministic offers for (legs, cabin, pax) from this supplier."""
        per_leg = [
            self._sellable(leg.origin, leg.destination, leg.departure_date) for leg in query.legs
        ]
        now = datetime.now(UTC)
        offers: list[Offer] = []
        for first in per_leg[0]:
            chain = _chain(first, per_leg[1:], query.journey_type)
            if chain is None:
                continue
            for family in families_for(first.carrier, query.cabin):
                if family.code not in self.profile.families:
                    continue
                offers.append(self._offer(query, chain, family, now))
        offers.sort(key=lambda o: o.total_inr)
        return offers[: self._max_offers]

    def _sellable(self, origin: str, destination: str, day: date) -> list[Itinerary]:
        return [
            itin
            for itin in build_itineraries(origin, destination, day)
            if seeded_rng("sells", self.SUPPLIER_ID, itin.key, day).random()
            < self.profile.availability
        ]

    def _offer(
        self,
        query: SearchQuery,
        chain: list[Itinerary],
        family: FareFamily,
        now: datetime,
    ) -> Offer:
        airports = [(get_airport(leg.origin), get_airport(leg.destination)) for leg in query.legs]
        domestic = all(
            a is not None and a.is_india and b is not None and b.is_india for a, b in airports
        )
        pax = query.pax_counts
        key: tuple[object, ...] = (
            *(_leg_code(leg) for leg in query.legs),
            *(itin.key for itin in chain),
            family.code,
        )

        per_pax = Decimal(0)
        for i, (leg, itin, (a, b)) in enumerate(zip(query.legs, chain, airports, strict=True)):
            assert a is not None and b is not None  # itineraries exist only for known airports
            per_pax += economy_fare_per_pax(
                distance_km(a, b),
                domestic,
                itin.carrier.lcc,
                itin.stops,
                leg.departure_date,
                self._today(),
                (*key, i),
            )
        if query.journey_type == JourneyType.RETURN:
            per_pax *= Decimal("0.95")  # round-trip discount
        pax_weight = (
            Decimal(pax.adults) + Decimal("0.9") * pax.children + Decimal("0.12") * pax.infants
        )
        airline_fare = per_pax * family.multiplier * pax_weight

        price = price_breakdown(airline_fare, pax.total, domestic, self.profile)
        baggage, rules = baggage_and_rules(family, domestic, self.profile, key)

        return Offer(
            supplier_id=self.SUPPLIER_ID,
            supplier_offer_ref=_make_ref(self.SUPPLIER_ID, query, chain, family),
            slices=[_slice(itin, query.cabin, family) for itin in chain],
            fare_basis=f"{family.rbd}{family.code[:3]}{'IN' if domestic else 'OW'}",
            fare_family=family.name,
            cabin_class=query.cabin,
            baggage=baggage,
            fare_rules=rules,
            price=price,
            total_inr=price.net_total,
            expires_at=now + timedelta(minutes=20),
            searched_at=now,
            pax_count=pax.total,
            pax_counts=pax,
            source="simulated",
        )

    # ── price ─────────────────────────────────────────────────────────────────

    async def _do_price(self, offer: Offer) -> PricedOffer:
        await self._simulate_network("price")
        query = _query_from_ref(offer.supplier_offer_ref)
        match = next(
            (
                o
                for o in self.generate_offers(query)
                if o.supplier_offer_ref == offer.supplier_offer_ref
            ),
            None,
        )
        if match is None:
            raise SoldOut(self.SUPPLIER_ID, offer.supplier_offer_ref)
        price = match.price
        if self._rand.random() < self._price_change_rate:
            bump = Decimal(str(round(self._rand.uniform(1.03, 1.08), 3)))
            price = price.model_copy(
                update={"base_fare": (price.base_fare * bump).quantize(Decimal("1"))}
            )
        now = datetime.now(UTC)
        return PricedOffer(
            **{
                **offer.model_dump(),
                "price": price,
                "total_inr": price.net_total,
                "priced_at": now,
                "valid_until": now + timedelta(minutes=10),
            }
        )

    # ── book ──────────────────────────────────────────────────────────────────

    async def _do_book(
        self,
        priced_offer: PricedOffer,
        passengers: list[Passenger],
        idempotency_key: str,
        payment: PaymentInstruction,
    ) -> Booking:
        await self._simulate_network("book")
        return Booking(
            idempotency_key=idempotency_key,
            priced_offer=priced_offer,
            passengers=passengers,
            state=BookingState.PNR_CREATED,
            pnr=f"SIM{uuid.uuid4().hex[:5].upper()}",  # simulated: no real ticket exists
            supplier_booking_ref=f"{self.SUPPLIER_ID}_ord_{uuid.uuid4().hex[:8]}",
        )


# ── helpers ───────────────────────────────────────────────────────────────────


def _slice(itin: Itinerary, cabin: CabinClass, family: FareFamily) -> Slice:
    return Slice(
        segments=[
            Segment(
                marketing_carrier=Carrier(iata_code=leg.carrier.code, name=leg.carrier.name),
                flight_number=leg.flight_number,
                origin=leg.origin.iata,
                destination=leg.destination.iata,
                departure_at=leg.departure,
                arrival_at=leg.arrival,
                cabin_class=cabin,
                booking_class=family.rbd,
                aircraft_type=leg.aircraft,
                duration_minutes=leg.minutes,
            )
            for leg in itin.legs
        ]
    )


def _chain(
    first: Itinerary, later: list[list[Itinerary]], journey: JourneyType | None
) -> list[Itinerary] | None:
    """
    The rest of the journey after `first`: one itinerary per later leg, each leaving at
    least two hours after the previous one lands. Same airline where it flies the leg;
    a return trip must be same-airline, a multi-city one may combine airlines.
    Seeded choices keep the journey stable for the same search.
    """
    chain = [first]
    for i, candidates in enumerate(later, start=1):
        prev = chain[-1]
        ready = [
            c for c in candidates if c.legs[0].departure >= prev.legs[-1].arrival + _MIN_CONNECT
        ]
        same = [c for c in ready if c.carrier.code == first.carrier.code]
        pool = same or ([] if journey == JourneyType.RETURN else ready)
        if not pool:
            return None
        chain.append(pool[seeded_rng("leg", i, *(c.key for c in chain)).randrange(len(pool))])
    return chain


def _leg_code(leg: SearchLeg) -> str:
    return f"{leg.origin}.{leg.destination}.{leg.departure_date.isoformat()}"


_JOURNEY_CODES = {JourneyType.ONE_WAY: "o", JourneyType.RETURN: "r", JourneyType.MULTI_CITY: "m"}


def _make_ref(
    supplier_id: str,
    query: SearchQuery,
    chain: list[Itinerary],
    family: FareFamily,
) -> str:
    p = query.pax_counts
    assert query.journey_type is not None  # filled by SearchQuery's validator
    return "|".join(
        [
            _REF_PREFIX,
            supplier_id,
            _JOURNEY_CODES[query.journey_type],
            "~".join(_leg_code(leg) for leg in query.legs),
            "~".join(itin.key for itin in chain),
            family.code,
            query.cabin.value,
            f"{p.adults}.{p.children}.{p.infants}",
        ]
    )


def _query_from_ref(ref: str) -> SearchQuery:
    parts = ref.split("|")
    if len(parts) != 8 or parts[0] != _REF_PREFIX:
        raise ValueError(f"Not a simulated offer reference: {ref!r}")
    _, _, journey, legs, _, _, cabin, pax = parts
    adults, children, infants = (int(x) for x in pax.split("."))
    journey_type = next(j for j, code in _JOURNEY_CODES.items() if code == journey)
    return SearchQuery.model_validate(
        {
            "journey_type": journey_type,
            "legs": [
                SearchLeg(origin=o, destination=d, departure_date=date.fromisoformat(day))
                for o, d, day in (leg.split(".") for leg in legs.split("~"))
            ],
            "cabin": CabinClass(cabin),
            "pax_counts": PaxCounts(adults=adults, children=children, infants=infants),
        }
    )


def simulated_connectors_from_env(ids: list[str] | None = None) -> list[SimulatedConnector]:
    """Simulated suppliers for `ids` (default: all profiles); latency etc. from env."""
    ids = [sid for sid in (ids if ids is not None else list(PROFILES)) if sid in PROFILES]
    latency = (
        float(os.getenv("SIM_LATENCY_MIN_S", "0.2")),
        float(os.getenv("SIM_LATENCY_MAX_S", "1.5")),
    )
    return [
        SimulatedConnector(
            PROFILES[sid],
            latency_s=latency,
            failure_rate=float(os.getenv("SIM_FAILURE_RATE", "0.08")),
            price_change_rate=float(os.getenv("SIM_PRICE_CHANGE_RATE", "0.05")),
        )
        for sid in ids
    ]
