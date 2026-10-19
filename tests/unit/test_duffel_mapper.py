"""
Tests for the Duffel mapper — pure functions, no network calls.
All fixtures are synthetic and clearly marked as such.
"""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from booking_agent.connectors.duffel.mapper import (
    map_conditions,
    map_offer,
    map_priced_offer,
    parse_iso_duration,
)
from booking_agent.domain._types import UNKNOWN
from booking_agent.domain.common import CabinClass
from booking_agent.domain.offer import Offer, PricedOffer

FIXTURES = Path(__file__).parent.parent / "fixtures" / "duffel"
SEARCHED_AT = datetime(2025, 1, 10, 11, 0, tzinfo=UTC)
FX_RATE = Decimal("1")
FX_TS = datetime(2025, 1, 10, 11, 0, tzinfo=UTC)


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


# ── parse_iso_duration ────────────────────────────────────────────────────────


class TestParseDuration:
    def test_hours_and_minutes(self):
        assert parse_iso_duration("PT2H25M") == 145

    def test_hours_only(self):
        assert parse_iso_duration("PT3H") == 180

    def test_minutes_only(self):
        assert parse_iso_duration("PT45M") == 45

    def test_days_hours_minutes(self):
        assert parse_iso_duration("P1DT2H30M") == 1590  # 1440+120+30

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            parse_iso_duration("2H30M")


# ── map_offer: one-way INR offer ──────────────────────────────────────────────


class TestMapOfferOneWay:
    def _load_offer(self) -> dict:
        return load("offer_request_response.json")["data"]["offers"][0]

    def test_returns_offer_instance(self):
        raw = self._load_offer()
        offer = map_offer(raw, SEARCHED_AT, FX_RATE, FX_TS)
        assert isinstance(offer, Offer)

    def test_supplier_id_is_duffel(self):
        raw = self._load_offer()
        offer = map_offer(raw, SEARCHED_AT, FX_RATE, FX_TS)
        assert offer.supplier_id == "duffel"

    def test_supplier_offer_ref_matches_id(self):
        raw = self._load_offer()
        offer = map_offer(raw, SEARCHED_AT, FX_RATE, FX_TS)
        assert offer.supplier_offer_ref == "off_0000AuzFnPbflPXiD0QJME"

    def test_total_inr_correct(self):
        raw = self._load_offer()
        offer = map_offer(raw, SEARCHED_AT, FX_RATE, FX_TS)
        assert offer.total_inr == Decimal("5240.00")

    def test_no_fx_audit_for_inr(self):
        raw = self._load_offer()
        offer = map_offer(raw, SEARCHED_AT, FX_RATE, FX_TS)
        assert offer.fx_audit is None

    def test_origin_destination(self):
        raw = self._load_offer()
        offer = map_offer(raw, SEARCHED_AT, FX_RATE, FX_TS)
        assert offer.slices[0].origin == "BOM"
        assert offer.slices[0].destination == "DEL"

    def test_segment_departure_is_aware(self):
        raw = self._load_offer()
        offer = map_offer(raw, SEARCHED_AT, FX_RATE, FX_TS)
        dep = offer.slices[0].segments[0].departure_at
        assert dep.tzinfo is not None

    def test_duration_minutes(self):
        raw = self._load_offer()
        offer = map_offer(raw, SEARCHED_AT, FX_RATE, FX_TS)
        assert offer.slices[0].segments[0].duration_minutes == 145

    def test_cabin_class_economy(self):
        raw = self._load_offer()
        offer = map_offer(raw, SEARCHED_AT, FX_RATE, FX_TS)
        assert offer.cabin_class == CabinClass.ECONOMY

    def test_fare_family(self):
        raw = self._load_offer()
        offer = map_offer(raw, SEARCHED_AT, FX_RATE, FX_TS)
        assert offer.fare_family == "IndiGo Saver"

    def test_fare_basis_from_segment_passenger(self):
        raw = self._load_offer()
        offer = map_offer(raw, SEARCHED_AT, FX_RATE, FX_TS)
        assert offer.fare_basis == "YOWFLEX"

    def test_baggage_known(self):
        raw = self._load_offer()
        offer = map_offer(raw, SEARCHED_AT, FX_RATE, FX_TS)
        assert offer.baggage.cabin_pieces == 1
        assert offer.baggage.checked_pieces == 1

    def test_change_allowed(self):
        raw = self._load_offer()
        offer = map_offer(raw, SEARCHED_AT, FX_RATE, FX_TS)
        assert offer.fare_rules.change.allowed is True

    def test_refund_not_allowed(self):
        raw = self._load_offer()
        offer = map_offer(raw, SEARCHED_AT, FX_RATE, FX_TS)
        assert offer.fare_rules.refund.allowed is False

    def test_not_test_data_for_real_airline(self):
        raw = self._load_offer()
        offer = map_offer(raw, SEARCHED_AT, FX_RATE, FX_TS)
        assert offer.is_test_data is False

    def test_supplier_passenger_ids_populated(self):
        raw = self._load_offer()
        offer = map_offer(raw, SEARCHED_AT, FX_RATE, FX_TS)
        assert offer.supplier_passenger_ids == ["pas_0000AuzFnPbflPXiD0QJMH"]


