"""
Journey types (one way / return / multi-city), per-flight time of day, and passenger mix.

Covers the search request contract, filtering by slice, the cache key, the Duffel payload,
the simulated suppliers, the orchestrator skipping suppliers without multi-city, and the
passenger checks made when a booking is prepared. No network.
"""

import json
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from pydantic import ValidationError

from booking_agent.api.models import SearchRequest
from booking_agent.cache.search_cache import _make_key
from booking_agent.connectors.duffel import DuffelConnector
from booking_agent.connectors.duffel.mapper import map_offer
from booking_agent.connectors.duffel_fx import FixedFxProvider
from booking_agent.connectors.fake import FakeConnector
from booking_agent.connectors.secrets import EnvSecretsProvider
from booking_agent.domain.booking import Passenger, PassengerType, passenger_mix_errors
from booking_agent.domain.common import CabinClass, Carrier
from booking_agent.domain.itinerary import Segment, Slice
from booking_agent.domain.search import JourneyType, PaxCounts, SearchQuery, TimeWindow
from booking_agent.orchestrator.events import SearchComplete, SourceSkipped
from booking_agent.orchestrator.ranking import FilterConfig, filter_config_from, filter_offers
from booking_agent.orchestrator.search import SearchOrchestrator
from booking_agent.simulated.connector import SimulatedConnector, _query_from_ref
from booking_agent.simulated.pricing import PROFILES
from tests.unit.offer_factory import make_offer

DAY = date.today() + timedelta(days=30)


def leg(o: str, d: str, day: date = DAY, window: str = "any") -> dict[str, Any]:
    return {"origin": o, "destination": d, "departure_date": day.isoformat(), "time_window": window}


def multi(*legs: dict[str, Any], **kw: Any) -> SearchQuery:
    return SearchQuery.model_validate({"journey_type": "multi_city", "legs": list(legs), **kw})


# ── request contract ──────────────────────────────────────────────────────────


class TestSearchRequest:
    def test_one_way_from_legs(self):
        q = SearchRequest.model_validate({"legs": [leg("DEL", "BOM")]}).to_query()
        assert q.journey_type == JourneyType.ONE_WAY
        assert (q.origin, q.destination, q.departure_date, q.return_date) == (
            "DEL",
            "BOM",
            DAY,
            None,
        )

    def test_return_from_legs_fills_return_date(self):
        back = DAY + timedelta(days=5)
        q = SearchRequest.model_validate(
            {"journey_type": "return", "legs": [leg("DEL", "BOM"), leg("BOM", "DEL", back)]}
        ).to_query()
        assert q.journey_type == JourneyType.RETURN
        assert q.return_date == back

    def test_same_day_return_is_allowed(self):
        q = SearchQuery(origin="DEL", destination="BOM", departure_date=DAY, return_date=DAY)
        assert q.journey_type == JourneyType.RETURN

    def test_flat_form_still_works_and_builds_legs(self):
        q = SearchRequest.model_validate(
            {
                "origin": "DEL",
                "destination": "BOM",
                "departure_date": DAY.isoformat(),
                "return_date": (DAY + timedelta(days=3)).isoformat(),
            }
        ).to_query()
        assert q.journey_type == JourneyType.RETURN
        assert [(lg.origin, lg.destination) for lg in q.legs] == [("DEL", "BOM"), ("BOM", "DEL")]

    def test_multi_city_keeps_every_leg(self):
        q = multi(
            leg("DEL", "BOM"),
            leg("BOM", "GOI", DAY + timedelta(days=2)),
            leg("GOI", "DEL", DAY + timedelta(days=4)),
        )
        assert len(q.legs) == 3
        assert q.return_date is None  # only a return journey has one

    def test_two_mirrored_legs_infer_return(self):
        q = SearchQuery.model_validate(
            {"legs": [leg("DEL", "BOM"), leg("BOM", "DEL", DAY + timedelta(days=1))]}
        )
        assert q.journey_type == JourneyType.RETURN

    @pytest.mark.parametrize(
        ("body", "message"),
        [
            (
                {"journey_type": "return", "legs": [leg("DEL", "BOM"), leg("BOM", "GOI")]},
                "there and back",
            ),
            ({"journey_type": "multi_city", "legs": [leg("DEL", "BOM")]}, "at least two legs"),
            (
                {"journey_type": "one_way", "legs": [leg("DEL", "BOM"), leg("BOM", "GOI")]},
                "exactly one leg",
            ),
            (
                {"legs": [leg("DEL", "BOM"), leg("BOM", "GOI", DAY - timedelta(days=1))]},
                "before the previous",
            ),
            ({"legs": [leg("DEL", "BOM", date.today() - timedelta(days=1))]}, "past"),
            ({"legs": [leg("DEL", "DEL")]}, "same"),
            ({"legs": [leg("DEL", "BOM", DAY + timedelta(days=i)) for i in range(7)]}, "at most 6"),
            ({}, "send legs"),
        ],
    )
    def test_invalid_journeys_are_rejected(self, body, message):
        with pytest.raises(ValidationError, match=message):
            SearchRequest.model_validate(body)

    def test_at_most_nine_passengers(self):
        with pytest.raises(ValidationError, match="at most 9"):
            PaxCounts(adults=5, children=5)


