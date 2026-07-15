"""Справочник пользователей (super-admin): создание с ролью, автогрант, доступ, пароль.

Нужна БД. Без неё - skip. TestClient без lifespan.
"""

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from fastapi.testclient import TestClient

from usprings_rag.api import app
from usprings_rag.db import SessionLocal
from usprings_rag.models import CollectionRow, Role, User, UserCollectionAccess
from usprings_rag.security import hash_password, verify_password

SUPER = "u8super"
PLAIN = "u8plain"
PW = "u8-pass-123"
# создаваемые тестом учётки - чистим по префиксу
MADE_PREFIX = "u8made_"


def _skip_if_no_db():
    try:
        db = SessionLocal()
        db.execute(text("select 1"))
        db.close()
    except OperationalError:
        pytest.skip("БД недоступна")


@pytest.fixture
def ctx():
    _skip_if_no_db()
    db = SessionLocal()
    db.execute(
        text("delete from users where login in (:a, :b)"), {"a": SUPER, "b": PLAIN}
    )
    db.execute(text("delete from users where login like :p"), {"p": MADE_PREFIX + "%"})
    db.commit()
    su = User(login=SUPER, full_name="Супер", password_hash=hash_password(PW),
              role=Role.SUPER_ADMIN)
    pl = User(login=PLAIN, full_name="Юзер", password_hash=hash_password(PW),
              role=Role.USER)
    db.add_all([su, pl])
    db.commit()
    yield {"super_id": su.id, "plain_id": pl.id}
    db.execute(
        text("delete from users where login in (:a, :b)"), {"a": SUPER, "b": PLAIN}
    )
    db.execute(text("delete from users where login like :p"), {"p": MADE_PREFIX + "%"})
    db.commit()
    db.close()


def _login(client, login):
    r = client.post(
        "/login", data={"login": login, "password": PW}, follow_redirects=False
    )
    assert r.status_code == 303


def test_non_super_denied(ctx):
    client = TestClient(app)
    _login(client, PLAIN)
    # HTML-страница: редирект на портал с уведомлением (не «сырой» 403)
    page = client.get("/admin/users", follow_redirects=False)
    assert page.status_code == 303 and "forbidden" in page.headers["location"]
    # API для fetch: честный 403
    assert client.get("/api/admin/users").status_code == 403


def test_create_user_role_and_autogrant(ctx):
    client = TestClient(app)
    _login(client, SUPER)
    # создаём обычного пользователя - автогрант на все активные коллекции
    r = client.post("/api/admin/users", json={
        "login": MADE_PREFIX + "user", "full_name": "Новый", "role": "user",
        "password": "new-pass-1",
    })
    assert r.status_code == 200
    new_id = r.json()["id"]
    with SessionLocal() as s:
        active = set(s.scalars(
            select(CollectionRow.id).where(CollectionRow.is_active.is_(True))
        ).all())
        granted = set(s.scalars(
            select(UserCollectionAccess.collection_id).where(
                UserCollectionAccess.user_id == new_id
            )
        ).all())
        assert granted == active and active


def test_create_collection_admin_no_autogrant(ctx):
    client = TestClient(app)
    _login(client, SUPER)
    r = client.post("/api/admin/users", json={
        "login": MADE_PREFIX + "cadmin", "full_name": "Админ колл", "role": "collection_admin",
        "password": "new-pass-2",
    })
    assert r.status_code == 200
    cadmin_id = r.json()["id"]
    with SessionLocal() as s:
        granted = s.scalars(
            select(UserCollectionAccess.collection_id).where(
                UserCollectionAccess.user_id == cadmin_id
            )
        ).all()
        assert granted == []  # доступ выдаётся отдельно


def test_create_duplicate_login_rejected(ctx):
    client = TestClient(app)
    _login(client, SUPER)
    r = client.post("/api/admin/users", json={
        "login": PLAIN, "full_name": "x", "role": "user", "password": "x-pass",
    })
    assert r.status_code == 422


def test_set_access_replaces_grants(ctx):
    client = TestClient(app)
    _login(client, SUPER)
    with SessionLocal() as s:
        erp_id = s.scalar(select(CollectionRow.id).where(CollectionRow.code == "erp"))
    r = client.put(
        f"/api/admin/users/{ctx['plain_id']}/access",
        json={"collection_ids": [erp_id]},
    )
    assert r.status_code == 200
    with SessionLocal() as s:
        granted = s.scalars(
            select(UserCollectionAccess.collection_id).where(
                UserCollectionAccess.user_id == ctx["plain_id"]
            )
        ).all()
        assert granted == [erp_id]


def test_reset_password_returns_temp_and_works(ctx):
    client = TestClient(app)
    _login(client, SUPER)
    r = client.post(f"/api/admin/users/{ctx['plain_id']}/reset-password")
    assert r.status_code == 200
    temp = r.json()["temp_password"]
    assert temp
    with SessionLocal() as s:
        user = s.get(User, ctx["plain_id"])
        assert verify_password(temp, user.password_hash)


def test_toggle_active_and_self_guard(ctx):
    client = TestClient(app)
    _login(client, SUPER)
    # деактивировать чужую учётку - можно
    r = client.post(f"/api/admin/users/{ctx['plain_id']}/active", params={"active": False})
    assert r.status_code == 200
    with SessionLocal() as s:
        assert s.get(User, ctx["plain_id"]).is_active is False
    # себя деактивировать нельзя
    r = client.post(f"/api/admin/users/{ctx['super_id']}/active", params={"active": False})
    assert r.status_code == 422
