"""
Tenant dependency for routes.

AuthMiddleware attaches a Principal to every request (the built-in visitor, or an API
key's tenant); this exposes the caller's tenant in the shape older routes expect.
"""

from dataclasses import dataclass, field

from fastapi import Request

from booking_agent.auth.principal import get_principal


@dataclass
class Tenant:
    tenant_id: str
    name: str
    enabled_suppliers: list[str] = field(default_factory=list)


async def get_tenant(request: Request) -> Tenant:
    """FastAPI dependency: the authenticated caller's tenant."""
    principal = get_principal(request)
    return Tenant(tenant_id=principal.tenant_id, name=principal.tenant_id)
