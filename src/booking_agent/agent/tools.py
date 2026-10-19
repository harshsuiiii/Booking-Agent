"""
Tool definitions (Anthropic JSON schema) and their implementations.

ToolExecutor wraps a session and orchestrator.  Each tool method returns a
JSON string — the raw content passed back to the model via tool_result.

The model never sees net fares, raw payloads, or supplier-internal IDs beyond
the opaque offer_id.
"""

import contextlib
import json
from decimal import Decimal
from typing import Any

from booking_agent.agent.compare import compare
from booking_agent.agent.session import AgentSession
from booking_agent.cache.search_cache import SearchCache
from booking_agent.dashboard.recent_searches import RecentSearch, RecentSearchStore
from booking_agent.domain._types import UNKNOWN
from booking_agent.domain.common import CabinClass
from booking_agent.domain.money import Money
from booking_agent.domain.offer import Offer
from booking_agent.domain.ranking import RankingPreferences, effective_price
from booking_agent.domain.search import PaxCounts, SearchFilters, SearchQuery
from booking_agent.orchestrator.events import SearchComplete
from booking_agent.orchestrator.ranking import explain, rank
from booking_agent.orchestrator.search import SearchOrchestrator, TenantContext
from booking_agent.rules.model import Rule

# ── Anthropic tool schemas ────────────────────────────────────────────────────

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "search_flights",
        "description": (
            "Search for available flights. Returns ranked offers with IDs and key details. "
            "Always call this before quoting prices or asking for an offer ID."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "origin": {
                    "type": "string",
                    "description": "3-letter IATA origin airport code (uppercase), e.g. BOM",
                },
                "destination": {
                    "type": "string",
                    "description": "3-letter IATA destination airport code (uppercase), e.g. DEL",
                },
                "departure_date": {
                    "type": "string",
                    "description": "Departure date in ISO 8601 format YYYY-MM-DD",
                },
                "return_date": {
                    "type": "string",
                    "description": "Return date YYYY-MM-DD. Omit for one-way.",
                },
                "cabin_class": {
                    "type": "string",
                    "enum": ["economy", "premium_economy", "business", "first"],
                    "description": "Cabin class. Defaults to economy.",
                },
                "adults": {
                    "type": "integer",
                    "description": "Number of adult passengers (default 1).",
                    "minimum": 1,
                    "maximum": 9,
                },
                "children": {
                    "type": "integer",
                    "description": "Number of child passengers (default 0).",
                    "minimum": 0,
                    "maximum": 9,
                },
                "bags_included": {
                    "type": "boolean",
                    "description": "If true, only show offers with known checked baggage included.",
                },
                "no_overnight_layover": {
                    "type": "boolean",
                    "description": "If true, exclude itineraries with layovers ≥ 8 hours overnight.",
                },
                "refundable_only": {
                    "type": "boolean",
                    "description": "If true, only show refundable fares.",
                },
                "max_stops": {
                    "type": "integer",
                    "description": "Maximum number of stops (0 = non-stop only).",
                    "minimum": 0,
                },
                "preferred_airlines": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": 'List of preferred airline IATA codes, e.g. ["6E", "AI"].',
                },
            },
            "required": ["origin", "destination", "departure_date"],
        },
    },
    {
        "name": "get_offer_details",
        "description": (
            "Get full details for a specific offer. Only valid for offer IDs returned "
            "by the most recent search_flights call."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "offer_id": {
                    "type": "string",
                    "description": "Offer ID from search_flights results.",
                }
            },
            "required": ["offer_id"],
        },
    },
    {
        "name": "compare_offers",
        "description": "Compare 2–5 offers side-by-side in a structured table.",
        "input_schema": {
            "type": "object",
            "properties": {
                "offer_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 2,
                    "maxItems": 5,
                    "description": "List of offer IDs to compare.",
                }
            },
            "required": ["offer_ids"],
        },
    },
    {
        "name": "explain_ranking",
        "description": (
            "Explain why an offer is ranked at its current position, including "
            "the effective price breakdown with any penalty adjustments."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "offer_id": {
                    "type": "string",
                    "description": "Offer ID to explain.",
                }
            },
            "required": ["offer_id"],
        },
    },
]


# ── helpers ───────────────────────────────────────────────────────────────────


def _bag_label(pieces: Any, kg: Any = None) -> str:
    """Spelled out ("1 pc × 15 kg") so neither the model nor the UI has to guess units."""
    if pieces is UNKNOWN:
        return "unknown"
    if pieces is None or pieces == 0:
        return "none"
    weight = f" × {kg.normalize():f} kg" if kg not in (None, UNKNOWN) else ""
    return f"{pieces} pc{weight}"


