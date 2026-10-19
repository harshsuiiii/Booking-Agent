"""
Tests for orchestrator.ranking — filter, rank, explain.
Includes property-based tests with hypothesis and hand-written best-value cases.
"""

from datetime import time
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from booking_agent.domain._types import UNKNOWN
from booking_agent.domain.ranking import RankingPreferences, SortMode
from booking_agent.orchestrator.ranking import (
    FilterConfig,
    RankExplanation,
    RankHighlight,
    explain,
    filter_offers,
    rank,
)
from tests.unit.offer_factory import make_offer

# ── filter_offers ─────────────────────────────────────────────────────────────


class TestFilterMaxStops:
    def test_allows_direct(self):
        direct = make_offer(n_segments=1)
        assert filter_offers([direct], FilterConfig(max_stops=0)) == [direct]

    def test_removes_connecting(self):
        connecting = make_offer(n_segments=2)
        assert filter_offers([connecting], FilterConfig(max_stops=0)) == []

    def test_allows_one_stop_when_limit_is_one(self):
        one_stop = make_offer(n_segments=2)
        assert filter_offers([one_stop], FilterConfig(max_stops=1)) == [one_stop]

    def test_no_limit_keeps_all(self):
        offers = [make_offer(n_segments=i) for i in range(1, 4)]
        assert filter_offers(offers, FilterConfig()) == offers


class TestFilterLayover:
    def test_max_layover_removes_long_layover(self):
        long_layover = make_offer(n_segments=2, layover_minutes=200)
        assert filter_offers([long_layover], FilterConfig(max_layover_minutes=180)) == []

    def test_max_layover_keeps_short_layover(self):
        short = make_offer(n_segments=2, layover_minutes=90)
        assert filter_offers([short], FilterConfig(max_layover_minutes=180)) == [short]

    def test_no_overnight_removes_long_layover(self):
        # 480 min = exactly overnight threshold
        overnight = make_offer(n_segments=2, layover_minutes=480)
        assert filter_offers([overnight], FilterConfig(no_overnight_layover=True)) == []

    def test_no_overnight_keeps_short_layover(self):
        short = make_offer(n_segments=2, layover_minutes=120)
        assert filter_offers([short], FilterConfig(no_overnight_layover=True)) == [short]

    def test_direct_flight_passes_overnight_filter(self):
        direct = make_offer(n_segments=1)
        assert filter_offers([direct], FilterConfig(no_overnight_layover=True)) == [direct]


class TestFilterBags:
    def test_no_bag_excluded(self):
        no_bag = make_offer(checked_pieces=0)
        assert filter_offers([no_bag], FilterConfig(bags_included=True)) == []

    def test_none_bag_excluded(self):
        none_bag = make_offer(checked_pieces=None)
        assert filter_offers([none_bag], FilterConfig(bags_included=True)) == []

    def test_unknown_bag_excluded(self):
        unk = make_offer(checked_pieces=UNKNOWN)
        assert filter_offers([unk], FilterConfig(bags_included=True)) == []

    def test_one_bag_passes(self):
        with_bag = make_offer(checked_pieces=1)
        assert filter_offers([with_bag], FilterConfig(bags_included=True)) == [with_bag]

    def test_bags_filter_false_keeps_all(self):
        offers = [make_offer(checked_pieces=p) for p in [0, 1, UNKNOWN]]
        assert filter_offers(offers, FilterConfig(bags_included=False)) == offers


class TestFilterRefundable:
    def test_non_refundable_excluded(self):
        nr = make_offer(refundable=False)
        assert filter_offers([nr], FilterConfig(refundable_only=True)) == []

    def test_unknown_refundable_excluded(self):
        unk = make_offer(refundable=UNKNOWN)
        assert filter_offers([unk], FilterConfig(refundable_only=True)) == []

    def test_refundable_passes(self):
        r = make_offer(refundable=True)
        assert filter_offers([r], FilterConfig(refundable_only=True)) == [r]


