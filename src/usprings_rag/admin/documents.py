"""Управление документами коллекции: список со статусом, архивация, возврат.

Загрузка файла и постановка задачи индексации - в api.py (там же проверки прав);
здесь - выборки и переключение soft-delete. Доступ ограничивает вызывающий
(check_collection_access с need_admin), сервис работает по уже разрешённым кодам.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Chunk, Document, IndexJob


@dataclass
class DocumentInfo:
    """Строка списка документов: статус, дата, число чанков - для админ-экрана."""

    id: int
    collection: str
    title: str
    source_path: str
    created_at: datetime
    archived: bool
    chunks: int


@dataclass
class JobInfo:
    """Строка списка задач индексации: статус и текст ошибки - для опроса из UI."""

    id: int
    collection: str
    source_path: str
    status: str
    error: str | None
    created_at: datetime
    finished_at: datetime | None


def list_documents(session: Session, codes: set[str]) -> list[DocumentInfo]:
    """Документы указанных коллекций с числом чанков и статусом, новые сверху."""
    if not codes:
        return []
    rows = session.execute(
        select(Document, func.count(Chunk.id))
        .outerjoin(Chunk, Chunk.document_id == Document.id)
        .where(Document.collection.in_(codes))
        .group_by(Document.id)
        .order_by(Document.created_at.desc())
    ).all()
    return [
        DocumentInfo(
            id=doc.id,
            collection=doc.collection,
            title=doc.title,
            source_path=doc.source_path,
            created_at=doc.created_at,
            archived=doc.archived_at is not None,
            chunks=count,
        )
        for doc, count in rows
    ]


def recent_jobs(session: Session, codes: set[str], limit: int = 20) -> list[JobInfo]:
    """Последние задачи индексации по коллекциям (код берём из collections по id)."""
    if not codes:
        return []
    from ..models import CollectionRow

    rows = session.execute(
        select(IndexJob, CollectionRow.code)
        .join(CollectionRow, CollectionRow.id == IndexJob.collection_id)
        .where(CollectionRow.code.in_(codes))
        .order_by(IndexJob.created_at.desc())
        .limit(limit)
    ).all()
    return [
        JobInfo(
            id=job.id,
            collection=code,
            source_path=job.source_path,
            status=job.status,
            error=job.error,
            created_at=job.created_at,
            finished_at=job.finished_at,
        )
        for job, code in rows
    ]


def collection_of(session: Session, doc_id: int) -> str | None:
    """Код коллекции документа - для проверки прав до изменения. None, если нет."""
    return session.scalar(select(Document.collection).where(Document.id == doc_id))


def set_archived(session: Session, doc_id: int, archived: bool, user_id: int) -> None:
    """Архивировать (archived=True) или вернуть документ. Права проверяет вызывающий.

    Переиндексация не нужна: чанки остаются в БД, из поиска документ исключает
    фильтр `archived_at IS NULL` (см. retrieval.search)."""
    doc = session.get(Document, doc_id)
    doc.archived_at = func.now() if archived else None
    doc.archived_by = user_id if archived else None
    session.commit()
