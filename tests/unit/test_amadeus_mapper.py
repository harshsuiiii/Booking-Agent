"""
Tests for the Amadeus mapper — pure functions, no network, no mocks needed.
All fixtures are synthetic and clearly marked as such.
"""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from booking_agent.connectors.amadeus.mapper import (
    decode_offer_ref,
    encode_offer_ref,
    map_baggage,
    map_fare_rules,
    map_offer,
    map_price_breakdown,
    map_priced_offer,
    parse_amadeus_datetime,
    parse_duration_minutes,
)
from booking_agent.domain._types import UNKNOWN
from booking_agent.domain.common import CabinClass
from booking_agent.domain.offer import PricedOffer

FIXTURES = Path(__file__).parent.parent / "fixtures" / "amadeus"
SEARCHED_AT = datetime(2025, 9, 1, 3, 0, tzinfo=UTC)


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


# ── helpers ───────────────────────────────────────────────────────────────────


class TestParseDuration:
    def test_hours_and_minutes(self):
        assert parse_duration_minutes("PT2H30M") == 150

    def test_hours_only(self):
        assert parse_duration_minutes("PT3H") == 180

    def test_minutes_only(self):
        assert parse_duration_minutes("PT45M") == 45

    def test_days_hours_minutes(self):
        assert parse_duration_minutes("P1DT2H30M") == 1590  # 1440 + 120 + 30

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            parse_duration_minutes("2H30M")


class TestParseAmadeusDatetime:
    def test_no_timezone_gets_utc(self):
        dt = parse_amadeus_datetime("2025-09-01T06:00:00")
        assert dt.tzinfo == UTC
        assert dt.year == 2025 and dt.month == 9 and dt.day == 1

    def test_with_offset_preserved(self):
        dt = parse_amadeus_datetime("2025-09-01T06:00:00+05:30")
        assert dt.tzinfo is not None

    def test_midnight_crossing(self):
        dt = parse_amadeus_datetime("2025-12-31T23:45:00")
        assert dt.day == 31

    def test_next_day_arrival(self):
        dt = parse_amadeus_datetime("2026-01-01T01:30:00")
        assert dt.year == 2026


class TestEncodeDecodeOfferRef:
    def test_round_trip(self):
        original = {"id": "1", "price": {"total": "3700.00"}}
        encoded = encode_offer_ref(original)
        assert isinstance(encoded, str)
        decoded = decode_offer_ref(encoded)
        assert decoded == original

    def test_opaque_string(self):
        ref = encode_offer_ref({"id": "1"})
        assert "id" not in ref  # should not be plaintext JSON


# ── baggage ───────────────────────────────────────────────────────────────────


class TestMapBaggage:
    def test_one_checked_bag(self):
        fd = {"includedCheckedBags": {"quantity": 1}}
        b = map_baggage(fd)
        assert b.checked_pieces == 1
        assert b.cabin_pieces == 1

    def test_two_checked_bags_with_weight(self):
        fd = {"includedCheckedBags": {"quantity": 2, "weight": 23, "weightUnit": "KG"}}
        b = map_baggage(fd)
        assert b.checked_pieces == 2
        assert b.checked_kg == Decimal("23")

    def test_missing_baggage_is_unknown(self):
        b = map_baggage({})
        assert b.checked_pieces == UNKNOWN
        assert b.checked_kg == UNKNOWN

    def test_pounds_converted_to_kg(self):
        fd = {"includedCheckedBags": {"quantity": 1, "weight": 50, "weightUnit": "LBS"}}
        b = map_baggage(fd)
        assert b.checked_kg is not None and b.checked_kg < Decimal("23")


# ── price breakdown ───────────────────────────────────────────────────────────


