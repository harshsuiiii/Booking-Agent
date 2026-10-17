import uuid
from datetime import date, datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

from booking_agent.domain.offer import PricedOffer


class PassengerType(str, Enum):
    ADULT = "ADULT"
    CHILD = "CHILD"
    INFANT = "INFANT"


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
    title: Optional[str] = None
    given_name: str
    family_name: str
    date_of_birth: date
    document: Optional[TravelDocument] = None


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


# Valid forward transitions only; illegal transitions raise ValueError
_TRANSITIONS: dict[BookingState, set[BookingState]] = {
    BookingState.DRAFT: {BookingState.PRICED, BookingState.EXPIRED},
    BookingState.PRICED: {BookingState.CONFIRMED_BY_USER, BookingState.EXPIRED},
    BookingState.CONFIRMED_BY_USER: {BookingState.PNR_CREATED, BookingState.FAILED},
    BookingState.PNR_CREATED: {BookingState.PAYMENT_PENDING, BookingState.CANCELLED},
    BookingState.PAYMENT_PENDING: {BookingState.PAID, BookingState.FAILED, BookingState.CANCELLED},
    BookingState.PAID: {BookingState.TICKETED, BookingState.FAILED},
    BookingState.TICKETED: set(),
    BookingState.FAILED: set(),
    BookingState.CANCELLED: set(),
    BookingState.EXPIRED: set(),
}


class StateTransition(BaseModel):
    from_state: BookingState
    to_state: BookingState
    occurred_at: datetime
    actor: str  # user_id or system component


class Booking(BaseModel):
    booking_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    idempotency_key: str
    priced_offer: PricedOffer
    passengers: list[Passenger] = Field(min_length=1)
    state: BookingState = BookingState.DRAFT
    pnr: Optional[str] = None
    transitions: list[StateTransition] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def transition_to(self, new_state: BookingState, actor: str) -> "Booking":
        allowed = _TRANSITIONS.get(self.state, set())
        if new_state not in allowed:
            raise ValueError(
                f"Illegal transition {self.state.value} → {new_state.value}. "
                f"Allowed: {[s.value for s in allowed]}"
            )
        transition = StateTransition(
            from_state=self.state,
            to_state=new_state,
            occurred_at=datetime.now(timezone.utc),
            actor=actor,
        )
        return self.model_copy(
            update={"state": new_state, "transitions": [*self.transitions, transition]}
        )
