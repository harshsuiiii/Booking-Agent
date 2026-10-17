from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from booking_agent.api.auth import Tenant, get_tenant
from booking_agent.api.models import PriceChangedOut, PricedOfferOut
from booking_agent.api.session import SearchSession
from booking_agent.connectors.errors import PriceChanged
from booking_agent.domain.offer import Offer

router = APIRouter(prefix="/v1/offers", tags=["price"])


def _find_offer(registry: dict[str, SearchSession], offer_id: str, tenant_id: str) -> Offer | None:
    for session in registry.values():
        if session.tenant_id != tenant_id:
            continue
        for group in session.groups:
            if group.primary.offer_id == offer_id:
                return group.primary
    return None


@router.post("/{offer_id}/price")
async def reprice_offer(
    offer_id: str,
    request: Request,
    tenant: Annotated[Tenant, Depends(get_tenant)],
) -> PricedOfferOut | PriceChangedOut:
    registry: dict[str, SearchSession] = request.app.state.search_registry
    offer = _find_offer(registry, offer_id, tenant.tenant_id)
    if offer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Offer not found")

    connectors = request.app.state.connectors
    connector = connectors.get(offer.supplier_id)
    if connector is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"No connector for supplier {offer.supplier_id}",
        )

    try:
        priced = await connector.price(offer)
        return PricedOfferOut(priced_offer=priced.model_dump(mode="json"))
    except PriceChanged as exc:
        return PriceChangedOut(
            offer_id=offer_id,
            old_price_inr=str(exc.old_price),
            new_price_inr=str(exc.new_price),
        )
