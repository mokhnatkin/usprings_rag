"""Очередь задач индексации `index_job`: постановка, захват, смена статуса.

Только работа с БД - без парсинга и модели (это в worker.py). Захват задачи
атомарен (`FOR UPDATE SKIP LOCKED`): даже если воркеров окажется несколько, одну
задачу не возьмут дважды. Смена статуса после обработки идёт по id в своей сессии,
чтобы сбой ingest не тянул за собой рассинхрон объекта.
"""

import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import IndexJob, IndexJobStatus

logger = logging.getLogger(__name__)


def enqueue(
    session: Session, collection_id: int, source_path: str, created_by: int
) -> int:
    """Поставить задачу индексации файла. Возвращает id (для опроса статуса)."""
    job = IndexJob(
        collection_id=collection_id,
        source_path=source_path,
        status=IndexJobStatus.QUEUED,
        created_by=created_by,
    )
    session.add(job)
    session.commit()
    return job.id


def claim_next(session: Session) -> IndexJob | None:
    """Взять старейшую задачу `queued`, перевести в `running`. None - очередь пуста."""
    job = session.scalars(
        select(IndexJob)
        .where(IndexJob.status == IndexJobStatus.QUEUED)
        .order_by(IndexJob.created_at, IndexJob.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    ).first()
    if job is None:
        return None
    job.status = IndexJobStatus.RUNNING
    job.started_at = func.now()
    session.commit()
    return job


def mark_done(session: Session, job_id: int, document_id: int | None) -> None:
    """Задача выполнена: `done`, привязка к созданному документу."""
    job = session.get(IndexJob, job_id)
    job.status = IndexJobStatus.DONE
    job.document_id = document_id
    job.error = None
    job.finished_at = func.now()
    session.commit()


def mark_error(session: Session, job_id: int, message: str) -> None:
    """Задача провалилась: `error` с текстом (после отката повреждённой транзакции)."""
    session.rollback()
    job = session.get(IndexJob, job_id)
    job.status = IndexJobStatus.ERROR
    job.error = message[:2000]
    job.finished_at = func.now()
    session.commit()


def reset_stale(session: Session) -> int:
    """Пометить `running` как `error` - вызывать при старте воркера.

    Задача в `running` после рестарта процесса зависла бы навсегда (воркер её уже
    не обрабатывает). Возвращает число сброшенных задач.
    """
    stale = session.scalars(
        select(IndexJob).where(IndexJob.status == IndexJobStatus.RUNNING)
    ).all()
    for job in stale:
        job.status = IndexJobStatus.ERROR
        job.error = "прервана рестартом приложения"
        job.finished_at = func.now()
    session.commit()
    if stale:
        logger.warning("Сброшено зависших задач индексации: %d", len(stale))
    return len(stale)
