from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from booking_agent.domain._types import UNKNOWN
from booking_agent.domain.common import CabinClass, Carrier
from booking_agent.domain.itinerary import Segment, Slice
from booking_agent.domain.offer import (
    BaggageAllowance,
    FareRulePolicy,
    FareRules,
    Offer,
    PriceBreakdown,
    TaxLine,
)

AI = Carrier(iata_code="AI")
T0 = datetime(2025, 6, 1, 6, 0, tzinfo=UTC)
T1 = datetime(2025, 6, 1, 8, 30, tzinfo=UTC)
T_EXPIRE = datetime(2025, 6, 1, 7, 0, tzinfo=UTC)


def make_offer(**overrides) -> Offer:
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
    defaults: dict = dict(
        supplier_id="amadeus",
        supplier_offer_ref="AMX123",
        slices=[sl],
        fare_basis="YOWCHIN",
        cabin_class=CabinClass.ECONOMY,
        baggage=BaggageAllowance(cabin_pieces=1, checked_pieces=1),
        fare_rules=FareRules(
            refund=FareRulePolicy(allowed=True),
            change=FareRulePolicy(allowed=True),
        ),
        price=PriceBreakdown(
            base_fare=Decimal("3000"),
            taxes=[TaxLine(code="YQ", amount=Decimal("500"))],
            fuel_surcharge=Decimal("200"),
            currency="INR",
        ),
        total_inr=Decimal("3700"),
        expires_at=T_EXPIRE,
    )
    defaults.update(overrides)
    return Offer(**defaults)


class TestBaggageAllowance:
    def test_defaults_are_unknown(self):
        b = BaggageAllowance()
        assert b.cabin_pieces == UNKNOWN
        assert b.cabin_kg == UNKNOWN
        assert b.checked_pieces == UNKNOWN
        assert b.checked_kg == UNKNOWN

    def test_none_is_distinct_from_unknown(self):
        b = BaggageAllowance(checked_pieces=None)
        assert b.checked_pieces is None
        assert b.checked_pieces != UNKNOWN

    def test_zero_is_distinct_from_unknown(self):
        b = BaggageAllowance(checked_pieces=0)
        assert b.checked_pieces == 0
        assert b.checked_pieces != UNKNOWN

    def test_explicit_count(self):
        b = BaggageAllowance(checked_pieces=2, checked_kg=Decimal("23"))
        assert b.checked_pieces == 2


class TestFareRulePolicy:
    def test_defaults_are_unknown(self):
        p = FareRulePolicy()
        assert p.allowed == UNKNOWN
        assert p.fee == UNKNOWN

    def test_false_is_distinct_from_unknown(self):
        p = FareRulePolicy(allowed=False)
        assert p.allowed is False
        assert p.allowed != UNKNOWN

    def test_none_fee_is_distinct_from_unknown(self):
        p = FareRulePolicy(allowed=True, fee=None)
        assert p.fee is None
        assert p.fee != UNKNOWN


class TestPriceBreakdown:
    def test_taxes_total(self):
        pb = PriceBreakdown(
            base_fare=Decimal("3000"),
            taxes=[
                TaxLine(code="YQ", amount=Decimal("500")),
                TaxLine(code="IN", amount=Decimal("200")),
            ],
            currency="INR",
        )
        assert pb.taxes_total == Decimal("700")

    def test_net_total(self):
        pb = PriceBreakdown(
            base_fare=Decimal("3000"),
            taxes=[TaxLine(code="YQ", amount=Decimal("500"))],
            fuel_surcharge=Decimal("200"),
            supplier_fee=Decimal("100"),
            currency="INR",
        )
        assert pb.net_total == Decimal("3800")

    def test_sell_total_includes_markup(self):
        pb = PriceBreakdown(
            base_fare=Decimal("3000"),
            taxes=[TaxLine(code="YQ", amount=Decimal("500"))],
            agency_markup=Decimal("300"),
            currency="INR",
        )
        assert pb.sell_total == Decimal("3800")

    def test_float_raises(self):
        with pytest.raises(ValidationError, match="float"):
            PriceBreakdown(base_fare=3000.0, currency="INR")

    def test_empty_taxes_defaults(self):
        pb = PriceBreakdown(base_fare=Decimal("3000"), currency="INR")
        assert pb.taxes_total == Decimal("0")
        assert pb.net_total == Decimal("3000")


class TestOffer:
    def test_valid(self):
        o = make_offer()
        assert o.supplier_id == "amadeus"
        assert o.total_inr == Decimal("3700")

    def test_unique_offer_ids(self):
        assert make_offer().offer_id != make_offer().offer_id

    def test_float_total_raises(self):
        with pytest.raises(ValidationError, match="float"):
            make_offer(total_inr=3700.0)

    def test_empty_slices_raises(self):
        with pytest.raises(ValidationError):
            make_offer(slices=[])

    def test_pax_count_zero_raises(self):
        with pytest.raises(ValidationError):
            make_offer(pax_count=0)

    def test_json_round_trip(self):
        o = make_offer()
        o2 = Offer.model_validate_json(o.model_dump_json())
        assert o2.offer_id == o.offer_id
        assert isinstance(o2.total_inr, Decimal)

    def test_searched_at_set_automatically(self):
        o = make_offer()
        assert o.searched_at is not None


class TestDedupeKey:
    def test_same_offer_same_key(self):
        o1 = make_offer()
        o2 = make_offer()
        # Same flight, same fare → same key regardless of offer_id
        assert o1.dedupe_key == o2.dedupe_key

    def test_different_supplier_same_key(self):
        """Two suppliers offering the same flight + fare = duplicate."""
        o1 = make_offer(supplier_id="amadeus", supplier_offer_ref="AMX1")
        o2 = make_offer(supplier_id="sabre", supplier_offer_ref="SAB1")
        assert o1.dedupe_key == o2.dedupe_key

    def test_different_fare_basis_different_key(self):
        o1 = make_offer(fare_basis="YOWCHIN")
        o2 = make_offer(fare_basis="SOWCHIN")
        assert o1.dedupe_key != o2.dedupe_key

    def test_different_departure_time_different_key(self):
        seg_late = Segment(
            marketing_carrier=AI,
            flight_number="AI102",
            origin="DEL",
            destination="BOM",
            departure_at=datetime(2025, 6, 1, 14, 0, tzinfo=UTC),
            arrival_at=datetime(2025, 6, 1, 16, 30, tzinfo=UTC),
            cabin_class=CabinClass.ECONOMY,
            booking_class="Y",
            duration_minutes=150,
        )
        o1 = make_offer()
        o2 = make_offer(slices=[Slice(segments=[seg_late])])
        assert o1.dedupe_key != o2.dedupe_key

    def test_key_is_16_chars(self):
        assert len(make_offer().dedupe_key) == 16

    def test_different_cabin_different_key(self):
        seg_biz = Segment(
            marketing_carrier=AI,
            flight_number="AI101",
            origin="DEL",
            destination="BOM",
            departure_at=T0,
            arrival_at=T1,
            cabin_class=CabinClass.BUSINESS,
            booking_class="C",
            duration_minutes=150,
        )
        o1 = make_offer(cabin_class=CabinClass.ECONOMY)
        o2 = make_offer(
            cabin_class=CabinClass.BUSINESS,
            slices=[Slice(segments=[seg_biz])],
        )
        assert o1.dedupe_key != o2.dedupe_key
