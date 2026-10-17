"""initial schema

Revision ID: 0001
Revises:
Create Date: 2025-09-01

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("name", sa.String, nullable=False),
        sa.Column("api_key_hash", sa.String, unique=True, nullable=False),
        sa.Column("enabled_suppliers", sa.JSON, nullable=False, server_default="[]"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_table(
        "search_logs",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("tenant_id", sa.String, sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("search_id", sa.String, nullable=False, index=True),
        sa.Column("query_json", sa.JSON, nullable=False),
        sa.Column("suppliers_called", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("suppliers_succeeded", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("suppliers_failed", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("latency_ms", sa.Float, nullable=True),
        sa.Column("offer_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_table(
        "supplier_calls",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("tenant_id", sa.String, nullable=False),
        sa.Column("search_id", sa.String, nullable=False),
        sa.Column("supplier_id", sa.String, nullable=False),
        sa.Column("operation", sa.String, nullable=False),
        sa.Column("latency_ms", sa.Float, nullable=True),
        sa.Column("status", sa.String, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_supplier_calls_tenant_id", "supplier_calls", ["tenant_id"])
    op.create_index("ix_supplier_calls_search_id", "supplier_calls", ["search_id"])

    op.create_table(
        "offer_snapshots",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("search_id", sa.String, nullable=False),
        sa.Column("tenant_id", sa.String, nullable=False),
        sa.Column("offers_json", sa.JSON, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_offer_snapshots_search_id", "offer_snapshots", ["search_id"])


def downgrade() -> None:
    op.drop_table("offer_snapshots")
    op.drop_table("supplier_calls")
    op.drop_table("search_logs")
    op.drop_table("tenants")
