"""
FxProvider protocol and concrete implementations used by DuffelConnector.

FrankfurterFxProvider: live ECB rates from frankfurter.app (no API key required).
FixedFxProvider: deterministic rates for tests.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol, runtime_checkable

import httpx

from booking_agent.connectors.errors import SupplierBadResponse


@runtime_checkable
class FxProvider(Protocol):
    async def get_rate_to_inr(
        self, from_currency: str, client: httpx.AsyncClient
    ) -> tuple[Decimal, datetime]: ...


class FrankfurterFxProvider:
    """Live FX rates from Frankfurter (ECB data, free, no key). Rates cached for an hour.

    api.frankfurter.app now permanently redirects to api.frankfurter.dev/v1 (found
    2026-10-18), so call the new endpoint directly.
    """

    URL = "https://api.frankfurter.dev/v1/latest"
    TTL = timedelta(hours=1)

    def __init__(self) -> None:
        self._cache: dict[str, tuple[Decimal, datetime]] = {}

    async def get_rate_to_inr(
        self, from_currency: str, client: httpx.AsyncClient
    ) -> tuple[Decimal, datetime]:
        if from_currency == "INR":
            return Decimal("1"), datetime.now(UTC)
        cached = self._cache.get(from_currency)
        if cached and datetime.now(UTC) - cached[1] < self.TTL:
            return cached
        try:
            resp = await client.get(self.URL, params={"base": from_currency, "symbols": "INR"})
            resp.raise_for_status()
            rate = Decimal(str(resp.json()["rates"]["INR"]))
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise SupplierBadResponse(
                "fx",
                getattr(getattr(exc, "response", None), "status_code", 0),
                f"no {from_currency}->INR rate from Frankfurter: {type(exc).__name__}",
                "fx",
            ) from exc
        self._cache[from_currency] = (rate, datetime.now(UTC))
        return self._cache[from_currency]


class FixedFxProvider:
    """Deterministic FX provider for unit tests; no network calls."""

    def __init__(self, rates: dict[str, Decimal]) -> None:
        self._rates = rates

    async def get_rate_to_inr(
        self, from_currency: str, client: httpx.AsyncClient
    ) -> tuple[Decimal, datetime]:
        if from_currency == "INR":
            return Decimal("1"), datetime.now(UTC)
        return self._rates[from_currency], datetime.now(UTC)
