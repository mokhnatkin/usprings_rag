"""Обратная связь «ответ неверный»: только своя запись лога.

Нужна БД. Без неё - skip.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from usprings_rag.api import app
from usprings_rag.collection import get_collection
from usprings_rag.db import SessionLocal
from usprings_rag.logging_qa import set_feedback
from usprings_rag.models import QueryLog, Role, User
from usprings_rag.security import hash_password

LOGIN = "fbtest"
OTHER = "fbother"
PW = "fb-pass-123"


def _make_user(db, login: str) -> int:
    db.execute(text("delete from users where login = :l"), {"l": login})
    db.commit()
    user = User(
        login=login, full_name="ОС", password_hash=hash_password(PW), role=Role.USER
    )
    db.add(user)
    db.commit()
    return user.id


def _make_log(db, user_id: int) -> int:
    erp = get_collection("erp")
    row = QueryLog(
        user_id=user_id,
        collection_id=erp.id,
        question="q",
        answer="a",
        outcome="answered",
        best_similarity=0.7,
        prompt_tokens=1,
        completion_tokens=1,
        total_tokens=2,
        elapsed_seconds=1.0,
        model_id="m",
        sources=[],
    )
    db.add(row)
    db.commit()
    return row.id


@pytest.fixture
def logs():
    """Два пользователя, у каждого по записи лога."""
    try:
        db = SessionLocal()
        db.execute(text("select 1"))
    except OperationalError:
        pytest.skip("БД недоступна")
    my_id = _make_user(db, LOGIN)
    other_id = _make_user(db, OTHER)
    my_log = _make_log(db, my_id)
    other_log = _make_log(db, other_id)
    yield {"my_id": my_id, "my_log": my_log, "other_log": other_log}
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


def test_set_feedback_own_row(logs):
    with SessionLocal() as session:
        assert set_feedback(session, logs["my_id"], logs["my_log"], "плохо") is True
        row = session.get(QueryLog, logs["my_log"])
        assert row.feedback is True
        assert row.feedback_comment == "плохо"
        assert row.feedback_at is not None


def test_set_feedback_foreign_row_rejected(logs):
    with SessionLocal() as session:
        assert set_feedback(session, logs["my_id"], logs["other_log"], None) is False
        row = session.get(QueryLog, logs["other_log"])
        assert row.feedback is None


def test_set_feedback_missing_row(logs):
    with SessionLocal() as session:
        assert set_feedback(session, logs["my_id"], 999_999_999, None) is False


# --- эндпоинт ---


def test_feedback_endpoint_marks_own(logs):
    client = TestClient(app)
    _login(client)
    r = client.post("/feedback", json={"log_id": logs["my_log"], "comment": "мимо"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    with SessionLocal() as session:
        row = session.get(QueryLog, logs["my_log"])
        assert row.feedback is True
        assert row.feedback_comment == "мимо"


def test_feedback_endpoint_foreign_is_404(logs):
    client = TestClient(app)
    _login(client)
    r = client.post("/feedback", json={"log_id": logs["other_log"]})
    assert r.status_code == 404


def test_feedback_requires_auth(logs):
    r = TestClient(app).post("/feedback", json={"log_id": logs["my_log"]})
    assert r.status_code == 401
