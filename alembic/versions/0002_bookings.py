"""bookings and booking state transitions

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-18

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "bookings",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("tenant_id", sa.String, nullable=False),
        sa.Column("idempotency_key", sa.String, nullable=False),
        sa.Column("state", sa.String, nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("priced_offer_json", sa.JSON, nullable=False),
        # Fernet-encrypted passenger list; never stored in plain text.
        sa.Column("passengers_encrypted", sa.String, nullable=False),
        sa.Column("pnr", sa.String, nullable=True),
        sa.Column("supplier_booking_ref", sa.String, nullable=True),
        sa.Column("payment_intent_id", sa.String, nullable=True),
        sa.Column("confirmation_token_hash", sa.String, nullable=True),
        sa.Column("confirmation_token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pnr_time_limit", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_detail", sa.String, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_bookings_tenant_idempotency"
        ),
    )
    op.create_index("ix_bookings_tenant_id", "bookings", ["tenant_id"])

    op.create_table(
        "booking_state_transitions",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("booking_id", sa.String, sa.ForeignKey("bookings.id"), nullable=False),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("from_state", sa.String, nullable=False),
        sa.Column("to_state", sa.String, nullable=False),
        sa.Column("actor", sa.String, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        # A history position can only be written once.
        sa.UniqueConstraint("booking_id", "seq", name="uq_booking_transitions_seq"),
    )
    op.create_index(
        "ix_booking_state_transitions_booking_id", "booking_state_transitions", ["booking_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_booking_state_transitions_booking_id", "booking_state_transitions")
    op.drop_table("booking_state_transitions")
    op.drop_index("ix_bookings_tenant_id", "bookings")
    op.drop_table("bookings")
