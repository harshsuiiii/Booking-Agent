"""Tests for hotel domain models."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from booking_agent.domain.booking import BookingState, Passenger, PassengerType
from booking_agent.domain.hotel import (
    CancellationPolicy,
    GuestCounts,
    HotelBooking,
    HotelOffer,
    HotelSearchQuery,
    PricedHotelOffer,
    RoomInfo,
)
from booking_agent.domain.offer import PriceBreakdown

CHECKIN = date(2027, 9, 1)
CHECKOUT = date(2027, 9, 5)
NOW = datetime.now(UTC)


def _price() -> PriceBreakdown:
    return PriceBreakdown(base_fare=Decimal("10000"), currency="INR")


def _room() -> RoomInfo:
    return RoomInfo(room_type="STANDARD", bed_type="DOUBLE", max_occupancy=2)


def _cancellation() -> CancellationPolicy:
    return CancellationPolicy(
        type="free",
        free_cancellation_before=NOW + timedelta(days=2),
    )


def _offer(**kwargs) -> HotelOffer:
    defaults = dict(
        supplier_id="test",
        supplier_offer_ref="REF001",
        property_id="prop_001",
        property_name="Test Hotel",
        star_rating=4.0,
        location="Mumbai",
        checkin_date=CHECKIN,
        checkout_date=CHECKOUT,
        nights=4,
        room=_room(),
        price=_price(),
        total_inr=Decimal("11800"),
        cancellation_policy=_cancellation(),
        expires_at=NOW + timedelta(hours=1),
    )
    defaults.update(kwargs)
    return HotelOffer(**defaults)


def _passenger() -> Passenger:
    return Passenger(
        pax_type=PassengerType.ADULT,
        given_name="Raj",
        family_name="Sharma",
        date_of_birth=date(1990, 1, 1),
    )


def _priced_offer() -> PricedHotelOffer:
    return PricedHotelOffer(
        **_offer().model_dump(),
        priced_at=NOW,
        valid_until=NOW + timedelta(minutes=10),
    )


# -- GuestCounts --------------------------------------------------------------


class TestGuestCounts:
    def test_total(self):
        g = GuestCounts(adults=2, children=1)
        assert g.total == 3

    def test_default_adults_only(self):
        g = GuestCounts()
        assert g.total == 1

    def test_adults_ge_1(self):
        with pytest.raises(ValidationError):
            GuestCounts(adults=0)

    def test_children_ge_0(self):
        with pytest.raises(ValidationError):
            GuestCounts(children=-1)


# -- HotelSearchQuery ---------------------------------------------------------


class TestHotelSearchQuery:
    def test_valid_query(self):
        q = HotelSearchQuery(location="Mumbai", checkin_date=CHECKIN, checkout_date=CHECKOUT)
        assert q.nights == 4

    def test_past_checkin_rejected(self):
        with pytest.raises(Exception, match="checkin_date must not be in the past"):
            HotelSearchQuery(
                location="Mumbai",
                checkin_date=date(2020, 1, 1),
                checkout_date=date(2020, 1, 5),
            )

    def test_checkout_before_checkin_rejected(self):
        with pytest.raises(Exception, match="checkout_date must be after checkin_date"):
            HotelSearchQuery(
                location="Mumbai",
                checkin_date=date(2027, 9, 5),
                checkout_date=date(2027, 9, 1),
            )

    def test_checkout_equal_checkin_rejected(self):
        with pytest.raises(Exception, match="checkout_date must be after checkin_date"):
            HotelSearchQuery(
                location="Mumbai",
                checkin_date=date(2027, 9, 1),
                checkout_date=date(2027, 9, 1),
            )

    def test_nights_calculation(self):
        q = HotelSearchQuery(
            location="Delhi",
            checkin_date=date(2027, 12, 25),
            checkout_date=date(2027, 12, 31),
        )
        assert q.nights == 6

    def test_default_room_count(self):
        q = HotelSearchQuery(location="Mumbai", checkin_date=CHECKIN, checkout_date=CHECKOUT)
        assert q.room_count == 1


# -- HotelOffer ---------------------------------------------------------------


class TestHotelOffer:
    def test_dedupe_key_stability(self):
        o1 = _offer()
        o2 = _offer()  # different offer_id, same key fields
        assert o1.dedupe_key == o2.dedupe_key

    def test_dedupe_key_differs_by_property(self):
        o1 = _offer(property_id="prop_001")
        o2 = _offer(property_id="prop_002")
        assert o1.dedupe_key != o2.dedupe_key

    def test_dedupe_key_differs_by_room_type(self):
        o1 = _offer(room=RoomInfo(room_type="STANDARD", bed_type="DOUBLE", max_occupancy=2))
        o2 = _offer(room=RoomInfo(room_type="DELUXE", bed_type="KING", max_occupancy=2))
        assert o1.dedupe_key != o2.dedupe_key

    def test_dedupe_key_differs_by_dates(self):
        o1 = _offer(checkin_date=date(2027, 9, 1), checkout_date=date(2027, 9, 5))
        o2 = _offer(checkin_date=date(2027, 9, 6), checkout_date=date(2027, 9, 10))
        assert o1.dedupe_key != o2.dedupe_key

    def test_dedupe_key_differs_by_meal_plan(self):
        o1 = _offer(meal_plan=None)
        o2 = _offer(meal_plan="BB")
        assert o1.dedupe_key != o2.dedupe_key

    def test_dedupe_key_is_16_chars(self):
        assert len(_offer().dedupe_key) == 16

    def test_star_rating_bounds(self):
        with pytest.raises(ValidationError):
            _offer(star_rating=0.5)
        with pytest.raises(ValidationError):
            _offer(star_rating=5.5)


# -- PricedHotelOffer ---------------------------------------------------------


class TestPricedHotelOffer:
    def test_creation(self):
        p = _priced_offer()
        assert p.priced_at is not None
        assert p.valid_until > p.priced_at

    def test_inherits_hotel_offer_fields(self):
        p = _priced_offer()
        assert p.property_name == "Test Hotel"
        assert p.nights == 4


# -- CancellationPolicy -------------------------------------------------------


class TestCancellationPolicy:
    def test_free_with_deadline(self):
        pol = CancellationPolicy(
            type="free",
            free_cancellation_before=NOW + timedelta(days=1),
        )
        assert pol.type == "free"
        assert pol.free_cancellation_before is not None

    def test_non_refundable(self):
        pol = CancellationPolicy(type="non_refundable")
        assert pol.penalty_inr is None
        assert pol.free_cancellation_before is None

    def test_partial_with_penalty(self):
        pol = CancellationPolicy(type="partial", penalty_inr=Decimal("500"))
        assert pol.penalty_inr == Decimal("500")

    def test_unknown(self):
        pol = CancellationPolicy(type="unknown")
        assert pol.type == "unknown"


# -- HotelBooking -------------------------------------------------------------


class TestHotelBooking:
    def _booking(self) -> HotelBooking:
        return HotelBooking(
            idempotency_key="idem_001",
            priced_offer=_priced_offer(),
            guests=[_passenger()],
        )

    def test_initial_state_is_draft(self):
        b = self._booking()
        assert b.state == BookingState.DRAFT

    def test_valid_transition_draft_to_priced(self):
        b = self._booking()
        b2 = b.transition_to(BookingState.PRICED, "system")
        assert b2.state == BookingState.PRICED
        assert len(b2.transitions) == 1

    def test_valid_transition_chain(self):
        b = self._booking()
        b = b.transition_to(BookingState.PRICED, "system")
        b = b.transition_to(BookingState.CONFIRMED_BY_USER, "user")
        b = b.transition_to(BookingState.PNR_CREATED, "system")
        b = b.transition_to(BookingState.PAYMENT_PENDING, "system")
        b = b.transition_to(BookingState.PAID, "gateway")
        b = b.transition_to(BookingState.TICKETED, "system")
        assert b.state == BookingState.TICKETED

    def test_illegal_transition_raises(self):
        b = self._booking()
        with pytest.raises(ValueError, match="Illegal transition"):
            b.transition_to(BookingState.TICKETED, "system")

    def test_cancel_from_priced(self):
        b = self._booking().transition_to(BookingState.PRICED, "system")
        b2 = b.transition_to(BookingState.CANCELLED, "user")
        assert b2.state == BookingState.CANCELLED

    def test_immutability(self):
        b = self._booking()
        b2 = b.transition_to(BookingState.PRICED, "system")
        assert b.state == BookingState.DRAFT  # original unchanged
        assert b2.state == BookingState.PRICED

    def test_transition_records_actor(self):
        b = self._booking().transition_to(BookingState.PRICED, "test_actor")
        assert b.transitions[0].actor == "test_actor"
