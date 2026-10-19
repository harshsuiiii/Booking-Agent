"""
Pydantic schemas for API request/response bodies.
Distinct from domain models so the API surface can evolve independently.
"""

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator

from booking_agent.domain.common import CabinClass
from booking_agent.domain.search import (
    MAX_LEGS,
    JourneyType,
    PaxCounts,
    SearchFilters,
    SearchLeg,
    SearchQuery,
    legs_from_input,
)

# ── requests ──────────────────────────────────────────────────────────────────


class SearchRequest(BaseModel):
    """
    A flight search. Send `journey_type` + `legs` (the search form), or the flat
    origin/destination/departure_date/return_date form (scripts, older clients).
    """

    journey_type: JourneyType | None = None
    legs: list[SearchLeg] = Field(default_factory=list, max_length=MAX_LEGS)
    origin: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    destination: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    departure_date: date | None = None
    return_date: date | None = None
    pax_counts: PaxCounts = Field(default_factory=PaxCounts)
    cabin: CabinClass = CabinClass.ECONOMY
    filters: SearchFilters = Field(default_factory=SearchFilters)

    @model_validator(mode="before")
    @classmethod
    def from_legs(cls, data: Any) -> Any:
        return legs_from_input(data)

    @model_validator(mode="after")
    def validate_journey(self) -> "SearchRequest":
        if not self.legs and (
            self.origin is None or self.destination is None or self.departure_date is None
        ):
            raise ValueError("send legs, or origin, destination and departure_date")
        self.to_query()  # raises ValueError for an invalid journey
        return self

    def to_query(self) -> SearchQuery:
        return SearchQuery.model_validate(self.model_dump(exclude_none=True))


# ── responses ─────────────────────────────────────────────────────────────────


class SearchStarted(BaseModel):
    search_id: str
    message: str = "Search started"


class AlternateSourceOut(BaseModel):
    supplier_id: str
    offer_id: str
    total_inr: str  # Decimal as string


class OfferOut(BaseModel):
    offer_id: str
    supplier_id: str
    total_inr: str
    fare_basis: str
    fare_family: str | None
    expires_at: datetime
    raw: dict[str, Any]  # full model_dump for the UI to render


class DedupeGroupOut(BaseModel):
    dedupe_key: str
    primary: OfferOut
    alternates: list[AlternateSourceOut]


class SearchSnapshot(BaseModel):
    search_id: str
    tenant_id: str
    is_complete: bool
    groups: list[dict[str, Any]]  # role-specific views (booking_agent.api.views)
    suppliers_succeeded: list[str]
    suppliers_failed: list[str]
    suppliers_timed_out: list[str]


class PricedOfferOut(BaseModel):
    """Re-price response; full priced_offer payload as dict."""

    priced_offer: dict[str, Any]


class PriceChangedOut(BaseModel):
    type: str = "price_changed"
    offer_id: str
    old_price_inr: str
    new_price_inr: str


class HealthOut(BaseModel):
    status: str = "ok"


class ReadyOut(BaseModel):
    status: str
    redis: bool
    db: bool
