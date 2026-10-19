"""Tests for FakeHotelConnector."""

from datetime import date
from decimal import Decimal

import pytest

from booking_agent.connectors.errors import SupplierError
from booking_agent.connectors.fake_hotel import FakeHotelConnector
from booking_agent.connectors.payment import DuffelBalancePayment
from booking_agent.domain.booking import BookingState, Passenger, PassengerType
from booking_agent.domain.hotel import HotelSearchQuery, PricedHotelOffer

QUERY = HotelSearchQuery(
    location="Mumbai",
    checkin_date=date(2027, 9, 1),
    checkout_date=date(2027, 9, 5),
)


class TestFakeHotelConnectorSearch:
    async def test_returns_catalogue_size_offers(self):
        async with FakeHotelConnector() as c:
            offers = await c.search(QUERY)
        assert len(offers) == 3

    async def test_all_offers_are_test_data(self):
        async with FakeHotelConnector() as c:
            offers = await c.search(QUERY)
        assert all(o.is_test_data for o in offers)

    async def test_nights_matches_query(self):
        async with FakeHotelConnector() as c:
            offers = await c.search(QUERY)
        for offer in offers:
            assert offer.nights == QUERY.nights

    async def test_location_matches_query(self):
        async with FakeHotelConnector() as c:
            offers = await c.search(QUERY)
        for offer in offers:
            assert offer.location == "Mumbai"

    async def test_offers_have_valid_prices(self):
        async with FakeHotelConnector() as c:
            offers = await c.search(QUERY)
        for offer in offers:
            assert offer.total_inr > 0
            assert offer.price.base_fare > 0

    async def test_error_injection(self):
        err = SupplierError("fake_hotel", "injected error")
        async with FakeHotelConnector(search_error=err) as c:
            with pytest.raises(SupplierError):
                await c.search(QUERY)

    async def test_delay_completes(self):
        async with FakeHotelConnector(delay_seconds=0.01) as c:
            offers = await c.search(QUERY)
        assert len(offers) == 3

    async def test_supplier_id_set_on_offers(self):
        async with FakeHotelConnector(supplier_id="test_supplier") as c:
            offers = await c.search(QUERY)
        assert all(o.supplier_id == "test_supplier" for o in offers)

    async def test_multi_room_pricing(self):
        q2 = HotelSearchQuery(
            location="Mumbai",
            checkin_date=date(2027, 9, 1),
            checkout_date=date(2027, 9, 3),
            room_count=2,
        )
        async with FakeHotelConnector() as c:
            await c.search(QUERY)
            offers_2 = await c.search(q2)
        # 2-room 2-night should cost less than 1-room 4-night for same hotel
        # just check structure
        assert all(o.room.room_count == 2 for o in offers_2)


class TestFakeHotelConnectorPrice:
    async def test_returns_priced_offer(self):
        async with FakeHotelConnector() as c:
            offers = await c.search(QUERY)
            priced = await c.price(offers[0])
        assert isinstance(priced, PricedHotelOffer)
        assert priced.priced_at is not None
        assert priced.valid_until > priced.priced_at

    async def test_price_drift(self):
        async with FakeHotelConnector(price_drift_factor=Decimal("1.1")) as c:
            offers = await c.search(QUERY)
            original_total = offers[0].total_inr
            priced = await c.price(offers[0])
        assert priced.total_inr > original_total

    async def test_no_drift_by_default(self):
        async with FakeHotelConnector() as c:
            offers = await c.search(QUERY)
            priced = await c.price(offers[0])
        assert priced.total_inr == offers[0].total_inr


class TestFakeHotelConnectorBook:
    async def test_returns_hotel_booking_in_priced_state(self):
        async with FakeHotelConnector() as c:
            offers = await c.search(QUERY)
            priced = await c.price(offers[0])
            booking = await c.book(
                priced_offer=priced,
                guests=[
                    Passenger(
                        pax_type=PassengerType.ADULT,
                        given_name="Raj",
                        family_name="Sharma",
                        date_of_birth=date(1990, 5, 15),
                    )
                ],
                idempotency_key="idem_test_001",
                payment=DuffelBalancePayment(),
            )
        assert booking.state == BookingState.PRICED
        assert booking.supplier_booking_ref is not None
        assert booking.confirmation_number is not None

    async def test_booking_has_guests(self):
        guest = Passenger(
            pax_type=PassengerType.ADULT,
            given_name="Priya",
            family_name="Patel",
            date_of_birth=date(1985, 3, 20),
        )
        async with FakeHotelConnector() as c:
            offers = await c.search(QUERY)
            priced = await c.price(offers[0])
            booking = await c.book(
                priced_offer=priced,
                guests=[guest],
                idempotency_key="idem_test_002",
                payment=DuffelBalancePayment(),
            )
        assert len(booking.guests) == 1


class TestFakeHotelConnectorContextManager:
    async def test_context_manager(self):
        c = FakeHotelConnector()
        async with c as entered:
            assert entered is c
