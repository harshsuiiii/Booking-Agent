"""
Rules engine: pure, deterministic evaluator.

apply_rules(offer, rules, tenant_id) → (Offer with agency_markup set, RuleTrace)

Design:
- All matching rules fire in priority order (lower priority number = fires first).
- Percentage markups and discounts are computed from the same net_total base so
  stacking rules is order-independent.
- Fixed amounts are in INR.
- Commission rules are informational: they appear in the trace but do not change price.
- The result is idempotent: calling apply_rules twice on the same offer with the
  same rules produces identical output.
"""

from decimal import ROUND_HALF_UP, Decimal

from booking_agent.domain.offer import Offer
from booking_agent.rules.model import FiredRule, Rule, RuleActionType, RuleTrace


def _markup_for_action(rule: Rule, net_total: Decimal) -> Decimal:
    """Compute the INR markup delta (positive = markup, negative = discount, 0 = commission)."""
    action = rule.action
    match action.action_type:
        case RuleActionType.PERCENTAGE_MARKUP:
            return (net_total * action.value / Decimal("100")).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        case RuleActionType.FIXED_MARKUP:
            return action.value
        case RuleActionType.PERCENTAGE_DISCOUNT:
            return -(net_total * action.value / Decimal("100")).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        case RuleActionType.FIXED_DISCOUNT:
            return -action.value
        case RuleActionType.COMMISSION:
            return Decimal("0")
        case _:
            return Decimal("0")


def apply_rules(
    offer: Offer,
    rules: list[Rule],
    tenant_id: str,
    customer_segment: str | None = None,
) -> tuple[Offer, RuleTrace]:
    """
    Apply all active matching rules to *offer* for *tenant_id*.

    Returns a new Offer with price.agency_markup set to the total markup from
    all fired rules, plus a RuleTrace listing each rule and its contribution.
    The original offer is never mutated.
    """
    active = [r for r in rules if r.is_active]
    matching = sorted(
        [r for r in active if r.matches(offer, tenant_id, customer_segment)],
        key=lambda r: r.priority,
    )

    if not matching:
        # No rules fired: ensure agency_markup is 0 (idempotent reset)
        if offer.price.agency_markup != Decimal("0"):
            new_price = offer.price.model_copy(update={"agency_markup": Decimal("0")})
            offer = offer.model_copy(update={"price": new_price})
        return offer, RuleTrace(offer_ref=offer.supplier_offer_ref)

    # Compute all markups from the same base (net_total before any markup)
    net_total = offer.price.net_total
    fired: list[FiredRule] = []
    total_markup = Decimal("0")

    for rule in matching:
        delta = _markup_for_action(rule, net_total)
        fired.append(
            FiredRule(
                rule_id=rule.rule_id,
                rule_name=rule.name,
                action_type=rule.action.action_type,
                markup_amount=delta,
            )
        )
        total_markup += delta

    new_price = offer.price.model_copy(update={"agency_markup": total_markup})
    updated_offer = offer.model_copy(update={"price": new_price})
    trace = RuleTrace(offer_ref=offer.supplier_offer_ref, fired_rules=fired)
    return updated_offer, trace


def apply_rules_batch(
    offers: list[Offer],
    rules: list[Rule],
    tenant_id: str,
    customer_segment: str | None = None,
) -> tuple[list[Offer], list[RuleTrace]]:
    """Apply rules to every offer in the batch. Returns parallel lists."""
    results = [apply_rules(o, rules, tenant_id, customer_segment) for o in offers]
    ruled_offers = [r[0] for r in results]
    traces = [r[1] for r in results]
    return ruled_offers, traces
