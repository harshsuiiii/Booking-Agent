"""Simulated supplier: airports, determinism, pricing differences, filters, price/book."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from booking_agent.connectors.errors import SupplierBadResponse
from booking_agent.connectors.payment import DuffelBalancePayment
from booking_agent.domain._types import UNKNOWN
from booking_agent.domain.booking import Passenger, PassengerType
from booking_agent.domain.common import CabinClass
from booking_agent.domain.offer import Offer
from booking_agent.domain.search import PaxCounts, SearchFilters, SearchQuery
from booking_agent.orchestrator.ranking import (
    FilterConfig,
    filter_offers,
    is_overnight_layover,
)
from booking_agent.orchestrator.search import SearchComplete, SearchOrchestrator
from booking_agent.simulated.airports import airports, distance_km, get_airport, search_airports
from booking_agent.simulated.connector import SimulatedConnector
from booking_agent.simulated.pricing import PROFILES

TODAY = date(2026, 10, 18)
DAY = TODAY + timedelta(days=21)


def _connector(supplier: str = "sim_gds", failure_rate: float = 0.0) -> SimulatedConnector:
    return SimulatedConnector(
        PROFILES[supplier],
        latency_s=(0, 0),
        failure_rate=failure_rate,
        price_change_rate=0,
        today=lambda: TODAY,
    )


def _query(o: str, d: str, day: date = DAY, **kw: object) -> SearchQuery:
    return SearchQuery(origin=o, destination=d, departure_date=day, **kw)  # type: ignore[arg-type]


def _fingerprint(offers: list[Offer]) -> list[tuple[object, ...]]:
    return [
        (
            o.supplier_offer_ref,
            o.total_inr,
            tuple(
                (s.flight_number, s.departure_at, s.arrival_at)
                for sl in o.slices
                for s in sl.segments
            ),
            o.baggage.checked_pieces,
            o.fare_rules.refund.allowed,
        )
        for o in offers
    ]


class TestAirports:
    def test_coverage(self) -> None:
        data = airports().values()
        assert sum(a.is_india for a in data) >= 40
        assert sum(not a.is_india for a in data) >= 40

    def test_search_by_city_and_code(self) -> None:
        assert search_airports("BOM")[0].iata == "BOM"
        assert search_airports("mumbai")[0].iata == "BOM"
        assert "LHR" in {a.iata for a in search_airports("london")}

    def test_distance_is_plausible(self) -> None:
        dist = distance_km(get_airport("DEL"), get_airport("BOM"))  # type: ignore[arg-type]
        assert 1100 < dist < 1200


class TestDeterminism:
    @pytest.mark.parametrize("route", [("DEL", "BOM"), ("BOM", "DXB"), ("DEL", "LHR")])
    def test_same_route_and_date_gives_same_offers(self, route: tuple[str, str]) -> None:
        first = _connector().generate_offers(_query(*route))
        second = _connector().generate_offers(_query(*route))
        assert first and _fingerprint(first) == _fingerprint(second)

    def test_different_date_gives_different_offers(self) -> None:
        a = _connector().generate_offers(_query("DEL", "BOM", DAY))
        b = _connector().generate_offers(_query("DEL", "BOM", DAY + timedelta(days=1)))
        assert _fingerprint(a) != _fingerprint(b)

    def test_unknown_airport_gives_no_offers(self) -> None:
        assert _connector().generate_offers(_query("DEL", "XXX")) == []


class TestRealism:
    def test_routes_use_real_regional_airlines(self) -> None:
        carriers = {
            o.slices[0].segments[0].marketing_carrier.iata_code
            for o in _connector().generate_offers(_query("BOM", "DXB"))
        }
        assert {"EK", "6E"} <= carriers or {"FZ", "AI"} <= carriers

    def test_times_are_airport_local(self) -> None:
        offer = _connector().generate_offers(_query("DEL", "LHR"))[0]
        seg = offer.slices[0].segments[0]
        assert seg.departure_at.utcoffset() == timedelta(hours=5, minutes=30)
        last = offer.slices[0].segments[-1]
        assert last.arrival_at.utcoffset() in (timedelta(0), timedelta(hours=1))

    def test_layovers_and_overnight_connections_exist(self) -> None:
        layovers = [
            lv.duration_minutes
            for o in _connector().generate_offers(_query("IXC", "GOI"))
            for lv in o.slices[0].layovers
        ]
        assert layovers and min(layovers) >= 60

    def test_all_offers_are_marked_simulated(self) -> None:
        offers = _connector().generate_offers(_query("MAA", "SIN"))
        assert offers and all(o.source == "simulated" for o in offers)

    def test_suppliers_price_the_same_flight_differently(self) -> None:
        q = _query("DEL", "LHR")
        by_supplier = {sid: _connector(sid).generate_offers(q) for sid in PROFILES}
        keys = [{o.dedupe_key: o.total_inr for o in offers} for offers in by_supplier.values()]
        shared = set(keys[0]) & set(keys[1])
        assert shared, "suppliers should overlap on some flights"
        assert any(keys[0][k] != keys[1][k] for k in shared)

    def test_consolidator_leaves_some_rules_unknown(self) -> None:
        offers = [
            o
            for route in [("DEL", "BOM"), ("BOM", "DXB"), ("BLR", "SIN"), ("DEL", "LHR")]
            for o in _connector("sim_consolidator").generate_offers(_query(*route))
        ]
        assert any(o.baggage.checked_pieces == UNKNOWN for o in offers)
        assert any(o.fare_rules.refund.allowed == UNKNOWN for o in offers)

    def test_longer_route_costs_more(self) -> None:
        dom = min(o.total_inr for o in _connector().generate_offers(_query("DEL", "BOM")))
        lon = min(o.total_inr for o in _connector().generate_offers(_query("DEL", "LHR")))
        assert lon > dom * 4

    def test_business_is_full_service_only_and_dearer(self) -> None:
        eco = _connector().generate_offers(_query("DEL", "BOM"))
        biz = _connector().generate_offers(_query("DEL", "BOM", cabin=CabinClass.BUSINESS))
        lcc = {"6E", "IX", "QP", "SG"}
        assert biz and all(
            o.slices[0].segments[0].marketing_carrier.iata_code not in lcc for o in biz
        )
        assert min(o.total_inr for o in biz) > min(o.total_inr for o in eco) * 2

    def test_round_trip_has_two_slices(self) -> None:
        q = _query("DEL", "BOM", return_date=DAY + timedelta(days=4))
        offers = _connector().generate_offers(q)
        assert offers and all(len(o.slices) == 2 for o in offers)
        assert offers[0].slices[1].origin == "BOM"

    def test_more_passengers_cost_more(self) -> None:
        one = _connector().generate_offers(_query("DEL", "BOM"))[0]
        two = next(
            o
            for o in _connector().generate_offers(
                _query("DEL", "BOM", pax_counts=PaxCounts(adults=2))
            )
            if o.dedupe_key == one.dedupe_key
        )
        assert two.pax_count == 2
        assert two.total_inr > one.total_inr * Decimal("1.8")


class TestNetworkBehaviour:
    async def test_failure_rate_one_always_fails(self) -> None:
        conn = _connector(failure_rate=1.0)
        with pytest.raises(SupplierBadResponse):
            await conn.search(_query("DEL", "BOM"))

    async def test_price_regenerates_same_fare_and_book_returns_sim_pnr(self) -> None:
        conn = _connector()
        offer = (await conn.search(_query("DEL", "BOM")))[0]
        priced = await conn.price(offer)
        assert priced.total_inr == offer.total_inr
        booking = await conn.book(
            priced,
            [
                Passenger(
                    pax_type=PassengerType.ADULT,
                    given_name="A",
                    family_name="B",
                    date_of_birth=date(1990, 1, 1),
                )
            ],
            idempotency_key="k1",
            payment=DuffelBalancePayment(),
        )
        assert booking.pnr and booking.pnr.startswith("SIM")


class TestOvernightFilter:
    IST = ZoneInfo("Asia/Kolkata")

    def _t(self, day: int, hh: int, mm: int = 0) -> datetime:
        return datetime(2026, 11, day, hh, mm, tzinfo=self.IST)

    def test_late_arrival_early_departure_is_overnight(self) -> None:
        assert is_overnight_layover(self._t(8, 23, 30), self._t(9, 4, 45))

    def test_daytime_connection_is_not_overnight(self) -> None:
        assert not is_overnight_layover(self._t(8, 10), self._t(8, 14))

    def test_eight_hours_counts_even_in_daytime(self) -> None:
        assert is_overnight_layover(self._t(8, 7), self._t(8, 15))

    def test_uses_the_connecting_airports_local_time(self) -> None:
        # 19:00-23:00 UTC is 00:30-04:30 next day in IST: overnight at an Indian hub.
        arr = datetime(2026, 11, 8, 19, 0, tzinfo=UTC).astimezone(self.IST)
        dep = datetime(2026, 11, 8, 23, 0, tzinfo=UTC)
        assert is_overnight_layover(arr, dep)

    def test_filter_removes_exactly_the_overnight_offers(self) -> None:
        offers = [
            o
            for route in [("IXC", "GOI"), ("COK", "JED"), ("BLR", "SFO"), ("DEL", "SYD")]
            for o in _connector().generate_offers(_query(*route))
        ]
        overnight = {
            o.offer_id
            for o in offers
            if any(
                is_overnight_layover(a.arrival_at, b.departure_at)
                for a, b in zip(o.slices[0].segments, o.slices[0].segments[1:], strict=False)
            )
        }
        assert overnight, "test routes should include overnight connections"
        kept = filter_offers(offers, FilterConfig(no_overnight_layover=True))
        assert {o.offer_id for o in kept} == {o.offer_id for o in offers} - overnight

    async def test_orchestrator_applies_query_filters(self) -> None:
        orch = SearchOrchestrator(connectors=[_connector()])
        q = _query("COK", "JED", filters=SearchFilters(no_overnight_layover=True, max_stops=1))
        complete = [e async for e in orch.search(q) if isinstance(e, SearchComplete)][0]
        offers = [g.primary for g in complete.groups]
        assert offers
        for o in offers:
            segs = o.slices[0].segments
            assert not any(
                is_overnight_layover(a.arrival_at, b.departure_at)
                for a, b in zip(segs, segs[1:], strict=False)
            )
