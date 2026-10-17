"""
Pure mapping functions: Amadeus API dicts → canonical domain models.

All functions are side-effect-free and independently testable.
"""

from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from booking_agent.domain._types import UNKNOWN
from booking_agent.domain.common import CabinClass, Carrier
from booking_agent.domain.itinerary import Segment, Slice
from booking_agent.domain.money import Money
from booking_agent.domain.offer import (
    BaggageAllowance,
    FareRulePolicy,
    FareRules,
    Offer,
    PricedOffer,
    PriceBreakdown,
    TaxLine,
)

# YQ/YR are fuel surcharge codes; all others go into taxes[]
_FUEL_SURCHARGE_CODES = {"YQ", "YR"}

_CABIN_MAP: dict[str, CabinClass] = {
    "ECONOMY": CabinClass.ECONOMY,
    "PREMIUM_ECONOMY": CabinClass.PREMIUM_ECONOMY,
    "BUSINESS": CabinClass.BUSINESS,
    "FIRST": CabinClass.FIRST,
}


# ── helpers ──────────────────────────────────────────────────────────────────

def parse_duration_minutes(iso: str) -> int:
    """Convert ISO 8601 duration (PT2H30M, P1DT10H) to total minutes."""
    m = re.fullmatch(
        r"P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso.strip()
    )
    if not m:
        raise ValueError(f"Unparseable ISO 8601 duration: {iso!r}")
    days = int(m.group(1) or 0)
    hours = int(m.group(2) or 0)
    minutes = int(m.group(3) or 0)
    return days * 1440 + hours * 60 + minutes


def parse_amadeus_datetime(s: str) -> datetime:
    """
    Parse Amadeus datetime string.
    Amadeus returns local-airport ISO 8601 without a UTC offset, e.g.
    "2025-09-01T06:00:00".  We attach UTC as an approximation.
    A production system should look up the airport IANA timezone.
    """
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def encode_offer_ref(raw_offer: dict[str, Any]) -> str:
    """
    Encode the full Amadeus FlightOffer JSON as base64.
    Amadeus pricing requires the complete offer object, not just an ID.
    Step 6 will replace this with a Redis-keyed store.
    """
    return base64.b64encode(json.dumps(raw_offer).encode()).decode()


def decode_offer_ref(ref: str) -> dict[str, Any]:
    """Decode a supplier_offer_ref back to the raw Amadeus FlightOffer dict."""
    return json.loads(base64.b64decode(ref.encode()).decode())  # type: ignore[no-any-return]


# ── segment / slice ───────────────────────────────────────────────────────────

def map_segment(seg: dict[str, Any], fare_detail: dict[str, Any]) -> Segment:
    cabin_str = fare_detail.get("cabin", "ECONOMY")
    cabin = _CABIN_MAP.get(cabin_str, CabinClass.ECONOMY)
    operating_code = (seg.get("operating") or {}).get("carrierCode")
    return Segment(
        marketing_carrier=Carrier(iata_code=seg["carrierCode"]),
        operating_carrier=Carrier(iata_code=operating_code) if operating_code else None,
        flight_number=seg["number"],
        origin=seg["departure"]["iataCode"],
        destination=seg["arrival"]["iataCode"],
        departure_at=parse_amadeus_datetime(seg["departure"]["at"]),
        arrival_at=parse_amadeus_datetime(seg["arrival"]["at"]),
        cabin_class=cabin,
        booking_class=fare_detail.get("class", "Y"),
        aircraft_type=(seg.get("aircraft") or {}).get("code"),
        duration_minutes=parse_duration_minutes(seg["duration"]),
        stops=seg.get("numberOfStops", 0),
    )


def map_slice(
    itinerary: dict[str, Any],
    fare_details_by_seg_id: dict[str, dict[str, Any]],
) -> Slice:
    segments = [
        map_segment(seg, fare_details_by_seg_id.get(seg["id"], {}))
        for seg in itinerary["segments"]
    ]
    return Slice(segments=segments)


# ── baggage ───────────────────────────────────────────────────────────────────

def map_baggage(fare_detail: dict[str, Any]) -> BaggageAllowance:
    checked_raw = fare_detail.get("includedCheckedBags")
    if checked_raw is None:
        # Supplier didn't tell us — mark as UNKNOWN, not zero
        return BaggageAllowance(cabin_pieces=1, checked_pieces=UNKNOWN, checked_kg=UNKNOWN)
    qty = checked_raw.get("quantity")
    weight = checked_raw.get("weight")
    weight_unit = checked_raw.get("weightUnit", "KG")
    if weight is not None:
        kg = Decimal(str(weight))
        if weight_unit != "KG":
            kg = (kg * Decimal("0.453592")).quantize(Decimal("0.1"))
    else:
        kg = None
    return BaggageAllowance(
        cabin_pieces=1,
        checked_pieces=qty if qty is not None else UNKNOWN,
        checked_kg=kg,
    )


# ── fare rules ────────────────────────────────────────────────────────────────

def _map_rule_policy(rule: dict[str, Any] | None) -> FareRulePolicy:
    if rule is None:
        return FareRulePolicy()  # all UNKNOWN
    if rule.get("notPermitted"):
        return FareRulePolicy(allowed=False, fee=None)
    penalties = rule.get("penalties", [])
    if not penalties:
        return FareRulePolicy(allowed=True, fee=None)
    first = penalties[0]
    amt = first.get("amount")
    if amt is None:
        return FareRulePolicy(allowed=True, fee=UNKNOWN)
    currency = first.get("currency", "INR")
    return FareRulePolicy(
        allowed=True,
        fee=Money(amount=Decimal(str(amt)), currency=currency),
    )


