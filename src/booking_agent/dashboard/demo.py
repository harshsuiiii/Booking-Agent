"""
Demo data for showcase builds (DEMO_MODE=true): the agency's markup rules and ~30 seeded
bookings across every booking state. Every seeded booking has is_demo=True and is built
from simulated offers, so nothing here is a real fare, customer or ticket.
"""

import random
from collections import deque
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from booking_agent.auth.users import DEMO_TENANT_ID
from booking_agent.booking.repository import BookingRepository
from booking_agent.domain.booking import (
    _TRANSITIONS,
    Booking,
    BookingState,
    Passenger,
    PassengerType,
    StateTransition,
)
from booking_agent.domain.common import CabinClass
from booking_agent.domain.offer import PricedOffer
from booking_agent.domain.search import (
    JourneyType,
    PaxCounts,
    SearchFilters,
    SearchLeg,
    SearchQuery,
)
from booking_agent.rules.engine import apply_rules
from booking_agent.rules.model import Rule, RuleAction, RuleActionType
from booking_agent.simulated.connector import SimulatedConnector
from booking_agent.simulated.pricing import PROFILES

_SEED = 20261018

_ROUTES = [
    ("DEL", "BOM"),
    ("BOM", "BLR"),
    ("DEL", "GOI"),
    ("BLR", "HYD"),
    ("MAA", "CCU"),
    ("DEL", "DXB"),
    ("BOM", "DXB"),
    ("COK", "DXB"),
    ("BLR", "SIN"),
    ("DEL", "LHR"),
    ("BOM", "LHR"),
    ("DEL", "BKK"),
    ("HYD", "DOH"),
    ("DEL", "JFK"),
    ("BOM", "SIN"),
    ("CCU", "BKK"),
    ("AMD", "DEL"),
    ("PNQ", "DEL"),
]

# 30 bookings: mostly ticketed, every state represented.
_STATES = (
    [BookingState.TICKETED] * 13
    + [BookingState.PAID] * 2
    + [BookingState.PAYMENT_PENDING] * 2
    + [BookingState.PNR_CREATED] * 2
    + [BookingState.CONFIRMED_BY_USER]
    + [BookingState.PRICED] * 2
    + [BookingState.FAILED] * 2
    + [BookingState.CANCELLED] * 3
    + [BookingState.EXPIRED] * 2
    + [BookingState.DRAFT]
)

_GIVEN = [
    "Aarav",
    "Priya",
    "Rohan",
    "Ananya",
    "Vikram",
    "Meera",
    "Kabir",
    "Isha",
    "Arjun",
    "Diya",
    "Rahul",
    "Sneha",
    "Karan",
    "Nisha",
    "Aditya",
    "Pooja",
    "Siddharth",
    "Riya",
]
_FAMILY = [
    "Sharma",
    "Iyer",
    "Mehta",
    "Reddy",
    "Kapoor",
    "Nair",
    "Gupta",
    "Das",
    "Patel",
    "Banerjee",
    "Menon",
    "Singh",
    "Joshi",
    "Rao",
    "Chopra",
    "Bose",
]
_REF_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def demo_markup_rules(tenant_id: str = DEMO_TENANT_ID) -> list[Rule]:
    """The demo agency's pricing: 4% markup on the net fare plus a ₹199 service fee."""
    return [
        Rule(
            tenant_id=tenant_id,
            name="Agency markup 4%",
            action=RuleAction(action_type=RuleActionType.PERCENTAGE_MARKUP, value=Decimal("4")),
            priority=10,
        ),
        Rule(
            tenant_id=tenant_id,
            name="Service fee",
            action=RuleAction(action_type=RuleActionType.FIXED_MARKUP, value=Decimal("199")),
            priority=20,
        ),
    ]


def _path_to(target: BookingState) -> list[BookingState]:
    """Shortest legal path from PRICED to `target` through the booking state machine."""
    if target == BookingState.PRICED:
        return []
    queue: deque[list[BookingState]] = deque([[BookingState.PRICED]])
    while queue:
        path = queue.popleft()
        for nxt in sorted(_TRANSITIONS[path[-1]], key=lambda s: s.value):
            if nxt == target:
                return [*path[1:], nxt]
            if nxt not in path:
                queue.append([*path, nxt])
    raise ValueError(f"No path to {target}")