class TestFilterAirlines:
    def test_preferred_airline_passes(self):
        ai = make_offer(iata_code="AI")
        assert filter_offers([ai], FilterConfig(preferred_airlines=["AI"])) == [ai]

    def test_other_airline_excluded(self):
        sg = make_offer(iata_code="SG")
        assert filter_offers([sg], FilterConfig(preferred_airlines=["AI"])) == []

    def test_empty_preferred_keeps_all(self):
        offers = [make_offer(iata_code="AI"), make_offer(iata_code="SG")]
        assert filter_offers(offers, FilterConfig()) == offers


class TestFilterDepartureTime:
    def test_before_window_excluded(self):
        # departure at 06:00 UTC; window starts at 08:00
        offer = make_offer()  # departs 06:00
        cfg = FilterConfig(departure_not_before=time(8, 0))
        assert filter_offers([offer], cfg) == []

    def test_after_window_excluded(self):
        offer = make_offer()  # departs 06:00
        cfg = FilterConfig(departure_not_after=time(5, 0))
        assert filter_offers([offer], cfg) == []

    def test_inside_window_passes(self):
        offer = make_offer()  # departs 06:00
        cfg = FilterConfig(departure_not_before=time(5, 0), departure_not_after=time(20, 0))
        assert filter_offers([offer], cfg) == [offer]


class TestFilterMaxPrice:
    def test_above_max_excluded(self):
        expensive = make_offer(total_inr=Decimal("10000"))
        assert filter_offers([expensive], FilterConfig(max_price_inr=Decimal("5000"))) == []

    def test_at_max_passes(self):
        offer = make_offer(total_inr=Decimal("5000"))
        assert filter_offers([offer], FilterConfig(max_price_inr=Decimal("5000"))) == [offer]


# ── rank ──────────────────────────────────────────────────────────────────────


class TestRankCheapest:
    def test_cheapest_first(self):
        offers = [
            make_offer(total_inr=Decimal("5000"), flight_number="202"),
            make_offer(total_inr=Decimal("3000"), flight_number="101"),
            make_offer(total_inr=Decimal("4000"), flight_number="303"),
        ]
        prefs = RankingPreferences(sort_mode=SortMode.CHEAPEST)
        ranked = rank(offers, prefs)
        assert ranked[0].total_inr == Decimal("3000")
        assert ranked[2].total_inr == Decimal("5000")

    def test_tiebreak_fewer_stops(self):
        direct = make_offer(total_inr=Decimal("3000"), n_segments=1, flight_number="101")
        one_stop = make_offer(total_inr=Decimal("3000"), n_segments=2, flight_number="202")
        prefs = RankingPreferences(sort_mode=SortMode.CHEAPEST)
        ranked = rank([one_stop, direct], prefs)
        assert ranked[0].slices[0].stops == 0


class TestRankFastest:
    def test_fastest_first(self):
        slow = make_offer(duration_minutes=300, flight_number="101")
        fast = make_offer(duration_minutes=120, flight_number="202")
        prefs = RankingPreferences(sort_mode=SortMode.FASTEST)
        ranked = rank([slow, fast], prefs)
        assert ranked[0].slices[0].duration_minutes == 120

    def test_long_layover_ranks_after_direct_of_similar_air_time(self):
        # 1-stop: 180 min in the air but a 5 h layover → 480 min door to door.
        one_stop = make_offer(
            n_segments=2, duration_minutes=180, layover_minutes=300, flight_number="303"
        )
        # Direct: slightly more air time (200 min) but no layover.
        direct = make_offer(duration_minutes=200, flight_number="404")
        assert one_stop.slices[0].duration_minutes < direct.slices[0].duration_minutes
        prefs = RankingPreferences(sort_mode=SortMode.FASTEST)
        ranked = rank([one_stop, direct], prefs)
        assert [o.offer_id for o in ranked] == [direct.offer_id, one_stop.offer_id]


