"""Просмотр журнала вопросов-ответов (query_log) для админов.

super_admin видит весь портал (allowed_ids=None), collection_admin - только свои
коллекции (allowed_ids - множество id его коллекций). Выборки отдают строку лога
вместе с логином пользователя и кодом коллекции; усечение вопроса для списка - на
стороне эндпоинта (в записи хранится полный текст).
"""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import CollectionRow, QueryLog, User

# Строка выборки: (QueryLog, login пользователя, code коллекции).
LogRow = tuple[QueryLog, str, str]


def _filters(
    allowed_ids: set[int] | None,
    collection_id: int | None,
    date_from: datetime | None,
    date_to: datetime | None,
):
    conditions = []
    if allowed_ids is not None:
        conditions.append(QueryLog.collection_id.in_(allowed_ids))
    if collection_id is not None:
        conditions.append(QueryLog.collection_id == collection_id)
    if date_from is not None:
        conditions.append(QueryLog.created_at >= date_from)
    if date_to is not None:
        conditions.append(QueryLog.created_at < date_to)
    return conditions


def list_logs(
    session: Session,
    allowed_ids: set[int] | None,
    collection_id: int | None,
    date_from: datetime | None,
    date_to: datetime | None,
    page: int,
    page_size: int,
) -> tuple[list[LogRow], int]:
    """Страница журнала по фильтрам, новые сверху. Возвращает (строки, всего)."""
    conditions = _filters(allowed_ids, collection_id, date_from, date_to)
    total = session.scalar(
        select(func.count()).select_from(QueryLog).where(*conditions)
    )
    rows = session.execute(
        select(QueryLog, User.login, CollectionRow.code)
        .join(User, User.id == QueryLog.user_id)
        .join(CollectionRow, CollectionRow.id == QueryLog.collection_id)
        .where(*conditions)
        .order_by(QueryLog.created_at.desc(), QueryLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return list(rows), total or 0


def get_log(
    session: Session, allowed_ids: set[int] | None, log_id: int
) -> LogRow | None:
    """Полная запись, если она в границах прав. None - нет записи или нет доступа."""
    row = session.execute(
        select(QueryLog, User.login, CollectionRow.code)
        .join(User, User.id == QueryLog.user_id)
        .join(CollectionRow, CollectionRow.id == QueryLog.collection_id)
        .where(QueryLog.id == log_id)
    ).first()
    if row is None:
        return None
    query_log = row[0]
    if allowed_ids is not None and query_log.collection_id not in allowed_ids:
        return None
    return row
