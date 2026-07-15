"""Журнал вопросов-ответов

query_log: привязка к пользователю и коллекции, полный текст вопроса и ответа,
расход токенов, диагностика (лучшее сходство, найденные документы, модель) и
поля обратной связи. Индексы под выборки истории и логов по пользователю/коллекции.

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-15
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "query_log",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "collection_id",
            sa.Integer,
            sa.ForeignKey("collections.id"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("question", sa.Text, nullable=False),
        sa.Column("answer", sa.Text, nullable=False),
        sa.Column("outcome", sa.Text, nullable=False),
        sa.Column("best_similarity", sa.Float, nullable=False),
        sa.Column("prompt_tokens", sa.Integer, nullable=False),
        sa.Column("completion_tokens", sa.Integer, nullable=False),
        sa.Column("total_tokens", sa.Integer, nullable=False),
        sa.Column("elapsed_seconds", sa.Float, nullable=False),
        sa.Column("model_id", sa.Text, nullable=False),
        sa.Column("sources", JSONB, nullable=True),
        sa.Column("feedback", sa.Boolean, nullable=True),
        sa.Column("feedback_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("feedback_comment", sa.Text, nullable=True),
    )
    op.create_index(
        "query_log_user_created_idx", "query_log", ["user_id", "created_at"]
    )
    op.create_index(
        "query_log_collection_created_idx",
        "query_log",
        ["collection_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("query_log")
