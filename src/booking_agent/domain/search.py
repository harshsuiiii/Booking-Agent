from datetime import date, time
from typing import Optional

from pydantic import BaseModel, Field, model_validator

from booking_agent.domain.common import CabinClass


class PaxCounts(BaseModel):
    adults: int = Field(default=1, ge=1, le=9)
    children: int = Field(default=0, ge=0, le=9)
    infants: int = Field(default=0, ge=0, le=9)

    @model_validator(mode="after")
    def infants_le_adults(self) -> "PaxCounts":
        if self.infants > self.adults:
            raise ValueError("infants cannot exceed adults (lap infant rule)")
        return self

    @property
    def total(self) -> int:
        return self.adults + self.children + self.infants


class DepartureWindow(BaseModel):
    earliest: time
    latest: time


class SearchFilters(BaseModel):
    max_stops: Optional[int] = Field(default=None, ge=0)
    no_overnight_layover: bool = False
    bags_included: bool = False
    refundable_only: bool = False
    preferred_airlines: list[str] = Field(default_factory=list)
    departure_window: Optional[DepartureWindow] = None
    max_layover_minutes: Optional[int] = Field(default=None, gt=0)


class SearchQuery(BaseModel):
    origin: str = Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    destination: str = Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    departure_date: date
    return_date: Optional[date] = None  # None = one-way
    pax_counts: PaxCounts = Field(default_factory=PaxCounts)
    cabin: CabinClass = CabinClass.ECONOMY
    filters: SearchFilters = Field(default_factory=SearchFilters)

    @model_validator(mode="after")
    def return_after_departure(self) -> "SearchQuery":
        if self.return_date is not None and self.return_date <= self.departure_date:
            raise ValueError("return_date must be after departure_date")
        return self
