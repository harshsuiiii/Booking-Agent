"""
Rules engine golden-scenario tests.

Ten canonical cases + edge cases verifying:
- precedence and conflict resolution
- Decimal rounding (ROUND_HALF_UP)
- trace correctness
- tenant isolation
- idempotent application
"""

from datetime import UTC, date, datetime
from decimal import Decimal

from booking_agent.domain.common import CabinClass, Carrier
from booking_agent.domain.itinerary import Segment, Slice
from booking_agent.domain.offer import Offer, PriceBreakdown
from booking_agent.rules.engine import apply_rules, apply_rules_batch
from booking_agent.rules.model import Rule, RuleAction, RuleActionType, RuleScope

# ── helpers ───────────────────────────────────────────────────────────────────

_DEPART = datetime(2025, 6, 15, 6, 0, tzinfo=UTC)
_ARRIVE = datetime(2025, 6, 15, 8, 0, tzinfo=UTC)


def _segment(
    origin: str = "BOM",
    destination: str = "DEL",
    carrier: str = "6E",
    departure: datetime = _DEPART,
    arrival: datetime = _ARRIVE,
) -> Segment:
    return Segment(
        marketing_carrier=Carrier(iata_code=carrier, name=carrier),
        flight_number="6E123",
        origin=origin,
        destination=destination,
        departure_at=departure,
        arrival_at=arrival,
        cabin_class=CabinClass.ECONOMY,
        booking_class="Y",
        duration_minutes=120,
    )


def _offer(
    net_total: Decimal = Decimal("5000"),
    supplier_id: str = "duffel",
    cabin_class: CabinClass = CabinClass.ECONOMY,
    fare_family: str | None = None,
    segment: Segment | None = None,
    tenant: str = "tenant_a",
) -> Offer:
    seg = segment or _segment()
    price = PriceBreakdown(
        base_fare=net_total,
        currency="INR",
    )
    return Offer(
        supplier_id=supplier_id,
        supplier_offer_ref="ref001",
        slices=[Slice(segments=[seg])],
        fare_basis="Y",
        fare_family=fare_family,
        cabin_class=cabin_class,
        price=price,
        total_inr=net_total,
        expires_at=datetime(2025, 6, 14, tzinfo=UTC),
    )


def _rule(
    action_type: RuleActionType,
    value: Decimal,
    tenant_id: str = "tenant_a",
    priority: int = 100,
    scope: RuleScope | None = None,
    name: str = "test rule",
) -> Rule:
    return Rule(
        tenant_id=tenant_id,
        name=name,
        scope=scope or RuleScope(),
        action=RuleAction(action_type=action_type, value=value),
        priority=priority,
    )


# ── 1. No-rules baseline ──────────────────────────────────────────────────────


def test_no_rules_agency_markup_is_zero() -> None:
    offer = _offer()
    result, trace = apply_rules(offer, [], "tenant_a")
    assert result.price.agency_markup == Decimal("0")
    assert trace.fired_rules == []
    assert trace.total_markup == Decimal("0")


def test_no_rules_idempotent_reset() -> None:
    """If offer already has a non-zero markup, it's reset to 0 when no rules fire."""
    price = PriceBreakdown(base_fare=Decimal("5000"), currency="INR", agency_markup=Decimal("500"))
    seg = _segment()
    offer = Offer(
        supplier_id="duffel",
        supplier_offer_ref="ref",
        slices=[Slice(segments=[seg])],
        fare_basis="Y",
        cabin_class=CabinClass.ECONOMY,
        price=price,
        total_inr=Decimal("5500"),
        expires_at=datetime(2025, 6, 14, tzinfo=UTC),
    )
    result, _ = apply_rules(offer, [], "tenant_a")
    assert result.price.agency_markup == Decimal("0")


# ── 2. Flat percentage markup ─────────────────────────────────────────────────


def test_percentage_markup_five_pct() -> None:
    offer = _offer(net_total=Decimal("5000"))
    rule = _rule(RuleActionType.PERCENTAGE_MARKUP, Decimal("5"))
    result, trace = apply_rules(offer, [rule], "tenant_a")
    assert result.price.agency_markup == Decimal("250.00")
    assert len(trace.fired_rules) == 1
    assert trace.fired_rules[0].markup_amount == Decimal("250.00")


