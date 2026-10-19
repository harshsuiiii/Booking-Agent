"""
Simulated route network: which real airlines plausibly fly a pair, non-stop or via a hub.

Everything is derived from a seeded RNG keyed on (origin, destination, date), so the same
search always yields the same flights, whichever simulated supplier asks. Suppliers then
differ in which of these flights they sell and at what price (see pricing.py).

This is SIMULATED data for development and demos, never real schedules.
"""

import hashlib
import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from booking_agent.simulated.airports import Airport, distance_km, get_airport

# Indian airports most foreign carriers serve, and the smaller set long-haul
# European/North American carriers serve.
IN_GATEWAYS = frozenset(
    [
        "DEL",
        "BOM",
        "BLR",
        "MAA",
        "HYD",
        "CCU",
        "COK",
        "AMD",
        "TRV",
        "CCJ",
        "GOI",
        "GOX",
        "LKO",
        "JAI",
        "ATQ",
        "PNQ",
    ]
)
IN_METROS = frozenset(["DEL", "BOM", "BLR", "HYD", "MAA"])
# Long-haul stations Air India serves non-stop from its metros.
AI_LONG_HAUL = frozenset(
    [
        "LHR",
        "BHX",
        "FRA",
        "CDG",
        "AMS",
        "VIE",
        "MXP",
        "FCO",
        "ZRH",
        "JFK",
        "EWR",
        "IAD",
        "ORD",
        "SFO",
        "YYZ",
        "YVR",
        "SYD",
        "MEL",
        "NRT",
        "HKG",
    ]
)


@dataclass(frozen=True)
class Carrier:
    code: str
    name: str
    lcc: bool
    hubs: tuple[str, ...]
    regions: frozenset[str]  # regions served non-stop from the hubs (besides home)
    max_range_km: int
    flight_numbers: tuple[int, int]
    indian: bool = False
    night_bank: bool = False  # Gulf/Asian carriers run 01:00-05:00 departures from India
    first_class: bool = False


def _c(
    code: str,
    name: str,
    lcc: bool,
    hubs: str,
    regions: str,
    max_range_km: int,
    numbers: tuple[int, int],
    **kw: bool,
) -> Carrier:
    return Carrier(
        code,
        name,
        lcc,
        tuple(hubs.split()),
        frozenset(regions.split()),
        max_range_km,
        numbers,
        **kw,
    )


