from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field, field_validator


class Money(BaseModel):
    amount: Decimal
    currency: str = Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")

    @field_validator("amount", mode="before")
    @classmethod
    def reject_float(cls, v: Any) -> Any:
        if isinstance(v, float):
            raise ValueError("Use Decimal for money, not float")
        return v

    def __add__(self, other: "Money") -> "Money":
        if self.currency != other.currency:
            raise ValueError(f"Cannot add {self.currency} and {other.currency}")
        return Money(amount=self.amount + other.amount, currency=self.currency)

    def __mul__(self, factor: Decimal) -> "Money":
        return Money(amount=self.amount * factor, currency=self.currency)

    def __repr__(self) -> str:
        return f"{self.currency} {self.amount}"


class FxAudit(BaseModel):
    """Full audit trail for a currency conversion."""

    original_amount: Decimal
    original_currency: str = Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    fx_rate: Decimal
    fx_timestamp: datetime
    converted_amount: Decimal
    converted_currency: str = Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")


def to_inr(money: Money, fx_rate: Decimal, fx_timestamp: datetime) -> tuple[Money, FxAudit]:
    """Convert any Money to INR, recording the full FX audit trail."""
    if money.currency == "INR":
        return money, FxAudit(
            original_amount=money.amount,
            original_currency="INR",
            fx_rate=Decimal("1"),
            fx_timestamp=fx_timestamp,
            converted_amount=money.amount,
            converted_currency="INR",
        )
    converted = money.amount * fx_rate
    return Money(amount=converted, currency="INR"), FxAudit(
        original_amount=money.amount,
        original_currency=money.currency,
        fx_rate=fx_rate,
        fx_timestamp=fx_timestamp,
        converted_amount=converted,
        converted_currency="INR",
    )
