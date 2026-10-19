"""
Pure mapper: Duffel API v2 JSON → domain objects.
No network calls; all inputs are plain dicts from already-parsed JSON.

- Segment departing_at/arriving_at are airport-local wall-clock times without an offset
  (verified against a live test-mode response); the airport's IANA time_zone gives the zone.
- Prices are converted to INR for the whole breakdown (so markup rules and sell prices are
  INR-consistent); the supplier's original amount/currency/rate live in fx_audit.
- Baggage and fare rules Duffel doesn't state are UNKNOWN, never "none"/"not allowed".
"""

import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from booking_agent.domain._types import UNKNOWN
from booking_agent.domain.common import CabinClass, Carrier
from booking_agent.domain.itinerary import Segment, Slice
from booking_agent.domain.money import FxAudit, Money
from booking_agent.domain.offer import (
    BaggageAllowance,
    FareRulePolicy,
    FareRules,
    Offer,
    PriceBreakdown,
    PricedOffer,
    TaxLine,
)
from booking_agent.domain.search import PaxCounts

_CABIN_MAP: dict[str, CabinClass] = {
    "economy": CabinClass.ECONOMY,
    "premium_economy": CabinClass.PREMIUM_ECONOMY,
    "business": CabinClass.BUSINESS,
    "first": CabinClass.FIRST,
}

_ISO_DURATION_RE = re.compile(r"^P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?$")


def parse_iso_duration(duration: str) -> int:
    """Convert ISO 8601 duration (PT2H30M, PT45M, P1DT2H) to minutes."""
    m = _ISO_DURATION_RE.match(duration)
    if not m:
        raise ValueError(f"Cannot parse ISO 8601 duration: {duration!r}")
    days = int(m.group(1) or 0)
    hours = int(m.group(2) or 0)
    minutes = int(m.group(3) or 0)
    return days * 1440 + hours * 60 + minutes


def _parse_dt(value: str) -> datetime:
    """Parse a Duffel top-level timestamp (e.g. expires_at, always UTC "Z")."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _airport_zone(airport: dict[str, Any]) -> ZoneInfo:
    """IANA zone for a Duffel airport object; falls back to our airport list, then UTC."""
    candidates = [airport.get("time_zone")]
    if not candidates[0]:
        from booking_agent.simulated.airports import get_airport

        known = get_airport(str(airport.get("iata_code", "")))
        candidates.append(known.tz if known else None)
    for name in candidates:
        if name:
            try:
                return ZoneInfo(name)
            except (ZoneInfoNotFoundError, ValueError):
                continue
    return ZoneInfo("UTC")


def _parse_local(value: str, airport: dict[str, Any]) -> datetime:
    """Segment times are local to the airport and carry no offset."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=_airport_zone(airport))


def _map_cabin(raw: str) -> CabinClass:
    return _CABIN_MAP.get(raw.lower(), CabinClass.ECONOMY)


_PAX_ORDER = {"adult": 0, "child": 1, "infant_without_seat": 2}


def _pax_counts(raw_pax: list[dict[str, Any]]) -> PaxCounts | None:
    """The offer's passenger mix, or None if Duffel didn't type every passenger."""
    types = [p.get("type") for p in raw_pax]
    if not types or any(t not in _PAX_ORDER for t in types):
        return None
    try:
        return PaxCounts(
            adults=types.count("adult"),
            children=types.count("child"),
            infants=types.count("infant_without_seat"),
        )
    except ValueError:
        return None