CARRIERS: dict[str, Carrier] = {
    c.code: c
    for c in (
        # Indian carriers
        _c(
            "6E",
            "IndiGo",
            True,
            "DEL BOM BLR HYD MAA CCU",
            "IN GULF SASIA SEA",
            5200,
            (100, 2999),
            indian=True,
        ),
        _c(
            "AI",
            "Air India",
            False,
            "DEL BOM BLR HYD MAA CCU",
            "IN GULF SASIA SEA EASIA EUR NAM OCE",
            15500,
            (101, 999),
            indian=True,
        ),
        _c(
            "IX",
            "Air India Express",
            True,
            "COK CCJ TRV BLR DEL BOM MAA",
            "IN GULF SASIA SEA",
            5000,
            (100, 999),
            indian=True,
        ),
        _c("QP", "Akasa Air", True, "BOM BLR DEL AMD", "IN GULF", 4500, (1101, 1999), indian=True),
        _c("SG", "SpiceJet", True, "DEL BOM AMD", "IN GULF", 4500, (101, 999), indian=True),
        # Gulf
        _c(
            "EK",
            "Emirates",
            False,
            "DXB",
            "IN GULF SASIA SEA EASIA EUR NAM OCE AFR",
            14500,
            (500, 999),
            night_bank=True,
            first_class=True,
        ),
        _c(
            "QR",
            "Qatar Airways",
            False,
            "DOH",
            "IN GULF SASIA SEA EASIA EUR NAM OCE AFR",
            14500,
            (500, 999),
            night_bank=True,
            first_class=True,
        ),
        _c(
            "EY",
            "Etihad Airways",
            False,
            "AUH",
            "IN GULF SASIA SEA EASIA EUR NAM OCE AFR",
            14000,
            (200, 499),
            night_bank=True,
            first_class=True,
        ),
        _c("FZ", "flydubai", True, "DXB", "IN GULF SASIA", 4500, (400, 999), night_bank=True),
        _c("G9", "Air Arabia", True, "SHJ", "IN GULF SASIA", 4500, (400, 499), night_bank=True),
        _c("WY", "Oman Air", False, "MCT", "IN GULF EUR", 7000, (200, 299), night_bank=True),
        _c("GF", "Gulf Air", False, "BAH", "IN GULF EUR", 7000, (100, 299), night_bank=True),
        # Asia
        _c(
            "SQ",
            "Singapore Airlines",
            False,
            "SIN",
            "IN SEA EASIA EUR NAM OCE SASIA",
            16000,
            (400, 529),
            night_bank=True,
            first_class=True,
        ),
        _c("TR", "Scoot", True, "SIN", "IN SEA EASIA", 6500, (500, 599), night_bank=True),
        _c(
            "MH",
            "Malaysia Airlines",
            False,
            "KUL",
            "IN SEA EASIA EUR OCE",
            11000,
            (170, 199),
            night_bank=True,
        ),
        _c("AK", "AirAsia", True, "KUL", "IN SEA", 4500, (20, 99), night_bank=True),
        _c(
            "TG",
            "Thai Airways",
            False,
            "BKK",
            "IN SEA EASIA EUR",
            10000,
            (310, 349),
            night_bank=True,
        ),
        _c("UL", "SriLankan Airlines", False, "CMB", "IN SEA GULF EUR", 9000, (120, 199)),
        # Europe and North America
        _c(
            "BA",
            "British Airways",
            False,
            "LHR",
            "IN EUR NAM GULF",
            14000,
            (100, 299),
            first_class=True,
        ),
        _c("VS", "Virgin Atlantic", False, "LHR", "IN NAM", 14000, (300, 399)),
        _c("LH", "Lufthansa", False, "FRA MUC", "IN EUR NAM", 14000, (750, 799), first_class=True),
        _c("AF", "Air France", False, "CDG", "IN EUR NAM", 14000, (210, 229), first_class=True),
        _c("KL", "KLM", False, "AMS", "IN EUR NAM", 14000, (870, 879)),
        _c("LX", "Swiss", False, "ZRH", "IN EUR NAM", 14000, (146, 149)),
        _c(
            "TK",
            "Turkish Airlines",
            False,
            "IST",
            "IN EUR NAM GULF SEA AFR SASIA",
            14000,
            (716, 799),
        ),
        _c("UA", "United Airlines", False, "EWR SFO ORD IAD", "IN EUR NAM", 15000, (48, 99)),
    )
}

# Hubs used to build 1-stop itineraries, with the carrier that connects there.
CONNECTING_HUBS: tuple[tuple[str, str], ...] = (
    ("EK", "DXB"),
    ("QR", "DOH"),
    ("EY", "AUH"),
    ("FZ", "DXB"),
    ("G9", "SHJ"),
    ("SQ", "SIN"),
    ("TR", "SIN"),
    ("MH", "KUL"),
    ("TG", "BKK"),
    ("UL", "CMB"),
    ("TK", "IST"),
    ("LH", "FRA"),
    ("BA", "LHR"),
    ("AF", "CDG"),
    ("KL", "AMS"),
    ("6E", "DEL"),
    ("6E", "BOM"),
    ("6E", "BLR"),
    ("6E", "HYD"),
    ("AI", "DEL"),
    ("AI", "BOM"),
    ("AI", "BLR"),
)

_MAX_DETOUR = 1.45  # (o→hub + hub→d) / (o→d)
_MAX_ONE_STOP = 10
_MIN_LEG_KM = 300  # no connection via a hub next door (e.g. SHJ for DXB)


@dataclass(frozen=True)
class Leg:
    carrier: Carrier
    flight_number: str
    origin: Airport
    destination: Airport
    departure: datetime  # aware, origin local time
    arrival: datetime  # aware, destination local time
    minutes: int
    aircraft: str


