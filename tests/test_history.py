"""История вопросов: последние N, пагинация, доступ только к своим записям.

Нужна БД. Без неё - skip.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from usprings_rag.api import app
from usprings_rag.collection import get_collection
from usprings_rag.db import SessionLocal
from usprings_rag.history import get_owned, paginate, recent
from usprings_rag.models import QueryLog, Role, User
from usprings_rag.security import hash_password

LOGIN = "histtest"
OTHER = "histother"
PW = "hist-pass-123"
BASE = datetime(2026, 7, 15, 10, 0, 0, tzinfo=timezone.utc)


def _make_user(db, login: str) -> int:
    db.execute(text("delete from users where login = :l"), {"l": login})
    db.commit()
    user = User(
        login=login, full_name="История", password_hash=hash_password(PW), role=Role.USER
    )
    db.add(user)
    db.commit()
    return user.id


def _make_log(db, user_id: int, i: int) -> int:
    erp = get_collection("erp")
    row = QueryLog(
        user_id=user_id,
        collection_id=erp.id,
        created_at=BASE + timedelta(minutes=i),
        question=f"вопрос {i}",
        answer=f"ответ {i}",
        outcome="answered",
        best_similarity=0.7,
        prompt_tokens=1,
        completion_tokens=1,
        total_tokens=2,
        elapsed_seconds=1.0,
        model_id="m",
        sources=[{"document_id": 1, "title": "Док", "source_path": "its_erp/d.pdf", "pages": "стр.1"}],
    )
    db.add(row)
    db.commit()
    return row.id


@pytest.fixture
def data():
    try:
        db = SessionLocal()
        db.execute(text("select 1"))
    except OperationalError:
        pytest.skip("БД недоступна")
    my_id = _make_user(db, LOGIN)
    other_id = _make_user(db, OTHER)
    my_logs = [_make_log(db, my_id, i) for i in range(5)]  # вопросы 0..4
    other_log = _make_log(db, other_id, 0)
    yield {"my_id": my_id, "my_logs": my_logs, "other_log": other_log}
    db.execute(
        text("delete from query_log where user_id in (:a, :b)"),
        {"a": my_id, "b": other_id},
    )
    db.execute(
        text("delete from users where login in (:a, :b)"), {"a": LOGIN, "b": OTHER}
    )
    db.commit()
    db.close()


def _login(client: TestClient) -> None:
    r = client.post(
        "/login", data={"login": LOGIN, "password": PW}, follow_redirects=False
    )
    assert r.status_code == 303


# --- уровень функции ---


def test_recent_returns_three_newest(data):
    with SessionLocal() as session:
        rows = recent(session, data["my_id"], 3)
    assert [r.question for r in rows] == ["вопрос 4", "вопрос 3", "вопрос 2"]


def test_paginate_counts_and_slices(data):
    with SessionLocal() as session:
        items, total = paginate(session, data["my_id"], page=1, page_size=2)
        assert total == 5
        assert [r.question for r in items] == ["вопрос 4", "вопрос 3"]
        items3, _ = paginate(session, data["my_id"], page=3, page_size=2)
        assert [r.question for r in items3] == ["вопрос 0"]


def test_get_owned_rejects_foreign(data):
    with SessionLocal() as session:
        assert get_owned(session, data["my_id"], data["my_logs"][0]) is not None
        assert get_owned(session, data["my_id"], data["other_log"]) is None


# --- эндпоинты ---


def test_api_recent_three(data):
    client = TestClient(app)
    _login(client)
    r = client.get("/api/history/recent")
    assert r.status_code == 200
    questions = [item["question"] for item in r.json()]
    assert questions == ["вопрос 4", "вопрос 3", "вопрос 2"]


def test_api_history_pagination(data):
    client = TestClient(app)
    _login(client)
    r = client.get("/api/history", params={"page": 1, "page_size": 2})
    body = r.json()
    assert body["total"] == 5
    assert body["pages"] == 3
    assert len(body["items"]) == 2


def test_api_entry_own_and_foreign(data):
    client = TestClient(app)
    _login(client)
    own = client.get(f"/api/history/{data['my_logs'][0]}")
    assert own.status_code == 200
    assert own.json()["answer"] == "ответ 0"
    foreign = client.get(f"/api/history/{data['other_log']}")
    assert foreign.status_code == 404


def test_history_entry_page_foreign_is_404(data):
    client = TestClient(app)
    _login(client)
    assert client.get(f"/history/{data['my_logs'][0]}").status_code == 200
    assert client.get(f"/history/{data['other_log']}").status_code == 404


def test_history_requires_auth(data):
    assert TestClient(app).get("/api/history/recent").status_code == 401