def _policy_label(val: Any) -> str:
    if val is UNKNOWN:
        return "unknown"
    if val is True:
        return "yes"
    if val is False:
        return "no"
    return str(val)


def offer_summary(offer: Offer, rank_pos: int, prefs: RankingPreferences) -> dict[str, Any]:
    eff, _ = effective_price(offer, prefs)
    customer_price = offer.total_inr + offer.price.agency_markup
    return {
        "offer_id": offer.offer_id,
        "rank": rank_pos,
        "is_test_data": offer.is_test_data,
        "airline": offer.slices[0].segments[0].marketing_carrier.iata_code,
        "flight_number": offer.slices[0].segments[0].flight_number,
        "route": f"{offer.slices[0].origin} → {offer.slices[-1].destination}",
        "departure": offer.slices[0].departure_at.isoformat(),
        "arrival": offer.slices[-1].arrival_at.isoformat(),
        # Door-to-door journey time per direction, layovers included (not just air time).
        "duration_minutes": sum(sl.journey_minutes for sl in offer.slices),
        "stops": sum(sl.stops for sl in offer.slices),
        "cabin": offer.cabin_class.value,
        "fare_basis": offer.fare_basis,
        "fare_family": offer.fare_family,
        "customer_price_inr": str(customer_price),
        "effective_price_inr": str(eff),
        "checked_baggage": _bag_label(offer.baggage.checked_pieces, offer.baggage.checked_kg),
        "cabin_baggage": _bag_label(offer.baggage.cabin_pieces, offer.baggage.cabin_kg),
        "refundable": _policy_label(offer.fare_rules.refund.allowed),
        "changeable": _policy_label(offer.fare_rules.change.allowed),
        "expires_at": offer.expires_at.isoformat(),
        "via": [seg.destination for seg in offer.slices[0].segments[:-1]],
        "source": offer.source,
    }


# Fields the model needs to answer and to call the other tools. Everything else stays
# out of the prompt: every extra field is paid for on every following model round.
_MODEL_FIELDS = (
    "offer_id",
    "rank",
    "airline",
    "flight_number",
    "route",
    "via",
    "departure",
    "arrival",
    "duration_minutes",
    "stops",
    "fare_family",
    "customer_price_inr",
    "effective_price_inr",
    "checked_baggage",
    "refundable",
    "changeable",
)
MODEL_TOP_N = 5


def model_offer_summary(offer: Offer, rank_pos: int, prefs: RankingPreferences) -> dict[str, Any]:
    full = offer_summary(offer, rank_pos, prefs)
    slim = {k: full[k] for k in _MODEL_FIELDS}
    if offer.is_test_data:
        slim["is_test_data"] = True
    return slim


# ── executor ──────────────────────────────────────────────────────────────────


