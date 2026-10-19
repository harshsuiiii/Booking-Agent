"""
BookingRepository: protocol, in-memory implementation, and Postgres implementation.

Both implementations enforce the same rules:
  - save() is a compare-and-set on Booking.version. Saving a stale copy raises
    BookingConflict, so two concurrent confirms cannot both proceed.
  - (tenant_id, idempotency_key) is unique; a second booking with the same key
    raises IdempotentReplay.
  - save() returns the stored copy with its new version; callers must use it.

Passengers are stored Fernet-encrypted in Postgres and never logged.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from booking_agent.booking.encryption import decrypt_passengers, encrypt_passengers
from booking_agent.booking.errors import BookingConflict, IdempotentReplay
from booking_agent.db.models import BookingRecord, BookingStateTransitionRecord
from booking_agent.domain.booking import Booking, BookingState, Passenger, StateTransition
from booking_agent.domain.offer import PricedOffer


@dataclass(frozen=True)
class BookingSummary:
    """One booking as a list/dashboard row. No passenger PII beyond a short display name."""

    booking_id: str
    reference: str
    tenant_id: str
    passenger_display: str
    origin: str
    destination: str
    travel_date: date
    supplier_id: str
    state: BookingState
    net_inr: Decimal
    markup_inr: Decimal
    sell_inr: Decimal
    currency: str
    pnr: str | None
    is_demo: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class BookingQuery:
    status: BookingState | None = None
    travel_from: date | None = None
    travel_to: date | None = None
    q: str | None = None  # reference, PNR, passenger or airport code
    page: int = 1
    page_size: int = 20


def passenger_display(passengers: list[Passenger]) -> str:
    """ "A. Sharma", or "A. Sharma +2" for groups. Full names stay encrypted."""
    lead = passengers[0]
    name = f"{lead.given_name[:1].upper()}. {lead.family_name}"
    return f"{name} +{len(passengers) - 1}" if len(passengers) > 1 else name


def summarize(b: Booking) -> BookingSummary:
    offer = b.priced_offer
    outbound = offer.slices[0]
    markup = offer.price.agency_markup
    return BookingSummary(
        booking_id=b.booking_id,
        reference=b.reference,
        tenant_id=b.tenant_id,
        passenger_display=passenger_display(b.passengers),
        origin=outbound.origin,
        destination=outbound.destination,
        travel_date=outbound.departure_at.date(),
        supplier_id=offer.supplier_id,
        state=b.state,
        net_inr=offer.total_inr,
        markup_inr=markup,
        sell_inr=offer.total_inr + markup,
        currency="INR",
        pnr=b.pnr,
        is_demo=b.is_demo,
        created_at=b.created_at,
        updated_at=b.updated_at,
    )


def _matches(s: BookingSummary, query: BookingQuery) -> bool:
    if query.status is not None and s.state != query.status:
        return False
    if query.travel_from is not None and s.travel_date < query.travel_from:
        return False
    if query.travel_to is not None and s.travel_date > query.travel_to:
        return False
    if query.q:
        needle = query.q.strip().lower()
        haystack = " ".join(
            [s.reference, s.passenger_display, s.origin, s.destination, s.pnr or ""]
        ).lower()
        if needle not in haystack:
            return False
    return True


@runtime_checkable
class BookingRepository(Protocol):
    async def save(self, booking: Booking) -> Booking: ...
    async def get(self, booking_id: str) -> Booking | None: ...
    async def get_by_idempotency_key(
        self, idempotency_key: str, tenant_id: str
    ) -> Booking | None: ...
    async def list_by_tenant(self, tenant_id: str) -> list[Booking]: ...
    async def summaries(self, tenant_id: str) -> list[BookingSummary]: ...
    async def query(
        self, tenant_id: str, query: BookingQuery
    ) -> tuple[list[BookingSummary], int]: ...


class InMemoryBookingRepository:
    """In-process store for tests and Docker-free local runs. Not durable."""

    def __init__(self) -> None:
        self._bookings: dict[str, Booking] = {}

    async def save(self, booking: Booking) -> Booking:
        stored = self._bookings.get(booking.booking_id)
        current_version = stored.version if stored else 0
        if booking.version != current_version:
            raise BookingConflict(booking.booking_id)
        if stored is None:
            dup = await self.get_by_idempotency_key(booking.idempotency_key, booking.tenant_id)
            if dup is not None:
                raise IdempotentReplay(dup.booking_id)
        saved = booking.model_copy(update={"version": current_version + 1})
        self._bookings[booking.booking_id] = saved
        return saved

    async def get(self, booking_id: str) -> Booking | None:
        return self._bookings.get(booking_id)

    async def get_by_idempotency_key(self, idempotency_key: str, tenant_id: str) -> Booking | None:
        for b in self._bookings.values():
            if b.idempotency_key == idempotency_key and b.tenant_id == tenant_id:
                return b
        return None

    async def list_by_tenant(self, tenant_id: str) -> list[Booking]:
        return [b for b in self._bookings.values() if b.tenant_id == tenant_id]

    async def summaries(self, tenant_id: str) -> list[BookingSummary]:
        rows = [summarize(b) for b in self._bookings.values() if b.tenant_id == tenant_id]
        return sorted(rows, key=lambda r: r.created_at, reverse=True)

    async def query(self, tenant_id: str, query: BookingQuery) -> tuple[list[BookingSummary], int]:
        rows = [r for r in await self.summaries(tenant_id) if _matches(r, query)]
        start = (query.page - 1) * query.page_size
        return rows[start : start + query.page_size], len(rows)


class SqlBookingRepository:
    """Postgres or SQLite store. Each save is one transaction: booking row + new transitions."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def save(self, booking: Booking) -> Booking:
        new_version = booking.version + 1
        fields = _row_fields(booking)
        async with self._sessions() as db, db.begin():
            if booking.version == 0:
                db.add(BookingRecord(id=booking.booking_id, version=new_version, **fields))
                try:
                    await db.flush()
                except IntegrityError as exc:
                    raise await self._insert_conflict(booking) from exc
                already_stored = 0
            else:
                result = await db.execute(
                    update(BookingRecord)
                    .where(
                        BookingRecord.id == booking.booking_id,
                        BookingRecord.version == booking.version,
                    )
                    .values(version=new_version, **fields)
                )
                if result.rowcount != 1:  # type: ignore[attr-defined]
                    raise BookingConflict(booking.booking_id)
                already_stored = await _transition_count(db, booking.booking_id)

            # History is append-only: insert only transitions not yet stored.
            for seq, t in enumerate(booking.transitions[already_stored:], start=already_stored):
                db.add(
                    BookingStateTransitionRecord(
                        booking_id=booking.booking_id,
                        seq=seq,
                        from_state=t.from_state.value,
                        to_state=t.to_state.value,
                        actor=t.actor,
                        occurred_at=t.occurred_at,
                    )
                )
        return booking.model_copy(update={"version": new_version})

    async def get(self, booking_id: str) -> Booking | None:
        async with self._sessions() as db:
            row = await db.get(BookingRecord, booking_id)
            return await _to_domain(db, row) if row else None

    async def get_by_idempotency_key(self, idempotency_key: str, tenant_id: str) -> Booking | None:
        async with self._sessions() as db:
            row = (
                await db.execute(
                    select(BookingRecord).where(
                        BookingRecord.tenant_id == tenant_id,
                        BookingRecord.idempotency_key == idempotency_key,
                    )
                )
            ).scalar_one_or_none()
            return await _to_domain(db, row) if row else None

    async def list_by_tenant(self, tenant_id: str) -> list[Booking]:
        async with self._sessions() as db:
            rows = (
                await db.execute(
                    select(BookingRecord)
                    .where(BookingRecord.tenant_id == tenant_id)
                    .order_by(BookingRecord.created_at.desc())
                )
            ).scalars()
            return [await _to_domain(db, r) for r in rows]

    async def summaries(self, tenant_id: str) -> list[BookingSummary]:
        rows, _ = await self._select(tenant_id, BookingQuery(page_size=0))
        return rows

    async def query(self, tenant_id: str, query: BookingQuery) -> tuple[list[BookingSummary], int]:
        return await self._select(tenant_id, query)

    async def _select(
        self, tenant_id: str, query: BookingQuery
    ) -> tuple[list[BookingSummary], int]:
        conditions = [BookingRecord.tenant_id == tenant_id]
        if query.status is not None:
            conditions.append(BookingRecord.state == query.status.value)
        if query.travel_from is not None:
            conditions.append(BookingRecord.travel_date >= query.travel_from)
        if query.travel_to is not None:
            conditions.append(BookingRecord.travel_date <= query.travel_to)
        if query.q and query.q.strip():
            like = f"%{query.q.strip().lower()}%"
            conditions.append(
                or_(
                    func.lower(BookingRecord.reference).like(like),
                    func.lower(BookingRecord.passenger_display).like(like),
                    func.lower(BookingRecord.origin).like(like),
                    func.lower(BookingRecord.destination).like(like),
                    func.lower(func.coalesce(BookingRecord.pnr, "")).like(like),
                )
            )
        stmt = select(BookingRecord).where(*conditions).order_by(BookingRecord.created_at.desc())
        if query.page_size > 0:
            stmt = stmt.offset((query.page - 1) * query.page_size).limit(query.page_size)
        async with self._sessions() as db:
            total = (
                await db.execute(select(func.count()).select_from(BookingRecord).where(*conditions))
            ).scalar_one()
            rows = (await db.execute(stmt)).scalars().all()
        return [_to_summary(r) for r in rows], int(total)

    async def _insert_conflict(self, booking: Booking) -> Exception:
        dup = await self.get_by_idempotency_key(booking.idempotency_key, booking.tenant_id)
        if dup is not None and dup.booking_id != booking.booking_id:
            return IdempotentReplay(dup.booking_id)
        return BookingConflict(booking.booking_id)