def test_percentage_markup_rounds_half_up() -> None:
    # 5% of 5001 = 250.05 → 250.05 (exact); use amount that forces rounding
    # 7% of 1001 = 70.07 (exact); try 1/3% of 100 = 0.3333... → 0.33
    offer = _offer(net_total=Decimal("1000"))
    rule = _rule(RuleActionType.PERCENTAGE_MARKUP, Decimal("0.555"))  # 5.55 → rounds to 5.55
    result, _ = apply_rules(offer, [rule], "tenant_a")
    # 1000 * 0.555 / 100 = 5.55 exact
    assert result.price.agency_markup == Decimal("5.55")


def test_percentage_markup_rounding_half_up() -> None:
    # 3% of 333 = 9.99; 5% of 333 = 16.65 exact; try 1/3 * 10 = 3.333... → 3.33
    offer = _offer(net_total=Decimal("333"))
    rule = _rule(RuleActionType.PERCENTAGE_MARKUP, Decimal("1"))  # 1% of 333 = 3.33
    result, _ = apply_rules(offer, [rule], "tenant_a")
    assert result.price.agency_markup == Decimal("3.33")


# ── 3. Fixed markup ───────────────────────────────────────────────────────────


def test_fixed_markup() -> None:
    offer = _offer(net_total=Decimal("5000"))
    rule = _rule(RuleActionType.FIXED_MARKUP, Decimal("200"))
    result, trace = apply_rules(offer, [rule], "tenant_a")
    assert result.price.agency_markup == Decimal("200")
    assert trace.fired_rules[0].markup_amount == Decimal("200")


def test_fixed_markup_independent_of_net_total() -> None:
    for base in ("1000", "50000"):
        offer = _offer(net_total=Decimal(base))
        rule = _rule(RuleActionType.FIXED_MARKUP, Decimal("500"))
        result, _ = apply_rules(offer, [rule], "tenant_a")
        assert result.price.agency_markup == Decimal("500")


# ── 4. Airline-specific markup ────────────────────────────────────────────────


def test_airline_scope_matches_correct_carrier() -> None:
    offer = _offer()  # segment is 6E by default
    rule = _rule(
        RuleActionType.PERCENTAGE_MARKUP,
        Decimal("3"),
        scope=RuleScope(airline_iata="6E"),
    )
    result, trace = apply_rules(offer, [rule], "tenant_a")
    assert len(trace.fired_rules) == 1
    assert result.price.agency_markup == Decimal("150.00")  # 3% of 5000


def test_airline_scope_skips_other_carrier() -> None:
    offer = _offer(segment=_segment(carrier="AI"))  # Air India
    rule = _rule(
        RuleActionType.PERCENTAGE_MARKUP,
        Decimal("3"),
        scope=RuleScope(airline_iata="6E"),
    )
    result, trace = apply_rules(offer, [rule], "tenant_a")
    assert result.price.agency_markup == Decimal("0")
    assert trace.fired_rules == []


# ── 5. Stacked markups (cumulative from same base) ────────────────────────────


def test_stacked_markups_cumulative() -> None:
    """Two percentage rules fire; both are computed from the original net_total."""
    offer = _offer(net_total=Decimal("10000"))
    rules = [
        _rule(RuleActionType.PERCENTAGE_MARKUP, Decimal("5"), priority=10, name="r1"),
        _rule(RuleActionType.PERCENTAGE_MARKUP, Decimal("3"), priority=20, name="r2"),
    ]
    result, trace = apply_rules(offer, rules, "tenant_a")
    # 5% of 10000 = 500; 3% of 10000 = 300; total = 800 (not 5% then 3% of 10500)
    assert result.price.agency_markup == Decimal("800.00")
    assert len(trace.fired_rules) == 2
    assert trace.total_markup == Decimal("800.00")


def test_stacked_mixed_types() -> None:
    """Fixed + percentage stack correctly."""
    offer = _offer(net_total=Decimal("4000"))
    rules = [
        _rule(RuleActionType.FIXED_MARKUP, Decimal("100"), priority=10, name="fixed"),
        _rule(RuleActionType.PERCENTAGE_MARKUP, Decimal("10"), priority=20, name="pct"),
    ]
    result, trace = apply_rules(offer, rules, "tenant_a")
    # fixed: 100; percentage: 10% of 4000 = 400; total: 500
    assert result.price.agency_markup == Decimal("500")
    assert trace.total_markup == Decimal("500")


