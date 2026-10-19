"""
Tests for BookingService: prepare, idempotency, price drift, confirm, cancel,
payment failure, and proof that the agent cannot reach the confirm endpoint.
"""

import hashlib
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from booking_agent.booking.errors import (
    BookingNotFound,
    IdempotentReplay,
    InvalidToken,
    PriceChangedError,
    TokenExpired,
)
from booking_agent.booking.payment_gateway import FakePaymentGateway
from booking_agent.booking.repository import InMemoryBookingRepository
from booking_agent.booking.service import BookingService
from booking_agent.connectors.fake import FakeConnector
from booking_agent.domain.booking import BookingState, Passenger, PassengerType
from booking_agent.domain.common import CabinClass
from booking_agent.domain.itinerary import Segment, Slice
from booking_agent.domain.money import Money
from booking_agent.domain.offer import (
    BaggageAllowance,
    FareRulePolicy,
    FareRules,
    Offer,
    PriceBreakdown,
    TaxLine,
)
from tests.conftest import AI, T0, T1, T_EXPIRE

# ── fixtures ──────────────────────────────────────────────────────────────────


def _make_offer(total_inr: Decimal = Decimal("3500")) -> Offer:
    seg = Segment(
        marketing_carrier=AI,
        flight_number="AI101",
        origin="DEL",
        destination="BOM",
        departure_at=T0,
        arrival_at=T1,
        cabin_class=CabinClass.ECONOMY,
        booking_class="Y",
        duration_minutes=150,
    )
    sl = Slice(segments=[seg])
    price = PriceBreakdown(
        base_fare=Decimal("3000"),
        taxes=[TaxLine(code="YQ", amount=Decimal("500"))],
        fuel_surcharge=Decimal("0"),
        currency="INR",
    )
    return Offer(
        supplier_id="fake",
        supplier_offer_ref="REF1",
        slices=[sl],
        fare_basis="ECON",
        cabin_class=CabinClass.ECONOMY,
        baggage=BaggageAllowance(cabin_pieces=1),
        fare_rules=FareRules(
            refund=FareRulePolicy(allowed=True, fee=Money(amount=Decimal("0"), currency="INR")),
            change=FareRulePolicy(allowed=True, fee=Money(amount=Decimal("0"), currency="INR")),
        ),
        price=price,
        total_inr=total_inr,
        expires_at=T_EXPIRE,
    )


def _pax() -> list[Passenger]:
    return [
        Passenger(
            pax_type=PassengerType.ADULT,
            given_name="Jane",
            family_name="Doe",
            date_of_birth=date(1990, 5, 15),
        )
    ]


def _service(
    price_drift: Decimal = Decimal("0"),
    payment_fail: bool = False,
    token_ttl: int = 300,
) -> tuple[BookingService, FakeConnector, InMemoryBookingRepository, FakePaymentGateway]:
    connector = FakeConnector(
        supplier_id="fake",
        retry_min_wait=0.0,
        retry_max_wait=0.0,
        price_drift=price_drift,
    )
    repo = InMemoryBookingRepository()
    gateway = FakePaymentGateway(fail=payment_fail)
    svc = BookingService(
        repository=repo,
        connectors={"fake": connector},
        payment_gateway=gateway,
        token_ttl_seconds=token_ttl,
    )
    return svc, connector, repo, gateway


# ── prepare ───────────────────────────────────────────────────────────────────


