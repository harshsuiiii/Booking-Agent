from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from booking_agent.domain.booking import Booking, Passenger
from booking_agent.domain.offer import Offer, PricedOffer
from booking_agent.domain.search import SearchQuery


class SupplierCapabilities(BaseModel):
    supplier_id: str
    supports_ndc: bool = False
    supports_ticketing: bool = False
    supports_hold: bool = False
    supports_round_trip: bool = True
    supports_multi_city: bool = False
    max_pax: int = 9
    currencies: list[str] = Field(default_factory=list)  # ISO codes accepted; empty = any


@runtime_checkable
class Connector(Protocol):
    """
    Structural interface every supplier adapter must satisfy.
    Use as an async context manager to manage the underlying HTTP client lifecycle.
    """

    @property
    def capabilities(self) -> SupplierCapabilities: ...

    async def search(self, query: SearchQuery) -> list[Offer]: ...

    async def price(self, offer: Offer) -> PricedOffer: ...

    async def book(
        self,
        priced_offer: PricedOffer,
        passengers: list[Passenger],
        idempotency_key: str,
    ) -> Booking: ...

    async def __aenter__(self) -> "Connector": ...

    async def __aexit__(self, *args: Any) -> None: ...
