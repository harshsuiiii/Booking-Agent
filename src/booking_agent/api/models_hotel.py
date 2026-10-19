"""Hotel API request/response models."""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from booking_agent.domain.hotel import GuestCounts, HotelFilters, HotelSearchQuery


class HotelSearchRequest(BaseModel):
    location: str = Field(min_length=1, max_length=200)
    checkin_date: date
    checkout_date: date
    room_count: int = Field(default=1, ge=1, le=10)
    guest_counts: GuestCounts = Field(default_factory=GuestCounts)
    filters: HotelFilters = Field(default_factory=HotelFilters)

    @model_validator(mode="after")
    def validate_dates(self) -> "HotelSearchRequest":
        today = date.today()
        if self.checkin_date < today:
            raise ValueError("checkin_date must not be in the past")
        if self.checkout_date <= self.checkin_date:
            raise ValueError("checkout_date must be after checkin_date")
        return self

    def to_query(self) -> HotelSearchQuery:
        return HotelSearchQuery(
            location=self.location,
            checkin_date=self.checkin_date,
            checkout_date=self.checkout_date,
            room_count=self.room_count,
            guest_counts=self.guest_counts,
            filters=self.filters,
        )


class HotelSearchStarted(BaseModel):
    search_id: str
    message: str = "Hotel search started"


class CancellationPolicyOut(BaseModel):
    type: Literal["free", "partial", "non_refundable", "unknown"]
    free_cancellation_before: datetime | None = None
    penalty_inr: str | None = None


class RoomInfoOut(BaseModel):
    room_type: str
    bed_type: str
    max_occupancy: int
    room_count: int
    amenities: list[str]


class HotelOfferOut(BaseModel):
    offer_id: str
    supplier_id: str
    property_id: str
    property_name: str
    star_rating: float
    location: str
    address: str | None = None
    checkin_date: date
    checkout_date: date
    nights: int
    room: RoomInfoOut
    total_inr: str
    cancellation_policy: CancellationPolicyOut
    meal_plan: str | None = None
    expires_at: datetime
    is_test_data: bool


class HotelAlternateSourceOut(BaseModel):
    supplier_id: str
    offer_id: str
    total_inr: str


class HotelDedupeGroupOut(BaseModel):
    dedupe_key: str
    primary: HotelOfferOut
    alternates: list[HotelAlternateSourceOut]


class HotelSearchSnapshot(BaseModel):
    search_id: str
    tenant_id: str
    is_complete: bool
    groups: list[HotelDedupeGroupOut]
    suppliers_succeeded: list[str]
    suppliers_failed: list[str]
    suppliers_timed_out: list[str]
