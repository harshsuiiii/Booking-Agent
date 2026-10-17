from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, model_validator

from booking_agent.domain.common import CabinClass, Carrier


class Segment(BaseModel):
    marketing_carrier: Carrier
    operating_carrier: Optional[Carrier] = None  # None = same as marketing
    flight_number: str
    origin: str = Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    destination: str = Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    departure_at: datetime  # must be timezone-aware
    arrival_at: datetime    # must be timezone-aware
    cabin_class: CabinClass
    booking_class: str = Field(max_length=2)  # RBD e.g. "Y", "K", "W"
    aircraft_type: Optional[str] = None
    duration_minutes: int = Field(gt=0)
    stops: int = Field(default=0, ge=0)  # technical stops (not connections)

    @model_validator(mode="after")
    def arrival_after_departure(self) -> "Segment":
        if self.arrival_at <= self.departure_at:
            raise ValueError("arrival_at must be after departure_at")
        return self


class LayoverInfo(BaseModel):
    airport: str = Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    duration_minutes: int = Field(ge=0)


class Slice(BaseModel):
    """One direction of travel (outbound or return). One or more connecting segments."""
    segments: list[Segment] = Field(min_length=1)

    @property
    def origin(self) -> str:
        return self.segments[0].origin

    @property
    def destination(self) -> str:
        return self.segments[-1].destination

    @property
    def departure_at(self) -> datetime:
        return self.segments[0].departure_at

    @property
    def arrival_at(self) -> datetime:
        return self.segments[-1].arrival_at

    @property
    def duration_minutes(self) -> int:
        return sum(s.duration_minutes for s in self.segments)

    @property
    def stops(self) -> int:
        """Connection count (segments - 1), not technical stops within a segment."""
        return len(self.segments) - 1

    @property
    def layovers(self) -> list[LayoverInfo]:
        result: list[LayoverInfo] = []
        for i in range(len(self.segments) - 1):
            gap = int(
                (self.segments[i + 1].departure_at - self.segments[i].arrival_at).total_seconds()
                / 60
            )
            result.append(LayoverInfo(airport=self.segments[i].destination, duration_minutes=gap))
        return result