def map_segment(raw_seg: dict[str, Any], pax_ids: list[str]) -> tuple[Segment, str]:
    """
    Map a Duffel segment dict to (Segment, fare_basis_code).
    fare_basis_code comes from the first matching passenger on this segment.
    """
    carrier_iata = raw_seg["marketing_carrier"]["iata_code"]
    carrier_name = raw_seg["marketing_carrier"].get("name")

    raw_fn = raw_seg.get("marketing_carrier_flight_number", "0")
    # Duffel sometimes returns just the number ("2104"), other times with prefix ("6E2104")
    flight_number = raw_fn.removeprefix(carrier_iata) or raw_fn

    op_raw = raw_seg.get("operating_carrier")
    operating = (
        Carrier(iata_code=op_raw["iata_code"], name=op_raw.get("name"))
        if op_raw and op_raw.get("iata_code") != carrier_iata
        else None
    )

    dep = _parse_local(raw_seg["departing_at"], raw_seg["origin"])
    arr = _parse_local(raw_seg["arriving_at"], raw_seg["destination"])
    duration = parse_iso_duration(raw_seg["duration"])
    aircraft = (raw_seg.get("aircraft") or {}).get("iata_code")

    # Defaults from segment-level cabin_class; overridden by per-passenger if available
    cabin = _map_cabin(raw_seg.get("cabin_class") or "economy")
    fare_basis = ""

    for p in raw_seg.get("passengers", []):
        if p.get("passenger_id") in pax_ids:
            if p.get("cabin_class"):
                cabin = _map_cabin(p["cabin_class"])
            fare_basis = p.get("fare_basis_code") or ""
            break

    # booking_class = first letter of fare basis (RBD approximation)
    booking_class = fare_basis[0].upper() if fare_basis else "Y"

    seg = Segment(
        marketing_carrier=Carrier(iata_code=carrier_iata, name=carrier_name),
        operating_carrier=operating,
        flight_number=flight_number,
        origin=raw_seg["origin"]["iata_code"],
        destination=raw_seg["destination"]["iata_code"],
        departure_at=dep,
        arrival_at=arr,
        cabin_class=cabin,
        booking_class=booking_class,
        aircraft_type=aircraft,
        duration_minutes=duration,
    )
    return seg, fare_basis


def map_baggage(slices: list[dict[str, Any]], pax_ids: list[str]) -> BaggageAllowance:
    """
    Aggregate baggage allowance from all segments; take the minimum (most restrictive).
    Absent or empty baggages list → UNKNOWN (supplier did not disclose).
    """
    cabin_min: int | None = None
    checked_min: int | None = None
    found_pax_baggage = False

    for sl in slices:
        for seg in sl.get("segments", []):
            for p in seg.get("passengers", []):
                if p.get("passenger_id") not in pax_ids:
                    continue
                baggages = p.get("baggages")
                if not baggages:
                    # Passenger entry exists but no baggage data — skip this segment
                    continue
                found_pax_baggage = True
                for bag in baggages:
                    qty = int(bag.get("quantity", 0))
                    if bag["type"] == "carry_on":
                        cabin_min = qty if cabin_min is None else min(cabin_min, qty)
                    elif bag["type"] == "checked":
                        checked_min = qty if checked_min is None else min(checked_min, qty)

    if not found_pax_baggage:
        return BaggageAllowance()  # all UNKNOWN

    return BaggageAllowance(
        cabin_pieces=cabin_min if cabin_min is not None else UNKNOWN,
        cabin_kg=UNKNOWN,
        checked_pieces=checked_min if checked_min is not None else UNKNOWN,
        checked_kg=UNKNOWN,
    )


_FxConvert = tuple[str, Decimal]  # (from currency, rate to INR)


def _map_policy(raw: dict[str, Any] | None, fx: _FxConvert | None = None) -> FareRulePolicy:
    if raw is None:
        return FareRulePolicy()  # UNKNOWN
    allowed = raw.get("allowed")
    if allowed is None:
        return FareRulePolicy()
    penalty = raw.get("penalty_amount")
    if penalty is not None:
        currency = raw.get("penalty_currency") or "INR"
        amount = Decimal(penalty)
        if fx is not None and currency == fx[0]:  # same currency as the fare: show in INR
            amount, currency = (amount * fx[1]).quantize(Decimal("0.01")), "INR"
        fee: Money | None = Money(amount=amount, currency=currency)
    else:
        fee = None
    return FareRulePolicy(allowed=allowed, fee=fee)


