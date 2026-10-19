from enum import Enum

from pydantic import BaseModel, Field


class CabinClass(str, Enum):
    ECONOMY = "ECONOMY"
    PREMIUM_ECONOMY = "PREMIUM_ECONOMY"
    BUSINESS = "BUSINESS"
    FIRST = "FIRST"


class Airport(BaseModel):
    iata_code: str = Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    name: str | None = None
    city: str | None = None
    country: str | None = None  # ISO 3166-1 alpha-2


class Carrier(BaseModel):
    iata_code: str = Field(min_length=2, max_length=3)
    name: str | None = None