class TestMapPriceBreakdown:
    def test_yq_goes_to_fuel_surcharge(self):
        price = {
            "currency": "INR",
            "base": "3000.00",
            "total": "3700.00",
            "taxes": [
                {"amount": "500.00", "code": "YQ"},
                {"amount": "200.00", "code": "IN"},
            ],
            "fees": [],
        }
        pb = map_price_breakdown(price)
        assert pb.fuel_surcharge == Decimal("500")
        assert len(pb.taxes) == 1
        assert pb.taxes[0].code == "IN"

    def test_no_yq_no_fuel_surcharge(self):
        price = {
            "currency": "INR",
            "base": "2000.00",
            "total": "2600.00",
            "taxes": [{"amount": "600.00", "code": "IN"}],
            "fees": [],
        }
        pb = map_price_breakdown(price)
        assert pb.fuel_surcharge == Decimal("0")
        assert len(pb.taxes) == 1

    def test_supplier_fee_from_ticketing_fee(self):
        price = {
            "currency": "INR",
            "base": "3000.00",
            "total": "3900.00",
            "taxes": [],
            "fees": [{"amount": "200.00", "type": "TICKETING"}],
        }
        pb = map_price_breakdown(price)
        assert pb.supplier_fee == Decimal("200")

    def test_decimal_not_float(self):
        price = {"currency": "INR", "base": "3000.00", "total": "3000.00", "taxes": [], "fees": []}
        pb = map_price_breakdown(price)
        assert isinstance(pb.base_fare, Decimal)


# ── fare rules ────────────────────────────────────────────────────────────────


class TestMapFareRules:
    def test_no_detailed_rules_uses_pricing_options(self):
        opts = {"refundableFare": True}
        fr = map_fare_rules(opts, detailed_rules=None)
        assert fr.refund.allowed is True

    def test_refundable_false_from_options(self):
        opts = {"refundableFare": False}
        fr = map_fare_rules(opts, detailed_rules=None)
        assert fr.refund.allowed is False

    def test_unknown_when_option_absent(self):
        fr = map_fare_rules({}, detailed_rules=None)
        assert fr.refund.allowed == UNKNOWN

    def test_detailed_rules_not_permitted(self):
        rules = [{"category": "REFUND", "notPermitted": True}]
        fr = map_fare_rules({}, detailed_rules=rules)
        assert fr.refund.allowed is False

    def test_detailed_rules_with_fee(self):
        rules = [
            {
                "category": "REFUND",
                "notPermitted": False,
                "penalties": [{"amount": "3500.00", "currency": "INR"}],
            },
            {
                "category": "EXCHANGE",
                "notPermitted": False,
                "penalties": [{"amount": "2000.00", "currency": "INR"}],
            },
        ]
        fr = map_fare_rules({}, detailed_rules=rules)
        assert fr.refund.allowed is True
        assert fr.change.allowed is True
        assert fr.refund.fee is not None and fr.refund.fee != UNKNOWN

    def test_no_penalty_means_free_change(self):
        rules = [{"category": "EXCHANGE", "notPermitted": False, "penalties": []}]
        fr = map_fare_rules({}, detailed_rules=rules)
        assert fr.change.allowed is True
        assert fr.change.fee is None


# ── one-way offer mapping ─────────────────────────────────────────────────────


class TestMapOfferOneWay:
    def setup_method(self):
        data = load("search_one_way.json")
        self.offer = map_offer(data["data"][0], SEARCHED_AT)

    def test_supplier_id(self):
        assert self.offer.supplier_id == "amadeus"

    def test_single_slice(self):
        assert len(self.offer.slices) == 1

    def test_segment_origin_destination(self):
        seg = self.offer.slices[0].segments[0]
        assert seg.origin == "DEL"
        assert seg.destination == "BOM"

    def test_segment_carrier(self):
        seg = self.offer.slices[0].segments[0]
        assert seg.marketing_carrier.iata_code == "AI"

    def test_segment_flight_number(self):
        assert self.offer.slices[0].segments[0].flight_number == "101"

    def test_segment_duration(self):
        assert self.offer.slices[0].segments[0].duration_minutes == 150

    def test_segment_cabin(self):
        assert self.offer.slices[0].segments[0].cabin_class == CabinClass.ECONOMY

    def test_fare_basis(self):
        assert self.offer.fare_basis == "YOWCHIN"

    def test_fare_family(self):
        assert self.offer.fare_family == "ECONOMY_SAVER"

    def test_total_inr(self):
        assert self.offer.total_inr == Decimal("3700")

    def test_baggage_one_piece(self):
        assert self.offer.baggage.checked_pieces == 1

    def test_expires_at_set(self):
        assert self.offer.expires_at is not None
        assert self.offer.expires_at.tzinfo is not None

    def test_dedupe_key_stable(self):
        data = load("search_one_way.json")
        offer2 = map_offer(data["data"][0], SEARCHED_AT)
        assert self.offer.dedupe_key == offer2.dedupe_key

    def test_non_refundable_flag_mapped(self):
        # pricingOptions.refundableFare is false in this fixture
        assert self.offer.fare_rules.refund.allowed is False

    def test_searched_at_set(self):
        assert self.offer.searched_at == SEARCHED_AT