def test_priority_ordering_reflected_in_trace() -> None:
    """Rules appear in the trace in priority order (lower priority number first)."""
    offer = _offer()
    rules = [
        _rule(RuleActionType.FIXED_MARKUP, Decimal("50"), priority=30, name="last"),
        _rule(RuleActionType.FIXED_MARKUP, Decimal("10"), priority=5, name="first"),
    ]
    _, trace = apply_rules(offer, rules, "tenant_a")
    assert trace.fired_rules[0].rule_name == "first"
    assert trace.fired_rules[1].rule_name == "last"


# ── 6. Discount for corporate segment ────────────────────────────────────────


def test_corporate_discount_fires_for_segment() -> None:
    offer = _offer(net_total=Decimal("8000"))
    rule = _rule(
        RuleActionType.PERCENTAGE_DISCOUNT,
        Decimal("10"),
        scope=RuleScope(customer_segment="corporate"),
    )
    result, trace = apply_rules(offer, [rule], "tenant_a", customer_segment="corporate")
    assert result.price.agency_markup == Decimal("-800.00")
    assert trace.fired_rules[0].markup_amount == Decimal("-800.00")


def test_corporate_discount_skips_default_segment() -> None:
    offer = _offer(net_total=Decimal("8000"))
    rule = _rule(
        RuleActionType.PERCENTAGE_DISCOUNT,
        Decimal("10"),
        scope=RuleScope(customer_segment="corporate"),
    )
    result, trace = apply_rules(offer, [rule], "tenant_a", customer_segment=None)
    assert result.price.agency_markup == Decimal("0")
    assert trace.fired_rules == []


def test_fixed_discount() -> None:
    offer = _offer(net_total=Decimal("5000"))
    rule = _rule(RuleActionType.FIXED_DISCOUNT, Decimal("300"))
    result, _ = apply_rules(offer, [rule], "tenant_a")
    assert result.price.agency_markup == Decimal("-300")


# ── 7. Date-range limited rule ────────────────────────────────────────────────


def test_date_range_fires_within_range() -> None:
    offer = _offer()  # departure 2025-06-15
    rule = _rule(
        RuleActionType.FIXED_MARKUP,
        Decimal("100"),
        scope=RuleScope(
            departure_date_from=date(2025, 6, 1),
            departure_date_to=date(2025, 6, 30),
        ),
    )
    result, _ = apply_rules(offer, [rule], "tenant_a")
    assert result.price.agency_markup == Decimal("100")


def test_date_range_skips_before_range() -> None:
    offer = _offer()  # departure 2025-06-15
    rule = _rule(
        RuleActionType.FIXED_MARKUP,
        Decimal("100"),
        scope=RuleScope(departure_date_from=date(2025, 7, 1)),
    )
    result, _ = apply_rules(offer, [rule], "tenant_a")
    assert result.price.agency_markup == Decimal("0")


def test_date_range_skips_after_range() -> None:
    offer = _offer()  # departure 2025-06-15
    rule = _rule(
        RuleActionType.FIXED_MARKUP,
        Decimal("100"),
        scope=RuleScope(departure_date_to=date(2025, 6, 14)),
    )
    result, _ = apply_rules(offer, [rule], "tenant_a")
    assert result.price.agency_markup == Decimal("0")


# ── 8. Tenant isolation ───────────────────────────────────────────────────────


def test_tenant_isolation_rule_does_not_cross_tenants() -> None:
    offer = _offer()
    rule = _rule(RuleActionType.FIXED_MARKUP, Decimal("500"), tenant_id="tenant_a")
    # Evaluate for tenant_b — rule should not fire
    result, trace = apply_rules(offer, [rule], "tenant_b")
    assert result.price.agency_markup == Decimal("0")
    assert trace.fired_rules == []


def test_tenant_isolation_rule_fires_for_own_tenant() -> None:
    offer = _offer()
    rule = _rule(RuleActionType.FIXED_MARKUP, Decimal("500"), tenant_id="tenant_a")
    result, _ = apply_rules(offer, [rule], "tenant_a")
    assert result.price.agency_markup == Decimal("500")


