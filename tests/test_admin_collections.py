"""Справочник коллекций (super-admin): список, создание из UI, правка порога/статуса.

Нужна БД. Без неё - skip. Создаваемую тест-коллекцию (строка + секция + папка)
чистим в конце.
"""

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from fastapi.testclient import TestClient

from usprings_rag.api import app
from usprings_rag.collection import get_collection, invalidate_cache
from usprings_rag.db import SessionLocal
from usprings_rag.models import CollectionRow, Role, User
from usprings_rag.security import hash_password

SUPER = "c8super"
PLAIN = "c8plain"
PW = "c8-pass-123"
TEST_CODE = "c8tst"


def _skip_if_no_db():
    try:
        db = SessionLocal()
        db.execute(text("select 1"))
        db.close()
    except OperationalError:
        pytest.skip("БД недоступна")


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    _skip_if_no_db()
    # Папку новой коллекции уводим во временный каталог, чтобы не мусорить в docs.
    from usprings_rag import collections_service

    monkeypatch.setattr(collections_service.settings, "manuals_dir", str(tmp_path))
    invalidate_cache()

    db = SessionLocal()
    db.execute(
        text("delete from users where login in (:a, :b)"), {"a": SUPER, "b": PLAIN}
    )
    db.commit()
    su = User(login=SUPER, full_name="Супер", password_hash=hash_password(PW),
              role=Role.SUPER_ADMIN)
    pl = User(login=PLAIN, full_name="Юзер", password_hash=hash_password(PW),
              role=Role.USER)
    db.add_all([su, pl])
    db.commit()
    yield {"tmp": tmp_path}
    db.execute(
        text("delete from users where login in (:a, :b)"), {"a": SUPER, "b": PLAIN}
    )
    db.execute(text(f"DROP TABLE IF EXISTS chunks_{TEST_CODE}"))
    db.execute(text("delete from collections where code = :c"), {"c": TEST_CODE})
    db.commit()
    db.close()
    invalidate_cache()


def _login(client, login):
    r = client.post(
        "/login", data={"login": login, "password": PW}, follow_redirects=False
    )
    assert r.status_code == 303


def test_non_super_denied(ctx):
    client = TestClient(app)
    _login(client, PLAIN)
    page = client.get("/admin/collections", follow_redirects=False)
    assert page.status_code == 303 and "forbidden" in page.headers["location"]
    assert client.get("/api/admin/collections").status_code == 403


def test_list_includes_seeded(ctx):
    client = TestClient(app)
    _login(client, SUPER)
    r = client.get("/api/admin/collections")
    assert r.status_code == 200
    codes = {c["code"] for c in r.json()}
    assert {"erp", "zup"} <= codes


def test_create_collection_usable(ctx):
    client = TestClient(app)
    _login(client, SUPER)
    r = client.post("/api/admin/collections", json={
        "code": TEST_CODE, "title": "Тест C8", "folder": "its_c8tst", "threshold": 0.5,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == TEST_CODE and body["is_active"] is True
    # read-model сразу видит коллекцию; папка создана; секция адресуется планом
    assert get_collection(TEST_CODE).title == "Тест C8"
    assert (ctx["tmp"] / "its_c8tst").is_dir()
    with SessionLocal() as s:
        plan = "\n".join(
            row[0]
            for row in s.execute(
                text(f"explain select id from chunks where collection = '{TEST_CODE}' limit 1")
            )
        )
        assert f"chunks_{TEST_CODE}" in plan


def test_update_threshold_and_active(ctx):
    client = TestClient(app)
    _login(client, SUPER)
    created = client.post("/api/admin/collections", json={
        "code": TEST_CODE, "title": "Тест C8", "folder": "its_c8tst", "threshold": 0.5,
    }).json()
    r = client.patch(f"/api/admin/collections/{created['id']}", json={
        "title": "Тест C8 изм", "threshold": 0.61, "is_active": False,
    })
    assert r.status_code == 200
    assert r.json()["threshold"] == 0.61
    # изменение действует в read-model сразу (порог влияет на поиск без деплоя)
    assert get_collection(TEST_CODE).threshold == 0.61
    with SessionLocal() as s:
        row = s.scalar(select(CollectionRow).where(CollectionRow.code == TEST_CODE))
        assert row.is_active is False and row.title == "Тест C8 изм"
