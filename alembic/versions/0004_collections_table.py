"""Справочник коллекций в БД (переезд из enum)

Коллекции перестают быть enum в коде и становятся строками таблицы `collections`.
Сид повторяет прежний справочник `collection.py` на момент переезда: `erp` (0.58)
и `zup` (0.55). `code` - стабильный ключ, им назван раздел секции `chunks`.

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-15
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    collections = op.create_table(
        "collections",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("code", sa.Text, nullable=False, unique=True),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("folder", sa.Text, nullable=False),
        sa.Column("threshold", sa.Float, nullable=False),
        sa.Column(
            "is_active", sa.Boolean, nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.bulk_insert(
        collections,
        [
            {
                "code": "erp",
                "title": "1С:ERP",
                "folder": "its_erp",
                "threshold": 0.58,
                "is_active": True,
            },
            {
                "code": "zup",
                "title": "1С:ЗУП",
                "folder": "its_zup",
                "threshold": 0.55,
                "is_active": True,
            },
        ],
    )


def downgrade() -> None:
    op.drop_table("collections")
