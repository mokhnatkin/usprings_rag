"""Аналитика: агрегаты сходятся с содержимым лога; разрез по правам.

Данные изолируем во временных коллекциях (строки справочника без секций - аналитике
нужен только query_log) и все проверки ведём по ним: в общем query_log есть реальные
записи пилота, портал-агрегаты по ним недетерминированы. Часы/дни недели проверяем
на сессии с TIME ZONE 'UTC' и tz-aware временами. Нужна БД. Без неё - skip.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from fastapi.testclient import TestClient

from usprings_rag.admin import analytics
from usprings_rag.api import app
from usprings_rag.collection import invalidate_cache
from usprings_rag.db import SessionLocal
from usprings_rag.models import CollectionRow, QueryLog, Role, User, UserCollectionAccess
from usprings_rag.security import hash_password

A1 = "an_author1"
A2 = "an_author2"
CADMIN = "an_cadmin"
PW = "an-pass-123"
C1 = "an_c1"
C2 = "an_c2"
UTC = timezone.utc

# Известные времена (UTC): пн 13.07 09:00/09:30, вт 14.07 14:00, ср 15.07 10:00.
D_MON = datetime(2026, 7, 13, 9, 0, tzinfo=UTC)
D_MON2 = datetime(2026, 7, 13, 9, 30, tzinfo=UTC)
D_TUE = datetime(2026, 7, 14, 14, 0, tzinfo=UTC)
D_WED = datetime(2026, 7, 15, 10, 0, tzinfo=UTC)


def _skip_if_no_db():
    try:
        db = SessionLocal()
        db.execute(text("select 1"))
        db.close()
    except OperationalError:
        pytest.skip("БД недоступна")


def _log(db, user_id, collection_id, when, outcome, tokens, feedback, question):
    db.add(QueryLog(
        user_id=user_id, collection_id=collection_id, created_at=when,
        question=question, answer="a", outcome=outcome, best_similarity=0.7,
        prompt_tokens=tokens, completion_tokens=0, total_tokens=tokens,
        elapsed_seconds=1.0, model_id="m", sources=[],
        feedback=feedback, feedback_at=(when if feedback else None),
    ))


def _cleanup(db):
    db.execute(text("delete from query_log where question in "
                    "('как отгрузить', 'погода', 'расчет зарплаты')"))
    db.execute(text("delete from user_collection_access where collection_id in "
                    "(select id from collections where code in (:c1, :c2))"),
               {"c1": C1, "c2": C2})
    for login in (A1, A2, CADMIN):
        db.execute(text("delete from users where login = :l"), {"l": login})
    db.execute(text("delete from collections where code in (:c1, :c2)"),
               {"c1": C1, "c2": C2})
    db.commit()


@pytest.fixture
def seed():
    _skip_if_no_db()
    db = SessionLocal()
    _cleanup(db)

    a1 = User(login=A1, full_name="a1", password_hash=hash_password(PW), role=Role.USER)
    a2 = User(login=A2, full_name="a2", password_hash=hash_password(PW), role=Role.USER)
    ca = User(login=CADMIN, full_name="ca", password_hash=hash_password(PW),
              role=Role.COLLECTION_ADMIN)
    c1 = CollectionRow(code=C1, title="Врем.1", folder="an1", threshold=0.5)
    c2 = CollectionRow(code=C2, title="Врем.2", folder="an2", threshold=0.5)
    db.add_all([a1, a2, ca, c1, c2])
    db.commit()
    invalidate_cache()  # read-model должна увидеть временные коллекции
    db.add(UserCollectionAccess(user_id=ca.id, collection_id=c1.id))
    db.commit()

    _log(db, a1.id, c1.id, D_MON, "answered", 100, None, "как отгрузить")
    _log(db, a1.id, c1.id, D_MON2, "answered", 50, None, "как отгрузить")
    _log(db, a2.id, c1.id, D_TUE, "refused", 0, True, "погода")
    _log(db, a1.id, c2.id, D_WED, "answered", 200, None, "расчет зарплаты")
    db.commit()

    ids = {"a1": a1.id, "a2": a2.id, "c1": c1.id, "c2": c2.id}
    yield ids

    _cleanup(db)
    db.close()
    invalidate_cache()


@pytest.fixture
def utc_session():
    """Сессия с UTC - детерминированные extract(hour/isodow)."""
    db = SessionLocal()
    db.execute(text("SET TIME ZONE 'UTC'"))
    try:
        yield db
    finally:
        db.close()


# --- Модуль compute (сверка с логом на изолированной выборке) ---


def test_summary(seed, utc_session):
    both = {seed["c1"], seed["c2"]}
    s = analytics.compute(utc_session, both, None, None, None, per_user=True)["summary"]
    assert s["total"] == 4
    assert s["answered"] == 3 and s["refused"] == 1
    assert s["feedback"] == 1
    assert s["refused_share"] == 0.25 and s["feedback_share"] == 0.25
    assert s["tokens_total"] == 350


def test_by_hour_and_weekday_buckets(seed, utc_session):
    both = {seed["c1"], seed["c2"]}
    rep = analytics.compute(utc_session, both, None, None, None)
    hours = {b["hour"]: b["count"] for b in rep["by_hour"]}
    assert hours[9] == 2 and hours[14] == 1 and hours[10] == 1
    assert sum(hours.values()) == 4
    assert len(rep["by_hour"]) == 24  # нули заполнены

    wd = {b["weekday"]: b["count"] for b in rep["by_weekday"]}
    assert wd[D_MON.isoweekday()] == 2
    assert wd[D_TUE.isoweekday()] == 1
    assert wd[D_WED.isoweekday()] == 1
    assert len(rep["by_weekday"]) == 7


def test_top_questions_counts_and_refused(seed, utc_session):
    both = {seed["c1"], seed["c2"]}
    top = analytics.compute(utc_session, both, None, None, None)["top_questions"]
    assert top[0]["question"] == "как отгрузить" and top[0]["count"] == 2
    assert top[0]["refused"] == 0
    pogoda = next(t for t in top if t["question"] == "погода")
    assert pogoda["count"] == 1 and pogoda["refused"] == 1


def test_by_user_slice(seed, utc_session):
    both = {seed["c1"], seed["c2"]}
    rep = analytics.compute(utc_session, both, None, None, None, per_user=True)
    by_user = {u["user_login"]: u for u in rep["by_user"]}
    assert by_user[A1]["count"] == 3 and by_user[A1]["tokens"] == 350
    assert by_user[A2]["count"] == 1 and by_user[A2]["refused"] == 1


def test_scoped_to_one_collection(seed, utc_session):
    rep = analytics.compute(utc_session, {seed["c1"]}, None, None, None)
    assert rep["summary"]["total"] == 3  # c2 исключён
    assert rep["summary"]["tokens_total"] == 150


def test_collection_filter(seed, utc_session):
    both = {seed["c1"], seed["c2"]}
    rep = analytics.compute(utc_session, both, seed["c2"], None, None)
    assert rep["summary"]["total"] == 1
    assert rep["top_questions"][0]["question"] == "расчет зарплаты"


def test_per_user_off_returns_none(seed, utc_session):
    rep = analytics.compute(utc_session, {seed["c1"]}, None, None, None, per_user=False)
    assert rep["by_user"] is None


# --- Эндпоинты (права и разрез) ---


def _login(client, login):
    r = client.post("/login", data={"login": login, "password": PW},
                    follow_redirects=False)
    assert r.status_code == 303


def test_plain_user_denied(seed):
    client = TestClient(app)
    _login(client, A1)  # роль user
    page = client.get("/admin/analytics", follow_redirects=False)
    assert page.status_code == 303 and "forbidden" in page.headers["location"]
    assert client.get("/api/admin/analytics").status_code == 403


def test_collection_admin_scoped_no_user_slice(seed):
    """collection-admin видит только свою коллекцию (c1 -> 3 записи), без среза по юзерам."""
    client = TestClient(app)
    _login(client, CADMIN)
    data = client.get("/api/admin/analytics").json()
    assert data["by_user"] is None
    assert data["summary"]["total"] == 3


def test_collection_admin_foreign_collection_empty(seed):
    client = TestClient(app)
    _login(client, CADMIN)
    data = client.get("/api/admin/analytics", params={"collection": C2}).json()
    assert data["summary"]["total"] == 0  # c2 не его коллекция
