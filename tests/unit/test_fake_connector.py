"""
Tests for FakeConnector — covers retry behavior, book-never-retried contract,
timeout/error mapping, price drift, and secret redaction in logs.
"""

import os
from datetime import date
from decimal import Decimal

import pytest
import structlog.testing

from booking_agent.connectors.errors import (
    SoldOut,
    SupplierAuthError,
    SupplierBadResponse,
    SupplierRateLimited,
    SupplierTimeout,
)
from booking_agent.connectors.fake import FakeConnector
from booking_agent.connectors.secrets import EnvSecretsProvider
from booking_agent.domain.offer import Offer, PricedOffer
from booking_agent.domain.search import SearchQuery
from tests.conftest import T_EXPIRE, AI

# ── helpers ──────────────────────────────────────────────────────────────────

def make_query(origin: str = "DEL", destination: str = "BOM") -> SearchQuery:
    return SearchQuery(
        origin=origin,
        destination=destination,
        departure_date=date(2025, 9, 1),
    )


def fast_connector(**kwargs) -> FakeConnector:
    """FakeConnector with zero retry delays so tests run instantly."""
    kwargs.setdefault("max_retries", 3)
    kwargs.setdefault("retry_min_wait", 0.0)
    kwargs.setdefault("retry_max_wait", 0.0)
    return FakeConnector(**kwargs)


# ── search: basic behavior ────────────────────────────────────────────────────

class TestFakeConnectorSearch:
    async def test_returns_offer_list(self):
        async with fast_connector() as c:
            offers = await c.search(make_query())
        assert len(offers) == 1
        assert isinstance(offers[0], Offer)

    async def test_offer_origin_destination(self):
        async with fast_connector() as c:
            offers = await c.search(make_query("DEL", "BOM"))
        seg = offers[0].slices[0].segments[0]
        assert seg.origin == "DEL"
        assert seg.destination == "BOM"

    async def test_offer_has_inr_total(self):
        async with fast_connector() as c:
            offers = await c.search(make_query())
        assert offers[0].total_inr > Decimal("0")
        assert offers[0].price.currency == "INR"

    async def test_call_count_increments(self):
        async with fast_connector() as c:
            await c.search(make_query())
            await c.search(make_query())
        assert c.search_call_count == 2


# ── retry behavior ────────────────────────────────────────────────────────────

class TestRetryBehavior:
    async def test_search_retries_on_timeout_then_succeeds(self):
        errors = [
            SupplierTimeout("fake", "search"),
            SupplierTimeout("fake", "search"),
        ]
        async with fast_connector(search_errors=errors) as c:
            offers = await c.search(make_query())
        assert c.search_call_count == 3   # 2 failures + 1 success
        assert len(offers) == 1

    async def test_search_retries_on_rate_limited(self):
        errors = [SupplierRateLimited("fake", retry_after=0)]
        async with fast_connector(search_errors=errors) as c:
            offers = await c.search(make_query())
        assert c.search_call_count == 2
        assert len(offers) == 1

    async def test_search_exhausts_retries_and_raises(self):
        errors = [SupplierTimeout("fake", "search")] * 3
        async with fast_connector(search_errors=errors, max_retries=3) as c:
            with pytest.raises(SupplierTimeout):
                await c.search(make_query())
        assert c.search_call_count == 3

    async def test_price_retries_on_timeout(self):
        errors = [SupplierTimeout("fake", "price")]
        async with fast_connector(price_errors=errors) as c:
            offers = await c.search(make_query())
            await c.price(offers[0])
        assert c.price_call_count == 2

    async def test_auth_error_not_retried(self):
        """SupplierAuthError is not retryable — should fail immediately."""
        errors = [SupplierAuthError("fake", "bad token")]
        async with fast_connector(search_errors=errors) as c:
            with pytest.raises(SupplierAuthError):
                await c.search(make_query())
        assert c.search_call_count == 1  # only one attempt

    async def test_bad_response_not_retried(self):
        errors = [SupplierBadResponse("fake", 422, "Unprocessable", "search")]
        async with fast_connector(search_errors=errors) as c:
            with pytest.raises(SupplierBadResponse):
                await c.search(make_query())
        assert c.search_call_count == 1


# ── book is never retried ─────────────────────────────────────────────────────

