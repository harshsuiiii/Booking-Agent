from datetime import date, time
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator

from booking_agent.domain.common import CabinClass

MAX_LEGS = 6


class PaxCounts(BaseModel):
    adults: int = Field(default=1, ge=1, le=9)
    children: int = Field(default=0, ge=0, le=9)
    infants: int = Field(default=0, ge=0, le=9)

    @model_validator(mode="after")
    def infants_le_adults(self) -> "PaxCounts":
        if self.infants > self.adults:
            raise ValueError("infants cannot exceed adults (lap infant rule)")
        if self.total > 9:
            raise ValueError("at most 9 passengers per search")
        return self

    @property
    def total(self) -> int:
        return self.adults + self.children + self.infants


class DepartureWindow(BaseModel):
    earliest: time
    latest: time


class TimeWindow(str, Enum):
    """Departure time of day, in the departure airport's local time."""

    ANY = "any"
    EARLY_MORNING = "early_morning"  # 00:00-05:59
    MORNING = "morning"  # 06:00-11:59
    AFTERNOON = "afternoon"  # 12:00-17:59
    EVENING = "evening"  # 18:00-23:59

    @property
    def bounds(self) -> tuple[time, time] | None:
        """Inclusive (earliest, latest) departure time, or None for any time."""
        return _WINDOW_BOUNDS.get(self)


_WINDOW_BOUNDS = {
    TimeWindow.EARLY_MORNING: (time(0, 0), time(5, 59)),
    TimeWindow.MORNING: (time(6, 0), time(11, 59)),
    TimeWindow.AFTERNOON: (time(12, 0), time(17, 59)),
    TimeWindow.EVENING: (time(18, 0), time(23, 59)),
}


class JourneyType(str, Enum):
    ONE_WAY = "one_way"
    RETURN = "return"
    MULTI_CITY = "multi_city"


class SearchLeg(BaseModel):
    """One flight of the journey: outbound, return, or a multi-city leg."""

    origin: str = Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    destination: str = Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    departure_date: date
    time_window: TimeWindow = TimeWindow.ANY

    @model_validator(mode="after")
    def different_airports(self) -> "SearchLeg":
        if self.origin == self.destination:
            raise ValueError(f"origin and destination are the same ({self.origin})")
        return self


class SearchFilters(BaseModel):
    max_stops: int | None = Field(default=None, ge=0)  # per slice
    no_overnight_layover: bool = False
    bags_included: bool = False
    refundable_only: bool = False
    preferred_airlines: list[str] = Field(default_factory=list)
    departure_window: DepartureWindow | None = None  # first slice only
    max_layover_minutes: int | None = Field(default=None, gt=0)


def legs_from_input(data: Any) -> Any:
    """
    Accept either `legs` or the flat origin/destination/departure_date/return_date form.

    With legs, the flat fields are derived from them (first leg; return_date only for a
    return journey), so code that reads query.origin etc. keeps working.
    """
    if not isinstance(data, dict) or not data.get("legs"):
        return data
    legs = [SearchLeg.model_validate(leg) for leg in data["legs"]]
    journey = data.get("journey_type")
    if journey is None:
        journey = _infer_journey(legs)
    journey = JourneyType(journey)
    first = legs[0]
    return {
        **data,
        "legs": legs,
        "journey_type": journey,
        "origin": first.origin,
        "destination": first.destination,
        "departure_date": first.departure_date,
        "return_date": legs[1].departure_date if journey == JourneyType.RETURN else None,
    }


def _infer_journey(legs: list[SearchLeg]) -> JourneyType:
    if len(legs) == 1:
        return JourneyType.ONE_WAY
    if len(legs) == 2 and _is_round_trip(legs[0], legs[1]):
        return JourneyType.RETURN
    return JourneyType.MULTI_CITY


def _is_round_trip(out: SearchLeg, back: SearchLeg) -> bool:
    return out.origin == back.destination and out.destination == back.origin


def check_journey(journey: JourneyType, legs: list[SearchLeg], today: date | None = None) -> None:
    """Raise ValueError if the legs don't fit the journey type or the dates are out of order."""
    if not legs:
        raise ValueError("at least one leg is required")
    if len(legs) > MAX_LEGS:
        raise ValueError(f"at most {MAX_LEGS} legs")
    if journey == JourneyType.ONE_WAY and len(legs) != 1:
        raise ValueError("a one-way journey has exactly one leg")
    if journey == JourneyType.RETURN and (len(legs) != 2 or not _is_round_trip(legs[0], legs[1])):
        raise ValueError("a return journey is two legs: there and back")
    if journey == JourneyType.MULTI_CITY and len(legs) < 2:
        raise ValueError("a multi-city journey needs at least two legs")
    if legs[0].departure_date < (today or date.today()):
        raise ValueError("departure_date must not be in the past")
    for i, (prev, nxt) in enumerate(zip(legs, legs[1:], strict=False), start=2):
        if nxt.departure_date < prev.departure_date:
            what = "return_date" if journey == JourneyType.RETURN else f"leg {i} date"
            raise ValueError(f"{what} must not be before the previous flight")


class SearchQuery(BaseModel):
    origin: str = Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    destination: str = Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    departure_date: date
    return_date: date | None = None  # None = one-way (or multi-city: see legs)
    pax_counts: PaxCounts = Field(default_factory=PaxCounts)
    cabin: CabinClass = CabinClass.ECONOMY
    filters: SearchFilters = Field(default_factory=SearchFilters)
    # Filled from the flat fields when not given; the source of truth for suppliers.
    journey_type: JourneyType | None = None
    legs: list[SearchLeg] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def from_legs(cls, data: Any) -> Any:
        return legs_from_input(data)

    @model_validator(mode="after")
    def validate_journey(self) -> "SearchQuery":
        if not self.legs:
            self.legs = [
                SearchLeg(
                    origin=self.origin,
                    destination=self.destination,
                    departure_date=self.departure_date,
                )
            ]
            if self.return_date is not None:
                self.legs.append(
                    SearchLeg(
                        origin=self.destination,
                        destination=self.origin,
                        departure_date=self.return_date,
                    )
                )
        if self.journey_type is None:
            self.journey_type = _infer_journey(self.legs)
        check_journey(self.journey_type, self.legs)
        return self