def map_conditions(conditions: dict[str, Any] | None, fx: _FxConvert | None = None) -> FareRules:
    """Map Duffel offer conditions to FareRules. Duffel has no separate cancel rule."""
    if not conditions:
        return FareRules()  # all UNKNOWN
    refund_policy = _map_policy(conditions.get("refund_before_departure"), fx)
    return FareRules(
        change=_map_policy(conditions.get("change_before_departure"), fx),
        cancel=refund_policy,  # Duffel merges cancel into refund
        refund=refund_policy,
    )


def map_offer(
    raw: dict[str, Any],
    searched_at: datetime,
    fx_rate: Decimal,
    fx_timestamp: datetime,
    test_mode: bool = False,
) -> Offer:
    """Map a single raw Duffel offer dict to a domain Offer.

    test_mode: offers from a duffel_test_ token are never real fares, whatever the airline.
    """
    # Adults first, then children, then infants: book() pairs our passengers in that order.
    raw_pax = sorted(raw.get("passengers", []), key=lambda p: _PAX_ORDER.get(p.get("type"), 0))
    pax_ids = [p["id"] for p in raw_pax]
    pax_counts = _pax_counts(raw_pax)

    slices: list[Slice] = []
    fare_basis_codes: list[str] = []
    for sl in raw["slices"]:
        segments: list[Segment] = []
        for seg_raw in sl.get("segments", []):
            seg, fb = map_segment(seg_raw, pax_ids)
            segments.append(seg)
            if fb:
                fare_basis_codes.append(fb)
        slices.append(Slice(segments=segments))

    fare_basis = fare_basis_codes[0] if fare_basis_codes else "UNKNOWN"
    baggage = map_baggage(raw["slices"], pax_ids)

    total_orig = Decimal(raw["total_amount"])
    orig_currency = raw["total_currency"]
    base_orig = Decimal(raw["base_amount"])
    tax_orig = Decimal(raw.get("tax_amount") or "0")
    fx = None if orig_currency == "INR" else (orig_currency, fx_rate)
    fare_rules = map_conditions(raw.get("conditions"), fx)

    if orig_currency == "INR":
        total_inr, base_inr, tax_inr = total_orig, base_orig, tax_orig
        fx_audit = None
    else:
        total_inr = (total_orig * fx_rate).quantize(Decimal("0.01"))
        base_inr = (base_orig * fx_rate).quantize(Decimal("0.01"))
        tax_inr = total_inr - base_inr  # so the INR breakdown sums exactly to total_inr
        fx_audit = FxAudit(
            original_amount=total_orig,
            original_currency=orig_currency,
            fx_rate=fx_rate,
            fx_timestamp=fx_timestamp,
            converted_amount=total_inr,
            converted_currency="INR",
        )
    taxes = [TaxLine(code="TAX", description="Taxes & fees", amount=tax_inr)] if tax_inr else []
    price = PriceBreakdown(base_fare=base_inr, taxes=taxes, currency="INR")

    owner_iata = raw.get("owner", {}).get("iata_code", "")
    is_test = test_mode or owner_iata == "ZZ"  # ZZ = Duffel Airways, the sandbox airline

    cabin = _map_cabin(raw.get("cabin_class") or "economy")

    return Offer(
        supplier_id="duffel",
        supplier_offer_ref=raw["id"],
        slices=slices,
        fare_basis=fare_basis,
        fare_family=raw.get("fare_brand_name"),
        cabin_class=cabin,
        baggage=baggage,
        fare_rules=fare_rules,
        price=price,
        total_inr=total_inr,
        fx_audit=fx_audit,
        expires_at=_parse_dt(raw["expires_at"]),
        searched_at=searched_at,
        is_test_data=is_test,
        pax_count=max(1, len(pax_ids)),
        pax_counts=pax_counts,
        supplier_passenger_ids=pax_ids,
    )


def map_priced_offer(
    raw: dict[str, Any],
    priced_at: datetime,
    fx_rate: Decimal,
    fx_timestamp: datetime,
    test_mode: bool = False,
) -> PricedOffer:
    """Wrap a re-priced Duffel offer into a PricedOffer."""
    offer = map_offer(raw, priced_at, fx_rate, fx_timestamp, test_mode)
    return PricedOffer(
        **offer.model_dump(),
        priced_at=priced_at,
        valid_until=offer.expires_at,
    )
