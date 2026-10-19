"""
Offer filtering, ranking, and explanation.

All functions are pure and deterministic — no I/O, no randomness.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel

from booking_agent.domain._types import UNKNOWN
from booking_agent.domain.offer import Offer
from booking_agent.domain.ranking import (
    EffectivePriceBreakdown,
    RankingPreferences,
    SortMode,
    effective_price,
)
from booking_agent.domain.search import SearchFilters, SearchLeg

# A layover is "overnight" if it overlaps 00:00-05:00 at the connecting airport
# (local time), or lasts this long or more.
_OVERNIGHT_MINUTES = 480
_NIGHT_START = time(0, 0)
_NIGHT_END = time(5, 0)


def is_overnight_layover(arrival: datetime, departure: datetime) -> bool:
    """Arrival/departure at the connecting airport, timezone-aware."""
    if departure - arrival >= timedelta(minutes=_OVERNIGHT_MINUTES):
        return True
    local_arr = arrival
    local_dep = departure.astimezone(arrival.tzinfo)  # both in the hub's local time
    day = local_arr.date()
    while day <= local_dep.date():
        night_start = datetime.combine(day, _NIGHT_START, tzinfo=local_arr.tzinfo)
        night_end = datetime.combine(day, _NIGHT_END, tzinfo=local_arr.tzinfo)
        if local_arr < night_end and local_dep > night_start:
            return True
        day += timedelta(days=1)
    return False


# ── filter ────────────────────────────────────────────────────────────────────


@dataclass
class FilterConfig:
    max_stops: int | None = None  # per slice: 0 = non-stop on every flight
    max_layover_minutes: int | None = None
    no_overnight_layover: bool = False
    bags_included: bool = False
    refundable_only: bool = False
    preferred_airlines: list[str] = field(default_factory=list)
    departure_not_before: time | None = None  # first slice, departure airport local time
    departure_not_after: time | None = None
    max_price_inr: Decimal | None = None
    # One (earliest, latest) window per slice, by index (None = any time). Local time.
    slice_windows: list[tuple[time, time] | None] = field(default_factory=list)


def _passes(offer: Offer, cfg: FilterConfig) -> bool:
    if cfg.max_stops is not None and any(sl.stops > cfg.max_stops for sl in offer.slices):
        return False

    for sl in offer.slices:
        for lv in sl.layovers:
            if (
                cfg.max_layover_minutes is not None
                and lv.duration_minutes > cfg.max_layover_minutes
            ):
                return False
        if cfg.no_overnight_layover:
            for inbound, outbound in zip(sl.segments, sl.segments[1:], strict=False):
                if is_overnight_layover(inbound.arrival_at, outbound.departure_at):
                    return False

    if cfg.bags_included:
        checked = offer.baggage.checked_pieces
        if checked == UNKNOWN or checked is None or checked == 0:
            return False

    if cfg.refundable_only and offer.fare_rules.refund.allowed is not True:
        return False

    if cfg.preferred_airlines:
        carrier_codes = {
            seg.marketing_carrier.iata_code for sl in offer.slices for seg in sl.segments
        }
        if not carrier_codes.intersection(cfg.preferred_airlines):
            return False

    dep_time = offer.slices[0].departure_at.time()
    if cfg.departure_not_before is not None and dep_time < cfg.departure_not_before:
        return False
    if cfg.departure_not_after is not None and dep_time > cfg.departure_not_after:
        return False

    for sl, window in zip(offer.slices, cfg.slice_windows, strict=False):
        if window is not None and not window[0] <= sl.departure_at.time() <= window[1]:
            return False

    return not (cfg.max_price_inr is not None and offer.total_inr > cfg.max_price_inr)


def filter_offers(offers: list[Offer], config: FilterConfig) -> list[Offer]:
    """Return the subset of offers that satisfy every active filter criterion."""
    return [o for o in offers if _passes(o, config)]


def filter_config_from(filters: SearchFilters, legs: Sequence[SearchLeg] = ()) -> FilterConfig:
    """The search query's filters (and each leg's time of day) as a FilterConfig."""
    window = filters.departure_window
    return FilterConfig(
        max_stops=filters.max_stops,
        max_layover_minutes=filters.max_layover_minutes,
        no_overnight_layover=filters.no_overnight_layover,
        bags_included=filters.bags_included,
        refundable_only=filters.refundable_only,
        preferred_airlines=[a.upper() for a in filters.preferred_airlines],
        departure_not_before=window.earliest if window else None,
        departure_not_after=window.latest if window else None,
        slice_windows=[leg.time_window.bounds for leg in legs],
    )


# ── rank ──────────────────────────────────────────────────────────────────────


def _sort_key(offer: Offer, prefs: RankingPreferences) -> tuple:  # type: ignore[type-arg]
    total_stops = sum(sl.stops for sl in offer.slices)
    total_duration = sum(sl.journey_minutes for sl in offer.slices)

    if prefs.sort_mode == SortMode.CHEAPEST:
        return (offer.total_inr, total_stops, total_duration, offer.offer_id)
    if prefs.sort_mode == SortMode.FASTEST:
        return (total_duration, offer.total_inr, total_stops, offer.offer_id)
    # BEST_VALUE
    eff, _ = effective_price(offer, prefs)
    return (eff, total_stops, total_duration, offer.offer_id)


def rank(offers: list[Offer], preferences: RankingPreferences) -> list[Offer]:
    """
    Sort offers by the selected sort mode.

    CHEAPEST  → total_inr, stops, duration, offer_id
    FASTEST   → total journey time (layovers included), total_inr, stops, offer_id
    BEST_VALUE → effective_price (with feature penalties), stops, duration, offer_id
    """
    return sorted(offers, key=lambda o: _sort_key(o, preferences))


# ── explain ───────────────────────────────────────────────────────────────────


class RankHighlight(str, Enum):
    CHEAPEST = "CHEAPEST"
    FASTEST = "FASTEST"
    MOST_FLEXIBLE = "MOST_FLEXIBLE"
    BEST_VALUE = "BEST_VALUE"
    DIRECT_FLIGHT = "DIRECT_FLIGHT"
    BAGS_INCLUDED = "BAGS_INCLUDED"


class RankExplanation(BaseModel):
    rank_position: int
    sort_mode: SortMode
    effective_price_inr: Decimal
    price_breakdown: EffectivePriceBreakdown
    total_stops: int
    total_duration_minutes: int
    highlights: list[RankHighlight]


def explain(
    offer: Offer,
    preferences: RankingPreferences,
    rank_position: int,
    all_offers: list[Offer],
) -> RankExplanation:
    """
    Structured, machine-readable explanation of why an offer ranked at rank_position.

    rank_position is 1-based. all_offers should be the full ranked list so that
    relative comparisons (cheapest, fastest) can be computed.
    """
    eff, breakdown = effective_price(offer, preferences)
    total_stops = sum(sl.stops for sl in offer.slices)
    total_duration = sum(sl.journey_minutes for sl in offer.slices)

    highlights: list[RankHighlight] = []

    # cheapest raw price
    if all_offers and offer.total_inr == min(o.total_inr for o in all_offers):
        highlights.append(RankHighlight.CHEAPEST)

    # fastest total journey time (layovers included)
    if all_offers and total_duration == min(
        sum(sl.journey_minutes for sl in o.slices) for o in all_offers
    ):
        highlights.append(RankHighlight.FASTEST)

    # best effective value
    if all_offers and eff == min(effective_price(o, preferences)[0] for o in all_offers):
        highlights.append(RankHighlight.BEST_VALUE)

    # direct flight
    if total_stops == 0:
        highlights.append(RankHighlight.DIRECT_FLIGHT)

    # known checked bags included
    checked = offer.baggage.checked_pieces
    if isinstance(checked, int) and checked > 0:
        highlights.append(RankHighlight.BAGS_INCLUDED)

    # flexible (refundable + changeable)
    if offer.fare_rules.refund.allowed is True and offer.fare_rules.change.allowed is True:
        highlights.append(RankHighlight.MOST_FLEXIBLE)

    return RankExplanation(
        rank_position=rank_position,
        sort_mode=preferences.sort_mode,
        effective_price_inr=eff,
        price_breakdown=breakdown,
        total_stops=total_stops,
        total_duration_minutes=total_duration,
        highlights=highlights,
    )


from booking_agent.orchestrator.dedupe import DedupeGroup  # noqa: E402 — after _sort_key


def rank_groups(
    groups: list[DedupeGroup],
    preferences: RankingPreferences,
) -> list[DedupeGroup]:
    """Sort DedupeGroups by their primary offer's sort key (same tie-breakers as rank())."""
    return sorted(groups, key=lambda g: _sort_key(g.primary, preferences))
