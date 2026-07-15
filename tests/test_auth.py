"""Аутентификация: защита портала, вход/выход, неактивные учётки.

TestClient создаём БЕЗ контекст-менеджера: тогда lifespan (загрузка весов BGE-m3)
не запускается. Проверяемые эндпоинты либо не требуют ресурсов, либо отбиваются
401 до обращения к ним. Флоу входа требует БД - такие тесты пропускаются без неё.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from usprings_rag.api import app
from usprings_rag.db import SessionLocal
from usprings_rag.models import Role, User
from usprings_rag.security import hash_password

client = TestClient(app)

TEST_LOGIN = "authtest"
TEST_PASSWORD = "s3cret-pass"


# --- Без БД: аноним не проходит ---


def test_anonymous_redirected_to_login():
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_login_page_available():
    assert client.get("/login").status_code == 200


def test_ask_requires_auth():
    r = client.post("/ask", json={"question": "x", "collection": "erp"})
    assert r.status_code == 401


def test_ask_stream_requires_auth():
    r = client.get("/ask/stream", params={"question": "x", "collection": "erp"})
    assert r.status_code == 401


# --- С БД: реальный вход/выход ---


@pytest.fixture
def test_user():
    try:
        db = SessionLocal()
        db.execute(text("select 1"))
    except OperationalError:
        pytest.skip("БД недоступна")
    db.execute(text("delete from users where login = :l"), {"l": TEST_LOGIN})
    db.commit()
    db.add(
        User(
            login=TEST_LOGIN,
            full_name="Тест",
            password_hash=hash_password(TEST_PASSWORD),
            role=Role.USER,
        )
    )
    db.commit()
    yield
    db.execute(text("delete from users where login = :l"), {"l": TEST_LOGIN})
    db.commit()
    db.close()


def test_wrong_password_redirects_with_error(test_user):
    r = client.post(
        "/login",
        data={"login": TEST_LOGIN, "password": "wrong"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "error" in r.headers["location"]


def test_login_then_access_then_logout(test_user):
    c = TestClient(app)  # свой cookie jar под сессию
    r = c.post(
        "/login",
        data={"login": TEST_LOGIN, "password": TEST_PASSWORD},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/"

    assert c.get("/", follow_redirects=False).status_code == 200

    r = c.post("/logout", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"

    assert c.get("/", follow_redirects=False).status_code == 303


def test_inactive_user_cannot_login(test_user):
    db = SessionLocal()
    db.execute(
        text("update users set is_active = false where login = :l"), {"l": TEST_LOGIN}
    )
    db.commit()
    db.close()
    r = client.post(
        "/login",
        data={"login": TEST_LOGIN, "password": TEST_PASSWORD},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "error" in r.headers["location"]
