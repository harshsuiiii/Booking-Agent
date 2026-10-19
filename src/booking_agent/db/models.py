import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from typing import Optional as _Opt  # noqa: F401 — used by SQLAlchemy mapped_column

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String, nullable=False)
    api_key_hash: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    enabled_suppliers: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    search_logs: Mapped[list["SearchLog"]] = relationship(back_populates="tenant")


class SearchLog(Base):
    __tablename__ = "search_logs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String, ForeignKey("tenants.id"), nullable=False)
    search_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    query_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    suppliers_called: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    suppliers_succeeded: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    suppliers_failed: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    latency_ms: Mapped[float] = mapped_column(Float, nullable=True)
    offer_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    tenant: Mapped["Tenant"] = relationship(back_populates="search_logs")


class SupplierCall(Base):
    __tablename__ = "supplier_calls"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    search_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    supplier_id: Mapped[str] = mapped_column(String, nullable=False)
    operation: Mapped[str] = mapped_column(String, nullable=False)  # "search" | "price" | "book"
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)  # "success" | "error" | "timeout"
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class OfferSnapshot(Base):
    """Short-retention snapshot of search results per search_id."""

    __tablename__ = "offer_snapshots"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    search_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String, nullable=False)
    offers_json: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class RuleRecord(Base):
    """Persistent storage for a markup/pricing rule."""

    __tablename__ = "rules"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(
        String, ForeignKey("tenants.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(String, default="", nullable=False)
    scope_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    action_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    audit_entries: Mapped[list["RuleAuditEntry"]] = relationship(back_populates="rule")


class RuleAuditEntry(Base):
    """Immutable record of every create/update/delete on a rule."""

    __tablename__ = "rule_audit_log"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    rule_id: Mapped[str] = mapped_column(String, ForeignKey("rules.id"), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)  # "created"|"updated"|"deleted"
    before_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    after_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    rule: Mapped["RuleRecord"] = relationship(back_populates="audit_entries")


class BookingRecord(Base):
    """Persisted booking; passengers stored encrypted."""

    __tablename__ = "bookings"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_bookings_tenant_idempotency"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False)
    # Optimistic lock: every save must match the version it loaded, then bumps it.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    priced_offer_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    # Passengers encrypted with Fernet; never stored in plain text.
    passengers_encrypted: Mapped[str] = mapped_column(String, nullable=False)
    pnr: Mapped[str | None] = mapped_column(String, nullable=True)
    supplier_booking_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    payment_intent_id: Mapped[str | None] = mapped_column(String, nullable=True)
    confirmation_token_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    confirmation_token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    pnr_time_limit: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    # Denormalised for lists, filters and dashboards (no decryption needed).
    reference: Mapped[str] = mapped_column(String, nullable=False, index=True)
    passenger_display: Mapped[str] = mapped_column(String, nullable=False)  # e.g. "A. Sharma"
    origin: Mapped[str] = mapped_column(String(3), nullable=False)
    destination: Mapped[str] = mapped_column(String(3), nullable=False)
    travel_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    supplier_id: Mapped[str] = mapped_column(String, nullable=False)
    net_inr: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    markup_inr: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    sell_inr: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")
    is_demo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    state_transitions: Mapped[list["BookingStateTransitionRecord"]] = relationship(
        back_populates="booking"
    )


class BookingStateTransitionRecord(Base):
    """Immutable audit log of every booking state change."""

    __tablename__ = "booking_state_transitions"
    __table_args__ = (UniqueConstraint("booking_id", "seq", name="uq_booking_transitions_seq"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    booking_id: Mapped[str] = mapped_column(
        String, ForeignKey("bookings.id"), nullable=False, index=True
    )
    # Position in the booking's history; transitions are append-only.
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    from_state: Mapped[str] = mapped_column(String, nullable=False)
    to_state: Mapped[str] = mapped_column(String, nullable=False)
    actor: Mapped[str] = mapped_column(String, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    booking: Mapped["BookingRecord"] = relationship(back_populates="state_transitions")


class UserRecord(Base):
    """Login user. Demo-grade: email + scrypt password hash + role, one tenant each."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    email: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False)  # "agent" | "admin"
    tenant_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class RecentSearchRecord(Base):
    """A user's recent flight searches, for the dashboard."""

    __tablename__ = "recent_searches"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String, nullable=False)
    user_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    origin: Mapped[str] = mapped_column(String(3), nullable=False)
    destination: Mapped[str] = mapped_column(String(3), nullable=False)
    departure_date: Mapped[date] = mapped_column(Date, nullable=False)
    passengers: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    cabin: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)  # "search" | "assistant"
    offer_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False, index=True
    )
