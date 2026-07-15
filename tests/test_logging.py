"""Запись вопросов-ответов в query_log: поля, отказ, устойчивость к сбою.

Нужна БД. Без неё - skip.
"""

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from usprings_rag.answer import Answer, Source
from usprings_rag.collection import Collection, get_collection
from usprings_rag.db import SessionLocal
from usprings_rag.logging_qa import log_query
from usprings_rag.models import QueryLog, Role, User
from usprings_rag.security import hash_password

LOGIN = "logtest"


@pytest.fixture
def user_and_erp():
    try:
        db = SessionLocal()
        db.execute(text("select 1"))
    except OperationalError:
        pytest.skip("БД недоступна")
    db.execute(text("delete from users where login = :l"), {"l": LOGIN})
    db.commit()
    user = User(
        login=LOGIN, full_name="Лог", password_hash=hash_password("x"), role=Role.USER
    )
    db.add(user)
    db.commit()
    user_id = user.id
    yield user_id, get_collection("erp")
    db.execute(text("delete from query_log where user_id = :u"), {"u": user_id})
    db.execute(text("delete from users where login = :l"), {"l": LOGIN})
    db.commit()
    db.close()


def _latest(session, user_id) -> QueryLog:
    return session.scalars(
        select(QueryLog)
        .where(QueryLog.user_id == user_id)
        .order_by(QueryLog.id.desc())
    ).first()


def test_answered_row_has_full_fields(user_and_erp):
    user_id, erp = user_and_erp
    answer = Answer(
        text="Полный ответ по инструкции.",
        refused=False,
        sources=[Source(document_id=7, title="Отгрузка", source_path="its_erp/a.pdf", pages="стр.1")],
        best_similarity=0.7321,
        elapsed_seconds=2.5,
        model_id="qwen",
        prompt_tokens=120,
        completion_tokens=40,
    )
    with SessionLocal() as session:
        log_query(session, user_id, erp, "Как отгрузить?", answer)
        row = _latest(session, user_id)
        assert row.outcome == "answered"
        assert row.question == "Как отгрузить?"
        assert row.answer == "Полный ответ по инструкции."
        assert row.collection_id == erp.id
        assert row.best_similarity == pytest.approx(0.7321)
        assert (row.prompt_tokens, row.completion_tokens, row.total_tokens) == (120, 40, 160)
        assert row.model_id == "qwen"
        assert row.sources == [{"document_id": 7, "title": "Отгрузка"}]


def test_refused_row_has_zero_tokens(user_and_erp):
    user_id, erp = user_and_erp
    answer = Answer(
        text="К сожалению, в инструкциях 1С:ERP нет информации...",
        refused=True,
        sources=[],
        best_similarity=0.41,
        elapsed_seconds=0.12,
    )
    with SessionLocal() as session:
        log_query(session, user_id, erp, "погода?", answer)
        row = _latest(session, user_id)
        assert row.outcome == "refused"
        assert (row.prompt_tokens, row.completion_tokens, row.total_tokens) == (0, 0, 0)
        assert row.model_id == ""
        assert row.sources == []


def test_stream_answer_text_overrides_empty(user_and_erp):
    # При стриме answer.text пуст; в лог идёт собранный текст из answer_text.
    user_id, erp = user_and_erp
    answer = Answer(
        text="",
        refused=False,
        sources=[],
        best_similarity=0.7,
        elapsed_seconds=1.0,
        model_id="m",
        prompt_tokens=5,
        completion_tokens=3,
    )
    with SessionLocal() as session:
        log_query(session, user_id, erp, "вопрос", answer, answer_text="Собранный ответ.")
        row = _latest(session, user_id)
        assert row.answer == "Собранный ответ."


def test_log_failure_does_not_raise(user_and_erp):
    # collection.id=None -> NOT NULL нарушение при вставке; log_query должен
    # проглотить сбой, не пробросив исключение пользователю.
    user_id, _ = user_and_erp
    broken = Collection(code="erp", title="1С:ERP", folder="its_erp", threshold=0.58)
    answer = Answer(text="x", refused=False, sources=[], best_similarity=0.7, elapsed_seconds=1.0)
    with SessionLocal() as session:
        log_query(session, user_id, broken, "вопрос", answer)  # не должно бросить
        # запись не создалась
        assert _latest(session, user_id) is None
