"""
Payment gateway abstraction.

v1: DuffelBalanceGateway — payment is part of connector.book(); nothing extra needed.
Stub: RazorpayGateway — interface defined, not implemented until Step 12+.
Tests: FakePaymentGateway — succeeds or fails on demand.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Protocol, runtime_checkable


@dataclass
class PaymentResult:
    payment_id: str
    status: Literal["pending", "captured", "failed"]
    amount: Decimal
    currency: str


@runtime_checkable
class PaymentGateway(Protocol):
    async def create_payment_intent(
        self,
        booking_id: str,
        amount: Decimal,
        currency: str,
        idempotency_key: str,
    ) -> PaymentResult: ...

    async def capture(self, payment_intent_id: str) -> PaymentResult: ...


class DuffelBalanceGateway:
    """
    For Duffel, payment is handled inside connector.book() via Duffel Balance.
    This gateway is a no-op stub that immediately reports "captured".
    """

    async def create_payment_intent(
        self,
        booking_id: str,
        amount: Decimal,
        currency: str,
        idempotency_key: str,
    ) -> PaymentResult:
        return PaymentResult(
            payment_id=f"duffel_balance_{booking_id}",
            status="captured",
            amount=amount,
            currency=currency,
        )

    async def capture(self, payment_intent_id: str) -> PaymentResult:
        return PaymentResult(
            payment_id=payment_intent_id,
            status="captured",
            amount=Decimal("0"),
            currency="INR",
        )


class FakePaymentGateway:
    """Deterministic test gateway. Set fail=True to simulate payment failure."""

    def __init__(self, fail: bool = False) -> None:
        self._fail = fail
        self.intents_created: list[str] = []
        self.captures_attempted: list[str] = []

    async def create_payment_intent(
        self,
        booking_id: str,
        amount: Decimal,
        currency: str,
        idempotency_key: str,
    ) -> PaymentResult:
        intent_id = f"pi_fake_{booking_id}"
        self.intents_created.append(intent_id)
        if self._fail:
            return PaymentResult(
                payment_id=intent_id, status="failed", amount=amount, currency=currency
            )
        return PaymentResult(
            payment_id=intent_id, status="pending", amount=amount, currency=currency
        )

    async def capture(self, payment_intent_id: str) -> PaymentResult:
        self.captures_attempted.append(payment_intent_id)
        if self._fail:
            return PaymentResult(
                payment_id=payment_intent_id,
                status="failed",
                amount=Decimal("0"),
                currency="INR",
            )
        return PaymentResult(
            payment_id=payment_intent_id,
            status="captured",
            amount=Decimal("0"),
            currency="INR",
        )
