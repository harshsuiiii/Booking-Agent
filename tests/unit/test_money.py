from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from booking_agent.domain.money import FxAudit, Money, to_inr

FX_TS = datetime(2025, 6, 1, tzinfo=UTC)


class TestMoney:
    def test_valid(self):
        m = Money(amount=Decimal("100"), currency="INR")
        assert m.amount == Decimal("100")
        assert m.currency == "INR"

    def test_float_raises(self):
        with pytest.raises(ValidationError, match="float"):
            Money(amount=100.0, currency="INR")

    def test_currency_too_short_raises(self):
        with pytest.raises(ValidationError):
            Money(amount=Decimal("100"), currency="IN")

    def test_currency_too_long_raises(self):
        with pytest.raises(ValidationError):
            Money(amount=Decimal("100"), currency="INRR")

    def test_currency_lowercase_raises(self):
        with pytest.raises(ValidationError):
            Money(amount=Decimal("100"), currency="inr")

    def test_add_same_currency(self):
        result = Money(amount=Decimal("100"), currency="INR") + Money(
            amount=Decimal("50"), currency="INR"
        )
        assert result.amount == Decimal("150")
        assert result.currency == "INR"

    def test_add_different_currency_raises(self):
        with pytest.raises(ValueError, match="Cannot add"):
            Money(amount=Decimal("100"), currency="INR") + Money(
                amount=Decimal("1"), currency="USD"
            )

    def test_multiply(self):
        result = Money(amount=Decimal("100"), currency="INR") * Decimal("3")
        assert result.amount == Decimal("300")


class TestToInr:
    def test_inr_passthrough(self):
        m = Money(amount=Decimal("1000"), currency="INR")
        converted, audit = to_inr(m, fx_rate=Decimal("1"), fx_timestamp=FX_TS)
        assert converted.amount == Decimal("1000")
        assert converted.currency == "INR"
        assert audit.fx_rate == Decimal("1")
        assert audit.original_currency == "INR"

    def test_usd_to_inr(self):
        m = Money(amount=Decimal("100"), currency="USD")
        rate = Decimal("83.5")
        converted, audit = to_inr(m, fx_rate=rate, fx_timestamp=FX_TS)
        assert converted.amount == Decimal("8350.0")
        assert converted.currency == "INR"

    def test_fx_audit_fields_populated(self):
        m = Money(amount=Decimal("50"), currency="EUR")
        rate = Decimal("90")
        converted, audit = to_inr(m, fx_rate=rate, fx_timestamp=FX_TS)
        assert audit.original_amount == Decimal("50")
        assert audit.original_currency == "EUR"
        assert audit.fx_rate == Decimal("90")
        assert audit.fx_timestamp == FX_TS
        assert audit.converted_amount == Decimal("4500")
        assert audit.converted_currency == "INR"

    def test_fx_audit_is_model(self):
        m = Money(amount=Decimal("10"), currency="USD")
        _, audit = to_inr(m, fx_rate=Decimal("83"), fx_timestamp=FX_TS)
        assert isinstance(audit, FxAudit)