def map_fare_rules(
    pricing_options: dict[str, Any],
    detailed_rules: list[dict[str, Any]] | None,
) -> FareRules:
    """
    Map FareRules from two sources:
    1. pricingOptions flags (coarse, available from search)
    2. detailed-fare-rules (granular, only from price() with ?include=detailed-fare-rules)
    """
    # Coarse signals from pricingOptions
    refundable_flag = pricing_options.get("refundableFare")
    no_penalty_flag = pricing_options.get("noPenaltyFare")

    if detailed_rules is None:
        # Only coarse info available — map what we know
        refund_allowed: bool | str
        change_allowed: bool | str
        if refundable_flag is True:
            refund_allowed = True
        elif refundable_flag is False:
            refund_allowed = False
        else:
            refund_allowed = UNKNOWN

        if no_penalty_flag is True:
            change_allowed = True
        else:
            change_allowed = UNKNOWN

        return FareRules(
            refund=FareRulePolicy(allowed=refund_allowed, fee=UNKNOWN if refund_allowed is True else None),
            change=FareRulePolicy(allowed=change_allowed),
        )

    # Detailed rules available
    by_category = {r.get("category"): r for r in (detailed_rules or [])}
    return FareRules(
        refund=_map_rule_policy(by_category.get("REFUND")),
        change=_map_rule_policy(by_category.get("EXCHANGE") or by_category.get("REISSUE")),
        cancel=_map_rule_policy(by_category.get("CANCELLATION")),
    )


# ── price breakdown ───────────────────────────────────────────────────────────

def map_price_breakdown(price: dict[str, Any]) -> PriceBreakdown:
    currency = price["currency"]
    base_fare = Decimal(str(price["base"]))

    taxes: list[TaxLine] = []
    fuel_surcharge = Decimal("0")
    for t in price.get("taxes", []):
        amt = Decimal(str(t["amount"]))
        if t["code"] in _FUEL_SURCHARGE_CODES:
            fuel_surcharge += amt
        else:
            taxes.append(TaxLine(code=t["code"], amount=amt))

    # Fees: TICKETING / SUPPLIER go to supplier_fee
    supplier_fee = sum(
        Decimal(str(f["amount"]))
        for f in price.get("fees", [])
        if f.get("type") in ("TICKETING", "SUPPLIER")
    )

    return PriceBreakdown(
        base_fare=base_fare,
        taxes=taxes,
        fuel_surcharge=fuel_surcharge,
        supplier_fee=supplier_fee,
        currency=currency,
    )


# ── offer ─────────────────────────────────────────────────────────────────────

def map_offer(
    raw: dict[str, Any],
    searched_at: datetime,
) -> Offer:
    # Build a lookup from segmentId → fareDetail for the first traveler
    first_traveler = raw["travelerPricings"][0]
    fare_details_by_seg_id = {
        fd["segmentId"]: fd for fd in first_traveler["fareDetailsBySegment"]
    }

    slices = [
        map_slice(itin, fare_details_by_seg_id)
        for itin in raw["itineraries"]
    ]

    # Use the first segment's fareDetail for offer-level fields
    first_fare_detail = first_traveler["fareDetailsBySegment"][0]
    fare_basis = first_fare_detail.get("fareBasis", "")
    fare_family = first_fare_detail.get("brandedFare")
    cabin_str = first_fare_detail.get("cabin", "ECONOMY")
    cabin = _CABIN_MAP.get(cabin_str, CabinClass.ECONOMY)

    baggage = map_baggage(first_fare_detail)
    fare_rules = map_fare_rules(raw.get("pricingOptions", {}), detailed_rules=None)
    price = map_price_breakdown(raw["price"])

    # total_inr: use grandTotal if currency is INR, else total
    # FX conversion to INR is handled at the orchestrator layer (Step 4)
    currency = raw["price"]["currency"]
    total_raw = Decimal(str(raw["price"].get("grandTotal") or raw["price"]["total"]))

    # lastTicketingDateTime (precise) > lastTicketingDate (date only)
    ltd = raw.get("lastTicketingDateTime") or raw.get("lastTicketingDate")
    if ltd:
        expires_at = parse_amadeus_datetime(ltd) if "T" in ltd else \
            datetime.fromisoformat(ltd).replace(hour=23, minute=59, tzinfo=timezone.utc)
    else:
        expires_at = searched_at + timedelta(minutes=30)

    return Offer(
        supplier_id="amadeus",
        supplier_offer_ref=encode_offer_ref(raw),
        slices=slices,
        fare_basis=fare_basis,
        fare_family=fare_family,
        cabin_class=cabin,
        baggage=baggage,
        fare_rules=fare_rules,
        price=price,
        total_inr=total_raw if currency == "INR" else Decimal("0"),  # FX handled upstream
        expires_at=expires_at,
        searched_at=searched_at,
    )


def map_priced_offer(
    raw: dict[str, Any],
    detailed_rules: list[dict[str, Any]] | None,
    priced_at: datetime,
) -> PricedOffer:
    """Map a Flight Offers Price response to PricedOffer."""
    base = map_offer(raw, searched_at=priced_at)
    fare_rules = map_fare_rules(raw.get("pricingOptions", {}), detailed_rules)

    ltd = raw.get("lastTicketingDateTime") or raw.get("lastTicketingDate")
    if ltd:
        valid_until = parse_amadeus_datetime(ltd) if "T" in ltd else \
            datetime.fromisoformat(ltd).replace(hour=23, minute=59, tzinfo=timezone.utc)
    else:
        valid_until = priced_at + timedelta(minutes=15)

    return PricedOffer(
        **{
            **base.model_dump(),
            "fare_rules": fare_rules,
            "priced_at": priced_at,
            "valid_until": valid_until,
        }
    )
