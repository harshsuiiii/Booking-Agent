"""
Who is calling. There is no login: every web request is the built-in visitor, an Agent of
the demo agency (sell prices only). Scripts with X-API-Key are Admin in their tenant.
"""

from dataclasses import dataclass
from typing import Literal

from fastapi import HTTPException, Request, status

Role = Literal["agent", "admin"]

DEMO_TENANT_ID = "demo-agency"


@dataclass(frozen=True)
class Principal:
    user_id: str
    email: str
    name: str
    role: Role
    tenant_id: str
    via: Literal["visitor", "api_key"]

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


# Agent, never Admin: net fares and markups must not reach an anonymous visitor.
VISITOR = Principal(
    user_id="visitor",
    email="visitor",
    name="Visitor",
    role="agent",
    tenant_id=DEMO_TENANT_ID,
    via="visitor",
)


def get_principal(request: Request) -> Principal:
    """FastAPI dependency: the principal AuthMiddleware attached to the request."""
    principal: Principal | None = getattr(request.state, "principal", None)
    return principal if principal is not None else VISITOR


def require_admin(request: Request) -> Principal:
    principal = get_principal(request)
    if not principal.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required")
    return principal
