"""RBAC: доступ к коллекциям и смена своего пароля.

Нужна БД (гранты и справочник). Без неё - skip. TestClient без lifespan: проверяем
права до обращения к модели/LLM (доступ отбивается 403 раньше).
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from usprings_rag.api import app
from usprings_rag.auth import accessible_codes
from usprings_rag.db import SessionLocal
from usprings_rag.models import CollectionRow, Role, User, UserCollectionAccess
from usprings_rag.security import hash_password

LOGIN = "accesstest"
PW = "pw-access-123"


def _skip_if_no_db():
    try:
        db = SessionLocal()
        db.execute(text("select 1"))
        db.close()
    except OperationalError:
        pytest.skip("БД недоступна")


@pytest.fixture
def erp_only_user():
    """Пользователь роли user с доступом только к коллекции erp."""
    _skip_if_no_db()
    db = SessionLocal()
    db.execute(text("delete from users where login = :l"), {"l": LOGIN})
    db.commit()
    user = User(
        login=LOGIN,
        full_name="Доступ",
        password_hash=hash_password(PW),
        role=Role.USER,
    )
    db.add(user)
    db.commit()
    erp_id = db.scalar(select(CollectionRow.id).where(CollectionRow.code == "erp"))
    db.add(UserCollectionAccess(user_id=user.id, collection_id=erp_id))
    db.commit()
    yield user
    db.execute(text("delete from users where login = :l"), {"l": LOGIN})  # каскад грантов
    db.commit()
    db.close()


def _login(client: TestClient) -> None:
    r = client.post(
        "/login", data={"login": LOGIN, "password": PW}, follow_redirects=False
    )
    assert r.status_code == 303


def test_accessible_codes_user_sees_only_granted(erp_only_user):
    with SessionLocal() as session:
        assert accessible_codes(session, erp_only_user) == {"erp"}


def test_super_admin_sees_all_active():
    _skip_if_no_db()
    # Транзиентный super-admin (не сохраняем): доступ считается без грантов.
    admin = User(
        login="x", full_name="x", password_hash="x", role=Role.SUPER_ADMIN
    )
    with SessionLocal() as session:
        assert accessible_codes(session, admin) == {"erp", "zup"}


def test_collections_filtered_by_access(erp_only_user):
    client = TestClient(app)
    _login(client)
    r = client.get("/collections")
    assert r.status_code == 200
    assert {item["code"] for item in r.json()} == {"erp"}


def test_ask_denied_for_inaccessible_collection(erp_only_user):
    client = TestClient(app)
    _login(client)
    r = client.post("/ask", json={"question": "x", "collection": "zup"})
    assert r.status_code == 403


def test_ask_stream_denied_for_inaccessible_collection(erp_only_user):
    client = TestClient(app)
    _login(client)
    r = client.get("/ask/stream", params={"question": "x", "collection": "zup"})
    assert r.status_code == 403


def test_password_change_flow(erp_only_user):
    client = TestClient(app)
    _login(client)

    # неверный старый пароль - отказ
    r = client.post(
        "/profile/password",
        data={"old_password": "nope", "new_password": "new-pw-999"},
        follow_redirects=False,
    )
    assert "error" in r.headers["location"]

    # верный старый - смена
    r = client.post(
        "/profile/password",
        data={"old_password": PW, "new_password": "new-pw-999"},
        follow_redirects=False,
    )
    assert "changed" in r.headers["location"]

    # новый пароль работает при следующем входе
    fresh = TestClient(app)
    r = fresh.post(
        "/login",
        data={"login": LOGIN, "password": "new-pw-999"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/"
