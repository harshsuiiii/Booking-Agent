"""
Markup and rules engine domain types.

A Rule defines:
- who it applies to (scope: tenant, supplier, airline, route, cabin, date range…)
- what it does (action: % markup, fixed markup, % discount, fixed discount, commission)
- when it fires relative to other rules (priority: lower = fires first)

The engine produces a RuleTrace listing every rule that fired, with the INR markup
each contributed, so the desk agent can explain every charge to the customer.
"""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from fnmatch import fnmatch
from typing import Any

from pydantic import BaseModel, Field, field_validator

from booking_agent.domain.common import CabinClass


class RuleActionType(str, Enum):
    PERCENTAGE_MARKUP = "percentage_markup"  # add X% of net_total
    FIXED_MARKUP = "fixed_markup"  # add fixed INR amount
    PERCENTAGE_DISCOUNT = "percentage_discount"  # subtract X% of net_total
    FIXED_DISCOUNT = "fixed_discount"  # subtract fixed INR amount
    COMMISSION = "commission"  # track X% commission; does not change sell price


class RuleScope(BaseModel):
    """
    All conditions are optional; a None value means "match anything".
    A rule matches an offer only if EVERY specified condition matches.
    """

    supplier_id: str | None = None
    airline_iata: str | None = None  # matches any marketing carrier in the offer
    origin: str | None = Field(default=None, min_length=3, max_length=3)
    destination: str | None = Field(default=None, min_length=3, max_length=3)
    cabin_class: CabinClass | None = None
    fare_family: str | None = None  # glob pattern, e.g. "*SAVER*"
    customer_segment: str | None = None
    departure_date_from: date | None = None
    departure_date_to: date | None = None


class RuleAction(BaseModel):
    action_type: RuleActionType
    # For percentage types: 0–100 (e.g. 5.0 = 5%).
    # For fixed types: amount in INR (always positive; sign is determined by action_type).
    value: Decimal

    @field_validator("value", mode="before")
    @classmethod
    def reject_float(cls, v: Any) -> Any:
        if isinstance(v, float):
            raise ValueError("Use Decimal for rule value, not float")
        return v


class FiredRule(BaseModel):
    """Record of a single rule application within a RuleTrace."""

    rule_id: str
    rule_name: str
    action_type: RuleActionType
    markup_amount: Decimal  # positive = markup, negative = discount, 0 = commission


class RuleTrace(BaseModel):
    """Full trace of which rules fired for one offer and the net result."""

    offer_ref: str
    fired_rules: list[FiredRule] = Field(default_factory=list)

    @property
    def total_markup(self) -> Decimal:
        return sum((r.markup_amount for r in self.fired_rules), Decimal("0"))


class Rule(BaseModel):
    """A single pricing rule. Stored in Postgres; evaluated by the pure engine."""

    rule_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    tenant_id: str
    name: str
    description: str = ""
    scope: RuleScope = Field(default_factory=RuleScope)
    action: RuleAction
    priority: int = Field(default=100, ge=0)  # lower = fires first
    is_active: bool = True
    version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def matches(
        self,
        offer: "Any",  # booking_agent.domain.offer.Offer — avoid circular import
        tenant_id: str,
        customer_segment: str | None = None,
    ) -> bool:
        """Return True if this rule's scope applies to the given offer and context."""
        if self.tenant_id != tenant_id:
            return False

        s = self.scope
        if s.supplier_id is not None and s.supplier_id != offer.supplier_id:
            return False

        if s.airline_iata is not None:
            carriers = {
                seg.marketing_carrier.iata_code for sl in offer.slices for seg in sl.segments
            }
            if s.airline_iata not in carriers:
                return False

        if s.origin is not None and offer.slices[0].origin != s.origin:
            return False

        if s.destination is not None and offer.slices[-1].destination != s.destination:
            return False

        if s.cabin_class is not None and s.cabin_class != offer.cabin_class:
            return False

        if s.fare_family is not None:
            if offer.fare_family is None:
                return False
            if not fnmatch(offer.fare_family, s.fare_family):
                return False

        if s.customer_segment is not None and s.customer_segment != customer_segment:
            return False

        departure_date = offer.slices[0].departure_at.date()
        if s.departure_date_from is not None and departure_date < s.departure_date_from:
            return False
        return not (s.departure_date_to is not None and departure_date > s.departure_date_to)
