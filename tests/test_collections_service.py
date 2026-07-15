"""Справочник коллекций в БД: read-model и создание коллекции.

Нужна БД (справочник и секции - свойство схемы). Без неё - skip.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from usprings_rag.collection import get_collection, invalidate_cache, list_collections
from usprings_rag.collections_service import create_collection
from usprings_rag.db import SessionLocal

TEST_CODE = "tst"


@pytest.fixture
def db_available():
    try:
        db = SessionLocal()
        db.execute(text("select 1"))
        db.close()
    except OperationalError:
        pytest.skip("БД недоступна")
    invalidate_cache()
    yield
    invalidate_cache()


def test_readmodel_returns_seeded_collections(db_available):
    erp = get_collection("erp")
    assert erp.title == "1С:ERP"
    assert erp.folder == "its_erp"
    assert erp.threshold == 0.58
    codes = {c.code for c in list_collections()}
    assert {"erp", "zup"} <= codes


def test_readmodel_unknown_code_raises(db_available):
    with pytest.raises(ValueError):
        get_collection("нет-такой")


def test_create_collection_makes_row_and_partition(db_available, tmp_path, monkeypatch):
    # Папку коллекции создаём во временном каталоге, чтобы не мусорить в docs/manuals.
    from usprings_rag import collections_service

    monkeypatch.setattr(collections_service.settings, "manuals_dir", str(tmp_path))

    with SessionLocal() as session:
        try:
            created = create_collection(
                session, TEST_CODE, "Тест", "its_tst", 0.5
            )
            assert created.code == TEST_CODE
            # read-model видит новую коллекцию сразу (кэш сброшен сервисом)
            assert get_collection(TEST_CODE).title == "Тест"
            assert (tmp_path / "its_tst").is_dir()
            # секция под коллекцию создана - план запроса адресует её
            plan = "\n".join(
                row[0]
                for row in session.execute(
                    text(
                        f"explain select id from chunks "
                        f"where collection = '{TEST_CODE}' limit 1"
                    )
                )
            )
            assert f"chunks_{TEST_CODE}" in plan
        finally:
            session.rollback()
            session.execute(text(f"DROP TABLE IF EXISTS chunks_{TEST_CODE}"))
            session.execute(
                text("DELETE FROM collections WHERE code = :c"), {"c": TEST_CODE}
            )
            session.commit()
            invalidate_cache()
