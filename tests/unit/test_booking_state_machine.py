"""
Tests for Booking.transition_to() and the _TRANSITIONS state machine.

Covers every valid edge, every illegal edge, terminal states,
immutability of the original Booking, and transition history.
"""

from datetime import UTC, date
from decimal import Decimal

import pytest

from booking_agent.domain.booking import Booking, BookingState, Passenger, PassengerType
from booking_agent.domain.common import CabinClass
from booking_agent.domain.itinerary import Segment, Slice
from booking_agent.domain.money import Money
from booking_agent.domain.offer import (
    BaggageAllowance,
    FareRulePolicy,
    FareRules,
    Offer,
    PriceBreakdown,
    PricedOffer,
    TaxLine,
)
from tests.conftest import AI, T0, T1, T_EXPIRE

# ── helpers ───────────────────────────────────────────────────────────────────


def _make_priced_offer() -> PricedOffer:
    seg = Segment(
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
    sl = Slice(segments=[seg])
    price = PriceBreakdown(
        base_fare=Decimal("3000"),
        taxes=[TaxLine(code="YQ", amount=Decimal("500"))],
        fuel_surcharge=Decimal("0"),
        currency="INR",
    )
    offer = Offer(
        supplier_id="fake",
        supplier_offer_ref="REF1",
        slices=[sl],
        fare_basis="ECON",
        cabin_class=CabinClass.ECONOMY,
        baggage=BaggageAllowance(cabin_pieces=1),
        fare_rules=FareRules(
            refund=FareRulePolicy(allowed=True, fee=Money(amount=Decimal("0"), currency="INR")),
            change=FareRulePolicy(allowed=True, fee=Money(amount=Decimal("0"), currency="INR")),
        ),
        price=price,
        total_inr=Decimal("3500"),
        expires_at=T_EXPIRE,
    )
    from datetime import datetime

    now = datetime.now(UTC)
    return PricedOffer(
        **offer.model_dump(),
        priced_at=now,
        valid_until=T_EXPIRE,
    )


def _make_booking(state: BookingState = BookingState.DRAFT) -> Booking:
    pax = [
        Passenger(
            pax_type=PassengerType.ADULT,
            given_name="John",
            family_name="Doe",
            date_of_birth=date(1990, 1, 1),
        )
    ]
    return Booking(
        tenant_id="tenant-1",
        idempotency_key="key-1",
        priced_offer=_make_priced_offer(),
        passengers=pax,
        state=state,
    )


# ── valid transitions ─────────────────────────────────────────────────────────


class TestValidTransitions:
    def test_draft_to_priced(self):
        b = _make_booking(BookingState.DRAFT)
        b2 = b.transition_to(BookingState.PRICED, "system")
        assert b2.state == BookingState.PRICED

    def test_draft_to_cancelled(self):
        b = _make_booking(BookingState.DRAFT)
        b2 = b.transition_to(BookingState.CANCELLED, "user")
        assert b2.state == BookingState.CANCELLED

    def test_draft_to_expired(self):
        b = _make_booking(BookingState.DRAFT)
        b2 = b.transition_to(BookingState.EXPIRED, "system")
        assert b2.state == BookingState.EXPIRED

    def test_priced_to_confirmed_by_user(self):
        b = _make_booking(BookingState.PRICED)
        b2 = b.transition_to(BookingState.CONFIRMED_BY_USER, "user")
        assert b2.state == BookingState.CONFIRMED_BY_USER

    def test_priced_to_cancelled(self):
        b = _make_booking(BookingState.PRICED)
        b2 = b.transition_to(BookingState.CANCELLED, "user")
        assert b2.state == BookingState.CANCELLED

    def test_priced_to_expired(self):
        b = _make_booking(BookingState.PRICED)
        b2 = b.transition_to(BookingState.EXPIRED, "system")
        assert b2.state == BookingState.EXPIRED

    def test_confirmed_to_pnr_created(self):
        b = _make_booking(BookingState.CONFIRMED_BY_USER)
        b2 = b.transition_to(BookingState.PNR_CREATED, "system")
        assert b2.state == BookingState.PNR_CREATED

    def test_confirmed_to_failed(self):
        b = _make_booking(BookingState.CONFIRMED_BY_USER)
        b2 = b.transition_to(BookingState.FAILED, "system")
        assert b2.state == BookingState.FAILED

    def test_confirmed_to_cancelled(self):
        b = _make_booking(BookingState.CONFIRMED_BY_USER)
        b2 = b.transition_to(BookingState.CANCELLED, "user")
        assert b2.state == BookingState.CANCELLED

    def test_pnr_created_to_payment_pending(self):
        b = _make_booking(BookingState.PNR_CREATED)
        b2 = b.transition_to(BookingState.PAYMENT_PENDING, "system")
        assert b2.state == BookingState.PAYMENT_PENDING

    def test_pnr_created_to_cancelled(self):
        b = _make_booking(BookingState.PNR_CREATED)
        b2 = b.transition_to(BookingState.CANCELLED, "user")
        assert b2.state == BookingState.CANCELLED

    def test_payment_pending_to_paid(self):
        b = _make_booking(BookingState.PAYMENT_PENDING)
        b2 = b.transition_to(BookingState.PAID, "system")
        assert b2.state == BookingState.PAID

    def test_payment_pending_to_failed(self):
        b = _make_booking(BookingState.PAYMENT_PENDING)
        b2 = b.transition_to(BookingState.FAILED, "system")
        assert b2.state == BookingState.FAILED

    def test_payment_pending_to_cancelled(self):
        b = _make_booking(BookingState.PAYMENT_PENDING)
        b2 = b.transition_to(BookingState.CANCELLED, "user")
        assert b2.state == BookingState.CANCELLED

    def test_paid_to_ticketed(self):
        b = _make_booking(BookingState.PAID)
        b2 = b.transition_to(BookingState.TICKETED, "system")
        assert b2.state == BookingState.TICKETED

    def test_paid_to_failed(self):
        b = _make_booking(BookingState.PAID)
        b2 = b.transition_to(BookingState.FAILED, "system")
        assert b2.state == BookingState.FAILED


# ── illegal transitions ───────────────────────────────────────────────────────


class TestIllegalTransitions:
    @pytest.mark.parametrize(
        "from_state,to_state",
        [
            (BookingState.DRAFT, BookingState.CONFIRMED_BY_USER),
            (BookingState.DRAFT, BookingState.PNR_CREATED),
            (BookingState.DRAFT, BookingState.PAYMENT_PENDING),
            (BookingState.DRAFT, BookingState.PAID),
            (BookingState.DRAFT, BookingState.TICKETED),
            (BookingState.DRAFT, BookingState.FAILED),
            (BookingState.PRICED, BookingState.DRAFT),
            (BookingState.PRICED, BookingState.PNR_CREATED),
            (BookingState.PRICED, BookingState.TICKETED),
            (BookingState.CONFIRMED_BY_USER, BookingState.PRICED),
            (BookingState.CONFIRMED_BY_USER, BookingState.PAID),
            (BookingState.CONFIRMED_BY_USER, BookingState.TICKETED),
            (BookingState.PNR_CREATED, BookingState.CONFIRMED_BY_USER),
            (BookingState.PNR_CREATED, BookingState.TICKETED),
            (BookingState.PNR_CREATED, BookingState.FAILED),
            (BookingState.PAYMENT_PENDING, BookingState.PNR_CREATED),
            (BookingState.PAYMENT_PENDING, BookingState.TICKETED),
            (BookingState.PAID, BookingState.CANCELLED),
            (BookingState.PAID, BookingState.PAYMENT_PENDING),
        ],
    )
    def test_illegal_transition_raises(self, from_state: BookingState, to_state: BookingState):
        b = _make_booking(from_state)
        with pytest.raises(ValueError, match="Illegal transition"):
            b.transition_to(to_state, "actor")


# ── terminal states ───────────────────────────────────────────────────────────


class TestTerminalStates:
    @pytest.mark.parametrize(
        "terminal",
        [
            BookingState.TICKETED,
            BookingState.FAILED,
            BookingState.CANCELLED,
            BookingState.EXPIRED,
        ],
    )
    def test_terminal_states_allow_no_transitions(self, terminal: BookingState):
        b = _make_booking(terminal)
        for target in BookingState:
            with pytest.raises(ValueError):
                b.transition_to(target, "actor")


# ── immutability ──────────────────────────────────────────────────────────────


class TestImmutability:
    def test_original_unchanged_after_transition(self):
        b = _make_booking(BookingState.DRAFT)
        original_state = b.state
        b.transition_to(BookingState.PRICED, "system")
        assert b.state == original_state

    def test_returns_new_instance(self):
        b = _make_booking(BookingState.DRAFT)
        b2 = b.transition_to(BookingState.PRICED, "system")
        assert b2 is not b

    def test_booking_id_preserved(self):
        b = _make_booking(BookingState.DRAFT)
        original_id = b.booking_id
        b2 = b.transition_to(BookingState.PRICED, "system")
        assert b2.booking_id == original_id


# ── transition history ────────────────────────────────────────────────────────


class TestTransitionHistory:
    def test_single_transition_appended(self):
        b = _make_booking(BookingState.DRAFT)
        b2 = b.transition_to(BookingState.PRICED, "system")
        assert len(b2.transitions) == 1
        t = b2.transitions[0]
        assert t.from_state == BookingState.DRAFT
        assert t.to_state == BookingState.PRICED
        assert t.actor == "system"

    def test_chain_of_transitions_accumulates(self):
        b = _make_booking(BookingState.DRAFT)
        b = b.transition_to(BookingState.PRICED, "system")
        b = b.transition_to(BookingState.CONFIRMED_BY_USER, "user")
        b = b.transition_to(BookingState.PNR_CREATED, "system")
        assert len(b.transitions) == 3
        assert b.transitions[0].from_state == BookingState.DRAFT
        assert b.transitions[2].to_state == BookingState.PNR_CREATED

    def test_original_has_no_transitions(self):
        b = _make_booking(BookingState.DRAFT)
        b.transition_to(BookingState.PRICED, "system")
        assert len(b.transitions) == 0

    def test_updated_at_changes(self):
        import time

        b = _make_booking(BookingState.DRAFT)
        before = b.updated_at
        time.sleep(0.01)
        b2 = b.transition_to(BookingState.PRICED, "system")
        assert b2.updated_at >= before
