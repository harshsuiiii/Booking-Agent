"""
Airport reference data (OurAirports, public domain), built by scripts/build_airports.py.

Used by the simulated supplier (coordinates, time zones, regions) and by the
/v1/airports autocomplete endpoint.
"""

import csv
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

_CSV = Path(__file__).parent.parent / "data" / "airports.csv"


@dataclass(frozen=True)
class Airport:
    iata: str
    name: str
    city: str
    country: str
    region: str  # IN, GULF, SASIA, SEA, EASIA, EUR, NAM, OCE, AFR
    lat: float
    lon: float
    tz: str

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    @property
    def is_india(self) -> bool:
        return self.country == "IN"


@lru_cache(maxsize=1)
def airports() -> dict[str, Airport]:
    with _CSV.open(encoding="utf-8") as f:
        return {
            r["iata"]: Airport(
                iata=r["iata"],
                name=r["name"],
                city=r["city"],
                country=r["country"],
                region=r["region"],
                lat=float(r["lat"]),
                lon=float(r["lon"]),
                tz=r["tz"],
            )
            for r in csv.DictReader(f)
        }


def get_airport(iata: str) -> Airport | None:
    return airports().get(iata.upper())


def search_airports(query: str, limit: int = 10) -> list[Airport]:
    """Match by IATA code first, then city, then airport name (case-insensitive)."""
    q = query.strip().lower()
    if not q:
        return []
    ranked: list[tuple[int, str, Airport]] = []
    for a in airports().values():
        if a.iata.lower() == q:
            score = 0
        elif a.iata.lower().startswith(q):
            score = 1
        elif a.city.lower().startswith(q):
            score = 2
        elif q in a.city.lower():
            score = 3
        elif q in a.name.lower():
            score = 4
        else:
            continue
        ranked.append((score, a.city, a))
    ranked.sort(key=lambda t: (t[0], t[1]))
    return [a for _, _, a in ranked[:limit]]


def distance_km(a: Airport, b: Airport) -> float:
    """Great-circle distance (haversine)."""
    r = 6371.0
    p1, p2 = math.radians(a.lat), math.radians(b.lat)
    dp = p2 - p1
    dl = math.radians(b.lon - a.lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))