def _row_fields(b: Booking) -> dict[str, object]:
    return {
        "tenant_id": b.tenant_id,
        "idempotency_key": b.idempotency_key,
        "state": b.state.value,
        "priced_offer_json": b.priced_offer.model_dump(mode="json"),
        "passengers_encrypted": encrypt_passengers(b.passengers),
        "pnr": b.pnr,
        "supplier_booking_ref": b.supplier_booking_ref,
        "payment_intent_id": b.payment_intent_id,
        "confirmation_token_hash": b.confirmation_token_hash,
        "confirmation_token_expires_at": b.confirmation_token_expires_at,
        "pnr_time_limit": b.pnr_time_limit,
        "error_detail": b.error_detail,
        "created_at": b.created_at,
        "updated_at": b.updated_at,
        **_summary_fields(b),
    }


def _summary_fields(b: Booking) -> dict[str, object]:
    s = summarize(b)
    return {
        "reference": s.reference,
        "passenger_display": s.passenger_display,
        "origin": s.origin,
        "destination": s.destination,
        "travel_date": s.travel_date,
        "supplier_id": s.supplier_id,
        "net_inr": s.net_inr,
        "markup_inr": s.markup_inr,
        "sell_inr": s.sell_inr,
        "currency": s.currency,
        "is_demo": s.is_demo,
    }