class TestRankBestValue:
    def test_cheapest_not_always_best_value(self):
        # Cheapest flight has no checked bag → penalty applied.
        # Slightly pricier flight includes a bag → better effective price.
        no_bag = make_offer(
            total_inr=Decimal("3000"),
            checked_pieces=0,
            flight_number="101",
        )
        with_bag = make_offer(
            total_inr=Decimal("3500"),
            checked_pieces=1,
            flight_number="202",
        )
        prefs = RankingPreferences(
            sort_mode=SortMode.BEST_VALUE,
            no_bag_penalty=Decimal("1500"),
        )
        ranked = rank([no_bag, with_bag], prefs)
        # no_bag effective = 3000 + 1500 = 4500 > with_bag effective = 3500
        assert ranked[0].baggage.checked_pieces == 1

    def test_non_refundable_penalised(self):
        non_ref = make_offer(
            total_inr=Decimal("3000"),
            refundable=False,
            flight_number="101",
        )
        refundable = make_offer(
            total_inr=Decimal("3400"),
            refundable=True,
            flight_number="202",
        )
        prefs = RankingPreferences(
            sort_mode=SortMode.BEST_VALUE,
            no_refund_penalty=Decimal("600"),
        )
        ranked = rank([non_ref, refundable], prefs)
        # non_ref effective = 3000 + 600 = 3600 > refundable = 3400
        assert ranked[0].fare_rules.refund.allowed is True

    def test_unknown_bag_less_penalty_than_no_bag(self):
        no_bag = make_offer(total_inr=Decimal("2000"), checked_pieces=0, flight_number="101")
        unk_bag = make_offer(total_inr=Decimal("2000"), checked_pieces=UNKNOWN, flight_number="202")
        prefs = RankingPreferences(
            sort_mode=SortMode.BEST_VALUE,
            no_bag_penalty=Decimal("1500"),
            unknown_bag_penalty=Decimal("750"),
        )
        ranked = rank([no_bag, unk_bag], prefs)
        assert ranked[0].baggage.checked_pieces == UNKNOWN


# ── explain ───────────────────────────────────────────────────────────────────


class TestExplain:
    def _ranked(self, offers, sort_mode=SortMode.BEST_VALUE):
        prefs = RankingPreferences(sort_mode=sort_mode)
        return rank(offers, prefs), prefs

    def test_returns_rank_explanation(self):
        offer = make_offer()
        ranked, prefs = self._ranked([offer])
        ex = explain(offer, prefs, rank_position=1, all_offers=ranked)
        assert isinstance(ex, RankExplanation)

    def test_rank_position_set(self):
        offer = make_offer()
        ranked, prefs = self._ranked([offer])
        ex = explain(offer, prefs, rank_position=2, all_offers=ranked)
        assert ex.rank_position == 2

    def test_direct_flight_highlight(self):
        direct = make_offer(n_segments=1)
        ranked, prefs = self._ranked([direct])
        ex = explain(direct, prefs, rank_position=1, all_offers=ranked)
        assert RankHighlight.DIRECT_FLIGHT in ex.highlights

    def test_bags_included_highlight(self):
        with_bag = make_offer(checked_pieces=1)
        ranked, prefs = self._ranked([with_bag])
        ex = explain(with_bag, prefs, rank_position=1, all_offers=ranked)
        assert RankHighlight.BAGS_INCLUDED in ex.highlights

    def test_no_bags_no_highlight(self):
        no_bag = make_offer(checked_pieces=0)
        ranked, prefs = self._ranked([no_bag])
        ex = explain(no_bag, prefs, rank_position=1, all_offers=ranked)
        assert RankHighlight.BAGS_INCLUDED not in ex.highlights

    def test_cheapest_highlight_on_lowest_price(self):
        cheap = make_offer(total_inr=Decimal("2000"), flight_number="101")
        expensive = make_offer(total_inr=Decimal("5000"), flight_number="202")
        ranked, prefs = self._ranked([cheap, expensive], SortMode.CHEAPEST)
        ex = explain(cheap, prefs, rank_position=1, all_offers=ranked)
        assert RankHighlight.CHEAPEST in ex.highlights

    def test_fastest_highlight_uses_journey_time(self):
        one_stop = make_offer(
            n_segments=2, duration_minutes=180, layover_minutes=300, flight_number="303"
        )
        direct = make_offer(duration_minutes=200, flight_number="404")
        ranked, prefs = self._ranked([one_stop, direct], SortMode.FASTEST)
        ex_direct = explain(direct, prefs, rank_position=1, all_offers=ranked)
        ex_one_stop = explain(one_stop, prefs, rank_position=2, all_offers=ranked)
        assert RankHighlight.FASTEST in ex_direct.highlights
        assert RankHighlight.FASTEST not in ex_one_stop.highlights
        assert ex_one_stop.total_duration_minutes == 480

    def test_most_flexible_highlight(self):
        flexible = make_offer(refundable=True, changeable=True)
        ranked, prefs = self._ranked([flexible])
        ex = explain(flexible, prefs, rank_position=1, all_offers=ranked)
        assert RankHighlight.MOST_FLEXIBLE in ex.highlights

    def test_effective_price_in_explanation(self):
        # refundable=True + changeable=True so only the bag penalty applies
        no_bag = make_offer(
            checked_pieces=0,
            total_inr=Decimal("3000"),
            refundable=True,
            changeable=True,
        )
        ranked, prefs = self._ranked([no_bag])
        ex = explain(no_bag, prefs, rank_position=1, all_offers=ranked)
        # effective_price = 3000 + no_bag_penalty (1500)
        assert ex.effective_price_inr == Decimal("4500")

    def test_penalty_in_breakdown(self):
        no_bag = make_offer(checked_pieces=0, total_inr=Decimal("3000"))
        ranked, prefs = self._ranked([no_bag])
        ex = explain(no_bag, prefs, rank_position=1, all_offers=ranked)
        reasons = [a.reason for a in ex.price_breakdown.adjustments]
        assert any("bag" in r.lower() for r in reasons)


