"""add whitelist mode

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-02-27 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "f6a7b8c9d0e1"
down_revision: Union[str, Sequence[str], None] = "e5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add whitelist_approved column to users
    op.add_column(
        "users",
        sa.Column(
            "whitelist_approved",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    # Create access_requests table
    op.create_table(
        "access_requests",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_username", sa.String(255), nullable=True),
        sa.Column("first_name", sa.String(255), nullable=False),
        sa.Column("language_code", sa.String(10), nullable=True),
        sa.Column("status", sa.String(10), nullable=False, server_default="pending"),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('pending','approved','rejected')",
            name="ck_access_requests_status",
        ),
    )
    op.create_index(
        "idx_access_requests_pending",
        "access_requests",
        ["status"],
        postgresql_where="status = 'pending'",
    )
    op.create_index(
        "idx_access_requests_telegram",
        "access_requests",
        ["telegram_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_access_requests_telegram", table_name="access_requests")
    op.drop_index("idx_access_requests_pending", table_name="access_requests")
    op.drop_table("access_requests")
    op.drop_column("users", "whitelist_approved")
