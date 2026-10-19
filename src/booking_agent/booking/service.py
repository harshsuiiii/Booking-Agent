"""
BookingService: gated booking flow.

prepare()  → live re-price, create PRICED booking, return confirmation token
confirm()  → validate token, book supplier, walk state machine to TICKETED
cancel()   → transition to CANCELLED from any pre-payment state

The confirmation token is a one-time secret returned to the caller (UI layer).
It is NEVER stored in plain text and NEVER returned to the agent/LLM.
"""

import hashlib
import logging
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from booking_agent.booking.errors import (
    BookingNotFound,
    IdempotentReplay,
    InvalidToken,
    PriceChangedError,
    TokenExpired,
)
from booking_agent.booking.payment_gateway import PaymentGateway
from booking_agent.booking.repository import (
    BookingQuery,
    BookingRepository,
    BookingSummary,
)
from booking_agent.connectors.errors import PriceChanged
from booking_agent.connectors.payment import DuffelBalancePayment
from booking_agent.connectors.protocol import Connector
from booking_agent.domain.booking import Booking, BookingState, Passenger
from booking_agent.domain.offer import Offer, PricedOffer

log = logging.getLogger(__name__)

# Price increase tolerance before requiring re-confirmation (INR).
_PRICE_TOLERANCE_INR = Decimal("1")


def _customer_price(offer: Offer) -> Decimal:
    """Customer-facing price: supplier net total (INR) plus any agency markup."""
    return offer.total_inr + offer.price.agency_markup


