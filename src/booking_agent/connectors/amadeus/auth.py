"""
OAuth2 client-credentials token manager for Amadeus.

Thread-safe: uses asyncio.Lock so concurrent requests don't trigger a
token flood. Refreshes proactively 60 s before expiry.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import httpx

from booking_agent.connectors.errors import SupplierAuthError

_REFRESH_BUFFER_SECONDS = 60


class TokenManager:
    def __init__(
        self,
        base_url: str,
        client_id: str,
        client_secret: str,
        client: httpx.AsyncClient,
    ) -> None:
        self._token_url = f"{base_url}/v1/security/oauth2/token"
        self._client_id = client_id
        self._client_secret = client_secret
        self._client = client
        self._token: str | None = None
        self._expires_at: datetime | None = None
        self._lock = asyncio.Lock()

    async def get_token(self) -> str:
        """Return a valid bearer token, fetching a new one if necessary."""
        async with self._lock:
            if not self._is_valid():
                await self._fetch()
            assert self._token is not None
            return self._token

    def _is_valid(self) -> bool:
        if self._token is None or self._expires_at is None:
            return False
        return datetime.now(timezone.utc) < self._expires_at - timedelta(
            seconds=_REFRESH_BUFFER_SECONDS
        )

    async def _fetch(self) -> None:
        response = await self._client.post(
            self._token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if response.status_code in (401, 403):
            raise SupplierAuthError("amadeus", "invalid client credentials")
        response.raise_for_status()
        body = response.json()
        self._token = body["access_token"]
        self._expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=int(body["expires_in"])
        )
