"""Базовые агрегаты поверх query_log для экрана аналитики.

Разрез задаётся `allowed_ids` (как в admin/logs): None - весь портал (super-admin),
множество id коллекций - его коллекции (collection-admin). Опциональный фильтр по
одной коллекции и период [date_from, date_to) - те же, что у журнала.

Активность по часам/дням недели считается через `extract` в часовом поясе сессии
Postgres (в контейнере обычно UTC). Для пилота приемлемо; при необходимости точной
локали - приводить created_at к нужному tz перед extract.
"""

from datetime import datetime

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from ..models import QueryLog, User

# Дни недели по isodow (1=Пн .. 7=Вс) - для подписи баров на экране.
WEEKDAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def _conditions(
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


def compute(
    session: Session,
    allowed_ids: set[int] | None,
    collection_id: int | None,
    date_from: datetime | None,
    date_to: datetime | None,
    *,
    top_n: int = 10,
    per_user: bool = False,
) -> dict:
    """Собрать все агрегаты в один отчёт (сводка + разрезы) по фильтрам."""
    cond = _conditions(allowed_ids, collection_id, date_from, date_to)
    return {
        "summary": _summary(session, cond),
        "by_weekday": _by_weekday(session, cond),
        "by_hour": _by_hour(session, cond),
        "top_questions": _top_questions(session, cond, top_n),
        "by_user": _by_user(session, cond) if per_user else None,
    }


def _summary(session: Session, cond) -> dict:
    """Итоги: число запросов, токены, доля отказов и доля помеченных неверными."""
    refused_f = QueryLog.outcome == "refused"
    row = session.execute(
        select(
            func.count(),
            func.count().filter(refused_f),
            func.count().filter(QueryLog.feedback.is_(True)),
            func.coalesce(func.sum(QueryLog.total_tokens), 0),
            func.coalesce(func.sum(QueryLog.prompt_tokens), 0),
            func.coalesce(func.sum(QueryLog.completion_tokens), 0),
        ).where(*cond)
    ).one()
    total, refused, feedback, tok_total, tok_prompt, tok_completion = row
    return {
        "total": total,
        "refused": refused,
        "answered": total - refused,
        "feedback": feedback,
        "refused_share": round(refused / total, 4) if total else 0.0,
        "feedback_share": round(feedback / total, 4) if total else 0.0,
        "tokens_total": int(tok_total),
        "tokens_prompt": int(tok_prompt),
        "tokens_completion": int(tok_completion),
    }


def _by_weekday(session: Session, cond) -> list[dict]:
    """Активность по дням недели (isodow 1..7), нули для пустых дней - для баров."""
    dow = func.extract("isodow", QueryLog.created_at)
    rows = session.execute(
        select(dow.label("d"), func.count()).where(*cond).group_by("d")
    ).all()
    counts = {int(d): c for d, c in rows}
    return [
        {"weekday": i, "label": WEEKDAYS_RU[i - 1], "count": counts.get(i, 0)}
        for i in range(1, 8)
    ]


def _by_hour(session: Session, cond) -> list[dict]:
    """Активность по часам суток (0..23), нули для пустых часов."""
    hour = func.extract("hour", QueryLog.created_at)
    rows = session.execute(
        select(hour.label("h"), func.count()).where(*cond).group_by("h")
    ).all()
    counts = {int(h): c for h, c in rows}
    return [{"hour": i, "count": counts.get(i, 0)} for i in range(24)]


def _top_questions(session: Session, cond, limit: int) -> list[dict]:
    """Самые частые вопросы: сколько раз задан и сколько раз дал отказ.

    Частый вопрос с высокой долей отказов - сигнал пробела в базе знаний."""
    refused_f = QueryLog.outcome == "refused"
    rows = session.execute(
        select(
            QueryLog.question,
            func.count().label("c"),
            func.count().filter(refused_f),
        )
        .where(*cond)
        .group_by(QueryLog.question)
        .order_by(desc("c"))
        .limit(limit)
    ).all()
    return [
        {"question": q, "count": c, "refused": r} for q, c, r in rows
    ]


def _by_user(session: Session, cond) -> list[dict]:
    """Срез по пользователям (для super-admin): запросы, токены, отказы."""
    refused_f = QueryLog.outcome == "refused"
    rows = session.execute(
        select(
            User.login,
            func.count().label("c"),
            func.coalesce(func.sum(QueryLog.total_tokens), 0),
            func.count().filter(refused_f),
        )
        .join(User, User.id == QueryLog.user_id)
        .where(*cond)
        .group_by(User.login)
        .order_by(desc("c"))
    ).all()
    return [
        {"user_login": login, "count": c, "tokens": int(tok), "refused": r}
        for login, c, tok, r in rows
    ]
