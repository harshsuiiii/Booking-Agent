from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from booking_agent.domain.common import CabinClass, Carrier
from booking_agent.domain.itinerary import Segment, Slice

AI = Carrier(iata_code="AI")

T0 = datetime(2025, 6, 1, 6, 0, tzinfo=timezone.utc)
T1 = datetime(2025, 6, 1, 8, 30, tzinfo=timezone.utc)
T2 = datetime(2025, 6, 1, 11, 0, tzinfo=timezone.utc)


def make_seg(origin="DEL", destination="BOM", dep=T0, arr=T1, duration_minutes=150, **kw) -> Segment:
    return Segment(
        marketing_carrier=AI,
        flight_number="AI101",
        origin=origin,
        destination=destination,
        departure_at=dep,
        arrival_at=arr,
        cabin_class=CabinClass.ECONOMY,
        booking_class="Y",
        duration_minutes=duration_minutes,
        **kw,
    )


class TestSegment:
    def test_valid(self):
        s = make_seg()
        assert s.origin == "DEL"
        assert s.flight_number == "AI101"
        assert s.stops == 0

    def test_arrival_before_departure_raises(self):
        with pytest.raises(ValidationError, match="arrival_at must be after departure_at"):
            make_seg(dep=T1, arr=T0)

    def test_arrival_equals_departure_raises(self):
        with pytest.raises(ValidationError, match="arrival_at must be after departure_at"):
            make_seg(dep=T0, arr=T0)

    def test_iata_lowercase_raises(self):
        with pytest.raises(ValidationError):
            make_seg(origin="del")

    def test_iata_too_short_raises(self):
        with pytest.raises(ValidationError):
            make_seg(origin="DE")

    def test_iata_digit_raises(self):
        with pytest.raises(ValidationError):
            make_seg(origin="D3L")

    def test_duration_zero_raises(self):
        with pytest.raises(ValidationError):
            make_seg(duration_minutes=0)

    def test_stops_negative_raises(self):
        with pytest.raises(ValidationError):
            make_seg(stops=-1)

    def test_operating_carrier_optional(self):
        s = make_seg()
        assert s.operating_carrier is None

    def test_timezone_midnight_crossing(self):
        """Arrival on next calendar day (midnight crossing) is valid."""
        dep = datetime(2025, 12, 31, 22, 0, tzinfo=timezone.utc)
        arr = datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc)
        s = make_seg(dep=dep, arr=arr, duration_minutes=180)
        assert s.arrival_at.year == 2026

    def test_dateline_crossing(self):
        """Flight crossing the international date line: arrival before departure in local
        wall-clock time but after in UTC."""
        dep = datetime(2025, 6, 1, 20, 0, tzinfo=timezone.utc)
        arr = datetime(2025, 6, 2, 6, 0, tzinfo=timezone.utc)
        s = make_seg(origin="NRT", destination="LAX", dep=dep, arr=arr, duration_minutes=600)
        assert s.arrival_at > s.departure_at


class TestSlice:
    def test_single_segment_properties(self):
        sl = Slice(segments=[make_seg()])
        assert sl.origin == "DEL"
        assert sl.destination == "BOM"
        assert sl.departure_at == T0
        assert sl.arrival_at == T1
        assert sl.duration_minutes == 150
        assert sl.stops == 0
        assert sl.layovers == []

    def test_two_segment_properties(self):
        s1 = make_seg(origin="DEL", destination="BLR", dep=T0, arr=T1, duration_minutes=150)
        s2 = make_seg(origin="BLR", destination="BOM", dep=T1, arr=T2, duration_minutes=90)
        sl = Slice(segments=[s1, s2])
        assert sl.origin == "DEL"
        assert sl.destination == "BOM"
        assert sl.departure_at == T0
        assert sl.arrival_at == T2
        assert sl.duration_minutes == 240
        assert sl.stops == 1

    def test_layover_duration_calculated(self):
        s1 = make_seg(origin="DEL", destination="BLR", dep=T0, arr=T1, duration_minutes=150)
        # 90-minute gap before next segment
        dep2 = datetime(2025, 6, 1, 10, 0, tzinfo=timezone.utc)
        s2 = make_seg(origin="BLR", destination="BOM", dep=dep2, arr=T2, duration_minutes=60)
        sl = Slice(segments=[s1, s2])
        assert len(sl.layovers) == 1
        assert sl.layovers[0].airport == "BLR"
        assert sl.layovers[0].duration_minutes == 90

    def test_empty_segments_raises(self):
        with pytest.raises(ValidationError):
            Slice(segments=[])
