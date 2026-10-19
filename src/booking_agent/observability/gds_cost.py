"""
GDS call cost tracker.

Tracks search/price/book call counts per supplier (and optionally per tenant)
to help monitor:
  - Look-to-book ratio (searches ÷ bookings) — most GDS contracts have limits
  - Absolute call volume approaching contractual thresholds
  - Per-tenant abuse

Wire into the orchestrator and BookingService. In production, persist counts
to Postgres/Redis rather than keeping them in-process.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

_log = logging.getLogger(__name__)


@dataclass
class _SupplierTenantKey:
    supplier_id: str
    tenant_id: str


@dataclass
class CallCounts:
    searches: int = 0
    prices: int = 0
    bookings: int = 0

    @property
    def look_to_book_ratio(self) -> float:
        """Searches per booking. Returns inf when no bookings have occurred."""
        if self.bookings == 0:
            return float("inf") if self.searches > 0 else 0.0
        return self.searches / self.bookings


class GdsCostTracker:
    """
    In-process GDS call cost tracker.

    Thresholds trigger structured-log warnings so they can be routed to
    alerting systems (PagerDuty, OpsGenie) via a log-based alert rule.

    Args:
        search_threshold:         Warn when a supplier's search count reaches this value.
        look_to_book_alert_ratio: Warn when searches ÷ bookings exceeds this value.
    """

    def __init__(
        self,
        search_threshold: int = 10_000,
        look_to_book_alert_ratio: float = 500.0,
    ) -> None:
        self._search_threshold = search_threshold
        self._l2b_alert = look_to_book_alert_ratio
        # (supplier_id, tenant_id) → CallCounts
        self._counts: dict[tuple[str, str], CallCounts] = {}

    # ── record ────────────────────────────────────────────────────────────────

    def record_search(self, supplier_id: str, tenant_id: str = "default") -> None:
        c = self._get(supplier_id, tenant_id)
        c.searches += 1
        self._check(supplier_id, tenant_id, c)

    def record_price(self, supplier_id: str, tenant_id: str = "default") -> None:
        self._get(supplier_id, tenant_id).prices += 1

    def record_booking(self, supplier_id: str, tenant_id: str = "default") -> None:
        c = self._get(supplier_id, tenant_id)
        c.bookings += 1
        self._check(supplier_id, tenant_id, c)

    # ── query ─────────────────────────────────────────────────────────────────

    def counts(self, supplier_id: str, tenant_id: str = "default") -> CallCounts:
        return self._get(supplier_id, tenant_id)

    def look_to_book_ratio(self, supplier_id: str, tenant_id: str = "default") -> float:
        return self._get(supplier_id, tenant_id).look_to_book_ratio

    def all_suppliers(self) -> list[str]:
        return list({k[0] for k in self._counts})

    # ── private ───────────────────────────────────────────────────────────────

    def _get(self, supplier_id: str, tenant_id: str) -> CallCounts:
        key = (supplier_id, tenant_id)
        if key not in self._counts:
            self._counts[key] = CallCounts()
        return self._counts[key]

    def _check(self, supplier_id: str, tenant_id: str, c: CallCounts) -> None:
        if c.searches >= self._search_threshold:
            _log.warning(
                "gds.threshold.search_count",
                extra={
                    "supplier_id": supplier_id,
                    "tenant_id": tenant_id,
                    "searches": c.searches,
                    "threshold": self._search_threshold,
                },
            )
        ratio = c.look_to_book_ratio
        if ratio != float("inf") and ratio >= self._l2b_alert:
            _log.warning(
                "gds.threshold.look_to_book",
                extra={
                    "supplier_id": supplier_id,
                    "tenant_id": tenant_id,
                    "ratio": ratio,
                    "threshold": self._l2b_alert,
                },
            )