# ── property-based tests ──────────────────────────────────────────────────────

_price_st = st.decimals(
    min_value=Decimal("500"),
    max_value=Decimal("50000"),
    places=2,
    allow_nan=False,
    allow_infinity=False,
)
_sort_mode_st = st.sampled_from(list(SortMode))
_flight_st = st.sampled_from(["101", "202", "303", "404"])
_fare_st = st.sampled_from(["YOWCHIN", "KFLX", "TSAVER"])
_bag_st = st.sampled_from([0, 1, None, UNKNOWN])
_refund_st = st.sampled_from([True, False, UNKNOWN])


@st.composite
def offer_list(draw, min_size=0, max_size=10):
    n = draw(st.integers(min_value=min_size, max_value=max_size))
    return [
        make_offer(
            total_inr=draw(_price_st),
            flight_number=draw(_flight_st),
            fare_basis=draw(_fare_st),
            checked_pieces=draw(_bag_st),
            refundable=draw(_refund_st),
        )
        for _ in range(n)
    ]


@given(offer_list(), _sort_mode_st)
@settings(max_examples=200)
def test_rank_deterministic(offers, sort_mode):
    prefs = RankingPreferences(sort_mode=sort_mode)
    r1 = [o.offer_id for o in rank(offers, prefs)]
    r2 = [o.offer_id for o in rank(offers, prefs)]
    assert r1 == r2


@given(offer_list(), _sort_mode_st)
@settings(max_examples=200)
def test_rank_stable(offers, sort_mode):
    """Re-ranking an already-ranked list yields the same order."""
    prefs = RankingPreferences(sort_mode=sort_mode)
    ranked = rank(offers, prefs)
    assert [o.offer_id for o in rank(ranked, prefs)] == [o.offer_id for o in ranked]


@given(offer_list(), _sort_mode_st)
@settings(max_examples=200)
def test_rank_length_preserved(offers, sort_mode):
    prefs = RankingPreferences(sort_mode=sort_mode)
    assert len(rank(offers, prefs)) == len(offers)


@given(offer_list())
@settings(max_examples=200)
def test_filters_never_add_offers(offers):
    cfg = FilterConfig(max_stops=1, bags_included=False)
    assert len(filter_offers(offers, cfg)) <= len(offers)


@given(offer_list())
@settings(max_examples=200)
def test_filters_subset_of_input(offers):
    cfg = FilterConfig(max_price_inr=Decimal("10000"), refundable_only=False)
    filtered = filter_offers(offers, cfg)
    original_ids = {o.offer_id for o in offers}
    for o in filtered:
        assert o.offer_id in original_ids


@given(offer_list(min_size=1), _sort_mode_st)
@settings(max_examples=100)
def test_explain_rank_position_matches_rank(offers, sort_mode):
    prefs = RankingPreferences(sort_mode=sort_mode)
    ranked = rank(offers, prefs)
    for pos, offer in enumerate(ranked, start=1):
        ex = explain(offer, prefs, rank_position=pos, all_offers=ranked)
        assert ex.rank_position == pos