# ── map_offer: ZZ test airline ────────────────────────────────────────────────


class TestMapOfferZZ:
    def _load_offer(self) -> dict:
        return load("offer_request_response_zz.json")["data"]["offers"][0]

    def test_is_test_data_true(self):
        raw = self._load_offer()
        offer = map_offer(raw, SEARCHED_AT, fx_rate=Decimal("105"), fx_timestamp=FX_TS)
        assert offer.is_test_data is True

    def test_fx_audit_populated_for_gbp(self):
        raw = self._load_offer()
        fx_rate = Decimal("105.50")
        offer = map_offer(raw, SEARCHED_AT, fx_rate=fx_rate, fx_timestamp=FX_TS)
        assert offer.fx_audit is not None
        assert offer.fx_audit.original_currency == "GBP"
        assert offer.fx_audit.fx_rate == fx_rate

    def test_total_inr_converted_from_gbp(self):
        raw = self._load_offer()
        fx_rate = Decimal("105")
        offer = map_offer(raw, SEARCHED_AT, fx_rate=fx_rate, fx_timestamp=FX_TS)
        expected = (Decimal("400.00") * fx_rate).quantize(Decimal("0.01"))
        assert offer.total_inr == expected


# ── map_offer: no baggage ─────────────────────────────────────────────────────


class TestMapOfferNoBaggage:
    def test_baggage_unknown_when_empty(self):
        raw = load("offer_request_response_no_baggage.json")["data"]["offers"][0]
        offer = map_offer(raw, SEARCHED_AT, FX_RATE, FX_TS)
        assert offer.baggage.checked_pieces is UNKNOWN
        assert offer.baggage.cabin_pieces is UNKNOWN

    def test_fare_rules_unknown_when_no_conditions(self):
        raw = load("offer_request_response_no_baggage.json")["data"]["offers"][0]
        offer = map_offer(raw, SEARCHED_AT, FX_RATE, FX_TS)
        assert offer.fare_rules.change.allowed is UNKNOWN
        assert offer.fare_rules.refund.allowed is UNKNOWN


# ── map_priced_offer ──────────────────────────────────────────────────────────


class TestMapPricedOffer:
    def test_returns_priced_offer_type(self):
        raw = load("offer_response.json")["data"]
        priced_at = datetime(2025, 1, 10, 11, 5, tzinfo=UTC)
        po = map_priced_offer(raw, priced_at, FX_RATE, FX_TS)
        assert isinstance(po, PricedOffer)

    def test_priced_at_set(self):
        raw = load("offer_response.json")["data"]
        priced_at = datetime(2025, 1, 10, 11, 5, tzinfo=UTC)
        po = map_priced_offer(raw, priced_at, FX_RATE, FX_TS)
        assert po.priced_at == priced_at

    def test_valid_until_matches_expires_at(self):
        raw = load("offer_response.json")["data"]
        priced_at = datetime(2025, 1, 10, 11, 5, tzinfo=UTC)
        po = map_priced_offer(raw, priced_at, FX_RATE, FX_TS)
        assert po.valid_until == po.expires_at


# ── map_conditions ────────────────────────────────────────────────────────────


class TestMapConditions:
    def test_none_conditions_all_unknown(self):
        rules = map_conditions(None)
        assert rules.change.allowed is UNKNOWN
        assert rules.refund.allowed is UNKNOWN
        assert rules.cancel.allowed is UNKNOWN

    def test_change_allowed_with_fee(self):
        rules = map_conditions(
            {
                "change_before_departure": {
                    "allowed": True,
                    "penalty_amount": "2000.00",
                    "penalty_currency": "INR",
                },
                "refund_before_departure": {
                    "allowed": False,
                    "penalty_amount": None,
                    "penalty_currency": None,
                },
            }
        )
        assert rules.change.allowed is True
        assert rules.change.fee is not None
        assert rules.refund.allowed is False
        assert rules.refund.fee is None

    def test_cancel_mirrors_refund(self):
        rules = map_conditions(
            {
                "change_before_departure": {
                    "allowed": True,
                    "penalty_amount": None,
                    "penalty_currency": None,
                },
                "refund_before_departure": {
                    "allowed": True,
                    "penalty_amount": None,
                    "penalty_currency": None,
                },
            }
        )
        assert rules.cancel.allowed == rules.refund.allowed
