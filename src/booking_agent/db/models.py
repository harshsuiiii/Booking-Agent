import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String, nullable=False)
    api_key_hash: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    enabled_suppliers: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    search_logs: Mapped[list["SearchLog"]] = relationship(back_populates="tenant")


class SearchLog(Base):
    __tablename__ = "search_logs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String, ForeignKey("tenants.id"), nullable=False)
    search_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    query_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    suppliers_called: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    suppliers_succeeded: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    suppliers_failed: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
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
    offers_json: Mapped[list] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
