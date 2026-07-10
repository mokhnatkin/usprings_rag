"""Индексы: HNSW по embedding (косинус) и B-tree по collection

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-10
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "chunks_embedding_hnsw_idx",
        "chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_index("documents_collection_idx", "documents", ["collection"])


def downgrade() -> None:
    op.drop_index("documents_collection_idx", table_name="documents")
    op.drop_index("chunks_embedding_hnsw_idx", table_name="chunks")
