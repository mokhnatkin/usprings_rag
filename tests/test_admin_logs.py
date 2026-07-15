"""Просмотр журнала: super-admin по порталу, collection-admin по своим коллекциям.

Записи метим маркером в тексте вопроса и фильтруем по нему - в общей query_log
могут быть посторонние строки. Нужна БД. Без неё - skip.
"""

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from fastapi.testclient import TestClient

from usprings_rag.api import app
from usprings_rag.db import SessionLocal
from usprings_rag.models import CollectionRow, QueryLog, Role, User, UserCollectionAccess
from usprings_rag.security import hash_password

SUPER = "l8super"
CADMIN = "l8cadmin"
AUTHOR = "l8author"
PW = "l8-pass-123"
MARK = "L8LOGTEST"


def _skip_if_no_db():
    try:
        db = SessionLocal()
        db.execute(text("select 1"))
        db.close()
    except OperationalError:
        pytest.skip("БД недоступна")


def _log(db, user_id, collection_id, marker_text):
    row = QueryLog(
        user_id=user_id, collection_id=collection_id, question=marker_text,
        answer="ответ", outcome="answered", best_similarity=0.7,
        prompt_tokens=1, completion_tokens=1, total_tokens=2, elapsed_seconds=1.0,
        model_id="m", sources=[],
    )
    db.add(row)
    db.commit()
    return row.id


@pytest.fixture
def ctx():
    _skip_if_no_db()
    db = SessionLocal()
    for login in (SUPER, CADMIN, AUTHOR):
        db.execute(text("delete from users where login = :l"), {"l": login})
    db.commit()
    su = User(login=SUPER, full_name="s", password_hash=hash_password(PW), role=Role.SUPER_ADMIN)
    ca = User(login=CADMIN, full_name="c", password_hash=hash_password(PW), role=Role.COLLECTION_ADMIN)
    au = User(login=AUTHOR, full_name="a", password_hash=hash_password(PW), role=Role.USER)
    db.add_all([su, ca, au])
    db.commit()
    erp_id = db.scalar(select(CollectionRow.id).where(CollectionRow.code == "erp"))
    zup_id = db.scalar(select(CollectionRow.id).where(CollectionRow.code == "zup"))
    db.add(UserCollectionAccess(user_id=ca.id, collection_id=erp_id))
    db.commit()
    erp_log1 = _log(db, au.id, erp_id, f"{MARK} erp один")
    erp_log2 = _log(db, au.id, erp_id, f"{MARK} erp два")
    zup_log = _log(db, au.id, zup_id, f"{MARK} zup один")
    yield {"erp_log1": erp_log1, "erp_log2": erp_log2, "zup_log": zup_log}
    db.execute(text("delete from query_log where user_id = :u"), {"u": au.id})
    for login in (SUPER, CADMIN, AUTHOR):
        db.execute(text("delete from users where login = :l"), {"l": login})
    db.commit()
    db.close()


def _login(client, login):
    r = client.post("/login", data={"login": login, "password": PW}, follow_redirects=False)
    assert r.status_code == 303


def _marked(items):
    return [i for i in items if i["question"].startswith(MARK)]


def test_plain_user_denied(ctx):
    client = TestClient(app)
    _login(client, AUTHOR)
    page = client.get("/admin/logs", follow_redirects=False)
    assert page.status_code == 303 and "forbidden" in page.headers["location"]
    assert client.get("/api/admin/logs").status_code == 403


def test_super_sees_all_collections(ctx):
    client = TestClient(app)
    _login(client, SUPER)
    items = _marked(client.get("/api/admin/logs", params={"page_size": 100}).json()["items"])
    assert len(items) == 3
    assert {i["collection"] for i in items} == {"erp", "zup"}
    assert all(i["total_tokens"] == 2 for i in items)  # колонка «Токены» в списке


def test_collection_admin_sees_only_own(ctx):
    client = TestClient(app)
    _login(client, CADMIN)
    items = _marked(client.get("/api/admin/logs", params={"page_size": 100}).json()["items"])
    assert {i["collection"] for i in items} == {"erp"}
    assert len(items) == 2


def test_collection_filter(ctx):
    client = TestClient(app)
    _login(client, SUPER)
    items = _marked(
        client.get("/api/admin/logs", params={"collection": "zup", "page_size": 100}).json()["items"]
    )
    assert len(items) == 1 and items[0]["collection"] == "zup"


def test_entry_access_by_collection(ctx):
    client = TestClient(app)
    # super открывает любую
    _login(client, SUPER)
    assert client.get(f"/api/admin/logs/{ctx['zup_log']}").status_code == 200
    # collection-admin: своя коллекция - 200, чужая - 404
    ca = TestClient(app)
    _login(ca, CADMIN)
    assert ca.get(f"/api/admin/logs/{ctx['erp_log1']}").status_code == 200
    r = ca.get(f"/api/admin/logs/{ctx['zup_log']}")
    assert r.status_code == 404


def test_entry_full_text_and_diagnostics(ctx):
    client = TestClient(app)
    _login(client, SUPER)
    body = client.get(f"/api/admin/logs/{ctx['erp_log1']}").json()
    assert body["question"] == f"{MARK} erp один"
    assert body["user_login"] == AUTHOR
    assert body["collection"] == "erp"
    assert body["total_tokens"] == 2