# ── time of day and stops, per slice ──────────────────────────────────────────


def _slice(o: str, d: str, dep: datetime, stops: int = 0) -> Slice:
    segs, t, here = [], dep, o
    for i in range(stops + 1):
        to = d if i == stops else f"H{chr(65 + i)}B"
        segs.append(
            Segment(
                marketing_carrier=Carrier(iata_code="AI", name="Air India"),
                flight_number=str(100 + i),
                origin=here,
                destination=to,
                departure_at=t,
                arrival_at=t + timedelta(hours=1),
                cabin_class=CabinClass.ECONOMY,
                booking_class="Y",
                duration_minutes=60,
            )
        )
        t, here = t + timedelta(hours=2), to
    return Slice(segments=segs)


def _round_trip(out_hour: int, back_hour: int, back_stops: int = 0):
    out = datetime(2027, 1, 10, out_hour, 0, tzinfo=UTC)
    back = datetime(2027, 1, 15, back_hour, 0, tzinfo=UTC)
    return make_offer().model_copy(
        update={"slices": [_slice("DEL", "BOM", out), _slice("BOM", "DEL", back, back_stops)]}
    )


class TestSliceFilters:
    def test_time_window_bounds(self):
        assert TimeWindow.ANY.bounds is None
        assert TimeWindow.MORNING.bounds == (time(6, 0), time(11, 59))
        assert TimeWindow.EVENING.bounds == (time(18, 0), time(23, 59))

    def test_each_slice_has_its_own_window(self):
        q = SearchQuery.model_validate(
            {
                "legs": [
                    leg("DEL", "BOM", window="morning"),
                    leg("BOM", "DEL", DAY + timedelta(days=5), "evening"),
                ]
            }
        )
        cfg = filter_config_from(q.filters, q.legs)
        assert filter_offers([_round_trip(8, 19)], cfg)
        assert not filter_offers([_round_trip(8, 9)], cfg)  # return leaves in the morning
        assert not filter_offers([_round_trip(14, 19)], cfg)  # outbound leaves in the afternoon

    def test_any_time_does_not_filter(self):
        q = SearchQuery.model_validate({"legs": [leg("DEL", "BOM")]})
        assert filter_offers([_round_trip(3, 23)], filter_config_from(q.filters, q.legs))

    def test_max_stops_applies_to_each_flight(self):
        one_stop_each = make_offer().model_copy(
            update={
                "slices": [
                    _slice("DEL", "BOM", datetime(2027, 1, 10, 8, tzinfo=UTC), 1),
                    _slice("BOM", "DEL", datetime(2027, 1, 15, 8, tzinfo=UTC), 1),
                ]
            }
        )
        assert filter_offers([one_stop_each], FilterConfig(max_stops=1)) == [one_stop_each]
        assert filter_offers([one_stop_each], FilterConfig(max_stops=0)) == []


# ── cache key ─────────────────────────────────────────────────────────────────


class TestCacheKey:
    def test_key_covers_every_leg_and_window(self):
        base = multi(leg("DEL", "BOM"), leg("BOM", "GOI", DAY + timedelta(days=2)))
        other_leg = multi(leg("DEL", "BOM"), leg("BOM", "CCU", DAY + timedelta(days=2)))
        other_time = multi(leg("DEL", "BOM"), leg("BOM", "GOI", DAY + timedelta(days=2), "evening"))
        one_way = SearchQuery.model_validate({"legs": [leg("DEL", "BOM")]})
        keys = {_make_key(q, "t", ["a"]) for q in (base, other_leg, other_time, one_way)}
        assert len(keys) == 4
        assert _make_key(base, "t", ["a"]) == _make_key(base.model_copy(), "t", ["a"])


# ── Duffel ────────────────────────────────────────────────────────────────────


