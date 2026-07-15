"""Запись вопросов-ответов в query_log.

Best-effort: сбой записи не должен ронять ответ пользователю (оборачиваем в
try/except, сам сбой логируем). Пишем и ответы, и отказы - по best_similarity
видно поведение порога на реальных вопросах, а сохранённая пара «вопрос-ответ» +
источники нужны для истории и разбора жалоб.
"""

import logging

from sqlalchemy import func
from sqlalchemy.orm import Session

from .answer import Answer
from .collection import Collection
from .models import QueryLog

logger = logging.getLogger(__name__)


def log_query(
    session: Session,
    user_id: int,
    collection: Collection,
    question: str,
    answer: Answer,
    answer_text: str | None = None,
) -> int | None:
    """Записать один вопрос-ответ, вернуть id строки (для привязки обратной связи).

    `answer_text` - для стрима (текст уже ушёл дельтами, `answer.text` пуст), иначе
    берём `answer.text`. При сбое возвращаем None - ответ пользователю не затрагиваем.
    """
    text = answer.text if answer_text is None else answer_text
    row = QueryLog(
        user_id=user_id,
        collection_id=collection.id,
        question=question,
        answer=text,
        outcome="refused" if answer.refused else "answered",
        best_similarity=answer.best_similarity,
        prompt_tokens=answer.prompt_tokens,
        completion_tokens=answer.completion_tokens,
        total_tokens=answer.prompt_tokens + answer.completion_tokens,
        elapsed_seconds=answer.elapsed_seconds,
        model_id=answer.model_id,
        sources=[
            {
                "document_id": s.document_id,
                "title": s.title,
                "source_path": s.source_path,
                "pages": s.pages,
            }
            for s in answer.sources
        ],
    )
    try:
        session.add(row)
        session.commit()
        return row.id
    except Exception:
        session.rollback()
        logger.exception("не удалось записать query_log (ответ пользователю не затронут)")
        return None


def set_feedback(
    session: Session, user_id: int, log_id: int, comment: str | None
) -> bool:
    """Пометить ответ неверным. False - записи нет или она чужая (не раскрываем что
    именно). Отметка идёт в ту же строку лога - вопрос, ответ и диагностика уже там."""
    row = session.get(QueryLog, log_id)
    if row is None or row.user_id != user_id:
        return False
    row.feedback = True
    row.feedback_at = func.now()
    row.feedback_comment = comment or None
    session.commit()
    return True
