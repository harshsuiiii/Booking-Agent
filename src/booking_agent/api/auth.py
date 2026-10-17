"""
API key authentication stub.

In production: hash the key and look it up in the tenants table.
The interface (Tenant dataclass, get_tenant dependency) stays stable when
real auth is wired in.
"""

import hashlib
import os
from dataclasses import dataclass, field

from fastapi import Header, HTTPException, status


@dataclass
class Tenant:
    tenant_id: str
    name: str
    enabled_suppliers: list[str] = field(default_factory=list)


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


# Stub registry: api_key_hash → Tenant
# Replace with a DB lookup (see db/models.py Tenant table) in production.
def _build_stub_registry() -> dict[str, Tenant]:
    dev_key = os.getenv("API_KEY", "dev-key")
    return {
        _hash_key(dev_key): Tenant(tenant_id="default", name="Default Tenant"),
    }


_STUB_REGISTRY: dict[str, Tenant] = _build_stub_registry()


async def get_tenant(x_api_key: str = Header(..., alias="X-API-Key")) -> Tenant:
    """FastAPI dependency — extracts and validates the API key header."""
    hashed = _hash_key(x_api_key)
    tenant = _STUB_REGISTRY.get(hashed)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
    return tenant