@dataclass(frozen=True)
class Itinerary:
    legs: tuple[Leg, ...]

    @property
    def carrier(self) -> Carrier:
        return self.legs[0].carrier

    @property
    def key(self) -> str:
        """Stable identifier, used in supplier_offer_ref to re-find the itinerary."""
        return "-".join(f"{leg.carrier.code}{leg.flight_number}" for leg in self.legs)

    @property
    def stops(self) -> int:
        return len(self.legs) - 1


def seeded_rng(*parts: object) -> random.Random:
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()
    return random.Random(int(digest[:16], 16))


def _serves(carrier: Carrier, hub: Airport, other: Airport) -> bool:
    """Can `carrier` fly non-stop between its hub and `other`?"""
    if other.iata == hub.iata or distance_km(hub, other) > carrier.max_range_km:
        return False
    if carrier.indian:
        if other.is_india:
            return True
        if other.region not in carrier.regions:
            return False
        if other.region in ("EUR", "NAM", "OCE", "EASIA"):
            return hub.iata in IN_METROS and other.iata in AI_LONG_HAUL
        return hub.iata in IN_GATEWAYS or hub.iata in carrier.hubs
    if other.is_india:
        if "IN" not in carrier.regions:
            return False
        long_haul = carrier.code in ("BA", "VS", "LH", "AF", "KL", "LX", "UA")
        return other.iata in (IN_METROS if long_haul else IN_GATEWAYS)
    return other.region in carrier.regions or other.region == hub.region


def _flies_nonstop(carrier: Carrier, a: Airport, b: Airport, rng: random.Random) -> bool:
    if a.is_india and b.is_india:
        if not carrier.indian:
            return False
        trunk = a.iata in IN_GATEWAYS and b.iata in IN_GATEWAYS
        at_hub = a.iata in carrier.hubs or b.iata in carrier.hubs
        if not (trunk or at_hub):
            return False
        share = {"6E": 0.95, "AI": 0.8}.get(carrier.code, 0.4)
        return rng.random() < share
    for hub, other in ((a, b), (b, a)):
        if hub.iata in carrier.hubs and _serves(carrier, hub, other):
            return True
    # Indian carriers also fly from gateways that aren't their hubs (e.g. COK-DXB on 6E).
    if carrier.indian:
        ind, foreign = (a, b) if a.is_india else (b, a)
        if ind.is_india and not foreign.is_india and _serves(carrier, ind, foreign):
            return rng.random() < 0.6
    return False


def _block_minutes(a: Airport, b: Airport) -> int:
    dist = distance_km(a, b)
    minutes = 25 + dist / 820 * 60 + (15 if dist > 3000 else 0)
    return int(round(minutes / 5) * 5)


def _aircraft(carrier: Carrier, minutes: int, rng: random.Random) -> str:
    if minutes <= 330:
        return rng.choice(("A320neo", "A321neo", "737 MAX 8"))
    if carrier.lcc:
        return "A321XLR"
    return rng.choice(("787-9", "777-300ER", "A350-900"))


def _flight_number(carrier: Carrier, a: Airport, b: Airport, idx: int) -> str:
    # Foreign carriers use their India number block only on India flights; elsewhere a
    # wider block, so the two legs of a connection never share a number.
    touches_india = a.is_india or b.is_india
    lo, hi = carrier.flight_numbers if (carrier.indian or touches_india) else (1000, 3999)
    return str(seeded_rng(carrier.code, a.iata, b.iata, idx).randint(lo, hi))


def _departure_minute(carrier: Carrier, origin: Airport, rng: random.Random) -> int:
    """Minutes after local midnight."""
    if carrier.night_bank and origin.is_india and rng.random() < 0.45:
        return rng.randrange(60, 300, 5)  # 01:00-05:00
    return rng.randrange(330, 1410, 5)  # 05:30-23:30


