"""
DuffelConnector: production connector for Duffel Flights API v2.

v1 constraints:
- Test tokens only (duffel_test_*); validated at __init__ time (DuffelTokenRefused).
- Instant orders with Duffel Balance payment; no hold orders.
- Booking is disabled by default; set BOOKING_LIVE_ENABLED=true to enable test bookings.
  A non-test token can never book (BookingDisabled), whatever the flag says.

Settings: DUFFEL_ACCESS_TOKEN, DUFFEL_API_BASE_URL, DUFFEL_API_VERSION,
DUFFEL_SUPPLIER_TIMEOUT_MS (how long Duffel waits for airlines; keep it under our
per-supplier timeout so Duffel answers with whatever airlines responded in time).
"""

from __future__ import annotations

import contextlib
import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import structlog

from booking_agent.connectors.base import BaseConnector, RecorderHook
from booking_agent.connectors.duffel.client import build_duffel_headers, raise_for_duffel_error
from booking_agent.connectors.duffel.mapper import map_offer, map_priced_offer
from booking_agent.connectors.duffel_fx import FxProvider
from booking_agent.connectors.errors import BookingDisabled, PriceChanged, SupplierBadResponse
from booking_agent.connectors.payment import PaymentInstruction
from booking_agent.connectors.protocol import SupplierCapabilities
from booking_agent.connectors.secrets import SecretsProvider
from booking_agent.domain.booking import (
    PAX_TYPE_ORDER,
    Booking,
    BookingState,
    Passenger,
    PassengerType,
)
from booking_agent.domain.offer import Offer, PricedOffer
from booking_agent.domain.search import SearchQuery

_PRICE_CHANGE_THRESHOLD = Decimal("0.01")
_TEST_PREFIX = "duffel_test_"

log = structlog.get_logger(__name__)


class DuffelTokenRefused(ValueError):
    """The configured token is not a test-mode token. Never includes the token itself."""


