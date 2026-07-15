"""Soft-delete: чанки архивного документа не попадают в выдачу, возврат восстанавливает.

Синтетический документ с вектором, идеально совпадающим с запросом (сходство 1.0):
если бы фильтр `archived_at IS NULL` не работал, архивный документ гарантированно
попал бы в top-k. Тесту нужна БД (секции - свойство схемы). Без неё - skip.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from usprings_rag.collection import Collection
from usprings_rag.db import SessionLocal
from usprings_rag.models import Chunk, Document
from usprings_rag.retrieval import search

ERP = Collection(code="erp", title="1С:ERP", folder="its_erp", threshold=0.58)
VECTOR = [0.1] * 1024


class FakeProvider:
    def embed_query(self, text: str) -> list[float]:
        return VECTOR

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [VECTOR for _ in texts]


@pytest.fixture
def session():
    try:
        db = SessionLocal()
        db.execute(text("select 1"))
    except OperationalError:
        pytest.skip("БД недоступна")
    try:
        yield db
    finally:
        db.rollback()
        db.close()


def _add_document(session, title: str) -> Document:
    document = Document(
        collection=ERP.code,
        title=title,
        source_path=f"{ERP.folder}/{title}.pdf",
        content_hash=f"hash-{title}",
        chunks=[
            Chunk(
                collection=ERP.code,
                chunk_index=0,
                page_from=1,
                page_to=1,
                content=f"текст {title}",
                embedding=VECTOR,
            )
        ],
    )
    session.add(document)
    session.flush()
    return document


def test_archived_document_excluded_and_restored(session):
    document = _add_document(session, "архивация-erp")

    # активный - находится
    result = search(session, FakeProvider(), "вопрос", ERP)
    assert any(h.document_id == document.id for h in result.hits)

    # архивный - вне выдачи
    document.archived_at = datetime.now(timezone.utc)
    session.flush()
    result = search(session, FakeProvider(), "вопрос", ERP)
    assert all(h.document_id != document.id for h in result.hits)

    # возврат из архива - снова находится (переиндексация не нужна, чанки на месте)
    document.archived_at = None
    session.flush()
    result = search(session, FakeProvider(), "вопрос", ERP)
    assert any(h.document_id == document.id for h in result.hits)
