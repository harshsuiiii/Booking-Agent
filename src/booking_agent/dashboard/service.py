"""
Dashboard figures, computed from booking summaries (no PII, no decryption).

"Sales" counts bookings the customer has paid for (PAID or TICKETED), at the sell price.
Months and days are in India time (the agency's market). Role rules: markup, net and
supplier performance appear only for admins; agents get sell-side figures.
"""

from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from booking_agent.auth.principal import Role
from booking_agent.booking.repository import BookingSummary
from booking_agent.dashboard.recent_searches import RecentSearch
from booking_agent.domain.booking import BookingState
from booking_agent.orchestrator.metrics import SupplierMetrics
from booking_agent.suppliers import supplier_label

IST = ZoneInfo("Asia/Kolkata")
SOLD = frozenset({BookingState.PAID, BookingState.TICKETED})
_D0 = Decimal("0")


def _local_date(dt: datetime) -> date:
    return dt.astimezone(IST).date()


def summary(
    rows: list[BookingSummary],
    role: Role,
    now: datetime,
    recent: list[RecentSearch],
) -> dict[str, Any]:
    today = _local_date(now)
    month_start = today.replace(day=1)
    this_month = [
        r
        for r in rows
        if _local_date(r.created_at) >= month_start and r.state != BookingState.DRAFT
    ]
    sold_this_month = [r for r in this_month if r.state in SOLD]
    upcoming = sorted(
        (r for r in rows if r.state in SOLD and r.travel_date >= today),
        key=lambda r: r.travel_date,
    )
    status_counts: dict[str, int] = defaultdict(int)
    for r in rows:
        status_counts[r.state.value] += 1

    data: dict[str, Any] = {
        "as_of": now.isoformat(),
        "month": month_start.strftime("%B %Y"),
        "bookings_this_month": len(this_month),
        "sales_this_month_inr": str(sum((r.sell_inr for r in sold_this_month), _D0)),
        "upcoming_trips": len(upcoming),
        "next_trips": [
            {
                "booking_id": r.booking_id,
                "reference": r.reference,
                "passenger": r.passenger_display,
                "origin": r.origin,
                "destination": r.destination,
                "travel_date": r.travel_date.isoformat(),
                "is_demo": r.is_demo,
            }
            for r in upcoming[:5]
        ],
        "status_counts": dict(status_counts),
        "recent_searches": [
            {
                "origin": s.origin,
                "destination": s.destination,
                "departure_date": s.departure_date.isoformat(),
                "passengers": s.passengers,
                "cabin": s.cabin,
                "source": s.source,
                "offer_count": s.offer_count,
                "searched_at": s.created_at.isoformat(),
            }
            for s in recent
        ],
    }
    if role == "admin":
        data["markup_this_month_inr"] = str(sum((r.markup_inr for r in sold_this_month), _D0))
    return data


def charts(
    rows: list[BookingSummary],
    role: Role,
    now: datetime,
    metrics: dict[str, SupplierMetrics],
    days: int = 30,
) -> dict[str, Any]:
    today = _local_date(now)
    start = today - timedelta(days=days - 1)
    by_day: dict[date, list[BookingSummary]] = defaultdict(list)
    for r in rows:
        d = _local_date(r.created_at)
        if start <= d <= today and r.state != BookingState.DRAFT:
            by_day[d].append(r)
    over_time = [
        {
            "date": (start + timedelta(days=i)).isoformat(),
            "bookings": len(by_day[start + timedelta(days=i)]),
            "sales_inr": str(
                sum((r.sell_inr for r in by_day[start + timedelta(days=i)] if r.state in SOLD), _D0)
            ),
        }
        for i in range(days)
    ]

    by_supplier: dict[str, list[BookingSummary]] = defaultdict(list)
    for r in rows:
        if r.state != BookingState.DRAFT:
            by_supplier[r.supplier_id].append(r)
    total_wins = sum(m.win_count for m in metrics.values())
    suppliers = []
    for supplier_id in sorted(set(by_supplier) | set(metrics)):
        bucket = by_supplier.get(supplier_id, [])
        sold = [r for r in bucket if r.state in SOLD]
        item: dict[str, Any] = {
            "supplier_id": supplier_id,
            "supplier_label": supplier_label(supplier_id),
            "bookings": len(bucket),
            "sales_inr": str(sum((r.sell_inr for r in sold), _D0)),
        }
        if role == "admin":
            m = metrics.get(supplier_id)
            item["supplier_performance"] = {
                "net_inr": str(sum((r.net_inr for r in sold), _D0)),
                "markup_inr": str(sum((r.markup_inr for r in sold), _D0)),
                "searches": m.search_count if m else 0,
                "wins": m.win_count if m else 0,
                # Share of searches where this supplier had the best-ranked offer.
                "win_rate": round(m.win_count / total_wins, 3) if m and total_wins else None,
                "error_rate": round(m.error_rate, 3) if m else None,
                "avg_latency_ms": round(m.avg_latency_ms) if m and m.success_count else None,
            }
        suppliers.append(item)

    return {
        "days": days,
        "bookings_over_time": over_time,
        "by_supplier": suppliers,
        "metrics_since": "API start" if role == "admin" else None,
    }