def _leg(
    carrier: Carrier,
    a: Airport,
    b: Airport,
    departure: datetime,
    idx: int,
    rng: random.Random,
) -> Leg:
    minutes = _block_minutes(a, b)
    arrival = (departure + timedelta(minutes=minutes)).astimezone(b.zone)
    return Leg(
        carrier=carrier,
        flight_number=_flight_number(carrier, a, b, idx),
        origin=a,
        destination=b,
        departure=departure,
        arrival=arrival,
        minutes=minutes,
        aircraft=_aircraft(carrier, minutes, rng),
    )


def _local(day: date, minute: int, airport: Airport) -> datetime:
    return datetime.combine(day, time(minute // 60, minute % 60), tzinfo=airport.zone)


def build_itineraries(origin: str, destination: str, day: date) -> list[Itinerary]:
    """All simulated itineraries for one direction on one day, sorted by departure."""
    a, b = get_airport(origin), get_airport(destination)
    if a is None or b is None or a.iata == b.iata:
        return []
    rng = seeded_rng("route", a.iata, b.iata, day.isoformat())
    direct = distance_km(a, b)
    result: list[Itinerary] = []

    # Non-stop flights
    for carrier in CARRIERS.values():
        if not _flies_nonstop(carrier, a, b, rng):
            continue
        busy = a.iata in IN_GATEWAYS and b.iata in IN_GATEWAYS and carrier.code in ("6E", "AI")
        frequencies = rng.randint(2, 4) if busy else rng.randint(1, 2)
        for idx in range(frequencies):
            dep = _local(day, _departure_minute(carrier, a, rng), a)
            result.append(Itinerary((_leg(carrier, a, b, dep, idx, rng),)))

    # 1-stop via real hubs
    one_stop: list[tuple[float, Itinerary]] = []
    for code, hub_code in CONNECTING_HUBS:
        carrier = CARRIERS[code]
        hub = get_airport(hub_code)
        if hub is None or hub.iata in (a.iata, b.iata):
            continue
        legs = (distance_km(a, hub), distance_km(hub, b))
        detour = sum(legs) / max(direct, 1.0)
        if detour > _MAX_DETOUR or min(legs) < _MIN_LEG_KM:
            continue
        if not (_serves(carrier, hub, a) and _serves(carrier, hub, b)):
            continue
        for idx in range(rng.randint(1, 2)):
            itin = _connection(carrier, a, hub, b, day, idx, rng)
            one_stop.append((detour + rng.random() * 0.1, itin))
    one_stop.sort(key=lambda t: t[0])
    result.extend(itin for _, itin in one_stop[:_MAX_ONE_STOP])

    result.sort(key=lambda i: i.legs[0].departure)
    return result


def _connection(
    carrier: Carrier,
    a: Airport,
    hub: Airport,
    b: Airport,
    day: date,
    idx: int,
    rng: random.Random,
) -> Itinerary:
    first_minutes = _block_minutes(a, hub)
    layover: int | None = rng.randrange(60, 365, 5)  # 1-6 h
    dep1 = _local(day, _departure_minute(carrier, a, rng), a)
    if rng.random() < 0.3:
        # Overnight connection: land at the hub late evening, leave early next morning.
        land_local = rng.randrange(21 * 60 + 30, 23 * 60 + 50, 5)
        land = _local(day, land_local, hub)
        overnight_dep = (land - timedelta(minutes=first_minutes)).astimezone(a.zone)
        if overnight_dep.date() == day:  # the trip must still start on the requested day
            dep1, layover = overnight_dep, None

    leg1 = _leg(carrier, a, hub, dep1, idx, rng)
    if layover is None:
        next_morning = leg1.arrival.date() + timedelta(days=1)
        dep2 = _local(next_morning, rng.randrange(4 * 60 + 30, 7 * 60 + 30, 5), hub)
        if dep2 - leg1.arrival < timedelta(hours=1):
            dep2 = leg1.arrival + timedelta(hours=2)
    else:
        dep2 = (leg1.arrival + timedelta(minutes=layover)).astimezone(hub.zone)
    leg2 = _leg(carrier, hub, b, dep2, idx + 10, rng)
    return Itinerary((leg1, leg2))