def _duffel(monkeypatch) -> DuffelConnector:
    monkeypatch.setenv("DUFFEL_ACCESS_TOKEN", "duffel_test_xyz123")
    return DuffelConnector(
        secrets=EnvSecretsProvider(),
        fx_provider=FixedFxProvider({"GBP": Decimal("105"), "USD": Decimal("83")}),
        max_retries=1,
        retry_min_wait=0.0,
        retry_max_wait=0.0,
    )


class TestDuffelJourneys:
    @respx.mock
    async def test_multi_city_sends_one_slice_per_leg_with_times(self, monkeypatch):
        route = respx.post("https://api.duffel.com/air/offer_requests").mock(
            return_value=httpx.Response(200, json={"data": {"id": "orq_1", "offers": []}})
        )
        q = multi(
            leg("LHR", "JFK", window="morning"),
            leg("JFK", "LAX", DAY + timedelta(days=3)),
            pax_counts={"adults": 2, "children": 1, "infants": 1},
        )
        async with _duffel(monkeypatch) as c:
            await c.search(q)
        data = json.loads(route.calls[0].request.content)["data"]
        assert [(s["origin"], s["destination"]) for s in data["slices"]] == [
            ("LHR", "JFK"),
            ("JFK", "LAX"),
        ]
        assert data["slices"][0]["departure_time"] == {"from": "06:00", "to": "11:59"}
        assert "departure_time" not in data["slices"][1]
        assert [p["type"] for p in data["passengers"]] == [
            "adult",
            "adult",
            "child",
            "infant_without_seat",
        ]

    def test_duffel_supports_multi_city(self, monkeypatch):
        assert _duffel(monkeypatch).capabilities.supports_multi_city

    def test_mapper_orders_passengers_and_reads_the_mix(self):
        raw = json.loads(
            Path("tests/fixtures/duffel/offer_request_response.json").read_text(encoding="utf-8")
        )["data"]["offers"][0]
        raw["passengers"] = [
            {"id": "pas_inf", "type": "infant_without_seat"},
            {"id": "pas_chd", "type": "child"},
            {"id": "pas_adt", "type": "adult"},
        ]
        offer = map_offer(raw, datetime.now(UTC), Decimal("1"), datetime.now(UTC))
        assert offer.supplier_passenger_ids == ["pas_adt", "pas_chd", "pas_inf"]
        assert offer.pax_counts == PaxCounts(adults=1, children=1, infants=1)

    def test_book_pairs_passengers_by_type_and_links_the_infant(self):
        def pax(t: PassengerType, name: str, born: date) -> Passenger:
            return Passenger(pax_type=t, given_name=name, family_name="Rao", date_of_birth=born)

        mapped = DuffelConnector._map_passengers(
            [
                pax(PassengerType.INFANT, "Baby", date(2026, 1, 1)),
                pax(PassengerType.ADULT, "Mum", date(1990, 1, 1)),
            ],
            ["pas_adt", "pas_inf"],
        )
        by_id = {m["id"]: m for m in mapped}
        assert by_id["pas_adt"]["given_name"] == "Mum"
        assert by_id["pas_inf"]["given_name"] == "Baby"
        assert by_id["pas_adt"]["infant_passenger_id"] == "pas_inf"


# ── simulated suppliers ───────────────────────────────────────────────────────


def _sim(sid: str = "sim_gds") -> SimulatedConnector:
    return SimulatedConnector(
        PROFILES[sid], latency_s=(0, 0), failure_rate=0.0, price_change_rate=0.0
    )


MULTI = (
    leg("DEL", "BOM"),
    leg("BOM", "DXB", DAY + timedelta(days=2)),
    leg("DXB", "DEL", DAY + timedelta(days=5)),
)


