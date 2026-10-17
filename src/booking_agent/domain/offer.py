import hashlib
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from booking_agent.domain._types import UNKNOWN, Unknown
from booking_agent.domain.common import CabinClass
from booking_agent.domain.itinerary import Slice
from booking_agent.domain.money import Money


class BaggageAllowance(BaseModel):
    """
    Allowance per passenger per direction.

    - int  = known count
    - None = explicitly not included / not applicable
    - UNKNOWN = supplier didn't disclose (show "unknown" in UI; apply penalty in ranking)
    """
    cabin_pieces: int | None | Unknown = UNKNOWN
    cabin_kg: Decimal | None | Unknown = UNKNOWN
    checked_pieces: int | None | Unknown = UNKNOWN
    checked_kg: Decimal | None | Unknown = UNKNOWN


class FareRulePolicy(BaseModel):
    """
    Policy for a single fare action (change / cancel / refund).

    - allowed: True/False = known; UNKNOWN = not disclosed
    - fee: Money = known fee; None = no fee; UNKNOWN = fee not disclosed
    """
    allowed: bool | Unknown = UNKNOWN
    fee: Money | None | Unknown = UNKNOWN


class FareRules(BaseModel):
    change: FareRulePolicy = Field(default_factory=FareRulePolicy)
    cancel: FareRulePolicy = Field(default_factory=FareRulePolicy)
    refund: FareRulePolicy = Field(default_factory=FareRulePolicy)


class TaxLine(BaseModel):
    code: str           # IATA tax code, e.g. "YQ", "IN", "K3"
    description: Optional[str] = None
    amount: Decimal

    @field_validator("amount", mode="before")
    @classmethod
    def reject_float(cls, v: Any) -> Any:
        if isinstance(v, float):
            raise ValueError("Use Decimal for money, not float")
        return v


class PriceBreakdown(BaseModel):
    base_fare: Decimal
    taxes: list[TaxLine] = Field(default_factory=list)
    fuel_surcharge: Decimal = Decimal("0")
    supplier_fee: Decimal = Decimal("0")
    agency_markup: Decimal = Decimal("0")
    currency: str = Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")

    @field_validator("base_fare", "fuel_surcharge", "supplier_fee", "agency_markup", mode="before")
    @classmethod
    def reject_floats(cls, v: Any) -> Any:
        if isinstance(v, float):
            raise ValueError("Use Decimal for money, not float")
        return v

    @property
    def taxes_total(self) -> Decimal:
        return sum((t.amount for t in self.taxes), Decimal("0"))

    @property
    def net_total(self) -> Decimal:
        return self.base_fare + self.taxes_total + self.fuel_surcharge + self.supplier_fee

    @property
    def sell_total(self) -> Decimal:
        return self.net_total + self.agency_markup


class Offer(BaseModel):
    offer_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    supplier_id: str
    supplier_offer_ref: str       # opaque ref needed for re-price and book
    raw_payload_ref: Optional[str] = None  # pointer to stored raw response; never inline

    slices: list[Slice] = Field(min_length=1)
    fare_basis: str               # overall fare basis; use per-segment if they differ
    fare_family: Optional[str] = None
    cabin_class: CabinClass
    baggage: BaggageAllowance = Field(default_factory=BaggageAllowance)
    fare_rules: FareRules = Field(default_factory=FareRules)

    price: PriceBreakdown
    total_inr: Decimal            # authoritative INR total for ranking; set from price + FX

    expires_at: datetime
    searched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    pax_count: int = Field(default=1, ge=1)

    @field_validator("total_inr", mode="before")
    @classmethod
    def reject_float_total(cls, v: Any) -> Any:
        if isinstance(v, float):
            raise ValueError("Use Decimal for total_inr, not float")
        return v

    @property
    def dedupe_key(self) -> str:
        """
        Deterministic fingerprint of itinerary + fare product, independent of supplier.
        Two offers with the same key are duplicates; keep the cheapest, surface the rest
        as alternates.
        """
        parts: list[str] = []
        for sl in self.slices:
            for seg in sl.segments:
                parts.append(
                    f"{seg.marketing_carrier.iata_code}{seg.flight_number}"
                    f"|{seg.departure_at.isoformat()}|{seg.arrival_at.isoformat()}"
                )
        parts.append(self.fare_basis)
        parts.append(self.cabin_class.value)
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


class PricedOffer(Offer):
    """Offer that has been re-priced live immediately before booking."""
    priced_at: datetime
    valid_until: datetime           # fare guarantee window from supplier
    hold_deadline: Optional[datetime] = None  # PNR must be ticketed by this time
