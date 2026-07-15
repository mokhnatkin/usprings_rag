"""Доступ пользователей к коллекциям (RBAC)

Связка user <-> collection. Для USER - право спрашивать, для COLLECTION_ADMIN -
право администрировать коллекцию. SUPER_ADMIN грантов не требует. Составной PK
исключает дубли.

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-15
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_collection_access",
        sa.Column(
            "user_id",
            sa.Integer,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "collection_id",
            sa.Integer,
            sa.ForeignKey("collections.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )


def downgrade() -> None:
    op.drop_table("user_collection_access")
