from dataclasses import dataclass, field
from datetime import datetime

from booking_agent.orchestrator.dedupe import DedupeGroup


@dataclass
class SourceStarted:
    supplier_id: str
    searched_at: datetime


@dataclass
class OffersPartial:
    supplier_id: str
    offers_count: int
    groups: list[DedupeGroup]  # full deduped+ranked snapshot so far


@dataclass
class SourceFailed:
    supplier_id: str
    reason: str  # "auth_error" | "rate_limited" | "bad_response" | "timeout" | "error"
    detail: str


@dataclass
class SourceTimedOut:
    supplier_id: str
    timeout_seconds: float


@dataclass
class SourceSkipped:
    supplier_id: str
    reason: str  # "circuit_open" | "multi_city_unsupported"


@dataclass
class SearchComplete:
    groups: list[DedupeGroup]
    suppliers_succeeded: list[str] = field(default_factory=list)
    suppliers_failed: list[str] = field(default_factory=list)
    suppliers_timed_out: list[str] = field(default_factory=list)
    suppliers_skipped: list[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0


SearchEvent = (
    SourceStarted | OffersPartial | SourceFailed | SourceTimedOut | SourceSkipped | SearchComplete
)
