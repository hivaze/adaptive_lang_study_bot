"""add idx_users_active_paging for proactive tick pagination

Revision ID: a1b2c3d4e5f6
Revises: e0695f4f8237
Create Date: 2026-02-23 22:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "e0695f4f8237"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "idx_users_active_paging",
        "users",
        ["telegram_id"],
        postgresql_where="is_active = TRUE",
    )


def downgrade() -> None:
    op.drop_index("idx_users_active_paging", table_name="users")
