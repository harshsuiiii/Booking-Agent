"""Hotel connector protocol and supplier capabilities."""

from typing import Any, Protocol, runtime_checkable

from booking_agent.connectors.payment import PaymentInstruction
from booking_agent.domain.booking import Passenger
from booking_agent.domain.hotel import (
    HotelBooking,
    HotelOffer,
    HotelSearchQuery,
    PricedHotelOffer,
)


class HotelSupplierCapabilities:
    def __init__(
        self,
        supplier_id: str,
        supports_instant_confirmation: bool = True,
        max_rooms: int = 5,
        meal_plans: list[str] | None = None,
    ) -> None:
        self.supplier_id = supplier_id
        self.supports_instant_confirmation = supports_instant_confirmation
        self.max_rooms = max_rooms
        self.meal_plans = meal_plans or []


@runtime_checkable
class HotelConnector(Protocol):
    supplier_id: str

    @property
    def capabilities(self) -> HotelSupplierCapabilities: ...

    async def search(self, query: HotelSearchQuery) -> list[HotelOffer]: ...

    async def price(self, offer: HotelOffer) -> PricedHotelOffer: ...

    async def book(
        self,
        priced_offer: PricedHotelOffer,
        guests: list[Passenger],
        idempotency_key: str,
        payment: PaymentInstruction,
    ) -> HotelBooking: ...

    async def __aenter__(self) -> "HotelConnector": ...
    async def __aexit__(self, *args: Any) -> None: ...