class TestSimulatedJourneys:
    def test_multi_city_offers_have_one_slice_per_leg(self):
        offers = _sim().generate_offers(multi(*MULTI))
        assert offers
        for o in offers:
            routes = [(s.segments[0].origin, s.segments[-1].destination) for s in o.slices]
            assert routes == [("DEL", "BOM"), ("BOM", "DXB"), ("DXB", "DEL")]

    def test_multi_city_is_deterministic(self):
        a = [(o.supplier_offer_ref, o.total_inr) for o in _sim().generate_offers(multi(*MULTI))]
        b = [(o.supplier_offer_ref, o.total_inr) for o in _sim().generate_offers(multi(*MULTI))]
        assert a == b

    def test_reference_rebuilds_the_search(self):
        q = multi(*MULTI, pax_counts={"adults": 2, "children": 1})
        offer = _sim().generate_offers(q)[0]
        back = _query_from_ref(offer.supplier_offer_ref)
        assert back.journey_type == JourneyType.MULTI_CITY
        assert [(lg.origin, lg.destination, lg.departure_date) for lg in back.legs] == [
            (lg.origin, lg.destination, lg.departure_date) for lg in q.legs
        ]
        assert back.pax_counts == q.pax_counts

    async def test_multi_city_offer_reprices(self):
        offer = _sim().generate_offers(multi(*MULTI))[0]
        priced = await _sim().price(offer)
        assert priced.total_inr == offer.total_inr

    def test_next_flight_leaves_at_least_two_hours_after_landing(self):
        q = multi(leg("DEL", "BOM"), leg("BOM", "GOI"))  # same day
        for o in _sim().generate_offers(q):
            assert o.slices[1].departure_at >= o.slices[0].arrival_at + timedelta(hours=2)

    def test_return_trip_stays_on_one_airline(self):
        q = SearchQuery(
            origin="DEL", destination="BOM", departure_date=DAY, return_date=DAY + timedelta(days=4)
        )
        for o in _sim().generate_offers(q):
            carriers = {s.segments[0].marketing_carrier.iata_code for s in o.slices}
            assert len(carriers) == 1

    def test_offer_records_the_passenger_mix(self):
        q = SearchQuery(
            origin="DEL",
            destination="BOM",
            departure_date=DAY,
            pax_counts=PaxCounts(adults=2, children=1, infants=1),
        )
        offer = _sim().generate_offers(q)[0]
        assert offer.pax_count == 4
        assert offer.pax_counts == PaxCounts(adults=2, children=1, infants=1)


# ── orchestrator ──────────────────────────────────────────────────────────────


class TestOrchestratorJourneys:
    async def test_supplier_without_multi_city_is_skipped_not_failed(self):
        fake = FakeConnector(
            supplier_id="fake", max_retries=1, retry_min_wait=0.0, retry_max_wait=0.0
        )
        async with SearchOrchestrator([fake, _sim()]) as orch:
            events = [e async for e in orch.search(multi(*MULTI))]
        skipped = [e for e in events if isinstance(e, SourceSkipped)]
        assert [(e.supplier_id, e.reason) for e in skipped] == [("fake", "multi_city_unsupported")]
        done = next(e for e in events if isinstance(e, SearchComplete))
        assert "fake" not in done.suppliers_failed
        assert "sim_gds" in done.suppliers_succeeded


# ── passenger mix at booking ──────────────────────────────────────────────────


TRAVEL = date(2027, 6, 1)


class TestPassengerMix:
    def test_matching_mix_and_ages_pass(self):
        pax = [
            (PassengerType.ADULT, date(1990, 1, 1)),
            (PassengerType.CHILD, date(2020, 1, 1)),
            (PassengerType.INFANT, date(2026, 3, 1)),
        ]
        assert (
            passenger_mix_errors(pax, PaxCounts(adults=1, children=1, infants=1), TRAVEL, TRAVEL)
            == []
        )

    def test_wrong_mix_is_reported(self):
        pax = [(PassengerType.ADULT, date(1990, 1, 1)), (PassengerType.CHILD, date(2020, 1, 1))]
        errors = passenger_mix_errors(pax, PaxCounts(adults=2), TRAVEL, TRAVEL)
        assert errors == ["Offer is priced for 2 adults, got 1 adult, 1 child."]

    def test_unknown_mix_checks_ages_only(self):
        pax = [(PassengerType.ADULT, date(1990, 1, 1))]
        assert passenger_mix_errors(pax, None, TRAVEL, TRAVEL) == []

    @pytest.mark.parametrize(
        ("pax_type", "born", "last", "fragment"),
        [
            (PassengerType.ADULT, date(2018, 1, 1), TRAVEL, "adult must be 12"),
            (PassengerType.CHILD, date(2026, 1, 1), TRAVEL, "child must be 2-11"),
            (PassengerType.CHILD, date(2000, 1, 1), TRAVEL, "child must be 2-11"),
            # Under 2 on the way out, 2 by the last flight.
            (
                PassengerType.INFANT,
                date(2025, 6, 10),
                date(2027, 6, 20),
                "under 2 for the whole trip",
            ),
            (PassengerType.ADULT, date.today() + timedelta(days=1), TRAVEL, "future"),
        ],
    )
    def test_age_must_fit_the_type(self, pax_type, born, last, fragment):
        errors = passenger_mix_errors([(pax_type, born)], None, TRAVEL, last)
        assert len(errors) == 1 and fragment in errors[0]
