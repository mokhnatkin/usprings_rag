"""Секционирование chunks по коллекции: изоляция поиска по базам знаний

Фильтр по коллекции обязан стоять на той же таблице, где вектор: с HNSW фильтр
применяется ПОСЛЕ обхода индекса, поэтому джойн на documents не спасает - top-k
съедали бы соседние коллекции. Отсюда денормализация `collection` в chunks и
PARTITION BY LIST: планировщик отсекает чужие секции до входа в индекс.

Данные не переносятся: старые строки лежат в коллекции `it_1c`, для которой
секции нет, а корпус всё равно пересобирается заново из папок коллекций
(docs/MVP/MVP0/backlog.md, решения 2-3 и задачи B2/B4).

id перестаёт быть уникальным сам по себе: ключ секционирования обязан входить
в PK, поэтому PK составной (id, collection). Identity-колонки на секционированных
таблицах Postgres умеет только с 17-й версии - у нас 16, поэтому последовательность
задаётся явно.

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-14
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

EMBEDDING_DIM = 1024
HNSW_OPTS = "WITH (m = 16, ef_construction = 64)"


def upgrade() -> None:
    op.execute("DROP TABLE chunks")
    op.execute("DELETE FROM documents")
    op.execute("ALTER TABLE documents ALTER COLUMN collection DROP DEFAULT")

    op.execute("CREATE SEQUENCE chunks_id_seq")
    op.execute(f"""
        CREATE TABLE chunks (
            id integer NOT NULL DEFAULT nextval('chunks_id_seq'),
            collection text NOT NULL,
            document_id integer NOT NULL
                REFERENCES documents(id) ON DELETE CASCADE,
            chunk_index integer NOT NULL,
            page_from integer,
            page_to integer,
            content text NOT NULL,
            embedding vector({EMBEDDING_DIM}) NOT NULL,
            PRIMARY KEY (id, collection)
        ) PARTITION BY LIST (collection)
    """)
    op.execute("ALTER SEQUENCE chunks_id_seq OWNED BY chunks.id")

    for code in ("erp", "zup"):
        op.execute(
            f"CREATE TABLE chunks_{code} PARTITION OF chunks FOR VALUES IN ('{code}')"
        )

    # Индекс на родителе: Postgres заводит его в каждой секции и сам создаёт
    # в секциях, добавленных позже (ingest создаёт их под новые коллекции).
    op.execute(f"""
        CREATE INDEX chunks_embedding_hnsw_idx ON chunks
        USING hnsw (embedding vector_cosine_ops) {HNSW_OPTS}
    """)


def downgrade() -> None:
    op.execute("DROP TABLE chunks")  # каскадом уходят секции и их индексы
    op.execute("DROP SEQUENCE IF EXISTS chunks_id_seq")
    op.execute(f"""
        CREATE TABLE chunks (
            id serial PRIMARY KEY,
            document_id integer NOT NULL
                REFERENCES documents(id) ON DELETE CASCADE,
            chunk_index integer NOT NULL,
            page_from integer,
            page_to integer,
            content text NOT NULL,
            embedding vector({EMBEDDING_DIM}) NOT NULL
        )
    """)
    op.execute(f"""
        CREATE INDEX chunks_embedding_hnsw_idx ON chunks
        USING hnsw (embedding vector_cosine_ops) {HNSW_OPTS}
    """)
    op.execute("ALTER TABLE documents ALTER COLUMN collection SET DEFAULT 'it_1c'")
