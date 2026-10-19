"""Payment instruction types passed to book()."""

from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable


@runtime_checkable
class PaymentInstruction(Protocol):
    """Marker protocol; concrete types carry payment-method-specific fields."""

    @property
    def payment_type(self) -> str: ...


@dataclass(frozen=True)
class DuffelBalancePayment:
    """Pay via Duffel Balance (test mode only in v1)."""

    payment_type: Literal["balance"] = field(default="balance")


@dataclass(frozen=True)
class GatewayPayment:
    """
    Pay via an external payment gateway (Razorpay, Stripe, etc.).

    The payment_intent_id is created and captured by BookingService before
    calling connector.book(), so the supplier sees a pre-authorised payment.
    """

    payment_intent_id: str
    gateway: str = "razorpay"
    payment_type: Literal["gateway"] = field(default="gateway")