class TestPrepare:
    async def test_returns_booking_and_token(self):
        svc, _, _, _ = _service()
        offer = _make_offer()
        async with FakeConnector(supplier_id="fake", retry_min_wait=0.0, retry_max_wait=0.0):
            booking, token = await svc.prepare(
                tenant_id="t1",
                offer=offer,
                passengers=_pax(),
                idempotency_key="ik-1",
                actor="agent",
            )
        assert booking.state == BookingState.PRICED
        assert booking.tenant_id == "t1"
        assert token  # non-empty string

    async def test_token_is_not_stored_plain(self):
        svc, _, repo, _ = _service()
        offer = _make_offer()
        async with FakeConnector(supplier_id="fake", retry_min_wait=0.0, retry_max_wait=0.0):
            booking, token = await svc.prepare(
                tenant_id="t1",
                offer=offer,
                passengers=_pax(),
                idempotency_key="ik-2",
                actor="agent",
            )
        stored = await repo.get(booking.booking_id)
        assert stored is not None
        # Stored hash must be SHA-256 of the token, not the token itself
        expected_hash = hashlib.sha256(token.encode()).hexdigest()
        assert stored.confirmation_token_hash == expected_hash
        assert stored.confirmation_token_hash != token

    async def test_booking_persisted(self):
        svc, _, repo, _ = _service()
        offer = _make_offer()
        async with FakeConnector(supplier_id="fake", retry_min_wait=0.0, retry_max_wait=0.0):
            booking, _ = await svc.prepare(
                tenant_id="t1",
                offer=offer,
                passengers=_pax(),
                idempotency_key="ik-3",
                actor="agent",
            )
        assert await repo.get(booking.booking_id) is not None

    async def test_token_has_expiry(self):
        svc, _, _, _ = _service(token_ttl=60)
        offer = _make_offer()
        async with FakeConnector(supplier_id="fake", retry_min_wait=0.0, retry_max_wait=0.0):
            booking, _ = await svc.prepare(
                tenant_id="t1",
                offer=offer,
                passengers=_pax(),
                idempotency_key="ik-4",
                actor="agent",
            )
        assert booking.confirmation_token_expires_at is not None
        delta = booking.confirmation_token_expires_at - datetime.now(UTC)
        assert timedelta(seconds=50) < delta < timedelta(seconds=70)


# ── idempotency ───────────────────────────────────────────────────────────────


class TestIdempotency:
    async def test_duplicate_key_raises(self):
        svc, _, _, _ = _service()
        offer = _make_offer()
        async with FakeConnector(supplier_id="fake", retry_min_wait=0.0, retry_max_wait=0.0):
            await svc.prepare(
                tenant_id="t1",
                offer=offer,
                passengers=_pax(),
                idempotency_key="dup-key",
                actor="agent",
            )
            with pytest.raises(IdempotentReplay) as exc_info:
                await svc.prepare(
                    tenant_id="t1",
                    offer=offer,
                    passengers=_pax(),
                    idempotency_key="dup-key",
                    actor="agent",
                )
        assert "dup-key" in str(exc_info.value) or exc_info.value.booking_id

    async def test_different_key_succeeds(self):
        svc, _, repo, _ = _service()
        offer = _make_offer()
        async with FakeConnector(supplier_id="fake", retry_min_wait=0.0, retry_max_wait=0.0):
            b1, _ = await svc.prepare(
                tenant_id="t1",
                offer=offer,
                passengers=_pax(),
                idempotency_key="key-a",
                actor="agent",
            )
            b2, _ = await svc.prepare(
                tenant_id="t1",
                offer=offer,
                passengers=_pax(),
                idempotency_key="key-b",
                actor="agent",
            )
        assert b1.booking_id != b2.booking_id
        assert len(await repo.list_by_tenant("t1")) == 2


# ── price drift ───────────────────────────────────────────────────────────────


class TestPriceDrift:
    async def test_price_increase_raises(self):
        svc, _, _, _ = _service(price_drift=Decimal("200"))
        offer = _make_offer(total_inr=Decimal("3500"))
        async with FakeConnector(
            supplier_id="fake",
            retry_min_wait=0.0,
            retry_max_wait=0.0,
            price_drift=Decimal("200"),
        ):
            with pytest.raises(PriceChangedError) as exc_info:
                await svc.prepare(
                    tenant_id="t1",
                    offer=offer,
                    passengers=_pax(),
                    idempotency_key="pd-1",
                    actor="agent",
                )
        assert exc_info.value.new_price > exc_info.value.old_price

    async def test_no_drift_succeeds(self):
        svc, _, _, _ = _service(price_drift=Decimal("0"))
        offer = _make_offer()
        async with FakeConnector(supplier_id="fake", retry_min_wait=0.0, retry_max_wait=0.0):
            booking, _ = await svc.prepare(
                tenant_id="t1",
                offer=offer,
                passengers=_pax(),
                idempotency_key="pd-2",
                actor="agent",
            )
        assert booking.state == BookingState.PRICED

    async def test_small_drift_within_tolerance_succeeds(self):
        """Drift of exactly 1 INR (the tolerance threshold) should succeed."""
        svc, _, _, _ = _service(price_drift=Decimal("1"))
        offer = _make_offer()
        async with FakeConnector(
            supplier_id="fake",
            retry_min_wait=0.0,
            retry_max_wait=0.0,
            price_drift=Decimal("1"),
        ):
            # price_drift=1 makes live re-price 1 INR higher == tolerance, should pass
            # The tolerance check is: after > before + 1, so equal to tolerance is OK
            booking, _ = await svc.prepare(
                tenant_id="t1",
                offer=offer,
                passengers=_pax(),
                idempotency_key="pd-3",
                actor="agent",
            )
        assert booking.state == BookingState.PRICED


