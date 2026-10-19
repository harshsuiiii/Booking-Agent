"""
Tests for the observability module:
  - SupplierMetrics percentile latency (p50, p95)
  - GdsCostTracker look-to-book ratio and threshold alerting
  - Log scrubber PII redaction
  - OTel setup (no crash without exporter)
"""

import logging

import pytest

from booking_agent.observability.gds_cost import GdsCostTracker
from booking_agent.observability.log_scrubber import scrub_pii
from booking_agent.observability.setup import configure_tracing
from booking_agent.orchestrator.metrics import MetricsCollector, SupplierMetrics

# ── SupplierMetrics percentile latency ────────────────────────────────────────


class TestPercentileLatency:
    def test_p50_empty_returns_zero(self):
        m = SupplierMetrics(supplier_id="fake")
        assert m.p50_latency_ms == 0.0

    def test_p95_empty_returns_zero(self):
        m = SupplierMetrics(supplier_id="fake")
        assert m.p95_latency_ms == 0.0

    def test_p50_single_value(self):
        m = SupplierMetrics(supplier_id="fake", latencies_ms=[100.0])
        assert m.p50_latency_ms == 100.0

    def test_p50_of_odd_count(self):
        # [1, 2, 3, 4, 5] → 50th percentile = 3 (index 2 of 5)
        m = SupplierMetrics(supplier_id="fake", latencies_ms=[5.0, 1.0, 3.0, 2.0, 4.0])
        assert m.p50_latency_ms == 3.0

    def test_p95_of_20_values(self):
        # 20 values 1–20; p95 → index = int(20 * 95 / 100) - 1 = 18 → value = 19
        lats = [float(i) for i in range(1, 21)]
        m = SupplierMetrics(supplier_id="fake", latencies_ms=lats)
        assert m.p95_latency_ms == 19.0

    def test_p50_lt_p95(self):
        import random

        random.seed(42)
        lats = [random.uniform(10, 500) for _ in range(100)]
        m = SupplierMetrics(supplier_id="fake", latencies_ms=lats)
        assert m.p50_latency_ms <= m.p95_latency_ms


# ── MetricsCollector ──────────────────────────────────────────────────────────


class TestMetricsCollector:
    def test_error_rate_zero_when_no_calls(self):
        c = MetricsCollector()
        m = c._get("fake")
        assert m.error_rate == 0.0

    def test_error_rate_calculation(self):
        c = MetricsCollector()
        c.record_search_started("fake")
        c.record_success("fake", latency_ms=100.0, offers_count=5)
        c.record_search_started("fake")
        c.record_error("fake")
        m = c.snapshot()["fake"]
        assert m.error_rate == pytest.approx(0.5)

    def test_win_count_increments(self):
        c = MetricsCollector()
        c.record_win("fake")
        c.record_win("fake")
        assert c.snapshot()["fake"].win_count == 2

    def test_latencies_accumulated(self):
        c = MetricsCollector()
        c.record_success("fake", 100.0, 3)
        c.record_success("fake", 200.0, 1)
        m = c.snapshot()["fake"]
        assert m.avg_latency_ms == 150.0


# ── GdsCostTracker ────────────────────────────────────────────────────────────


