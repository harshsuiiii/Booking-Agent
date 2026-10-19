"""
Amadeus Self-Service connector.

Implements search / price / book against:
  - Flight Offers Search  GET /v2/shopping/flight-offers
  - Flight Offers Price   POST /v1/shopping/flight-offers/pricing
  - Flight Create Orders  POST /v1/booking/flight-orders

Self-service limitations (as of 2024):
  - book() creates a PNR/hold but CANNOT issue e-tickets (enterprise only).
  - NDC content, LCC sources, and margins are enterprise-only.
  - Test base URL returns a reduced content subset; use major routes.
  - Rate limit: 10 TPS test / 40 TPS production. Handled by circuit breaker in Step 5.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from booking_agent.connectors.amadeus.auth import TokenManager
from booking_agent.connectors.amadeus.mapper import (
    decode_offer_ref,
    map_offer,
    map_priced_offer,
)
from booking_agent.connectors.base import BaseConnector, RecorderHook
from booking_agent.connectors.errors import (
    FareExpired,
    PriceChanged,
    SupplierAuthError,
    SupplierBadResponse,
    SupplierRateLimited,
)
from booking_agent.connectors.payment import PaymentInstruction
from booking_agent.connectors.protocol import SupplierCapabilities
from booking_agent.connectors.secrets import SecretsProvider
from booking_agent.domain.booking import Booking, BookingState, Passenger
from booking_agent.domain.offer import Offer, PricedOffer
from booking_agent.domain.search import SearchQuery

_PRICE_CHANGE_THRESHOLD = Decimal("0.01")


class AmadeusConnector(BaseConnector):
    SUPPLIER_ID = "amadeus"

    def __init__(
        self,
        secrets: SecretsProvider,
        base_url: str = "https://test.api.amadeus.com",
        timeout_seconds: float = 8.0,
        max_retries: int = 3,
        retry_min_wait: float = 1.0,
        retry_max_wait: float = 10.0,
        recorder: RecorderHook | None = None,
    ) -> None:
        super().__init__(
            secrets=secrets,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            retry_min_wait=retry_min_wait,
            retry_max_wait=retry_max_wait,
            recorder=recorder,
        )
        self._base_url = base_url.rstrip("/")
        self._token_manager: TokenManager | None = None

    @property
    def capabilities(self) -> SupplierCapabilities:
        return SupplierCapabilities(
            supplier_id=self.SUPPLIER_ID,
            supports_ndc=False,
            supports_ticketing=False,  # enterprise only
            supports_hold=True,
            supports_round_trip=True,
            supports_multi_city=True,
            max_pax=9,
            currencies=["INR", "USD", "EUR", "GBP"],
        )

    # ── lifecycle ──────────────────────────────────────────────────────────

    async def __aenter__(self) -> AmadeusConnector:
        await super().__aenter__()
        assert self._client is not None
        self._token_manager = TokenManager(
            base_url=self._base_url,
            client_id=self._secrets.get("AMADEUS_CLIENT_ID"),
            client_secret=self._secrets.get("AMADEUS_CLIENT_SECRET"),
            client=self._client,
        )
        return self

    async def __aexit__(self, *args: Any) -> None:
        self._token_manager = None
        await super().__aexit__(*args)

    # ── search ─────────────────────────────────────────────────────────────

    async def _do_search(self, query: SearchQuery) -> list[Offer]:
        assert self._token_manager is not None
        token = await self._token_manager.get_token()
        params = self._build_search_params(query)
        response = await self._request(
            "GET",
            f"{self._base_url}/v2/shopping/flight-offers",
            "search",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
        )
        self._raise_for_supplier_error(response, "search")
        body = response.json()
        searched_at = datetime.now(UTC)
        return [map_offer(raw, searched_at) for raw in body.get("data", [])]

    def _build_search_params(self, query: SearchQuery) -> dict[str, Any]:
        params: dict[str, Any] = {
            "originLocationCode": query.origin,
            "destinationLocationCode": query.destination,
            "departureDate": query.departure_date.isoformat(),
            "adults": query.pax_counts.adults,
            "currencyCode": "INR",
            "max": 50,
        }
        if query.pax_counts.children:
            params["children"] = query.pax_counts.children
        if query.pax_counts.infants:
            params["infants"] = query.pax_counts.infants
        if query.return_date:
            params["returnDate"] = query.return_date.isoformat()
        if query.cabin != query.cabin.ECONOMY:
            params["travelClass"] = query.cabin.value
        if query.filters.max_stops == 0:
            params["nonStop"] = "true"
        if query.filters.preferred_airlines:
            params["includedAirlineCodes"] = ",".join(query.filters.preferred_airlines)
        return params

    # ── price ──────────────────────────────────────────────────────────────

    async def _do_price(self, offer: Offer) -> PricedOffer:
        assert self._token_manager is not None
        token = await self._token_manager.get_token()
        raw_offer = decode_offer_ref(offer.supplier_offer_ref)
        body: dict[str, Any] = {
            "data": {
                "type": "flight-offer-pricing",
                "flightOffers": [raw_offer],
            }
        }
        response = await self._request(
            "POST",
            f"{self._base_url}/v1/shopping/flight-offers/pricing",
            "price",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            params={"include": "detailed-fare-rules,bags"},
            json=body,
        )
        self._raise_for_supplier_error(response, "price")

        data = response.json()["data"]
        priced_raw = data["flightOffers"][0]
        detailed_rules = data.get("fareRules", {}).get("rules")
        priced_at = datetime.now(UTC)

        priced = map_priced_offer(priced_raw, detailed_rules, priced_at)

        # Detect price drift — raise so the caller can ask the user to confirm
        if abs(priced.total_inr - offer.total_inr) > _PRICE_CHANGE_THRESHOLD:
            raise PriceChanged(
                supplier_id=self.SUPPLIER_ID,
                offer_ref=offer.supplier_offer_ref[:16],
                old_price=offer.total_inr,
                new_price=priced.total_inr,
            )
        return priced

    # ── book ───────────────────────────────────────────────────────────────

    async def _do_book(
        self,
        priced_offer: PricedOffer,
        passengers: list[Passenger],
        idempotency_key: str,
        payment: PaymentInstruction,
    ) -> Booking:
        """
        Creates a PNR via Flight Create Orders.

        IMPORTANT: The self-service tier creates a booking hold but CANNOT issue
        e-tickets. Ticketing requires an enterprise Amadeus account. The returned
        Booking will be in PNR_CREATED state; a separate ticketing step (outside
        self-service scope) is required before the booking is fully confirmed.
        """
        assert self._token_manager is not None
        token = await self._token_manager.get_token()
        raw_offer = decode_offer_ref(priced_offer.supplier_offer_ref)
        travelers = [self._map_passenger(pax) for pax in passengers]
        body: dict[str, Any] = {
            "data": {
                "type": "flight-order",
                "flightOffers": [raw_offer],
                "travelers": travelers,
            }
        }
        response = await self._request(
            "POST",
            f"{self._base_url}/v1/booking/flight-orders",
            "book",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Idempotency-Key": idempotency_key,
            },
            json=body,
        )
        self._raise_for_supplier_error(response, "book")

        order = response.json()["data"]
        pnr = (order.get("associatedRecords") or [{}])[0].get("reference", order["id"])
        return Booking(
            idempotency_key=idempotency_key,
            priced_offer=priced_offer,
            passengers=passengers,
            state=BookingState.PNR_CREATED,
            pnr=pnr,
        )

    # ── helpers ────────────────────────────────────────────────────────────

    def _raise_for_supplier_error(self, response: Any, operation: str) -> None:
        status = response.status_code
        if status == 200 or status == 201:
            return
        if status == 401:
            raise SupplierAuthError(self.SUPPLIER_ID, "token expired or invalid")
        if status == 429:
            retry_after = int(response.headers.get("Retry-After", 0)) or None
            raise SupplierRateLimited(self.SUPPLIER_ID, retry_after=retry_after)
        if status == 404:
            errors = response.json().get("errors") or [{}]
            title = errors[0].get("title", "not found")
            if "fare" in title.lower() or "expired" in title.lower():
                raise FareExpired(self.SUPPLIER_ID, offer_ref=operation)
        raise SupplierBadResponse(
            supplier_id=self.SUPPLIER_ID,
            status_code=status,
            body=response.text[:500],
            operation=operation,
        )

    @staticmethod
    def _map_passenger(pax: Passenger) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": pax.passenger_id,
            "dateOfBirth": pax.date_of_birth.isoformat(),
            "name": {
                "firstName": pax.given_name,
                "lastName": pax.family_name,
            },
            "gender": "MALE",  # placeholder; real impl would have gender on Passenger
            "contact": {},
            "documents": [],
        }
        if pax.document:
            data["documents"] = [
                {
                    "documentType": pax.document.doc_type.value,
                    "number": pax.document.number,
                    "expiryDate": pax.document.expiry_date.isoformat(),
                    "issuanceCountry": pax.document.issuing_country,
                    "nationality": pax.document.nationality,
                    "holder": True,
                }
            ]
        return data