class DuffelConnector(BaseConnector):
    SUPPLIER_ID = "duffel"

    def __init__(
        self,
        secrets: SecretsProvider,
        fx_provider: FxProvider,
        base_url: str | None = None,
        timeout_seconds: float = 8.0,
        supplier_timeout_ms: int | None = None,
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
        token = secrets.get("DUFFEL_ACCESS_TOKEN")
        if not token.startswith(_TEST_PREFIX):
            raise DuffelTokenRefused(
                "DUFFEL_ACCESS_TOKEN is not a test-mode token (duffel_test_...). "
                "Only Duffel test mode is supported in v1."
            )
        self._token = token
        self._base_url = (
            base_url or os.getenv("DUFFEL_API_BASE_URL") or "https://api.duffel.com"
        ).rstrip("/")
        self._api_version = "v2"
        with contextlib.suppress(KeyError):
            self._api_version = secrets.get("DUFFEL_API_VERSION")
        self._supplier_timeout_ms = supplier_timeout_ms or int(
            os.getenv("DUFFEL_SUPPLIER_TIMEOUT_MS", "5000")
        )
        self._fx = fx_provider

    @property
    def test_mode(self) -> bool:
        return self._token.startswith(_TEST_PREFIX)

    def _json_data(self, response: Any, operation: str) -> Any:
        """response.json()["data"], or SupplierBadResponse if the body isn't that shape."""
        try:
            return response.json()["data"]
        except (json.JSONDecodeError, ValueError, KeyError, TypeError) as exc:
            raise SupplierBadResponse(
                self.SUPPLIER_ID, response.status_code, "malformed response body", operation
            ) from exc

    @property
    def capabilities(self) -> SupplierCapabilities:
        return SupplierCapabilities(
            supplier_id=self.SUPPLIER_ID,
            supports_ndc=True,
            supports_ticketing=True,
            supports_hold=False,
            supports_round_trip=True,
            supports_multi_city=True,
            max_pax=9,
            currencies=["GBP", "EUR", "USD", "INR"],
        )

    # ── search ─────────────────────────────────────────────────────────────

    async def _do_search(self, query: SearchQuery) -> list[Offer]:
        assert self._client is not None
        payload = self._build_search_payload(query)
        response = await self._request(
            "POST",
            f"{self._base_url}/air/offer_requests",
            "search",
            headers=build_duffel_headers(self._token, self._api_version),
            params={"return_offers": "true", "supplier_timeout": self._supplier_timeout_ms},
            json=payload,
        )
        raise_for_duffel_error(response, self.SUPPLIER_ID, "search")

        data = self._json_data(response, "search")
        raw_offers = data.get("offers", []) if isinstance(data, dict) else None
        if not isinstance(raw_offers, list):
            raise SupplierBadResponse(
                self.SUPPLIER_ID, response.status_code, "offers missing", "search"
            )
        if not raw_offers:
            return []

        # All offers share the same currency; fetch FX rate once per search
        currency = raw_offers[0].get("total_currency", "INR")
        if currency == "INR":
            fx_rate, fx_ts = Decimal("1"), datetime.now(UTC)
        else:
            fx_rate, fx_ts = await self._fx.get_rate_to_inr(currency, self._client)

        searched_at = datetime.now(UTC)
        offers: list[Offer] = []
        skipped = 0
        for raw in raw_offers:
            try:
                offers.append(map_offer(raw, searched_at, fx_rate, fx_ts, self.test_mode))
            except (KeyError, ValueError, TypeError):
                skipped += 1  # one odd offer shouldn't sink the whole search
        if skipped:
            log.warning("duffel.offers_skipped", skipped=skipped, mapped=len(offers))
        return offers

    def _build_search_payload(self, query: SearchQuery) -> dict[str, Any]:
        # One slice per leg (one way, return, or multi-city). The time of day is also
        # sent to Duffel to narrow results; the orchestrator filters again in code.
        slices: list[dict[str, Any]] = []
        for leg in query.legs:
            item: dict[str, Any] = {
                "origin": leg.origin,
                "destination": leg.destination,
                "departure_date": leg.departure_date.isoformat(),
            }
            bounds = leg.time_window.bounds
            if bounds is not None:
                item["departure_time"] = {
                    "from": bounds[0].strftime("%H:%M"),
                    "to": bounds[1].strftime("%H:%M"),
                }
            slices.append(item)

        passengers: list[dict[str, Any]] = [
            {"type": "adult"} for _ in range(query.pax_counts.adults)
        ]
        passengers += [{"type": "child"} for _ in range(query.pax_counts.children)]
        passengers += [{"type": "infant_without_seat"} for _ in range(query.pax_counts.infants)]

        return {
            "data": {
                "slices": slices,
                "passengers": passengers,
                "cabin_class": query.cabin.value.lower(),
            }
        }

    # ── price ──────────────────────────────────────────────────────────────

    async def _do_price(self, offer: Offer) -> PricedOffer:
        assert self._client is not None
        response = await self._request(
            "GET",
            f"{self._base_url}/air/offers/{offer.supplier_offer_ref}",
            "price",
            headers=build_duffel_headers(self._token, self._api_version),
        )
        raise_for_duffel_error(response, self.SUPPLIER_ID, "price")

        raw = self._json_data(response, "price")
        currency = raw.get("total_currency", "INR")
        if currency == "INR":
            fx_rate, fx_ts = Decimal("1"), datetime.now(UTC)
        else:
            fx_rate, fx_ts = await self._fx.get_rate_to_inr(currency, self._client)

        new_total_orig = Decimal(raw["total_amount"])

        # Compare in original currency when available to avoid FX-drift false positives
        if offer.fx_audit is not None:
            if abs(new_total_orig - offer.fx_audit.original_amount) > _PRICE_CHANGE_THRESHOLD:
                new_inr = (new_total_orig * fx_rate).quantize(Decimal("0.01"))
                raise PriceChanged(
                    supplier_id=self.SUPPLIER_ID,
                    offer_ref=offer.supplier_offer_ref,
                    old_price=offer.total_inr,
                    new_price=new_inr,
                )
        else:
            if abs(new_total_orig - offer.total_inr) > _PRICE_CHANGE_THRESHOLD:
                raise PriceChanged(
                    supplier_id=self.SUPPLIER_ID,
                    offer_ref=offer.supplier_offer_ref,
                    old_price=offer.total_inr,
                    new_price=new_total_orig,
                )

        priced_at = datetime.now(UTC)
        return map_priced_offer(raw, priced_at, fx_rate, fx_ts, self.test_mode)

    # ── book ───────────────────────────────────────────────────────────────

    async def _do_book(
        self,
        priced_offer: PricedOffer,
        passengers: list[Passenger],
        idempotency_key: str,
        payment: PaymentInstruction,
    ) -> Booking:
        if not self.test_mode:
            raise BookingDisabled(self.SUPPLIER_ID, "only test-mode (duffel_test_) bookings")
        live_enabled = os.getenv("BOOKING_LIVE_ENABLED", "").lower() == "true"
        if not live_enabled:
            raise BookingDisabled(
                self.SUPPLIER_ID,
                "set BOOKING_LIVE_ENABLED=true to enable test bookings",
            )

        duffel_passengers = self._map_passengers(passengers, priced_offer.supplier_passenger_ids)
        duffel_payment = self._map_payment(payment, priced_offer)

        payload: dict[str, Any] = {
            "data": {
                "type": "instant",
                "selected_offers": [priced_offer.supplier_offer_ref],
                "passengers": duffel_passengers,
                "payments": [duffel_payment],
            }
        }

        response = await self._request(
            "POST",
            f"{self._base_url}/air/orders",
            "book",
            headers={
                **build_duffel_headers(self._token, self._api_version),
                "Idempotency-Key": idempotency_key,
            },
            json=payload,
        )
        raise_for_duffel_error(response, self.SUPPLIER_ID, "book")

        order = self._json_data(response, "book")
        return Booking(
            idempotency_key=idempotency_key,
            priced_offer=priced_offer,
            passengers=passengers,
            state=BookingState.PNR_CREATED,
            pnr=order.get("booking_reference"),
            supplier_booking_ref=order["id"],
        )

    # ── helpers ────────────────────────────────────────────────────────────

    @staticmethod
    def _map_payment(payment: PaymentInstruction, offer: PricedOffer) -> dict[str, Any]:
        if payment.payment_type == "balance":
            # Duffel is paid the airline's amount in the airline's currency, never our
            # INR conversion or agency markup.
            audit = offer.fx_audit
            return {
                "type": "balance",
                "amount": str(audit.original_amount if audit else offer.price.net_total),
                "currency": audit.original_currency if audit else offer.price.currency,
            }
        raise ValueError(f"Unsupported payment type in v1: {payment.payment_type!r}")

    @staticmethod
    def _map_passengers(
        passengers: list[Passenger],
        supplier_ids: list[str],
    ) -> list[dict[str, Any]]:
        if len(passengers) != len(supplier_ids):
            raise ValueError(
                f"Passenger count mismatch: {len(passengers)} passengers "
                f"vs {len(supplier_ids)} supplier IDs on offer"
            )
        # supplier_ids are adults, then children, then infants (see the mapper).
        ordered = sorted(passengers, key=lambda p: PAX_TYPE_ORDER.index(p.pax_type))
        result: list[dict[str, Any]] = []
        for pax, sup_id in zip(ordered, supplier_ids, strict=True):
            pax_dict: dict[str, Any] = {
                "id": sup_id,
                "title": (pax.title or "mr").lower(),
                "given_name": pax.given_name,
                "family_name": pax.family_name,
                "born_on": pax.date_of_birth.isoformat(),
                # Placeholder contact; Step 11 collects real contact info via checkout
                "phone_number": "+919999999999",
                "email": "booking@example.com",
                "identity_documents": [],
            }
            if pax.document:
                pax_dict["identity_documents"] = [
                    {
                        "type": pax.document.doc_type.value.lower().replace("_", "-"),
                        "unique_identifier": pax.document.number,
                        "issuing_country_code": pax.document.issuing_country,
                        "expires_on": pax.document.expiry_date.isoformat(),
                        "nationality": pax.document.nationality,
                    }
                ]
            result.append(pax_dict)
        # Each infant travels on an adult's lap: Duffel links them on the adult.
        adults = [
            d for d, p in zip(result, ordered, strict=True) if p.pax_type == PassengerType.ADULT
        ]
        infants = [
            d for d, p in zip(result, ordered, strict=True) if p.pax_type == PassengerType.INFANT
        ]
        for adult, infant in zip(adults, infants, strict=False):
            adult["infant_passenger_id"] = infant["id"]
        return result
