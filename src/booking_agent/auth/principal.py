"""Who is calling: a signed-in user (session cookie) or a script (API key)."""

from dataclasses import dataclass
from typing import Literal

from fastapi import HTTPException, Request, status

Role = Literal["agent", "admin"]


@dataclass(frozen=True)
class Principal:
    user_id: str
    email: str
    name: str
    role: Role
    tenant_id: str
    via: Literal["session", "api_key"]

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def get_principal(request: Request) -> Principal:
    """FastAPI dependency: the principal AuthMiddleware attached to the request."""
    principal: Principal | None = getattr(request.state, "principal", None)
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required"
        )
    return principal


def require_admin(request: Request) -> Principal:
    principal = get_principal(request)
    if not principal.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required")
    return principal
