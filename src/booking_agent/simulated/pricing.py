"""
Simulated fares: distance-based price model, fare families, and per-supplier profiles.

Base economy fare per passenger grows with great-circle distance, then:
  full-service ×1.30 vs low-cost; 1-stop ×0.88; day of week; days before departure;
  seeded noise (±~9%); fare family and cabin multipliers; supplier net factor, fee and
  markup. All money is Decimal and rounded to whole rupees.

Deterministic for (route, date, itinerary, fare family, supplier); the only time-varying
input is "today" (advance-purchase effect), which callers can inject.
"""

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from booking_agent.domain._types import UNKNOWN
from booking_agent.domain.common import CabinClass
from booking_agent.domain.money import Money
from booking_agent.domain.offer import (
    BaggageAllowance,
    FareRulePolicy,
    FareRules,
    PriceBreakdown,
    TaxLine,
)
from booking_agent.simulated.network import Carrier, seeded_rng

D = Decimal


@dataclass(frozen=True)
class FareFamily:
    code: str
    name: str
    multiplier: Decimal
    rbd: str
    checked_dom: tuple[int | None, int | None]  # (pieces, kg); None = not included
    checked_intl: tuple[int | None, int | None]
    change_fee: int | None  # None = free change
    refundable: bool
    refund_fee: int | None  # None = full refund


FAMILIES: dict[str, FareFamily] = {
    f.code: f
    for f in (
        FareFamily("SAVER", "Saver", D("1.00"), "T", (1, 15), (None, None), 3000, False, None),
        FareFamily("FLEXI", "Flexi", D("1.22"), "Q", (1, 15), (1, 20), None, True, 1500),
        FareFamily(
            "LITE", "Economy Lite", D("0.95"), "K", (None, None), (None, None), 3500, False, None
        ),
        FareFamily(
            "CLASSIC", "Economy Classic", D("1.12"), "M", (1, 15), (1, 25), 2500, True, 3500
        ),
        FareFamily("FLEX", "Economy Flex", D("1.38"), "Y", (1, 25), (2, 23), None, True, None),
        FareFamily(
            "PREMIUM", "Premium Economy", D("1.75"), "W", (2, 23), (2, 23), None, True, 2000
        ),
        FareFamily("BUSINESS", "Business", D("3.10"), "J", (2, 32), (2, 32), None, True, 5000),
        FareFamily("FIRST", "First", D("5.20"), "F", (3, 32), (3, 32), None, True, None),
    )
}


def families_for(carrier: Carrier, cabin: CabinClass) -> list[FareFamily]:
    if cabin == CabinClass.ECONOMY:
        codes = ["SAVER", "FLEXI"] if carrier.lcc else ["LITE", "CLASSIC", "FLEX"]
    elif carrier.lcc:
        codes = []  # low-cost carriers sell economy only
    elif cabin == CabinClass.PREMIUM_ECONOMY:
        codes = ["PREMIUM"]
    elif cabin == CabinClass.BUSINESS:
        codes = ["BUSINESS"]
    else:
        codes = ["FIRST"] if carrier.first_class else []
    return [FAMILIES[c] for c in codes]


@dataclass(frozen=True)
class SupplierProfile:
    supplier_id: str
    label: str
    net_factor: Decimal  # applied to the airline fare (negotiated net fares < 1)
    fee_per_pax_dom: Decimal
    fee_per_pax_intl: Decimal
    markup_pct: Decimal  # supplier's own markup, shown as supplier_fee
    unknown_rate: float  # share of offers with baggage/refund rules not disclosed
    availability: float  # share of the network's itineraries this supplier sells
    families: frozenset[str]


ALL_FAMILIES = frozenset(FAMILIES)

PROFILES: dict[str, SupplierProfile] = {
    p.supplier_id: p
    for p in (
        SupplierProfile(
            "sim_gds",
            "Simulated GDS",
            D("1.000"),
            D("150"),
            D("300"),
            D("0"),
            0.03,
            0.9,
            ALL_FAMILIES,
        ),
        SupplierProfile(
            "sim_consolidator",
            "Simulated consolidator",
            D("0.965"),
            D("0"),
            D("0"),
            D("0"),
            0.3,
            0.75,
            frozenset({"SAVER", "LITE", "CLASSIC", "BUSINESS"}),
        ),
        SupplierProfile(
            "sim_ota",
            "Simulated OTA",
            D("1.000"),
            D("0"),
            D("0"),
            D("0.06"),
            0.05,
            0.85,
            ALL_FAMILIES - {"LITE"},
        ),
    )
}