# ── 9. Commission (informational only, no price change) ───────────────────────


def test_commission_does_not_change_price() -> None:
    offer = _offer(net_total=Decimal("5000"))
    rule = _rule(RuleActionType.COMMISSION, Decimal("5"))
    result, trace = apply_rules(offer, [rule], "tenant_a")
    assert result.price.agency_markup == Decimal("0")
    assert len(trace.fired_rules) == 1
    assert trace.fired_rules[0].markup_amount == Decimal("0")
    assert trace.fired_rules[0].action_type == RuleActionType.COMMISSION


def test_commission_and_markup_combined() -> None:
    """Commission appears in trace but only the markup changes the price."""
    offer = _offer(net_total=Decimal("5000"))
    rules = [
        _rule(RuleActionType.PERCENTAGE_MARKUP, Decimal("5"), priority=10, name="markup"),
        _rule(RuleActionType.COMMISSION, Decimal("3"), priority=20, name="commission"),
    ]
    result, trace = apply_rules(offer, rules, "tenant_a")
    assert result.price.agency_markup == Decimal("250.00")
    assert len(trace.fired_rules) == 2
    assert trace.total_markup == Decimal("250.00")


# ── 10. Route-specific scope ──────────────────────────────────────────────────


def test_route_scope_fires_for_matching_route() -> None:
    offer = _offer()  # BOM→DEL
    rule = _rule(
        RuleActionType.FIXED_MARKUP,
        Decimal("150"),
        scope=RuleScope(origin="BOM", destination="DEL"),
    )
    result, _ = apply_rules(offer, [rule], "tenant_a")
    assert result.price.agency_markup == Decimal("150")


def test_route_scope_skips_reverse_route() -> None:
    offer = _offer(segment=_segment(origin="DEL", destination="BOM"))
    rule = _rule(
        RuleActionType.FIXED_MARKUP,
        Decimal("150"),
        scope=RuleScope(origin="BOM", destination="DEL"),
    )
    result, _ = apply_rules(offer, [rule], "tenant_a")
    assert result.price.agency_markup == Decimal("0")


def test_origin_only_scope() -> None:
    offer = _offer()  # BOM→DEL
    rule = _rule(RuleActionType.FIXED_MARKUP, Decimal("50"), scope=RuleScope(origin="BOM"))
    result, _ = apply_rules(offer, [rule], "tenant_a")
    assert result.price.agency_markup == Decimal("50")


# ── Inactive rule ─────────────────────────────────────────────────────────────


def test_inactive_rule_does_not_fire() -> None:
    offer = _offer()
    rule = Rule(
        tenant_id="tenant_a",
        name="inactive",
        scope=RuleScope(),
        action=RuleAction(action_type=RuleActionType.FIXED_MARKUP, value=Decimal("500")),
        is_active=False,
    )
    result, trace = apply_rules(offer, [rule], "tenant_a")
    assert result.price.agency_markup == Decimal("0")
    assert trace.fired_rules == []


# ── Cabin class scope ──────────────────────────────────────────────────────────


def test_cabin_scope_matches() -> None:
    seg2 = Segment(
        marketing_carrier=Carrier(iata_code="6E", name="6E"),
        flight_number="6E123",
        origin="BOM",
        destination="DEL",
        departure_at=_DEPART,
        arrival_at=_ARRIVE,
        cabin_class=CabinClass.BUSINESS,
        booking_class="C",
        duration_minutes=120,
    )
    biz_offer = Offer(
        supplier_id="duffel",
        supplier_offer_ref="ref",
        slices=[Slice(segments=[seg2])],
        fare_basis="C",
        cabin_class=CabinClass.BUSINESS,
        price=PriceBreakdown(base_fare=Decimal("20000"), currency="INR"),
        total_inr=Decimal("20000"),
        expires_at=datetime(2025, 6, 14, tzinfo=UTC),
    )
    rule = _rule(
        RuleActionType.FIXED_MARKUP,
        Decimal("1000"),
        scope=RuleScope(cabin_class=CabinClass.BUSINESS),
    )
    result, _ = apply_rules(biz_offer, [rule], "tenant_a")
    assert result.price.agency_markup == Decimal("1000")


