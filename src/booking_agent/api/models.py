"""
Pydantic schemas for API request/response bodies.
Distinct from domain models so the API surface can evolve independently.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from pydantic import BaseModel, Field

from booking_agent.domain.common import CabinClass
from booking_agent.domain.search import PaxCounts, SearchFilters, SearchQuery


# ── requests ──────────────────────────────────────────────────────────────────

class SearchRequest(BaseModel):
    origin: str = Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    destination: str = Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    departure_date: date
    return_date: Optional[date] = None
    pax_counts: PaxCounts = Field(default_factory=PaxCounts)
    cabin: CabinClass = CabinClass.ECONOMY
    filters: SearchFilters = Field(default_factory=SearchFilters)

    def to_query(self) -> SearchQuery:
        return SearchQuery(
            origin=self.origin,
            destination=self.destination,
            departure_date=self.departure_date,
            return_date=self.return_date,
            pax_counts=self.pax_counts,
            cabin=self.cabin,
            filters=self.filters,
        )


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
    fare_family: Optional[str]
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
    groups: list[DedupeGroupOut]
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