class TestBookNeverRetried:
    async def test_book_called_exactly_once_on_error(self):
        """
        Even with max_retries=3, book() must call _do_book exactly once.
        Retrying a book would create duplicate PNRs at the GDS.
        """
        async with fast_connector(
            book_error=SoldOut("fake", "REF1"),
            max_retries=3,
        ) as c:
            offers = await c.search(make_query())
            priced = await c.price(offers[0])
            from tests.conftest import AI
            from booking_agent.domain.booking import Passenger, PassengerType
            from datetime import date
            pax = [Passenger(
                pax_type=PassengerType.ADULT,
                given_name="Test",
                family_name="User",
                date_of_birth=date(1990, 1, 1),
            )]
            with pytest.raises(SoldOut):
                await c.book(priced, pax, idempotency_key="idem-1")

        assert c.book_call_count == 1   # never retried regardless of max_retries

    async def test_book_timeout_also_not_retried(self):
        async with fast_connector(
            book_error=SupplierTimeout("fake", "book"),
            max_retries=3,
        ) as c:
            offers = await c.search(make_query())
            priced = await c.price(offers[0])
            from booking_agent.domain.booking import Passenger, PassengerType
            from datetime import date
            pax = [Passenger(
                pax_type=PassengerType.ADULT,
                given_name="T",
                family_name="U",
                date_of_birth=date(1990, 1, 1),
            )]
            with pytest.raises(SupplierTimeout):
                await c.book(priced, pax, idempotency_key="idem-2")

        assert c.book_call_count == 1


# ── price drift ───────────────────────────────────────────────────────────────

class TestPriceDrift:
    async def test_price_drift_applied(self):
        drift = Decimal("200")
        async with fast_connector(price_drift=drift) as c:
            offers = await c.search(make_query())
            original_total = offers[0].total_inr
            priced = await c.price(offers[0])
        assert priced.total_inr == original_total + drift

    async def test_zero_drift_no_change(self):
        async with fast_connector(price_drift=Decimal("0")) as c:
            offers = await c.search(make_query())
            priced = await c.price(offers[0])
        assert priced.total_inr == offers[0].total_inr

    async def test_priced_offer_is_priced_offer_type(self):
        async with fast_connector() as c:
            offers = await c.search(make_query())
            priced = await c.price(offers[0])
        assert isinstance(priced, PricedOffer)
        assert priced.priced_at is not None
        assert priced.valid_until is not None


# ── successful book ───────────────────────────────────────────────────────────

class TestSuccessfulBook:
    async def test_book_returns_booking(self):
        from booking_agent.domain.booking import Booking, BookingState, Passenger, PassengerType
        from datetime import date
        pax = [Passenger(
            pax_type=PassengerType.ADULT,
            given_name="Jane",
            family_name="Doe",
            date_of_birth=date(1990, 5, 15),
        )]
        async with fast_connector() as c:
            offers = await c.search(make_query())
            priced = await c.price(offers[0])
            booking = await c.book(priced, pax, idempotency_key="key-123")
        assert isinstance(booking, Booking)
        assert booking.state == BookingState.PNR_CREATED
        assert booking.pnr is not None
        assert booking.idempotency_key == "key-123"


# ── secrets in logs ───────────────────────────────────────────────────────────

class TestSecretsNotInLogs:
    async def test_env_secret_not_logged(self, monkeypatch):
        """
        EnvSecretsProvider must not emit the secret value to structlog.
        This verifies the provider itself and BaseConnector log nothing sensitive.
        """
        secret_value = "VERY_SECRET_API_KEY_XYZ_789"
        monkeypatch.setenv("TEST_FAKE_KEY", secret_value)

        provider = EnvSecretsProvider()
        connector = fast_connector(secrets=provider)

        with structlog.testing.capture_logs() as logs:
            async with connector:
                await connector.search(make_query())

        all_text = str(logs)
        assert secret_value not in all_text

    async def test_offer_ref_logged_not_secret(self, monkeypatch):
        """Offer refs (non-sensitive) DO appear in logs; secrets do not."""
        monkeypatch.setenv("ANOTHER_SECRET", "s3cr3t-p@ssw0rd")

        with structlog.testing.capture_logs() as logs:
            async with fast_connector() as c:
                offers = await c.search(make_query())
                await c.price(offers[0])

        all_text = str(logs)
        assert "s3cr3t-p@ssw0rd" not in all_text
        # Structural fields like origin/destination should be present
        assert "DEL" in all_text


# ── capabilities ──────────────────────────────────────────────────────────────

class TestCapabilities:
    def test_supplier_id(self):
        c = FakeConnector()
        assert c.capabilities.supplier_id == "fake"

    def test_satisfies_connector_protocol(self):
        from booking_agent.connectors.protocol import Connector
        c = FakeConnector()
        assert isinstance(c, Connector)
