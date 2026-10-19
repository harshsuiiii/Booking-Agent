import secrets
import uuid
from datetime import UTC, date, datetime
from enum import Enum

from pydantic import BaseModel, Field

from booking_agent.domain.offer import PricedOffer
from booking_agent.domain.search import PaxCounts


class PassengerType(str, Enum):
    ADULT = "ADULT"
    CHILD = "CHILD"
    INFANT = "INFANT"


# Age on the day of travel: adult 12+, child 2-11, infant under 2 (for the whole trip).
_CHILD_FROM, _ADULT_FROM = 2, 12

PAX_TYPE_ORDER = (PassengerType.ADULT, PassengerType.CHILD, PassengerType.INFANT)


def age_on(born: date, day: date) -> int:
    return day.year - born.year - ((day.month, day.day) < (born.month, born.day))


def passenger_mix_errors(
    pax: list[tuple[PassengerType, date]],
    priced_for: PaxCounts | None,
    first_flight: date,
    last_flight: date,
) -> list[str]:
    """
    Problems with the passengers' types and ages for this offer (empty = fine).

    pax: (type, date of birth) per passenger. priced_for: the offer's passenger mix,
    when the supplier reports it.
    """
    errors: list[str] = []
    if priced_for is not None:
        got = {t: sum(1 for p, _ in pax if p == t) for t in PAX_TYPE_ORDER}
        want = {
            PassengerType.ADULT: priced_for.adults,
            PassengerType.CHILD: priced_for.children,
            PassengerType.INFANT: priced_for.infants,
        }
        if got != want:
            errors.append(f"Offer is priced for {_describe(want)}, got {_describe(got)}.")
    for i, (pax_type, born) in enumerate(pax, start=1):
        if born > date.today():
            errors.append(f"Passenger {i}: date of birth is in the future.")
            continue
        age = age_on(born, first_flight)
        if pax_type == PassengerType.ADULT and age < _ADULT_FROM:
            errors.append(
                f"Passenger {i}: an adult must be {_ADULT_FROM} or older on the day of travel."
            )
        elif pax_type == PassengerType.CHILD and not _CHILD_FROM <= age < _ADULT_FROM:
            errors.append(
                f"Passenger {i}: a child must be {_CHILD_FROM}-{_ADULT_FROM - 1} on the day of travel."
            )
        elif pax_type == PassengerType.INFANT and age_on(born, last_flight) >= _CHILD_FROM:
            errors.append(
                f"Passenger {i}: an infant must be under {_CHILD_FROM} for the whole trip."
            )
    return errors


def _describe(counts: dict[PassengerType, int]) -> str:
    parts = [f"{n} {t.value.lower()}{'' if n == 1 else 's'}" for t, n in counts.items() if n]
    return ", ".join(parts) or "no passengers"


class DocumentType(str, Enum):
    PASSPORT = "PASSPORT"
    NATIONAL_ID = "NATIONAL_ID"


class TravelDocument(BaseModel):
    doc_type: DocumentType
    number: str
    issuing_country: str = Field(min_length=2, max_length=2)  # ISO 3166-1 alpha-2
    expiry_date: date
    nationality: str = Field(min_length=2, max_length=2)


class Passenger(BaseModel):
    passenger_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    pax_type: PassengerType
    title: str | None = None
    given_name: str
    family_name: str
    date_of_birth: date
    document: TravelDocument | None = None


_REFERENCE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I


def new_reference() -> str:
    return "BA-" + "".join(secrets.choice(_REFERENCE_ALPHABET) for _ in range(6))


class BookingState(str, Enum):
    DRAFT = "DRAFT"
    PRICED = "PRICED"
    CONFIRMED_BY_USER = "CONFIRMED_BY_USER"
    PNR_CREATED = "PNR_CREATED"
    PAYMENT_PENDING = "PAYMENT_PENDING"
    PAID = "PAID"
    TICKETED = "TICKETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


# Valid forward transitions. Illegal transitions raise ValueError.
# CANCELLED is reachable from any pre-payment state.
# FAILED is terminal (reconciliation handles paid-but-not-ticketed).
_TRANSITIONS: dict[BookingState, set[BookingState]] = {
    BookingState.DRAFT: {
        BookingState.PRICED,
        BookingState.CANCELLED,
        BookingState.EXPIRED,
    },
    BookingState.PRICED: {
        BookingState.CONFIRMED_BY_USER,
        BookingState.CANCELLED,
        BookingState.EXPIRED,
    },
    BookingState.CONFIRMED_BY_USER: {
        BookingState.PNR_CREATED,
        BookingState.FAILED,
        BookingState.CANCELLED,
    },
    BookingState.PNR_CREATED: {
        BookingState.PAYMENT_PENDING,
        BookingState.CANCELLED,
    },
    BookingState.PAYMENT_PENDING: {
        BookingState.PAID,
        BookingState.FAILED,
        BookingState.CANCELLED,
    },
    BookingState.PAID: {
        BookingState.TICKETED,
        BookingState.FAILED,
    },
    # Terminal states
    BookingState.TICKETED: set(),
    BookingState.FAILED: set(),
    BookingState.CANCELLED: set(),
    BookingState.EXPIRED: set(),
}


class StateTransition(BaseModel):
    from_state: BookingState
    to_state: BookingState
    occurred_at: datetime
    actor: str  # user_id, "system", or service component name


class Booking(BaseModel):
    booking_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    tenant_id: str = ""
    idempotency_key: str
    priced_offer: PricedOffer
    passengers: list[Passenger] = Field(min_length=1)
    state: BookingState = BookingState.DRAFT
    pnr: str | None = None  # airline booking reference (e.g. "RZPNX8")
    supplier_booking_ref: str | None = None  # supplier's internal order ID (e.g. Duffel ord_…)
    pnr_time_limit: datetime | None = None  # deadline for ticketing; auto-cancel after this
    payment_intent_id: str | None = None
    error_detail: str | None = None
    # Confirmation token is stored as a SHA-256 hash only; plain token is never persisted.
    confirmation_token_hash: str | None = None
    confirmation_token_expires_at: datetime | None = None
    transitions: list[StateTransition] = Field(default_factory=list)
    # Storage version for optimistic locking; 0 = never saved. Set by the repository.
    version: int = 0
    # Short human-readable reference shown in the UI (e.g. "BA-7KX2Q9").
    reference: str = Field(default_factory=lambda: new_reference())
    # Seeded demo data (shown with a "demo data" label), never a real customer booking.
    is_demo: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def transition_to(self, new_state: BookingState, actor: str) -> "Booking":
        allowed = _TRANSITIONS.get(self.state, set())
        if new_state not in allowed:
            raise ValueError(
                f"Illegal transition {self.state.value} → {new_state.value}. "
                f"Allowed: {[s.value for s in allowed]}"
            )
        t = StateTransition(
            from_state=self.state,
            to_state=new_state,
            occurred_at=datetime.now(UTC),
            actor=actor,
        )
        return self.model_copy(
            update={
                "state": new_state,
                "transitions": [*self.transitions, t],
                "updated_at": datetime.now(UTC),
            }
        )