class BookingService:
    def __init__(
        self,
        repository: BookingRepository,
        connectors: dict[str, Connector],
        payment_gateway: PaymentGateway,
        token_ttl_seconds: int = 300,
    ) -> None:
        self._repo = repository
        self._connectors = connectors
        self._gateway = payment_gateway
        self._token_ttl = token_ttl_seconds

    # ── prepare ───────────────────────────────────────────────────────────────

    async def prepare(
        self,
        *,
        tenant_id: str,
        offer: Offer,
        passengers: list[Passenger],
        idempotency_key: str,
        actor: str,
    ) -> tuple[Booking, str]:
        """
        Live re-price the offer and create a PRICED booking.

        Returns (booking, confirmation_token). The token is a single-use secret
        that expires after `token_ttl_seconds`. Pass it to confirm() to proceed.

        Raises:
            IdempotentReplay: a booking with this idempotency key already exists.
            PriceChangedError: the live re-price returned a higher price.
        """
        existing = await self._repo.get_by_idempotency_key(idempotency_key, tenant_id)
        if existing is not None:
            raise IdempotentReplay(existing.booking_id)

        connector = self._connector_for(offer.supplier_id)
        try:
            priced_offer = with_markup_of(offer, await connector.price(offer))
        except PriceChanged as exc:
            # Some suppliers (Duffel) report the change themselves; same customer message.
            markup = offer.price.agency_markup
            raise PriceChangedError(
                old_price=exc.old_price + markup, new_price=exc.new_price + markup
            ) from exc

        before = _customer_price(offer)
        after = _customer_price(priced_offer)
        if after > before + _PRICE_TOLERANCE_INR:
            raise PriceChangedError(old_price=before, new_price=after)

        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        expires_at = datetime.now(UTC) + timedelta(seconds=self._token_ttl)

        booking = Booking(
            tenant_id=tenant_id,
            idempotency_key=idempotency_key,
            priced_offer=priced_offer,
            passengers=passengers,
            state=BookingState.PRICED,
            confirmation_token_hash=token_hash,
            confirmation_token_expires_at=expires_at,
        )
        booking = await self._repo.save(booking)

        log.info(
            "booking.prepared",
            extra={
                "booking_id": booking.booking_id,
                "tenant_id": tenant_id,
                "passenger_count": len(passengers),
                "supplier_id": offer.supplier_id,
            },
        )
        return booking, token

    # ── confirm ───────────────────────────────────────────────────────────────

    async def confirm(
        self,
        *,
        booking_id: str,
        tenant_id: str,
        confirmation_token: str,
        actor: str,
    ) -> Booking:
        """
        Human-initiated confirmation.

        Validates the token, calls connector.book(), and walks the booking through
        CONFIRMED_BY_USER → PNR_CREATED → PAYMENT_PENDING → PAID → TICKETED.

        This method MUST NOT be reachable from agent tool output.
        """
        booking = await self._load(booking_id, tenant_id)

        if booking.state != BookingState.PRICED:
            raise ValueError(f"confirm() requires PRICED state; booking is {booking.state.value}")

        self._validate_token(booking, confirmation_token)

        # ── PRICED → CONFIRMED_BY_USER ────────────────────────────────────────
        booking = booking.transition_to(BookingState.CONFIRMED_BY_USER, actor)
        booking = await self._repo.save(booking)

        # ── call supplier ─────────────────────────────────────────────────────
        connector = self._connector_for(booking.priced_offer.supplier_id)
        try:
            supplier_booking = await connector.book(
                priced_offer=booking.priced_offer,
                passengers=booking.passengers,
                idempotency_key=booking.idempotency_key,
                payment=DuffelBalancePayment(),
            )
        except Exception as exc:
            booking = booking.model_copy(update={"error_detail": str(exc)[:500]})
            booking = booking.transition_to(BookingState.FAILED, "system")
            booking = await self._repo.save(booking)
            log.error(
                "booking.supplier_failed",
                extra={"booking_id": booking_id, "error": str(exc)[:200]},
            )
            raise

        # Merge supplier-assigned fields
        booking = booking.model_copy(
            update={
                "pnr": supplier_booking.pnr or booking.pnr,
                "supplier_booking_ref": supplier_booking.supplier_booking_ref,
            }
        )

        # ── PNR_CREATED ───────────────────────────────────────────────────────
        booking = booking.transition_to(BookingState.PNR_CREATED, "system")
        booking = await self._repo.save(booking)

        # ── payment ───────────────────────────────────────────────────────────
        customer_price = _customer_price(booking.priced_offer)
        payment_result = await self._gateway.create_payment_intent(
            booking_id=booking_id,
            amount=customer_price,
            currency="INR",
            idempotency_key=f"{booking.idempotency_key}:payment",
        )

        booking = booking.model_copy(update={"payment_intent_id": payment_result.payment_id})
        booking = booking.transition_to(BookingState.PAYMENT_PENDING, "system")
        booking = await self._repo.save(booking)

        if payment_result.status == "failed":
            booking = booking.model_copy(update={"error_detail": "Payment gateway returned failed"})
            booking = booking.transition_to(BookingState.FAILED, "system")
            booking = await self._repo.save(booking)
            raise RuntimeError("Payment failed")

        # Capture (for DuffelBalance: no-op; for Razorpay: charge the customer)
        captured = await self._gateway.capture(payment_result.payment_id)
        if captured.status != "captured":
            booking = booking.transition_to(BookingState.FAILED, "system")
            booking = await self._repo.save(booking)
            raise RuntimeError(f"Payment capture failed: {captured.status}")

        booking = booking.transition_to(BookingState.PAID, "system")
        booking = await self._repo.save(booking)

        # ── ticketing ─────────────────────────────────────────────────────────
        # For Duffel instant orders, the supplier books and tickets in one call.
        booking = booking.transition_to(BookingState.TICKETED, "system")
        booking = await self._repo.save(booking)

        log.info(
            "booking.ticketed",
            extra={
                "booking_id": booking_id,
                "pnr": booking.pnr,
                "supplier_booking_ref": booking.supplier_booking_ref,
            },
        )
        return booking

    # ── cancel ────────────────────────────────────────────────────────────────

    async def cancel(
        self,
        *,
        booking_id: str,
        tenant_id: str,
        actor: str,
    ) -> Booking:
        """Cancel a booking. Only valid before payment has been captured (PAID/TICKETED)."""
        booking = await self._load(booking_id, tenant_id)
        booking = booking.transition_to(BookingState.CANCELLED, actor)
        booking = await self._repo.save(booking)
        log.info("booking.cancelled", extra={"booking_id": booking_id, "actor": actor})
        return booking

    # ── expire ────────────────────────────────────────────────────────────────

    async def expire(self, booking_id: str, tenant_id: str) -> Booking:
        """Transition a booking to EXPIRED (called by the PNR time-limit job)."""
        booking = await self._load(booking_id, tenant_id)
        booking = booking.transition_to(BookingState.EXPIRED, "system")
        booking = await self._repo.save(booking)
        return booking

    # ── reads ─────────────────────────────────────────────────────────────────

    async def get(self, booking_id: str, tenant_id: str) -> Booking:
        """Raises BookingNotFound for unknown IDs and for other tenants' bookings."""
        return await self._load(booking_id, tenant_id)

    @property
    def repository(self) -> BookingRepository:
        return self._repo

    async def summaries(self, tenant_id: str) -> list[BookingSummary]:
        return await self._repo.summaries(tenant_id)

    async def query(self, tenant_id: str, query: BookingQuery) -> tuple[list[BookingSummary], int]:
        return await self._repo.query(tenant_id, query)

    async def list_for_tenant(self, tenant_id: str) -> list[Booking]:
        return await self._repo.list_by_tenant(tenant_id)

    # ── private helpers ───────────────────────────────────────────────────────

    async def _load(self, booking_id: str, tenant_id: str) -> Booking:
        booking = await self._repo.get(booking_id)
        if booking is None or booking.tenant_id != tenant_id:
            raise BookingNotFound(booking_id)
        return booking

    def _connector_for(self, supplier_id: str) -> Connector:
        connector = self._connectors.get(supplier_id)
        if connector is None:
            raise KeyError(f"No connector registered for supplier: {supplier_id!r}")
        return connector

    @staticmethod
    def _validate_token(booking: Booking, token: str) -> None:
        if booking.confirmation_token_hash is None:
            raise InvalidToken("Booking has no confirmation token.")
        provided_hash = hashlib.sha256(token.encode()).hexdigest()
        if provided_hash != booking.confirmation_token_hash:
            raise InvalidToken("Confirmation token is invalid.")
        if (
            booking.confirmation_token_expires_at is not None
            and datetime.now(UTC) > booking.confirmation_token_expires_at
        ):
            raise TokenExpired("Confirmation token has expired.")


def with_markup_of(offer: Offer, priced: PricedOffer) -> PricedOffer:
    """Suppliers re-price the net fare only; keep the agency markup the customer was shown."""
    if priced.price.agency_markup == offer.price.agency_markup:
        return priced
    price = priced.price.model_copy(update={"agency_markup": offer.price.agency_markup})
    return priced.model_copy(update={"price": price})
