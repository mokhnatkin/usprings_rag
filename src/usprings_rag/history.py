"""История вопросов пользователя: выборки из query_log.

Только свои записи. Порядок - по убыванию времени (id как устойчивый разрыв
ничьей при равном created_at).
"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import QueryLog


def recent(session: Session, user_id: int, limit: int = 3) -> list[QueryLog]:
    """Последние `limit` записей пользователя."""
    return list(
        session.scalars(
            select(QueryLog)
            .where(QueryLog.user_id == user_id)
            .order_by(QueryLog.created_at.desc(), QueryLog.id.desc())
            .limit(limit)
        )
    )


def paginate(
    session: Session, user_id: int, page: int, page_size: int
) -> tuple[list[QueryLog], int]:
    """Страница истории пользователя и общее число записей."""
    total = session.scalar(
        select(func.count()).select_from(QueryLog).where(QueryLog.user_id == user_id)
    )
    items = session.scalars(
        select(QueryLog)
        .where(QueryLog.user_id == user_id)
        .order_by(QueryLog.created_at.desc(), QueryLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list(items), total or 0


def get_owned(session: Session, user_id: int, entry_id: int) -> QueryLog | None:
    """Запись по id, только если принадлежит пользователю (иначе None)."""
    row = session.get(QueryLog, entry_id)
    if row is None or row.user_id != user_id:
        return None
    return row
