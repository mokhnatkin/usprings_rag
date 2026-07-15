"""Запись вопросов-ответов в query_log.

Best-effort: сбой записи не должен ронять ответ пользователю (оборачиваем в
try/except, сам сбой логируем). Пишем и ответы, и отказы - по best_similarity
видно поведение порога на реальных вопросах, а сохранённая пара «вопрос-ответ» +
источники нужны для истории и разбора жалоб.
"""

import logging

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
) -> None:
    """Записать один вопрос-ответ. `answer_text` - для стрима (текст уже ушёл
    дельтами, `answer.text` пуст), иначе берём `answer.text`."""
    text = answer.text if answer_text is None else answer_text
    try:
        session.add(
            QueryLog(
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
                    {"document_id": s.document_id, "title": s.title}
                    for s in answer.sources
                ],
            )
        )
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("не удалось записать query_log (ответ пользователю не затронут)")
