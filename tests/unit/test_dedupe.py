"""
Tests for orchestrator.dedupe — pure functions, no I/O.
Includes hand-written cases and property-based tests with hypothesis.
"""

from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from booking_agent.orchestrator.dedupe import AlternateSource, DedupeGroup, dedupe
from tests.unit.offer_factory import make_offer


# ── hand-written cases ────────────────────────────────────────────────────────

class TestDedupeBasic:
    def test_empty_input(self):
        assert dedupe([]) == []

    def test_single_offer_no_alternates(self):
        offer = make_offer()
        groups = dedupe([offer])
        assert len(groups) == 1
        assert groups[0].primary is offer
        assert groups[0].alternates == []

    def test_two_identical_itinerary_different_suppliers(self):
        # Same flight, same fare → same dedupe_key
        a = make_offer(supplier_id="amadeus", total_inr=Decimal("3000"))
        b = make_offer(supplier_id="sabre",   total_inr=Decimal("3200"))
        groups = dedupe([a, b])
        assert len(groups) == 1
        grp = groups[0]
        assert grp.primary.supplier_id == "amadeus"   # cheaper is primary
        assert len(grp.alternates) == 1
        assert grp.alternates[0].supplier_id == "sabre"
        assert grp.alternates[0].total_inr == Decimal("3200")

    def test_cheaper_supplier_wins_primary(self):
        expensive = make_offer(supplier_id="amadeus", total_inr=Decimal("5000"))
        cheap     = make_offer(supplier_id="sabre",   total_inr=Decimal("2500"))
        groups = dedupe([expensive, cheap])
        assert groups[0].primary.supplier_id == "sabre"

    def test_two_different_itineraries_two_groups(self):
        a = make_offer(flight_number="101", total_inr=Decimal("3000"))
        b = make_offer(flight_number="202", total_inr=Decimal("3500"))
        groups = dedupe([a, b])
        assert len(groups) == 2

    def test_three_suppliers_same_flight(self):
        a = make_offer(supplier_id="amadeus", total_inr=Decimal("3000"))
        b = make_offer(supplier_id="sabre",   total_inr=Decimal("3100"))
        c = make_offer(supplier_id="gds3",    total_inr=Decimal("2900"))
        groups = dedupe([a, b, c])
        assert len(groups) == 1
        grp = groups[0]
        assert grp.primary.supplier_id == "gds3"
        assert len(grp.alternates) == 2

    def test_alternate_source_fields(self):
        a = make_offer(supplier_id="amadeus", total_inr=Decimal("3000"))
        b = make_offer(supplier_id="sabre",   total_inr=Decimal("3200"))
        grp = dedupe([a, b])[0]
        alt = grp.alternates[0]
        assert isinstance(alt, AlternateSource)
        assert alt.offer_id == b.offer_id
        assert alt.supplier_offer_ref == b.supplier_offer_ref

    def test_dedupe_key_on_group(self):
        offer = make_offer()
        grp = dedupe([offer])[0]
        assert grp.dedupe_key == offer.dedupe_key

    def test_cheapest_inr_property(self):
        a = make_offer(total_inr=Decimal("2800"))
        b = make_offer(total_inr=Decimal("3200"))
        grp = dedupe([a, b])[0]
        assert grp.cheapest_inr == Decimal("2800")

    def test_all_supplier_ids(self):
        a = make_offer(supplier_id="amadeus")
        b = make_offer(supplier_id="sabre")
        c = make_offer(supplier_id="gds3")
        grp = dedupe([a, b, c])[0]
        assert set(grp.all_supplier_ids) == {"amadeus", "sabre", "gds3"}

    def test_stable_tiebreak_by_offer_id(self):
        # Two offers with identical price — output should be deterministic
        a = make_offer(supplier_id="amadeus", total_inr=Decimal("3000"))
        b = make_offer(supplier_id="sabre",   total_inr=Decimal("3000"))
        g1 = dedupe([a, b])
        g2 = dedupe([b, a])
        assert g1[0].primary.offer_id == g2[0].primary.offer_id

    def test_different_fare_basis_different_groups(self):
        a = make_offer(fare_basis="YOWCHIN")
        b = make_offer(fare_basis="KFLX")
        groups = dedupe([a, b])
        assert len(groups) == 2


# ── property-based tests ──────────────────────────────────────────────────────

_price_st = st.decimals(
    min_value=Decimal("500"),
    max_value=Decimal("50000"),
    places=2,
    allow_nan=False,
    allow_infinity=False,
)

_supplier_st = st.sampled_from(["amadeus", "sabre", "gds3", "travelport"])
_flight_st   = st.sampled_from(["101", "202", "303"])
_fare_st     = st.sampled_from(["YOWCHIN", "KFLX", "TSAVER"])


@st.composite
def offer_list(draw: st.DrawFn, min_size: int = 0, max_size: int = 12):
    n = draw(st.integers(min_value=min_size, max_value=max_size))
    return [
        make_offer(
            total_inr=draw(_price_st),
            supplier_id=draw(_supplier_st),
            flight_number=draw(_flight_st),
            fare_basis=draw(_fare_st),
        )
        for _ in range(n)
    ]


@given(offer_list())
@settings(max_examples=200)
def test_dedupe_idempotent(offers):
    """Deduping the primaries of a deduped set returns one group per primary."""
    groups = dedupe(offers)
    primaries = [g.primary for g in groups]
    groups2 = dedupe(primaries)
    assert len(groups2) == len(groups)
    for g in groups2:
        assert g.alternates == []


@given(offer_list())
@settings(max_examples=200)
def test_dedupe_no_offers_lost(offers):
    """Total offer count is preserved across groups."""
    groups = dedupe(offers)
    total = sum(1 + len(g.alternates) for g in groups)
    assert total == len(offers)


@given(offer_list())
@settings(max_examples=200)
def test_primary_always_cheapest(offers):
    """Primary must have the lowest (or tied-lowest) total_inr in its group."""
    groups = dedupe(offers)
    for grp in groups:
        for alt in grp.alternates:
            assert grp.primary.total_inr <= alt.total_inr


@given(offer_list())
@settings(max_examples=200)
def test_dedupe_deterministic(offers):
    """Same input always produces same group structure."""
    g1 = [(g.dedupe_key, g.primary.offer_id) for g in dedupe(offers)]
    g2 = [(g.dedupe_key, g.primary.offer_id) for g in dedupe(offers)]
    assert g1 == g2


@given(offer_list(min_size=1))
@settings(max_examples=200)
def test_groups_cover_all_keys(offers):
    """Every dedupe_key in input appears exactly once in output."""
    expected_keys = {o.dedupe_key for o in offers}
    actual_keys   = {g.dedupe_key for g in dedupe(offers)}
    assert expected_keys == actual_keys