def test_cabin_scope_skips_mismatch() -> None:
    offer = _offer(cabin_class=CabinClass.ECONOMY)
    rule = _rule(
        RuleActionType.FIXED_MARKUP,
        Decimal("1000"),
        scope=RuleScope(cabin_class=CabinClass.BUSINESS),
    )
    result, _ = apply_rules(offer, [rule], "tenant_a")
    assert result.price.agency_markup == Decimal("0")


# ── Fare family glob matching ──────────────────────────────────────────────────


def test_fare_family_glob_fires() -> None:
    offer = _offer(fare_family="SUPER_SAVER_V3")
    rule = _rule(
        RuleActionType.FIXED_MARKUP,
        Decimal("50"),
        scope=RuleScope(fare_family="*SAVER*"),
    )
    result, _ = apply_rules(offer, [rule], "tenant_a")
    assert result.price.agency_markup == Decimal("50")


def test_fare_family_glob_skips_no_match() -> None:
    offer = _offer(fare_family="FLEXI_PLUS")
    rule = _rule(
        RuleActionType.FIXED_MARKUP,
        Decimal("50"),
        scope=RuleScope(fare_family="*SAVER*"),
    )
    result, _ = apply_rules(offer, [rule], "tenant_a")
    assert result.price.agency_markup == Decimal("0")


def test_fare_family_scope_skips_offer_without_family() -> None:
    offer = _offer(fare_family=None)
    rule = _rule(
        RuleActionType.FIXED_MARKUP,
        Decimal("50"),
        scope=RuleScope(fare_family="*SAVER*"),
    )
    result, _ = apply_rules(offer, [rule], "tenant_a")
    assert result.price.agency_markup == Decimal("0")


# ── Idempotent application ─────────────────────────────────────────────────────


def test_apply_rules_is_idempotent() -> None:
    """Calling apply_rules twice yields the same result as calling it once."""
    offer = _offer(net_total=Decimal("5000"))
    rule = _rule(RuleActionType.PERCENTAGE_MARKUP, Decimal("5"))
    result1, _ = apply_rules(offer, [rule], "tenant_a")
    result2, _ = apply_rules(result1, [rule], "tenant_a")
    assert result1.price.agency_markup == result2.price.agency_markup == Decimal("250.00")


# ── apply_rules_batch ──────────────────────────────────────────────────────────


def test_apply_rules_batch_returns_parallel_lists() -> None:
    offers = [_offer(net_total=Decimal(str(n * 1000))) for n in range(1, 4)]
    rule = _rule(RuleActionType.FIXED_MARKUP, Decimal("100"))
    ruled, traces = apply_rules_batch(offers, [rule], "tenant_a")
    assert len(ruled) == 3
    assert len(traces) == 3
    for o in ruled:
        assert o.price.agency_markup == Decimal("100")


def test_apply_rules_batch_empty_list() -> None:
    ruled, traces = apply_rules_batch([], [], "tenant_a")
    assert ruled == []
    assert traces == []


# ── Supplier scope ─────────────────────────────────────────────────────────────


def test_supplier_scope_matches() -> None:
    offer = _offer(supplier_id="duffel")
    rule = _rule(
        RuleActionType.FIXED_MARKUP,
        Decimal("75"),
        scope=RuleScope(supplier_id="duffel"),
    )
    result, _ = apply_rules(offer, [rule], "tenant_a")
    assert result.price.agency_markup == Decimal("75")


def test_supplier_scope_skips_other_supplier() -> None:
    offer = _offer(supplier_id="amadeus")
    rule = _rule(
        RuleActionType.FIXED_MARKUP,
        Decimal("75"),
        scope=RuleScope(supplier_id="duffel"),
    )
    result, _ = apply_rules(offer, [rule], "tenant_a")
    assert result.price.agency_markup == Decimal("0")


# ── Original offer is never mutated ───────────────────────────────────────────


def test_original_offer_not_mutated() -> None:
    offer = _offer(net_total=Decimal("5000"))
    original_markup = offer.price.agency_markup
    rule = _rule(RuleActionType.FIXED_MARKUP, Decimal("999"))
    apply_rules(offer, [rule], "tenant_a")
    assert offer.price.agency_markup == original_markup
