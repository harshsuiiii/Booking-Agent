from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, Field

from booking_agent.domain._types import UNKNOWN
from booking_agent.domain.offer import Offer


class SortMode(str, Enum):
    CHEAPEST = "CHEAPEST"
    FASTEST = "FASTEST"
    BEST_VALUE = "BEST_VALUE"


class RankingPreferences(BaseModel):
    """
    Penalty values (in INR) applied when an offer is missing features.
    Penalties are added to total_inr so the effective price reflects true cost.
    """
    no_bag_penalty: Decimal = Decimal("1500")      # add bag separately
    unknown_bag_penalty: Decimal = Decimal("750")  # half-penalty: uncertain
    no_refund_penalty: Decimal = Decimal("500")    # non-refundable fare
    unknown_refund_penalty: Decimal = Decimal("250")
    no_change_penalty: Decimal = Decimal("300")
    unknown_change_penalty: Decimal = Decimal("150")
    sort_mode: SortMode = SortMode.BEST_VALUE


class AdjustmentItem(BaseModel):
    reason: str
    amount: Decimal = Field(ge=0)


class EffectivePriceBreakdown(BaseModel):
    base_price: Decimal
    adjustments: list[AdjustmentItem]
    effective_price: Decimal


def effective_price(
    offer: Offer, preferences: RankingPreferences
) -> tuple[Decimal, EffectivePriceBreakdown]:
    """
    Pure function. Returns (effective_price_inr, breakdown).

    Adds penalty amounts for features the offer lacks or doesn't disclose.
    Lower effective price = better value for the traveler.
    Deterministic: same offer + same preferences always returns the same result.
    """
    adjustments: list[AdjustmentItem] = []

    # --- checked baggage ---
    checked = offer.baggage.checked_pieces
    if checked == UNKNOWN:
        adjustments.append(
            AdjustmentItem(reason="Checked baggage not disclosed", amount=preferences.unknown_bag_penalty)
        )
    elif checked is None or checked == 0:
        adjustments.append(
            AdjustmentItem(reason="No checked baggage included", amount=preferences.no_bag_penalty)
        )
    # else: checked > 0, no penalty

    # --- refundability ---
    refund_allowed = offer.fare_rules.refund.allowed
    if refund_allowed == UNKNOWN:
        adjustments.append(
            AdjustmentItem(reason="Refundability not disclosed", amount=preferences.unknown_refund_penalty)
        )
    elif refund_allowed is False:
        adjustments.append(
            AdjustmentItem(reason="Non-refundable fare", amount=preferences.no_refund_penalty)
        )

    # --- changeability ---
    change_allowed = offer.fare_rules.change.allowed
    if change_allowed == UNKNOWN:
        adjustments.append(
            AdjustmentItem(reason="Changeability not disclosed", amount=preferences.unknown_change_penalty)
        )
    elif change_allowed is False:
        adjustments.append(
            AdjustmentItem(reason="Non-changeable fare", amount=preferences.no_change_penalty)
        )

    total_penalty = sum((a.amount for a in adjustments), Decimal("0"))
    eff = offer.total_inr + total_penalty

    return eff, EffectivePriceBreakdown(
        base_price=offer.total_inr,
        adjustments=adjustments,
        effective_price=eff,
    )
