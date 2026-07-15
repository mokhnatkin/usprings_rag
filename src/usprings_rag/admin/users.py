"""Справочник пользователей (super-admin): список, создание, доступ, пароли.

Роль на учётку одна. Доступ к коллекциям - через `user_collection_access`:
для `user` это право спрашивать, для `collection_admin` - право администрировать.
`super_admin` грантов не требует (видит всё). При создании `user` получает автогрант
на все активные на тот момент коллекции (новые потом не открываются автоматически).
"""

import secrets
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..models import CollectionRow, Role, User, UserCollectionAccess
from ..security import hash_password


@dataclass
class UserInfo:
    """Строка справочника пользователей + коды коллекций, к которым есть доступ."""

    id: int
    login: str
    full_name: str
    email: str | None
    role: str
    is_active: bool
    created_at: datetime
    collection_codes: list[str]


def list_users(session: Session) -> list[UserInfo]:
    """Все пользователи (новые сверху) с их грантами доступа."""
    users = session.scalars(select(User).order_by(User.created_at.desc())).all()
    grants = session.execute(
        select(UserCollectionAccess.user_id, CollectionRow.code).join(
            CollectionRow, CollectionRow.id == UserCollectionAccess.collection_id
        )
    ).all()
    by_user: dict[int, list[str]] = defaultdict(list)
    for uid, code in grants:
        by_user[uid].append(code)
    return [
        UserInfo(
            id=u.id,
            login=u.login,
            full_name=u.full_name,
            email=u.email,
            role=u.role,
            is_active=u.is_active,
            created_at=u.created_at,
            collection_codes=sorted(by_user.get(u.id, [])),
        )
        for u in users
    ]


def create_user(
    session: Session,
    login: str,
    full_name: str,
    email: str | None,
    role: str,
    password: str,
) -> int:
    """Создать пользователя. ValueError - если роль неизвестна или логин занят.

    `user` получает автогрант на все активные коллекции; `collection_admin` доступ
    выдаётся отдельно (set_access), `super_admin` грантов не требует.
    """
    if role not in {r.value for r in Role}:
        raise ValueError(f"неизвестная роль: {role}")
    if not login.strip():
        raise ValueError("логин обязателен")
    if session.scalar(select(User).where(User.login == login)):
        raise ValueError(f"логин {login!r} уже занят")

    user = User(
        login=login,
        full_name=full_name,
        email=email or None,
        password_hash=hash_password(password),
        role=role,
    )
    session.add(user)
    session.flush()
    if role == Role.USER:
        active_ids = session.scalars(
            select(CollectionRow.id).where(CollectionRow.is_active.is_(True))
        ).all()
        for cid in active_ids:
            session.add(UserCollectionAccess(user_id=user.id, collection_id=cid))
    session.commit()
    return user.id


def set_active(session: Session, user_id: int, active: bool) -> bool:
    """Включить/выключить учётку. False - пользователя нет."""
    user = session.get(User, user_id)
    if user is None:
        return False
    user.is_active = active
    session.commit()
    return True


def reset_password(session: Session, user_id: int) -> str | None:
    """Сбросить пароль на временный, вернуть его (показать один раз). None - нет учётки."""
    user = session.get(User, user_id)
    if user is None:
        return None
    temp = secrets.token_urlsafe(9)
    user.password_hash = hash_password(temp)
    session.commit()
    return temp


def set_access(session: Session, user_id: int, collection_ids: list[int]) -> bool:
    """Задать полный набор грантов доступа пользователю (replace-all). False - нет учётки."""
    user = session.get(User, user_id)
    if user is None:
        return False
    session.execute(
        delete(UserCollectionAccess).where(UserCollectionAccess.user_id == user_id)
    )
    for cid in collection_ids:
        session.add(UserCollectionAccess(user_id=user_id, collection_id=cid))
    session.commit()
    return True
