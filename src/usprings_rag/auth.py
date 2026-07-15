"""Аутентификация: сессия, текущий пользователь, вход, bootstrap super-admin.

Внутренние учётки (логин + пароль). Провайдер спрятан за `authenticate` - вторая
реализация (LDAP/AD) добавится сюда, не трогая эндпоинты. Сессия - подписанная
cookie (Starlette SessionMiddleware); в ней держим только `user_id`.
"""

import logging

from fastapi import HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .collection import list_collections
from .config import settings
from .db import SessionLocal
from .models import CollectionRow, Role, User, UserCollectionAccess
from .security import hash_password, verify_password

logger = logging.getLogger(__name__)

SESSION_USER_KEY = "user_id"


def authenticate(session: Session, login: str, password: str) -> User | None:
    """Проверить логин и пароль. None - если нет пользователя, пароль неверен или
    учётка неактивна (по всем случаям одинаковый исход - не подсказываем причину)."""
    user = session.scalar(select(User).where(User.login == login))
    if user is None or not user.is_active:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


def login_user(request: Request, user: User) -> None:
    """Записать пользователя в сессию (после успешной проверки)."""
    request.session[SESSION_USER_KEY] = user.id


def logout_user(request: Request) -> None:
    """Очистить сессию."""
    request.session.clear()


def current_user_or_none(request: Request) -> User | None:
    """Текущий пользователь из сессии или None. Неактивного/удалённого разлогиниваем."""
    user_id = request.session.get(SESSION_USER_KEY)
    if user_id is None:
        return None
    with SessionLocal() as session:
        user = session.get(User, user_id)
    if user is None or not user.is_active:
        request.session.clear()
        return None
    return user


def get_current_user(request: Request) -> User:
    """Зависимость FastAPI: требует авторизацию, иначе 401."""
    user = current_user_or_none(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Требуется авторизация")
    return user


def accessible_codes(session: Session, user: User) -> set[str]:
    """Коды коллекций, доступных пользователю. super_admin - все активные."""
    if user.role == Role.SUPER_ADMIN:
        return {c.code for c in list_collections(active_only=True)}
    codes = session.scalars(
        select(CollectionRow.code)
        .join(
            UserCollectionAccess,
            UserCollectionAccess.collection_id == CollectionRow.id,
        )
        .where(
            UserCollectionAccess.user_id == user.id,
            CollectionRow.is_active.is_(True),
        )
    ).all()
    return set(codes)


def check_collection_access(
    session: Session, user: User, code: str, need_admin: bool = False
) -> None:
    """Проверить доступ к коллекции, иначе 403.

    `need_admin=True` дополнительно требует роль администратора (collection_admin
    или super_admin) - для управляющих операций. super_admin проходит всё.
    """
    if need_admin and user.role not in (Role.COLLECTION_ADMIN, Role.SUPER_ADMIN):
        raise HTTPException(status_code=403, detail="Недостаточно прав")
    if user.role == Role.SUPER_ADMIN:
        return
    if code not in accessible_codes(session, user):
        raise HTTPException(status_code=403, detail="Нет доступа к этой коллекции")


def change_password(
    session: Session, user_id: int, old_password: str, new_password: str
) -> bool:
    """Сменить пароль по проверке старого. False - старый неверен или нет учётки."""
    user = session.get(User, user_id)
    if user is None or not verify_password(old_password, user.password_hash):
        return False
    user.password_hash = hash_password(new_password)
    session.commit()
    return True


def bootstrap_super_admin(session: Session) -> None:
    """Создать первую учётку super-admin, если пользователей ещё нет.

    Идемпотентно: при непустой таблице ничего не делаем (пароль молча не меняем).
    """
    count = session.scalar(select(func.count()).select_from(User))
    if count:
        return
    if not settings.superadmin_login or not settings.superadmin_password:
        logger.warning(
            "Пользователей нет, но SUPERADMIN_LOGIN/SUPERADMIN_PASSWORD не заданы - "
            "super-admin не создан. Задайте их в .env и перезапустите."
        )
        return
    session.add(
        User(
            login=settings.superadmin_login,
            full_name="Администратор",
            password_hash=hash_password(settings.superadmin_password),
            role=Role.SUPER_ADMIN,
        )
    )
    session.commit()
    logger.info("Создан super-admin %s (bootstrap)", settings.superadmin_login)