# ── confirm ───────────────────────────────────────────────────────────────────


class TestConfirm:
    async def test_full_confirm_reaches_ticketed(self):
        svc, connector, _, gateway = _service()
        offer = _make_offer()
        async with connector:
            booking, token = await svc.prepare(
                tenant_id="t1",
                offer=offer,
                passengers=_pax(),
                idempotency_key="cf-1",
                actor="agent",
            )
            confirmed = await svc.confirm(
                booking_id=booking.booking_id,
                tenant_id="t1",
                confirmation_token=token,
                actor="user",
            )
        assert confirmed.state == BookingState.TICKETED
        assert confirmed.pnr is not None
        assert confirmed.supplier_booking_ref is not None

    async def test_confirm_records_payment_intent(self):
        svc, connector, _, gateway = _service()
        offer = _make_offer()
        async with connector:
            booking, token = await svc.prepare(
                tenant_id="t1",
                offer=offer,
                passengers=_pax(),
                idempotency_key="cf-2",
                actor="agent",
            )
            confirmed = await svc.confirm(
                booking_id=booking.booking_id,
                tenant_id="t1",
                confirmation_token=token,
                actor="user",
            )
        assert confirmed.payment_intent_id is not None
        assert len(gateway.captures_attempted) == 1

    async def test_wrong_token_raises_invalid_token(self):
        svc, connector, _, _ = _service()
        offer = _make_offer()
        async with connector:
            booking, _ = await svc.prepare(
                tenant_id="t1",
                offer=offer,
                passengers=_pax(),
                idempotency_key="cf-3",
                actor="agent",
            )
            with pytest.raises(InvalidToken):
                await svc.confirm(
                    booking_id=booking.booking_id,
                    tenant_id="t1",
                    confirmation_token="wrong-token",
                    actor="user",
                )

    async def test_expired_token_raises_token_expired(self):
        svc, connector, repo, _ = _service(token_ttl=0)
        offer = _make_offer()
        async with connector:
            booking, token = await svc.prepare(
                tenant_id="t1",
                offer=offer,
                passengers=_pax(),
                idempotency_key="cf-4",
                actor="agent",
            )
            # Force expiry by setting expires_at to the past
            expired_booking = booking.model_copy(
                update={"confirmation_token_expires_at": datetime(2000, 1, 1, tzinfo=UTC)}
            )
            await repo.save(expired_booking)

            with pytest.raises(TokenExpired):
                await svc.confirm(
                    booking_id=booking.booking_id,
                    tenant_id="t1",
                    confirmation_token=token,
                    actor="user",
                )

    async def test_confirm_wrong_state_raises(self):
        svc, connector, _, _ = _service()
        offer = _make_offer()
        async with connector:
            booking, token = await svc.prepare(
                tenant_id="t1",
                offer=offer,
                passengers=_pax(),
                idempotency_key="cf-5",
                actor="agent",
            )
            # Confirm once successfully
            await svc.confirm(
                booking_id=booking.booking_id,
                tenant_id="t1",
                confirmation_token=token,
                actor="user",
            )
            # Attempt to confirm again — booking is now TICKETED
            with pytest.raises(ValueError, match="PRICED"):
                await svc.confirm(
                    booking_id=booking.booking_id,
                    tenant_id="t1",
                    confirmation_token=token,
                    actor="user",
                )

    async def test_booking_not_found_raises(self):
        svc, connector, _, _ = _service()
        async with connector:
            with pytest.raises(BookingNotFound):
                await svc.confirm(
                    booking_id="no-such-id",
                    tenant_id="t1",
                    confirmation_token="anything",
                    actor="user",
                )

    async def test_tenant_mismatch_raises_not_found(self):
        svc, connector, _, _ = _service()
        offer = _make_offer()
        async with connector:
            booking, token = await svc.prepare(
                tenant_id="t1",
                offer=offer,
                passengers=_pax(),
                idempotency_key="cf-6",
                actor="agent",
            )
            with pytest.raises(BookingNotFound):
                await svc.confirm(
                    booking_id=booking.booking_id,
                    tenant_id="t2",  # wrong tenant
                    confirmation_token=token,
                    actor="user",
                )