async def seed_demo_bookings(
    repo: BookingRepository,
    tenant_id: str = DEMO_TENANT_ID,
    now: datetime | None = None,
) -> int:
    """Create the demo bookings once. Returns how many were created (0 if already there)."""
    if any(s.is_demo for s in await repo.summaries(tenant_id)):
        return 0
    now = now or datetime.now(UTC)
    rng = random.Random(_SEED)
    rules = demo_markup_rules(tenant_id)
    states = list(_STATES)
    rng.shuffle(states)
    created = 0

    for i, state in enumerate(states):
        # A third this month, the rest spread over the previous ~3 months.
        days_ago = rng.randint(0, 17) if i % 3 == 0 else rng.randint(0, 88)
        created_at = (now - timedelta(days=days_ago)).replace(
            hour=rng.randint(4, 16), minute=rng.randrange(0, 60, 5), second=0, microsecond=0
        )
        if created_at > now:
            created_at = now - timedelta(minutes=rng.randint(5, 300))
        travel = created_at.date() + timedelta(days=rng.randint(3, 75))
        booking = _build_booking(i, state, created_at, travel, rules, tenant_id, rng)
        if booking is None:
            continue
        await repo.save(booking)
        created += 1
    return created


def _build_booking(
    i: int,
    state: BookingState,
    created_at: datetime,
    travel: date,
    rules: list[Rule],
    tenant_id: str,
    rng: random.Random,
) -> Booking | None:
    origin, destination = _ROUTES[i % len(_ROUTES)]
    pax = rng.choice([1, 1, 1, 2, 2, 3])
    cabin = CabinClass.BUSINESS if rng.random() < 0.12 else CabinClass.ECONOMY
    profile = PROFILES[rng.choice(sorted(PROFILES))]
    connector = SimulatedConnector(
        profile, latency_s=(0, 0), failure_rate=0, today=lambda: created_at.date()
    )
    # model_construct: demo trips may already be in the past, which SearchQuery rejects.
    query = SearchQuery.model_construct(
        origin=origin,
        destination=destination,
        departure_date=travel,
        return_date=None,
        pax_counts=PaxCounts(adults=pax),
        cabin=cabin,
        filters=SearchFilters(),
        journey_type=JourneyType.ONE_WAY,
        legs=[SearchLeg(origin=origin, destination=destination, departure_date=travel)],
    )
    offers = connector.generate_offers(query)
    if not offers:
        return None
    offer, _ = apply_rules(rng.choice(offers[:8]), rules, tenant_id)
    priced = PricedOffer(
        **offer.model_dump(),
        priced_at=created_at,
        valid_until=created_at + timedelta(minutes=10),
    )
    passengers = [
        Passenger(
            pax_type=PassengerType.ADULT,
            given_name=rng.choice(_GIVEN),
            family_name=rng.choice(_FAMILY),
            date_of_birth=date(rng.randint(1965, 2002), rng.randint(1, 12), rng.randint(1, 28)),
        )
        for _ in range(pax)
    ]

    transitions: list[StateTransition] = []
    at = created_at
    current = BookingState.PRICED
    if state != BookingState.DRAFT:
        for nxt in _path_to(state):
            at += timedelta(minutes=rng.randint(1, 40))
            actor = (
                "agent@demo.test"
                if nxt in (BookingState.CONFIRMED_BY_USER, BookingState.CANCELLED)
                else "system"
            )
            transitions.append(
                StateTransition(from_state=current, to_state=nxt, occurred_at=at, actor=actor)
            )
            current = nxt
    reached_pnr = any(t.to_state == BookingState.PNR_CREATED for t in transitions)

    return Booking(
        tenant_id=tenant_id,
        idempotency_key=f"demo-seed-{i}",
        priced_offer=priced,
        passengers=passengers,
        state=state,
        reference="BA-" + "".join(rng.choice(_REF_ALPHABET) for _ in range(6)),
        pnr=("SIM" + "".join(rng.choice(_REF_ALPHABET) for _ in range(5))) if reached_pnr else None,
        supplier_booking_ref=f"{profile.supplier_id}_ord_demo{i:03d}" if reached_pnr else None,
        error_detail="Simulated supplier error during ticketing"
        if state == BookingState.FAILED
        else None,
        transitions=transitions,
        is_demo=True,
        created_at=created_at,
        updated_at=at,
    )