class ToolExecutor:
    def __init__(
        self,
        session: AgentSession,
        orchestrator: SearchOrchestrator,
        rules: list[Rule] | None = None,
        preferences: RankingPreferences | None = None,
        cache: SearchCache | None = None,
        recent_searches: RecentSearchStore | None = None,
    ) -> None:
        self._session = session
        self._recent = recent_searches
        self._orchestrator = orchestrator
        self._rules = rules or []
        self._prefs = preferences or RankingPreferences()
        self._cache = cache
        # One-line outcome of the last tool call, for UI progress messages.
        self.last_status: str | None = None

    async def execute(self, name: str, tool_input: dict[str, Any]) -> str:
        self.last_status = None
        match name:
            case "search_flights":
                return await self._search_flights(tool_input)
            case "get_offer_details":
                return self._get_offer_details(tool_input)
            case "compare_offers":
                return self._compare_offers(tool_input)
            case "explain_ranking":
                return self._explain_ranking(tool_input)
            case _:
                return json.dumps({"error": f"Unknown tool: {name}"})

    # ── search_flights ────────────────────────────────────────────────────────

    async def _search_flights(self, params: dict[str, Any]) -> str:
        try:
            query = self._build_query(params)
        except (ValueError, TypeError) as exc:
            return json.dumps({"error": f"Invalid search parameters: {exc}"})

        tenant_ctx = TenantContext(tenant_id=self._session.tenant_id)
        stats: dict[str, list[str]] = {"succeeded": [], "failed": []}

        async def run_search() -> list[Offer]:
            found: list[Offer] = []
            async for event in self._orchestrator.search(
                query, tenant_ctx, rules=self._rules if self._rules else None
            ):
                if isinstance(event, SearchComplete):
                    found = [group.primary for group in event.groups]
                    stats["succeeded"] = event.suppliers_succeeded
                    stats["failed"] = event.suppliers_failed + event.suppliers_timed_out
            return found

        # Short-lived cache (display hint only; booking always re-prices live). A search
        # with failed suppliers is not cached, so a retry can get the full picture.
        key = (
            self._cache.make_key(query, tenant_ctx.tenant_id, tenant_ctx.enabled_suppliers)
            if self._cache is not None
            else None
        )
        cached: list[Offer] | None = None
        if self._cache is not None and key:
            try:
                cached = await self._cache.get(key)
            except Exception:  # cache is best-effort; a broken cache is a miss
                cached = None
        from_cache = cached is not None
        if cached is not None:
            all_offers = cached
        else:
            all_offers = await run_search()
            if self._cache is not None and key and all_offers and not stats["failed"]:
                with contextlib.suppress(Exception):  # cache is best-effort
                    await self._cache.set(key, all_offers)

        if from_cache:
            self.last_status = f"Found {len(all_offers)} offers (cached search)."
        else:
            failed = f", {len(stats['failed'])} failed" if stats["failed"] else ""
            self.last_status = (
                f"Found {len(all_offers)} offers from "
                f"{len(stats['succeeded'])} supplier(s){failed}."
            )

        if self._recent is not None and self._session.user_id:
            with contextlib.suppress(Exception):  # history is best-effort
                await self._recent.add(
                    RecentSearch.from_query(
                        query,
                        self._session.tenant_id,
                        self._session.user_id,
                        "assistant",
                        len(all_offers),
                    )
                )

        if not all_offers:
            return json.dumps({"total_results": 0, "offers": [], "message": "No flights found."})

        ranked = rank(all_offers, self._prefs)
        self._session.ranked_offers = ranked
        # Keep earlier results too, so offer cards from previous turns stay bookable
        # in the UI (booking always re-prices live).
        self._session.stored_offers.update({o.offer_id: o for o in ranked})

        summaries = [
            model_offer_summary(o, i, self._prefs) for i, o in enumerate(ranked[:MODEL_TOP_N], 1)
        ]

        # Register every customer price the model can see as allowed for post-check.
        for s in summaries:
            self._session.allowed_prices.add(str(Decimal(s["customer_price_inr"])))
            self._session.allowed_prices.add(str(Decimal(s["effective_price_inr"])))

        result: dict[str, Any] = {
            "total_results": len(ranked),
            "showing_top": len(summaries),
            "offers": summaries,
        }
        if any(o.source == "simulated" for o in ranked):
            result["simulated_data"] = True
        if stats["failed"]:
            result["suppliers_failed"] = stats["failed"]
        return json.dumps(result)

    @staticmethod
    def _build_query(params: dict[str, Any]) -> SearchQuery:
        cabin_map = {
            "economy": CabinClass.ECONOMY,
            "premium_economy": CabinClass.PREMIUM_ECONOMY,
            "business": CabinClass.BUSINESS,
            "first": CabinClass.FIRST,
        }
        cabin_str = (params.get("cabin_class") or "economy").lower()
        cabin = cabin_map.get(cabin_str, CabinClass.ECONOMY)

        pax = PaxCounts(
            adults=int(params.get("adults") or 1),
            children=int(params.get("children") or 0),
        )
        filters = SearchFilters(
            bags_included=bool(params.get("bags_included")),
            no_overnight_layover=bool(params.get("no_overnight_layover")),
            refundable_only=bool(params.get("refundable_only")),
            max_stops=params.get("max_stops"),
            preferred_airlines=list(params.get("preferred_airlines") or []),
        )
        return SearchQuery(
            origin=str(params["origin"]).upper(),
            destination=str(params["destination"]).upper(),
            departure_date=params["departure_date"],
            return_date=params.get("return_date"),
            pax_counts=pax,
            cabin=cabin,
            filters=filters,
        )

    # ── get_offer_details ─────────────────────────────────────────────────────

    def _get_offer_details(self, params: dict[str, Any]) -> str:
        offer_id = params.get("offer_id", "")
        offer = self._session.stored_offers.get(offer_id)
        if offer is None:
            return json.dumps({"error": f"Offer {offer_id!r} not found. Run search_flights first."})

        # Find rank position
        rank_pos = next(
            (i for i, o in enumerate(self._session.ranked_offers, 1) if o.offer_id == offer_id),
            0,
        )
        summary = offer_summary(offer, rank_pos, self._prefs)

        # Add detailed fare rules
        summary["fare_rules"] = {
            "change": {
                "allowed": _policy_label(offer.fare_rules.change.allowed),
                "fee": (
                    str(offer.fare_rules.change.fee.amount)
                    if isinstance(offer.fare_rules.change.fee, Money)
                    else _policy_label(offer.fare_rules.change.fee)
                ),
            },
            "cancel": {
                "allowed": _policy_label(offer.fare_rules.cancel.allowed),
                "fee": (
                    str(offer.fare_rules.cancel.fee.amount)
                    if isinstance(offer.fare_rules.cancel.fee, Money)
                    else _policy_label(offer.fare_rules.cancel.fee)
                ),
            },
            "refund": {
                "allowed": _policy_label(offer.fare_rules.refund.allowed),
                "fee": (
                    str(offer.fare_rules.refund.fee.amount)
                    if isinstance(offer.fare_rules.refund.fee, Money)
                    else _policy_label(offer.fare_rules.refund.fee)
                ),
            },
        }
        # Detailed per-slice itinerary
        summary["slices"] = [
            {
                "origin": sl.origin,
                "destination": sl.destination,
                "departure": sl.departure_at.isoformat(),
                "arrival": sl.arrival_at.isoformat(),
                "duration_minutes": sl.journey_minutes,
                "air_minutes": sl.duration_minutes,
                "segments": [
                    {
                        "flight": f"{seg.marketing_carrier.iata_code}{seg.flight_number}",
                        "origin": seg.origin,
                        "destination": seg.destination,
                        "departure": seg.departure_at.isoformat(),
                        "arrival": seg.arrival_at.isoformat(),
                        "cabin": seg.cabin_class.value,
                        "booking_class": seg.booking_class,
                        "aircraft": seg.aircraft_type,
                    }
                    for seg in sl.segments
                ],
            }
            for sl in offer.slices
        ]

        customer_price = offer.total_inr + offer.price.agency_markup
        self._session.allowed_prices.add(str(Decimal(str(customer_price))))
        return json.dumps(summary)

    # ── compare_offers ────────────────────────────────────────────────────────

    def _compare_offers(self, params: dict[str, Any]) -> str:
        offer_ids: list[str] = list(params.get("offer_ids") or [])
        offers = [self._session.stored_offers.get(oid) for oid in offer_ids]
        missing = [oid for oid, o in zip(offer_ids, offers, strict=True) if o is None]
        if missing:
            return json.dumps(
                {"error": f"Offer(s) not found: {missing}. Run search_flights first."}
            )

        result = compare([o for o in offers if o is not None], self._prefs)
        for row in result["offers"]:
            self._session.allowed_prices.add(str(Decimal(row["customer_price_inr"])))
            self._session.allowed_prices.add(str(Decimal(row["effective_price_inr"])))
        return json.dumps(result)

    # ── explain_ranking ───────────────────────────────────────────────────────

    def _explain_ranking(self, params: dict[str, Any]) -> str:
        offer_id = params.get("offer_id", "")
        offer = self._session.stored_offers.get(offer_id)
        if offer is None:
            return json.dumps({"error": f"Offer {offer_id!r} not found. Run search_flights first."})

        ranked = self._session.ranked_offers
        rank_pos = next((i for i, o in enumerate(ranked, 1) if o.offer_id == offer_id), 0)
        explanation = explain(offer, self._prefs, rank_pos, ranked)
        result = {
            "offer_id": offer_id,
            "rank_position": explanation.rank_position,
            "sort_mode": explanation.sort_mode.value,
            "customer_price_inr": str(offer.total_inr + offer.price.agency_markup),
            "effective_price_inr": str(explanation.effective_price_inr),
            "adjustments": [
                {"reason": a.reason, "amount_inr": str(a.amount)}
                for a in explanation.price_breakdown.adjustments
            ],
            "total_stops": explanation.total_stops,
            "total_duration_minutes": explanation.total_duration_minutes,
            "highlights": [h.value for h in explanation.highlights],
        }
        self._session.allowed_prices.add(str(Decimal(result["customer_price_inr"])))
        self._session.allowed_prices.add(str(Decimal(result["effective_price_inr"])))
        return json.dumps(result)