class TestGdsCostTracker:
    def test_look_to_book_zero_when_no_activity(self):
        tracker = GdsCostTracker()
        assert tracker.look_to_book_ratio("fake") == 0.0

    def test_look_to_book_inf_when_searches_no_bookings(self):
        tracker = GdsCostTracker()
        tracker.record_search("fake")
        assert tracker.look_to_book_ratio("fake") == float("inf")

    def test_look_to_book_ratio(self):
        tracker = GdsCostTracker()
        for _ in range(100):
            tracker.record_search("fake")
        tracker.record_booking("fake")
        assert tracker.look_to_book_ratio("fake") == pytest.approx(100.0)

    def test_price_calls_tracked(self):
        tracker = GdsCostTracker()
        tracker.record_price("fake")
        tracker.record_price("fake")
        assert tracker.counts("fake").prices == 2

    def test_threshold_warning_search_count(self, caplog):
        tracker = GdsCostTracker(search_threshold=3)
        with caplog.at_level(logging.WARNING, logger="booking_agent.observability.gds_cost"):
            for _ in range(3):
                tracker.record_search("fake")
        assert any("gds.threshold.search_count" in r.message for r in caplog.records)

    def test_threshold_warning_look_to_book(self, caplog):
        tracker = GdsCostTracker(look_to_book_alert_ratio=5.0)
        with caplog.at_level(logging.WARNING, logger="booking_agent.observability.gds_cost"):
            for _ in range(6):
                tracker.record_search("fake")
            tracker.record_booking("fake")
        assert any("gds.threshold.look_to_book" in r.message for r in caplog.records)

    def test_per_tenant_isolation(self):
        tracker = GdsCostTracker()
        tracker.record_search("fake", "tenant-a")
        tracker.record_search("fake", "tenant-a")
        tracker.record_booking("fake", "tenant-b")
        # tenant-a: 2 searches, 0 bookings → inf ratio
        assert tracker.look_to_book_ratio("fake", "tenant-a") == float("inf")
        # tenant-b: 0 searches, 1 booking → 0
        assert tracker.look_to_book_ratio("fake", "tenant-b") == 0.0

    def test_all_suppliers_returns_unique_list(self):
        tracker = GdsCostTracker()
        tracker.record_search("amadeus")
        tracker.record_search("duffel")
        tracker.record_search("amadeus")
        assert set(tracker.all_suppliers()) == {"amadeus", "duffel"}


# ── Log scrubber ──────────────────────────────────────────────────────────────


class TestLogScrubber:
    def _scrub(self, **kwargs: object) -> dict:
        return scrub_pii(None, "info", dict(kwargs))

    def test_passport_number_redacted(self):
        result = self._scrub(event="passenger boarded", ref="P1234567")
        assert result["ref"] == "[PASSPORT_REDACTED]"

    def test_pan_number_redacted(self):
        result = self._scrub(event="tax check", pan="ABCDE1234F")
        assert result["pan"] == "[REDACTED]"  # sensitive key match first

    def test_card_number_redacted(self):
        result = self._scrub(event="payment", card="4111 1111 1111 1111")
        assert "[CARD_REDACTED]" in result["card"] or "[REDACTED]" in result["card"]

    def test_email_redacted(self):
        result = self._scrub(event="contact", email="john.doe@example.com")
        assert "[EMAIL_REDACTED]" in result["email"] or "[REDACTED]" in result["email"]

    def test_safe_field_preserved(self):
        result = self._scrub(event="search.start", origin="DEL", destination="BOM")
        assert result["origin"] == "DEL"
        assert result["destination"] == "BOM"

    def test_sensitive_key_always_redacted(self):
        result = self._scrub(event="auth", token="my-secret-token-123")
        assert result["token"] == "[REDACTED]"

    def test_api_key_redacted(self):
        result = self._scrub(event="auth", api_key="duffel_test_abc123")
        assert result["api_key"] == "[REDACTED]"

    def test_event_message_preserved(self):
        result = self._scrub(event="booking.prepared", booking_id="bk-123")
        assert result["event"] == "booking.prepared"


# ── OTel setup ────────────────────────────────────────────────────────────────


class TestOtelSetup:
    def test_configure_tracing_no_endpoint_does_not_raise(self):
        configure_tracing(service_name="test-service", otlp_endpoint=None)

    def test_configure_tracing_bad_endpoint_warns_gracefully(self):
        """Without the optional exporter installed, configure_tracing should not crash."""
        configure_tracing(
            service_name="test-service",
            otlp_endpoint="http://nonexistent:4317",
        )

    def test_get_tracer_returns_tracer(self):

        from booking_agent.observability.setup import get_tracer

        tracer = get_tracer("test.module")
        assert tracer is not None
        with tracer.start_as_current_span("test-span"):
            pass  # must not raise
