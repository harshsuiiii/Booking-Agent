"""
Airport lookup for UI autocomplete.

GET /v1/airports?q=del&limit=8 → matches by IATA code, then city, then airport name.
"""

from fastapi import APIRouter, Query
from pydantic import BaseModel

from booking_agent.simulated.airports import search_airports

router = APIRouter(prefix="/v1/airports", tags=["airports"])


class AirportOut(BaseModel):
    iata: str
    name: str
    city: str
    country: str


@router.get("", response_model=list[AirportOut])
async def find_airports(
    q: str = Query(min_length=1, max_length=50),
    limit: int = Query(default=8, ge=1, le=20),
) -> list[AirportOut]:
    return [
        AirportOut(iata=a.iata, name=a.name, city=a.city, country=a.country)
        for a in search_airports(q, limit)
    ]
