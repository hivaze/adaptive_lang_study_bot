"""add learning_plans table

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-02-26 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

# revision identifiers, used by Alembic.
revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, Sequence[str], None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "learning_plans",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.telegram_id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("current_level", sa.String(2), nullable=False),
        sa.Column("target_level", sa.String(2), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("target_end_date", sa.Date(), nullable=False),
        sa.Column("total_weeks", sa.SmallInteger(), nullable=False),
        sa.Column("plan_data", JSONB(), nullable=False),
        sa.Column("times_adapted", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("last_adapted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "current_level IN ('A1','A2','B1','B2','C1','C2')",
            name="ck_learning_plans_current_level",
        ),
        sa.CheckConstraint(
            "target_level IN ('A1','A2','B1','B2','C1','C2')",
            name="ck_learning_plans_target_level",
        ),
    )


def downgrade() -> None:
    op.drop_table("learning_plans")
