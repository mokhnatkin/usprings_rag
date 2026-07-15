"""Soft-delete документов и очередь фоновой индексации

documents.archived_at/archived_by - пометка «неактуально» (документ исключается
из поиска, но остаётся в БД для аудита и старых логов). index_job - задачи
фоновой индексации загруженного PDF со статусом.

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-15
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "documents",
        sa.Column(
            "archived_by", sa.Integer, sa.ForeignKey("users.id"), nullable=True
        ),
    )
    op.create_index("documents_archived_idx", "documents", ["archived_at"])

    op.create_table(
        "index_job",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "collection_id",
            sa.Integer,
            sa.ForeignKey("collections.id"),
            nullable=False,
        ),
        sa.Column(
            "document_id",
            sa.Integer,
            sa.ForeignKey("documents.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("source_path", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column(
            "created_by", sa.Integer, sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "index_job_status_idx", "index_job", ["status", "created_at"]
    )


def downgrade() -> None:
    op.drop_table("index_job")
    op.drop_index("documents_archived_idx", table_name="documents")
    op.drop_column("documents", "archived_by")
    op.drop_column("documents", "archived_at")
