"""
Hotel domain models. Shares money, price breakdown, and booking state machine
with the flight vertical; keeps hotel-specific types separate.
"""

import hashlib
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from booking_agent.domain.booking import _TRANSITIONS, BookingState, Passenger, StateTransition
from booking_agent.domain.money import FxAudit
from booking_agent.domain.offer import PriceBreakdown


class GuestCounts(BaseModel):
    adults: int = Field(default=1, ge=1, le=6)
    children: int = Field(default=0, ge=0, le=4)

    @property
    def total(self) -> int:
        return self.adults + self.children


class HotelFilters(BaseModel):
    min_star_rating: float | None = Field(default=None, ge=1.0, le=5.0)
    max_distance_km: float | None = Field(default=None, gt=0)
    amenities_required: list[str] = Field(default_factory=list)
    cancellation_type: Literal["free", "partial", "non_refundable"] | None = None
    meal_plan: str | None = None


class HotelSearchQuery(BaseModel):
    location: str = Field(min_length=1, max_length=200)
    checkin_date: date
    checkout_date: date
    room_count: int = Field(default=1, ge=1, le=10)
    guest_counts: GuestCounts = Field(default_factory=GuestCounts)
    filters: HotelFilters = Field(default_factory=HotelFilters)

    @model_validator(mode="after")
    def validate_dates(self) -> "HotelSearchQuery":
        today = date.today()
        if self.checkin_date < today:
            raise ValueError("checkin_date must not be in the past")
        if self.checkout_date <= self.checkin_date:
            raise ValueError("checkout_date must be after checkin_date")
        return self

    @property
    def nights(self) -> int:
        return (self.checkout_date - self.checkin_date).days


class CancellationPolicy(BaseModel):
    """
    - free: full refund before free_cancellation_before
    - partial: penalty_inr applies
    - non_refundable: no refund
    - unknown: supplier did not disclose terms
    """

    type: Literal["free", "partial", "non_refundable", "unknown"]
    free_cancellation_before: datetime | None = None
    penalty_inr: Decimal | None = None


class RoomInfo(BaseModel):
    room_type: str
    bed_type: str
    max_occupancy: int = Field(ge=1)
    room_count: int = Field(default=1, ge=1)
    amenities: list[str] = Field(default_factory=list)


class HotelOffer(BaseModel):
    offer_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    supplier_id: str
    supplier_offer_ref: str

    property_id: str
    property_name: str
    star_rating: float = Field(ge=1.0, le=5.0)
    location: str
    address: str | None = None
    latitude: float | None = None
    longitude: float | None = None

    checkin_date: date
    checkout_date: date
    nights: int = Field(ge=1)
    room: RoomInfo

    price: PriceBreakdown
    total_inr: Decimal
    fx_audit: FxAudit | None = None

    cancellation_policy: CancellationPolicy
    meal_plan: str | None = None

    expires_at: datetime
    searched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    is_test_data: bool = False

    @property
    def dedupe_key(self) -> str:
        """
        Cross-supplier fingerprint: property_id + room_type + meal_plan + dates.
        v1 limitation: relies on suppliers using a common property_id reference.
        """
        meal = self.meal_plan or ""
        raw = f"{self.property_id}:{self.room.room_type}:{meal}:{self.checkin_date}:{self.checkout_date}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


class PricedHotelOffer(HotelOffer):
    priced_at: datetime
    valid_until: datetime


class HotelBooking(BaseModel):
    """
    Hotel booking record. Reuses flight BookingState/StateTransition/Passenger
    but wraps PricedHotelOffer instead of PricedOffer.
    """

    booking_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    tenant_id: str = ""
    idempotency_key: str
    priced_offer: PricedHotelOffer
    guests: list[Passenger] = Field(min_length=1)
    state: BookingState = BookingState.DRAFT
    confirmation_number: str | None = None
    supplier_booking_ref: str | None = None
    payment_intent_id: str | None = None
    error_detail: str | None = None
    confirmation_token_hash: str | None = None
    confirmation_token_expires_at: datetime | None = None
    transitions: list[StateTransition] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def transition_to(self, new_state: BookingState, actor: str) -> "HotelBooking":
        allowed = _TRANSITIONS.get(self.state, set())
        if new_state not in allowed:
            raise ValueError(
                f"Illegal transition {self.state.value} -> {new_state.value}. "
                f"Allowed: {[s.value for s in allowed]}"
            )
        t = StateTransition(
            from_state=self.state,
            to_state=new_state,
            occurred_at=datetime.now(UTC),
            actor=actor,
        )
        return self.model_copy(
            update={
                "state": new_state,
                "transitions": [*self.transitions, t],
                "updated_at": datetime.now(UTC),
            }
        )
