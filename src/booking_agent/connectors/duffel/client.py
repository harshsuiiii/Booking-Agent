"""HTTP header builder and error mapper for Duffel API v2."""

import httpx

from booking_agent.connectors.errors import (
    FareExpired,
    SupplierAuthError,
    SupplierBadResponse,
    SupplierRateLimited,
)
from booking_agent.observability.redaction import redact_secrets


def build_duffel_headers(token: str, api_version: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Duffel-Version": api_version,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def raise_for_duffel_error(
    response: httpx.Response,
    supplier_id: str,
    operation: str,
) -> None:
    status = response.status_code
    if status in (200, 201):
        return

    code = ""
    title = ""
    try:
        errors = response.json().get("errors", [])
        if errors:
            code = errors[0].get("code", "")
            title = errors[0].get("title", "")
    except Exception:
        pass

    if status in (401, 403):
        raise SupplierAuthError(supplier_id, title or "authentication failed")
    if status == 422 and "offer_no_longer_available" in code:
        raise FareExpired(supplier_id, offer_ref=operation)
    if status == 429:
        retry_after = int(response.headers.get("Retry-After", 0)) or None
        raise SupplierRateLimited(supplier_id, retry_after=retry_after)
    raise SupplierBadResponse(
        supplier_id=supplier_id,
        status_code=status,
        body=redact_secrets(response.text[:500]),
        operation=operation,
    )