_DOW_FACTOR = {0: "1.05", 1: "0.93", 2: "0.94", 3: "1.00", 4: "1.10", 5: "0.97", 6: "1.12"}


def _advance_factor(days_out: int) -> Decimal:
    if days_out <= 2:
        return D("1.55")
    if days_out <= 6:
        return D("1.30")
    if days_out <= 13:
        return D("1.15")
    if days_out <= 29:
        return D("1.03")
    if days_out <= 59:
        return D("1.00")
    return D("0.93")


def economy_fare_per_pax(
    distance_km: float,
    domestic: bool,
    lcc: bool,
    stops: int,
    departure: date,
    today: date,
    noise_key: tuple[object, ...],
) -> Decimal:
    dist = D(round(distance_km))
    if domestic:
        fare = D("1200") + D("3.1") * dist
    elif distance_km <= 4000:
        fare = D("2500") + D("4.6") * dist
    else:
        fare = D("4000") + D("5.2") * dist
    if not lcc:
        fare *= D("1.30")
    if stops:
        fare *= D("0.88")
    fare *= D(_DOW_FACTOR[departure.weekday()])
    fare *= _advance_factor((departure - today).days)
    noise = seeded_rng("noise", *noise_key).uniform(0.92, 1.10)
    return fare * D(str(round(noise, 3)))


def _rupees(x: Decimal) -> Decimal:
    return x.quantize(D("1"), rounding=ROUND_HALF_UP)


def price_breakdown(
    airline_fare: Decimal, pax: int, domestic: bool, profile: SupplierProfile
) -> PriceBreakdown:
    """Split a total airline fare into base, fuel surcharge, taxes and supplier fee."""
    net = airline_fare * profile.net_factor
    fee_per_pax = profile.fee_per_pax_dom if domestic else profile.fee_per_pax_intl
    return PriceBreakdown(
        base_fare=_rupees(net * D("0.80")),
        fuel_surcharge=_rupees(net * D("0.08")),
        taxes=[
            TaxLine(code="K3", description="GST", amount=_rupees(net * D("0.05"))),
            TaxLine(code="P2", description="Airport fees", amount=_rupees(net * D("0.07"))),
        ],
        supplier_fee=_rupees(fee_per_pax * pax + net * profile.markup_pct),
        currency="INR",
    )


def baggage_and_rules(
    family: FareFamily,
    domestic: bool,
    profile: SupplierProfile,
    key: tuple[object, ...],
) -> tuple[BaggageAllowance, FareRules]:
    rng = seeded_rng("rules", profile.supplier_id, *key)
    pieces, kg = family.checked_dom if domestic else family.checked_intl
    baggage = BaggageAllowance(
        cabin_pieces=1,
        cabin_kg=D("7"),
        checked_pieces=pieces,
        checked_kg=D(kg) if kg is not None else None,
    )
    if rng.random() < profile.unknown_rate:  # supplier didn't disclose baggage
        baggage = BaggageAllowance(cabin_pieces=1, cabin_kg=D("7"))

    def fee(amount: int | None) -> Money | None:
        return Money(amount=D(amount), currency="INR") if amount is not None else None

    change = FareRulePolicy(allowed=True, fee=fee(family.change_fee))
    if family.refundable:
        refund = FareRulePolicy(allowed=True, fee=fee(family.refund_fee))
        cancel = FareRulePolicy(allowed=True, fee=fee(family.refund_fee))
    else:
        refund = FareRulePolicy(allowed=False, fee=None)
        cancel = FareRulePolicy(allowed=False, fee=None)
    if rng.random() < profile.unknown_rate:  # supplier didn't disclose refund rules
        refund = FareRulePolicy(allowed=UNKNOWN, fee=UNKNOWN)
        cancel = FareRulePolicy(allowed=UNKNOWN, fee=UNKNOWN)
    return baggage, FareRules(change=change, cancel=cancel, refund=refund)
