"""
Structured offer comparison — produced entirely by code, never by the LLM.
"""

from typing import Any

from booking_agent.domain._types import UNKNOWN
from booking_agent.domain.offer import Offer
from booking_agent.domain.ranking import RankingPreferences, effective_price


def _bag_label(val: Any) -> str:
    if val is UNKNOWN:
        return "unknown"
    if val is None or val == 0:
        return "none"
    return str(val)


def _policy_label(val: Any) -> str:
    if val is UNKNOWN:
        return "unknown"
    if val is True:
        return "yes"
    if val is False:
        return "no"
    return str(val)


def compare(offers: list[Offer], preferences: RankingPreferences) -> dict[str, Any]:
    """
    Return a dict suitable for JSON serialisation that compares *offers* side-by-side.
    All numeric amounts are returned as strings to avoid float precision issues.
    """
    rows = []
    for i, offer in enumerate(offers, 1):
        eff, breakdown = effective_price(offer, preferences)
        customer_price = offer.total_inr + offer.price.agency_markup
        total_stops = sum(sl.stops for sl in offer.slices)
        total_duration = sum(sl.journey_minutes for sl in offer.slices)

        adjustments = [
            {"reason": a.reason, "amount_inr": str(a.amount)} for a in breakdown.adjustments
        ]

        rows.append(
            {
                "rank": i,
                "offer_id": offer.offer_id,
                "is_test_data": offer.is_test_data,
                "airline": offer.slices[0].segments[0].marketing_carrier.iata_code,
                "route": f"{offer.slices[0].origin} → {offer.slices[-1].destination}",
                "departure": offer.slices[0].departure_at.isoformat(),
                "arrival": offer.slices[-1].arrival_at.isoformat(),
                "duration_minutes": total_duration,
                "stops": total_stops,
                "cabin": offer.cabin_class.value,
                "fare_family": offer.fare_family,
                "customer_price_inr": str(customer_price),
                "effective_price_inr": str(eff),
                "effective_price_adjustments": adjustments,
                "checked_baggage": _bag_label(offer.baggage.checked_pieces),
                "refundable": _policy_label(offer.fare_rules.refund.allowed),
                "changeable": _policy_label(offer.fare_rules.change.allowed),
                "expires_at": offer.expires_at.isoformat(),
            }
        )

    return {"compared_count": len(rows), "offers": rows}
