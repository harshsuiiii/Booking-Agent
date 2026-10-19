"""users, recent searches, and booking summary columns

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-18

Booking summary columns are denormalised from priced_offer_json so lists, filters and
dashboards need no decryption. Existing rows are backfilled; their passenger display
name can't be derived without the encryption key, so it becomes "—".
Batch mode keeps this working on SQLite as well as Postgres.
"""

import secrets
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("email", sa.String, nullable=False, unique=True),
        sa.Column("name", sa.String, nullable=False),
        sa.Column("role", sa.String, nullable=False),
        sa.Column("tenant_id", sa.String, nullable=False),
        sa.Column("password_hash", sa.String, nullable=False),
        sa.Column("is_demo", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_users_tenant_id", "users", ["tenant_id"])

    op.create_table(
        "recent_searches",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("tenant_id", sa.String, nullable=False),
        sa.Column("user_id", sa.String, nullable=False),
        sa.Column("origin", sa.String(3), nullable=False),
        sa.Column("destination", sa.String(3), nullable=False),
        sa.Column("departure_date", sa.Date, nullable=False),
        sa.Column("passengers", sa.Integer, nullable=False, server_default="1"),
        sa.Column("cabin", sa.String, nullable=False),
        sa.Column("source", sa.String, nullable=False),
        sa.Column("offer_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_recent_searches_user_id", "recent_searches", ["user_id"])
    op.create_index("ix_recent_searches_created_at", "recent_searches", ["created_at"])

    with op.batch_alter_table("bookings") as batch:
        batch.add_column(sa.Column("reference", sa.String, nullable=True))
        batch.add_column(sa.Column("passenger_display", sa.String, nullable=True))
        batch.add_column(sa.Column("origin", sa.String(3), nullable=True))
        batch.add_column(sa.Column("destination", sa.String(3), nullable=True))
        batch.add_column(sa.Column("travel_date", sa.Date, nullable=True))
        batch.add_column(sa.Column("supplier_id", sa.String, nullable=True))
        batch.add_column(sa.Column("net_inr", sa.Numeric(12, 2), nullable=True))
        batch.add_column(sa.Column("markup_inr", sa.Numeric(12, 2), nullable=True))
        batch.add_column(sa.Column("sell_inr", sa.Numeric(12, 2), nullable=True))
        batch.add_column(sa.Column("currency", sa.String(3), nullable=True))
        batch.add_column(
            sa.Column("is_demo", sa.Boolean, nullable=False, server_default=sa.false())
        )

    _backfill()

    with op.batch_alter_table("bookings") as batch:
        for name in (
            "reference", "passenger_display", "origin", "destination", "travel_date",
            "supplier_id", "net_inr", "markup_inr", "sell_inr", "currency",
        ):
            batch.alter_column(name, nullable=False)
        batch.create_index("ix_bookings_reference", ["reference"])
        batch.create_index("ix_bookings_travel_date", ["travel_date"])


def _backfill() -> None:
    conn = op.get_bind()
    bookings = sa.table(
        "bookings",
        sa.column("id", sa.String),
        sa.column("priced_offer_json", sa.JSON),
        sa.column("reference", sa.String),
        sa.column("passenger_display", sa.String),
        sa.column("origin", sa.String),
        sa.column("destination", sa.String),
        sa.column("travel_date", sa.Date),
        sa.column("supplier_id", sa.String),
        sa.column("net_inr", sa.Numeric),
        sa.column("markup_inr", sa.Numeric),
        sa.column("sell_inr", sa.Numeric),
        sa.column("currency", sa.String),
    )
    for row in conn.execute(sa.select(bookings.c.id, bookings.c.priced_offer_json)).all():
        offer: dict[str, Any] = row.priced_offer_json or {}
        segments = offer["slices"][0]["segments"]
        net = Decimal(str(offer["total_inr"]))
        markup = Decimal(str(offer["price"].get("agency_markup", "0")))
        conn.execute(
            bookings.update()
            .where(bookings.c.id == row.id)
            .values(
                reference="BA-" + "".join(secrets.choice(_ALPHABET) for _ in range(6)),
                passenger_display="—",
                origin=segments[0]["origin"],
                destination=segments[-1]["destination"],
                travel_date=datetime.fromisoformat(segments[0]["departure_at"]).date(),
                supplier_id=offer["supplier_id"],
                net_inr=net,
                markup_inr=markup,
                sell_inr=net + markup,
                currency="INR",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("bookings") as batch:
        batch.drop_index("ix_bookings_travel_date")
        batch.drop_index("ix_bookings_reference")
        for name in (
            "is_demo", "currency", "sell_inr", "markup_inr", "net_inr", "supplier_id",
            "travel_date", "destination", "origin", "passenger_display", "reference",
        ):
            batch.drop_column(name)
    op.drop_index("ix_recent_searches_created_at", "recent_searches")
    op.drop_index("ix_recent_searches_user_id", "recent_searches")
    op.drop_table("recent_searches")
    op.drop_index("ix_users_tenant_id", "users")
    op.drop_table("users")
