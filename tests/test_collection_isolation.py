"""Изоляция коллекций: поиск не выходит за пределы выбранной базы знаний.

Синтетические документы, а не корпус: чанки обеих коллекций получают ОДИН И ТОТ ЖЕ
вектор, то есть идеально совпадают с запросом. Если бы фильтр не работал, чужой
документ гарантированно попал бы в выдачу - проверка ловит именно утечку между
базами, а не «повезло с расстояниями».

Тесту нужна БД (секционирование - свойство схемы, в памяти его не проверить).
Без неё - skip.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from usprings_rag.collection import Collection
from usprings_rag.db import SessionLocal
from usprings_rag.models import Chunk, Document
from usprings_rag.retrieval import search

# Коллекции - значения-держатели (code/folder); тест про изоляцию секций, не про
# справочник, поэтому БД-справочник здесь не нужен.
ERP = Collection(code="erp", title="1С:ERP", folder="its_erp", threshold=0.58)
ZUP = Collection(code="zup", title="1С:ЗУП", folder="its_zup", threshold=0.55)

VECTOR = [0.1] * 1024


class FakeProvider:
    """Возвращает тот же вектор, что записан в чанки: сходство = 1.0."""

    def embed_query(self, text: str) -> list[float]:
        return VECTOR

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [VECTOR for _ in texts]


@pytest.fixture
def session():
    """Сессия с откатом: синтетика не остаётся в базе."""
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


def add_document(session, collection, title: str) -> Document:
    document = Document(
        collection=collection.code,
        title=title,
        source_path=f"{collection.folder}/{title}.pdf",
        content_hash=f"hash-{title}",
        chunks=[
            Chunk(
                collection=collection.code,
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


def test_search_in_erp_never_returns_zup_documents(session):
    add_document(session, ERP, "изоляция-erp")
    add_document(session, ZUP, "изоляция-zup")

    result = search(session, FakeProvider(), "вопрос", ERP)

    assert result.hits, "документ своей коллекции должен находиться"
    assert all(hit.source_path.startswith("its_erp/") for hit in result.hits)


def test_search_in_zup_never_returns_erp_documents(session):
    add_document(session, ERP, "изоляция-erp")
    add_document(session, ZUP, "изоляция-zup")

    result = search(session, FakeProvider(), "вопрос", ZUP)

    assert result.hits
    assert all(hit.source_path.startswith("its_zup/") for hit in result.hits)


def test_query_plan_touches_only_one_partition(session):
    """Изоляция обеспечена схемой, а не фильтром поверх выдачи: чужая секция не читается."""
    plan = "\n".join(
        row[0]
        for row in session.execute(
            text(
                "explain select id from chunks where collection = 'erp' "
                "order by embedding <=> :v limit 5"
            ),
            {"v": str(VECTOR)},
        )
    )
    assert "chunks_erp" in plan
    assert "chunks_zup" not in plan
