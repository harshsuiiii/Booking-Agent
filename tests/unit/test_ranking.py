from datetime import datetime, timezone
from decimal import Decimal

import pytest

from booking_agent.domain._types import UNKNOWN
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
from booking_agent.domain.ranking import RankingPreferences, effective_price

AI = Carrier(iata_code="AI")
T0 = datetime(2025, 6, 1, 6, 0, tzinfo=timezone.utc)
T1 = datetime(2025, 6, 1, 8, 30, tzinfo=timezone.utc)
T_EXPIRE = datetime(2025, 6, 1, 7, 0, tzinfo=timezone.utc)
PREFS = RankingPreferences()


def _offer(total_inr: Decimal, baggage: BaggageAllowance, fare_rules: FareRules) -> Offer:
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
    return Offer(
        supplier_id="amadeus",
        supplier_offer_ref="X",
        slices=[Slice(segments=[seg])],
        fare_basis="YOWCHIN",
        cabin_class=CabinClass.ECONOMY,
        baggage=baggage,
        fare_rules=fare_rules,
        price=PriceBreakdown(base_fare=total_inr, currency="INR"),
        total_inr=total_inr,
        expires_at=T_EXPIRE,
    )


def fully_flexible_offer(total_inr: Decimal) -> Offer:
    return _offer(
        total_inr,
        baggage=BaggageAllowance(checked_pieces=1, cabin_pieces=1),
        fare_rules=FareRules(
            refund=FareRulePolicy(allowed=True, fee=None),
            change=FareRulePolicy(allowed=True, fee=None),
        ),
    )


def bare_unknown_offer(total_inr: Decimal) -> Offer:
    return _offer(
        total_inr,
        baggage=BaggageAllowance(),  # all UNKNOWN
        fare_rules=FareRules(),       # all UNKNOWN
    )


class TestEffectivePrice:
    def test_fully_flexible_no_adjustments(self):
        o = fully_flexible_offer(Decimal("3700"))
        eff, bd = effective_price(o, PREFS)
        assert eff == Decimal("3700")
        assert bd.adjustments == []

    def test_unknown_bag_adds_penalty(self):
        o = _offer(
            Decimal("3000"),
            baggage=BaggageAllowance(checked_pieces=UNKNOWN),
            fare_rules=FareRules(
                refund=FareRulePolicy(allowed=True),
                change=FareRulePolicy(allowed=True),
            ),
        )
        eff, bd = effective_price(o, PREFS)
        assert eff == Decimal("3000") + PREFS.unknown_bag_penalty
        assert any("not disclosed" in a.reason.lower() for a in bd.adjustments)

    def test_no_bag_adds_full_penalty(self):
        o = _offer(
            Decimal("3000"),
            baggage=BaggageAllowance(checked_pieces=0),
            fare_rules=FareRules(
                refund=FareRulePolicy(allowed=True),
                change=FareRulePolicy(allowed=True),
            ),
        )
        eff, bd = effective_price(o, PREFS)
        assert eff == Decimal("3000") + PREFS.no_bag_penalty

    def test_none_bag_same_as_zero(self):
        o = _offer(
            Decimal("3000"),
            baggage=BaggageAllowance(checked_pieces=None),
            fare_rules=FareRules(
                refund=FareRulePolicy(allowed=True),
                change=FareRulePolicy(allowed=True),
            ),
        )
        eff, _ = effective_price(o, PREFS)
        assert eff == Decimal("3000") + PREFS.no_bag_penalty

    def test_non_refundable_adds_penalty(self):
        o = _offer(
            Decimal("3000"),
            baggage=BaggageAllowance(checked_pieces=1),
            fare_rules=FareRules(refund=FareRulePolicy(allowed=False)),
        )
        eff, bd = effective_price(o, PREFS)
        # no_bag penalty not applied (1 bag ok), change unknown adds penalty
        assert any("Non-refundable" in a.reason for a in bd.adjustments)

    def test_all_unknown_stacks_all_penalties(self):
        o = bare_unknown_offer(Decimal("2000"))
        eff, bd = effective_price(o, PREFS)
        expected = (
            Decimal("2000")
            + PREFS.unknown_bag_penalty
            + PREFS.unknown_refund_penalty
            + PREFS.unknown_change_penalty
        )
        assert eff == expected
        assert len(bd.adjustments) == 3

    def test_breakdown_sums_correctly(self):
        o = bare_unknown_offer(Decimal("2000"))
        eff, bd = effective_price(o, PREFS)
        total_adj = sum(a.amount for a in bd.adjustments)
        assert bd.base_price + total_adj == bd.effective_price

    def test_deterministic(self):
        o = bare_unknown_offer(Decimal("2000"))
        r1 = effective_price(o, PREFS)
        r2 = effective_price(o, PREFS)
        assert r1[0] == r2[0]
        assert r1[1].adjustments == r2[1].adjustments

    def test_pricier_with_bag_beats_cheaper_without(self):
        """A 500 INR more expensive offer with a bag included should rank better."""
        cheap_no_bag = _offer(
            Decimal("3200"),
            baggage=BaggageAllowance(checked_pieces=0),
            fare_rules=FareRules(
                refund=FareRulePolicy(allowed=True),
                change=FareRulePolicy(allowed=True),
            ),
        )
        pricier_with_bag = _offer(
            Decimal("3700"),
            baggage=BaggageAllowance(checked_pieces=1),
            fare_rules=FareRules(
                refund=FareRulePolicy(allowed=True),
                change=FareRulePolicy(allowed=True),
            ),
        )
        eff_cheap, _ = effective_price(cheap_no_bag, PREFS)
        eff_pricier, _ = effective_price(pricier_with_bag, PREFS)
        assert eff_pricier < eff_cheap

    def test_custom_penalties(self):
        custom = RankingPreferences(no_bag_penalty=Decimal("2000"))
        o = _offer(
            Decimal("3000"),
            baggage=BaggageAllowance(checked_pieces=0),
            fare_rules=FareRules(
                refund=FareRulePolicy(allowed=True),
                change=FareRulePolicy(allowed=True),
            ),
        )
        eff, _ = effective_price(o, custom)
        assert eff == Decimal("5000")