# ── cancel ────────────────────────────────────────────────────────────────────


class TestCancel:
    async def test_cancel_priced_booking(self):
        svc, connector, _, _ = _service()
        offer = _make_offer()
        async with connector:
            booking, _ = await svc.prepare(
                tenant_id="t1",
                offer=offer,
                passengers=_pax(),
                idempotency_key="cn-1",
                actor="agent",
            )
            cancelled = await svc.cancel(
                booking_id=booking.booking_id,
                tenant_id="t1",
                actor="user",
            )
        assert cancelled.state == BookingState.CANCELLED

    async def test_cancel_updates_repo(self):
        svc, connector, repo, _ = _service()
        offer = _make_offer()
        async with connector:
            booking, _ = await svc.prepare(
                tenant_id="t1",
                offer=offer,
                passengers=_pax(),
                idempotency_key="cn-2",
                actor="agent",
            )
            await svc.cancel(
                booking_id=booking.booking_id,
                tenant_id="t1",
                actor="user",
            )
        stored = await repo.get(booking.booking_id)
        assert stored is not None
        assert stored.state == BookingState.CANCELLED

    async def test_cancel_ticketed_raises(self):
        """TICKETED is terminal — cannot be cancelled."""
        svc, connector, _, _ = _service()
        offer = _make_offer()
        async with connector:
            booking, token = await svc.prepare(
                tenant_id="t1",
                offer=offer,
                passengers=_pax(),
                idempotency_key="cn-3",
                actor="agent",
            )
            await svc.confirm(
                booking_id=booking.booking_id,
                tenant_id="t1",
                confirmation_token=token,
                actor="user",
            )
            with pytest.raises(ValueError, match="Illegal transition"):
                await svc.cancel(
                    booking_id=booking.booking_id,
                    tenant_id="t1",
                    actor="user",
                )


# ── payment failure ───────────────────────────────────────────────────────────


class TestPaymentFailure:
    async def test_payment_intent_failed_transitions_to_failed(self):
        svc, connector, repo, _ = _service(payment_fail=True)
        offer = _make_offer()
        async with connector:
            booking, token = await svc.prepare(
                tenant_id="t1",
                offer=offer,
                passengers=_pax(),
                idempotency_key="pf-1",
                actor="agent",
            )
            with pytest.raises(RuntimeError, match="Payment"):
                await svc.confirm(
                    booking_id=booking.booking_id,
                    tenant_id="t1",
                    confirmation_token=token,
                    actor="user",
                )
        stored = await repo.get(booking.booking_id)
        assert stored is not None
        assert stored.state == BookingState.FAILED


# ── agent-cannot-reach-confirm proof ─────────────────────────────────────────


class TestAgentCannotConfirm:
    def test_no_confirm_in_tool_definitions(self):
        """
        The TOOL_DEFINITIONS list must not contain any tool whose name includes
        'confirm'. This is the authoritative proof that the agent cannot call confirm.
        """
        from booking_agent.agent.tools import TOOL_DEFINITIONS

        tool_names = [t["name"] for t in TOOL_DEFINITIONS]
        assert not any("confirm" in name for name in tool_names), (
            f"Found a 'confirm' tool in agent tools: {tool_names}"
        )

    def test_prepare_booking_is_not_an_agent_tool(self):
        """
        Passenger details are collected in the UI form only, so the agent has no
        prepare_booking tool (PII must never reach the LLM).
        """
        from booking_agent.agent.tools import TOOL_DEFINITIONS

        tool_names = {t["name"] for t in TOOL_DEFINITIONS}
        assert "prepare_booking" not in tool_names

    def test_tool_definitions_are_read_only(self):
        """
        The agent must not have book, confirm, pay, ticket, or cancel tools.
        """
        from booking_agent.agent.tools import TOOL_DEFINITIONS

        forbidden_exact = {
            "prepare_booking",
            "confirm_booking",
            "book_flight",
            "pay",
            "ticket",
            "cancel_booking",
        }
        tool_names = {t["name"] for t in TOOL_DEFINITIONS}
        violations = tool_names & forbidden_exact
        assert not violations, f"Agent has forbidden tools: {violations}"
