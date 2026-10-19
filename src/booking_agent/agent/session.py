import uuid
from dataclasses import dataclass, field
from typing import Any

from booking_agent.domain.offer import Offer


@dataclass
class AgentSession:
    tenant_id: str
    user_id: str = ""
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    history: list[dict[str, Any]] = field(default_factory=list)
    stored_offers: dict[str, Offer] = field(default_factory=dict)
    ranked_offers: list[Offer] = field(default_factory=list)
    # Prices from tool results — used by post-check to catch invented prices.
    allowed_prices: set[str] = field(default_factory=set)
