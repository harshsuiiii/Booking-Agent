"""
Role-specific views of offers and prices. Every API response that carries a price goes
through here, so role rules are enforced in one place:

  agent → sell price only (incl. taxes). No net fare, markup, base fare, supplier fee,
          fuel surcharge or FX audit (which holds the supplier's original net amount).
  admin → everything, plus explicit net_inr / markup_inr / sell_inr.

AGENT_FORBIDDEN_KEYS is what tests assert never appears anywhere in an Agent response.
"""

from decimal import Decimal
from typing import Any

from booking_agent.auth.principal import Role
from booking_agent.domain.offer import Offer
from booking_agent.orchestrator.dedupe import AlternateSource, DedupeGroup
from booking_agent.suppliers import supplier_label

AGENT_FORBIDDEN_KEYS = frozenset(
    {
        "net_inr",
        "markup_inr",
        "net_total",
        "total_inr",
        "agency_markup",
        "markup",
        "base_fare",
        "supplier_fee",
        "fuel_surcharge",
        "fx_audit",
        "net",
        "markup_this_month",
        "net_by_supplier",
        "markup_by_supplier",
        "win_rate",
        "supplier_performance",
    }
)


def sell_inr(offer: Offer) -> Decimal:
    """What the customer pays, in INR: net (total_inr) + agency markup."""
    return offer.total_inr + offer.price.agency_markup


def offer_view(offer: Offer, role: Role) -> dict[str, Any]:
    data = offer.model_dump(mode="json")
    data["supplier_label"] = supplier_label(offer.supplier_id)
    sell = sell_inr(offer)
    if role == "admin":
        data["sell_inr"] = str(sell)
        data["net_inr"] = str(offer.total_inr)
        data["markup_inr"] = str(offer.price.agency_markup)
        return data
    for key in ("price", "total_inr", "fx_audit", "raw_payload_ref"):
        data.pop(key, None)
    data["price"] = {
        "currency": "INR",
        "sell_total": str(sell),
        "taxes_total": str(offer.price.taxes_total),
    }
    data["sell_inr"] = str(sell)
    return data


def alternate_view(alt: AlternateSource, role: Role) -> dict[str, Any]:
    data: dict[str, Any] = {
        "supplier_id": alt.supplier_id,
        "supplier_label": supplier_label(alt.supplier_id),
        "offer_id": alt.offer_id,
        "sell_inr": str(alt.total_inr + alt.markup_inr),
    }
    if role == "admin":
        data["net_inr"] = str(alt.total_inr)
        data["markup_inr"] = str(alt.markup_inr)
        data["supplier_offer_ref"] = alt.supplier_offer_ref
    return data


def group_view(group: DedupeGroup, role: Role) -> dict[str, Any]:
    return {
        "dedupe_key": group.dedupe_key,
        "primary": offer_view(group.primary, role),
        "alternates": [alternate_view(a, role) for a in group.alternates],
    }


def money_view(
    role: Role, *, sell: Decimal, net: Decimal, markup: Decimal, currency: str = "INR"
) -> dict[str, Any]:
    """Price fields for a booking or summary row."""
    data: dict[str, Any] = {"sell_inr": str(sell), "currency": currency}
    if role == "admin":
        data["net_inr"] = str(net)
        data["markup_inr"] = str(markup)
    return data


def find_forbidden_keys(payload: Any, path: str = "$") -> list[str]:
    """Paths of AGENT_FORBIDDEN_KEYS anywhere in a JSON-like payload (for tests/guards)."""
    found: list[str] = []
    if isinstance(payload, dict):
        for k, v in payload.items():
            if k in AGENT_FORBIDDEN_KEYS:
                found.append(f"{path}.{k}")
            found.extend(find_forbidden_keys(v, f"{path}.{k}"))
    elif isinstance(payload, list):
        for i, v in enumerate(payload):
            found.extend(find_forbidden_keys(v, f"{path}[{i}]"))
    return found