# ── round-trip offer mapping ──────────────────────────────────────────────────


class TestMapOfferRoundTrip:
    def setup_method(self):
        data = load("search_round_trip.json")
        self.offer = map_offer(data["data"][0], SEARCHED_AT)

    def test_two_slices(self):
        assert len(self.offer.slices) == 2

    def test_outbound_slice(self):
        assert self.offer.slices[0].origin == "DEL"
        assert self.offer.slices[0].destination == "BOM"

    def test_return_slice(self):
        assert self.offer.slices[1].origin == "BOM"
        assert self.offer.slices[1].destination == "DEL"

    def test_refundable_flag_mapped(self):
        # pricingOptions.refundableFare is true in this fixture
        assert self.offer.fare_rules.refund.allowed is True


# ── connecting flight (2 segments) ───────────────────────────────────────────


class TestMapOfferConnecting:
    def setup_method(self):
        data = load("search_connecting.json")
        self.offer = map_offer(data["data"][0], SEARCHED_AT)

    def test_two_segments_in_one_slice(self):
        assert len(self.offer.slices) == 1
        assert len(self.offer.slices[0].segments) == 2

    def test_slice_origin_destination(self):
        sl = self.offer.slices[0]
        assert sl.origin == "DEL"
        assert sl.destination == "CCU"

    def test_one_layover(self):
        layovers = self.offer.slices[0].layovers
        assert len(layovers) == 1
        assert layovers[0].airport == "BLR"

    def test_total_duration(self):
        assert self.offer.slices[0].duration_minutes == 300  # 150 + 150


# ── missing baggage → UNKNOWN ─────────────────────────────────────────────────


class TestMapOfferNoBaggage:
    def setup_method(self):
        data = load("search_no_baggage.json")
        self.offer = map_offer(data["data"][0], SEARCHED_AT)

    def test_checked_pieces_unknown(self):
        assert self.offer.baggage.checked_pieces == UNKNOWN

    def test_checked_kg_unknown(self):
        assert self.offer.baggage.checked_kg == UNKNOWN


# ── priced offer mapping ──────────────────────────────────────────────────────


class TestMapPricedOffer:
    def setup_method(self):
        data = load("price_response.json")
        priced_at = datetime(2025, 9, 1, 4, 0, tzinfo=UTC)
        raw = data["data"]["flightOffers"][0]
        rules = data["data"]["fareRules"]["rules"]
        self.priced = map_priced_offer(raw, rules, priced_at)

    def test_is_priced_offer_type(self):
        assert isinstance(self.priced, PricedOffer)

    def test_priced_at_set(self):
        assert self.priced.priced_at is not None

    def test_valid_until_set(self):
        assert self.priced.valid_until is not None

    def test_fare_rules_from_detailed(self):
        # Fixture has REFUND with penalty 3500 INR
        assert self.priced.fare_rules.refund.allowed is True
        assert self.priced.fare_rules.change.allowed is True

    def test_refund_fee_amount(self):
        fee = self.priced.fare_rules.refund.fee
        assert fee is not None and fee != UNKNOWN
        from booking_agent.domain.money import Money

        assert isinstance(fee, Money)
        assert fee.amount == Decimal("3500")
