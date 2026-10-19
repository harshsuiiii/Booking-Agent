"""
Offer deduplication.

Offers with the same dedupe_key represent the same itinerary + fare product from
different suppliers (or the same supplier at different prices). Keep the cheapest
as the primary; surface the rest as alternates so the UI can show
"also available via X at Y price."
"""

from dataclasses import dataclass, field
from decimal import Decimal

from booking_agent.domain.offer import Offer


@dataclass(frozen=True)
class AlternateSource:
    supplier_id: str
    offer_id: str
    total_inr: Decimal  # net
    supplier_offer_ref: str
    markup_inr: Decimal = Decimal("0")  # agency markup; sell = total_inr + markup_inr


@dataclass
class DedupeGroup:
    primary: Offer
    alternates: list[AlternateSource] = field(default_factory=list)

    @property
    def dedupe_key(self) -> str:
        return self.primary.dedupe_key

    @property
    def cheapest_inr(self) -> Decimal:
        return self.primary.total_inr

    @property
    def all_supplier_ids(self) -> list[str]:
        return [self.primary.supplier_id] + [a.supplier_id for a in self.alternates]


def dedupe(offers: list[Offer]) -> list[DedupeGroup]:
    """
    Group offers by dedupe_key. Within each group the offer with the lowest
    total_inr becomes the primary; ties are broken by offer_id for stability.
    Returns one DedupeGroup per unique itinerary + fare product.
    """
    groups: dict[str, list[Offer]] = {}
    for offer in offers:
        key = offer.dedupe_key
        groups.setdefault(key, []).append(offer)

    result: list[DedupeGroup] = []
    for group_offers in groups.values():
        sorted_offers = sorted(group_offers, key=lambda o: (o.total_inr, o.offer_id))
        primary = sorted_offers[0]
        alternates = [
            AlternateSource(
                supplier_id=o.supplier_id,
                offer_id=o.offer_id,
                total_inr=o.total_inr,
                supplier_offer_ref=o.supplier_offer_ref,
                markup_inr=o.price.agency_markup,
            )
            for o in sorted_offers[1:]
        ]
        result.append(DedupeGroup(primary=primary, alternates=alternates))

    return result