def _utc(dt: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; everything is stored in UTC."""
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def _to_summary(row: BookingRecord) -> BookingSummary:
    return BookingSummary(
        booking_id=row.id,
        reference=row.reference,
        tenant_id=row.tenant_id,
        passenger_display=row.passenger_display,
        origin=row.origin,
        destination=row.destination,
        travel_date=row.travel_date,
        supplier_id=row.supplier_id,
        state=BookingState(row.state),
        net_inr=Decimal(row.net_inr),
        markup_inr=Decimal(row.markup_inr),
        sell_inr=Decimal(row.sell_inr),
        currency=row.currency,
        pnr=row.pnr,
        is_demo=row.is_demo,
        created_at=_utc(row.created_at) or row.created_at,
        updated_at=_utc(row.updated_at) or row.updated_at,
    )


async def _transition_count(db: AsyncSession, booking_id: str) -> int:
    rows = await db.execute(
        select(BookingStateTransitionRecord.id).where(
            BookingStateTransitionRecord.booking_id == booking_id
        )
    )
    return len(rows.all())


async def _to_domain(db: AsyncSession, row: BookingRecord) -> Booking:
    transitions = (
        await db.execute(
            select(BookingStateTransitionRecord)
            .where(BookingStateTransitionRecord.booking_id == row.id)
            .order_by(BookingStateTransitionRecord.seq)
        )
    ).scalars()
    return Booking(
        booking_id=row.id,
        tenant_id=row.tenant_id,
        idempotency_key=row.idempotency_key,
        priced_offer=PricedOffer.model_validate(row.priced_offer_json),
        passengers=decrypt_passengers(row.passengers_encrypted),
        state=BookingState(row.state),
        pnr=row.pnr,
        supplier_booking_ref=row.supplier_booking_ref,
        pnr_time_limit=_utc(row.pnr_time_limit),
        payment_intent_id=row.payment_intent_id,
        error_detail=row.error_detail,
        confirmation_token_hash=row.confirmation_token_hash,
        confirmation_token_expires_at=_utc(row.confirmation_token_expires_at),
        transitions=[
            StateTransition(
                from_state=BookingState(t.from_state),
                to_state=BookingState(t.to_state),
                occurred_at=_utc(t.occurred_at) or t.occurred_at,
                actor=t.actor,
            )
            for t in transitions
        ],
        version=row.version,
        reference=row.reference,
        is_demo=row.is_demo,
        created_at=_utc(row.created_at) or row.created_at,
        updated_at=_utc(row.updated_at) or row.updated_at,
    )
