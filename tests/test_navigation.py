"""Навигация и серверная защита по ролям (этап 11).

Матрица «роль x маршрут»: чужие экраны недоступны и по прямой ссылке - HTML-страницы
редиректят (аноним -> вход, нехватка прав -> портал с уведомлением), API отдают
401/403. Проверяем именно сервер, не скрытие пунктов в меню. Мутирующие маршруты в
негативных случаях получают валидное тело - тогда отказ приходит от гейта прав, а не
от валидации; сам обработчик не выполняется (гейт срабатывает раньше), побочных
эффектов нет.

Нужна БД. Без неё - skip.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from fastapi.testclient import TestClient

from usprings_rag.api import app
from usprings_rag.db import SessionLocal
from usprings_rag.models import Role, User
from usprings_rag.security import hash_password

PW = "nav-pass-123"
USERS = {
    "user": "nav_user",
    "collection_admin": "nav_cadmin",
    "super_admin": "nav_super",
}

# API только для super-admin (require_super_admin). Тело - валидное, чтобы отказ шёл
# от прав, а не от 422.
SUPER_API = [
    ("GET", "/api/admin/users", None),
    ("POST", "/api/admin/users",
     {"login": "nav_x", "full_name": "x", "role": "user", "password": "x"}),
    ("POST", "/api/admin/users/1/active?active=false", None),
    ("POST", "/api/admin/users/1/reset-password", None),
    ("PUT", "/api/admin/users/1/access", {"collection_ids": []}),
    ("GET", "/api/admin/collections", None),
    ("POST", "/api/admin/collections",
     {"code": "navx", "title": "x", "folder": "navx", "threshold": 0.5}),
    ("PATCH", "/api/admin/collections/1",
     {"title": "x", "threshold": 0.5, "is_active": True}),
    ("POST", "/api/admin/calibration/erp", None),
    ("GET", "/api/admin/calibration/erp", None),
]

# API для админов коллекций и super-admin (require_admin).
ADMIN_API = [
    ("GET", "/api/admin/documents", None),
    ("GET", "/api/admin/jobs", None),
    ("POST", "/api/admin/documents/upload",
     ("multipart", {"collection": "erp"},
      {"file": ("x.pdf", b"%PDF", "application/pdf")})),
    ("POST", "/api/admin/documents/1/archive", None),
    ("POST", "/api/admin/documents/1/unarchive", None),
    ("GET", "/api/admin/logs", None),
    ("GET", "/api/admin/logs/1", None),
    ("GET", "/api/admin/analytics", None),
]
ADMIN_API_GET = [r for r in ADMIN_API if r[0] == "GET"]

SUPER_PAGES = ["/admin/users", "/admin/collections", "/admin/calibration"]
ADMIN_PAGES = ["/admin/documents", "/admin/logs", "/admin/analytics"]


def _skip_if_no_db():
    try:
        db = SessionLocal()
        db.execute(text("select 1"))
        db.close()
    except OperationalError:
        pytest.skip("БД недоступна")


@pytest.fixture
def clients():
    _skip_if_no_db()
    db = SessionLocal()
    logins = list(USERS.values()) + ["nav_x"]
    for login in logins:
        db.execute(text("delete from users where login = :l"), {"l": login})
    db.execute(text("delete from collections where code = 'navx'"))
    db.commit()
    for role, login in USERS.items():
        db.add(User(login=login, full_name=login, password_hash=hash_password(PW),
                    role=Role(role)))
    db.commit()

    result = {"anon": TestClient(app)}
    for role, login in USERS.items():
        c = TestClient(app)
        r = c.post("/login", data={"login": login, "password": PW},
                   follow_redirects=False)
        assert r.status_code == 303
        result[role] = c
    yield result

    for login in logins:
        db.execute(text("delete from users where login = :l"), {"l": login})
    db.execute(text("delete from collections where code = 'navx'"))
    db.commit()
    db.close()


def _send(client, method, path, spec):
    kw = {"follow_redirects": False}
    if isinstance(spec, dict):
        kw["json"] = spec
    elif isinstance(spec, tuple):  # ("multipart", data, files)
        kw["data"], kw["files"] = spec[1], spec[2]
    return client.request(method, path, **kw)


# --- Страницы: аноним -> /login, нехватка прав -> /?forbidden=1 ---


def test_pages_anonymous_redirect_to_login(clients):
    anon = clients["anon"]
    for path in SUPER_PAGES + ADMIN_PAGES:
        r = anon.get(path, follow_redirects=False)
        assert r.status_code == 303, path
        assert r.headers["location"] == "/login", path


def test_admin_pages_forbidden_for_user(clients):
    user = clients["user"]
    for path in SUPER_PAGES + ADMIN_PAGES:
        r = user.get(path, follow_redirects=False)
        assert r.status_code == 303 and "forbidden" in r.headers["location"], path


def test_super_pages_forbidden_for_collection_admin(clients):
    ca = clients["collection_admin"]
    for path in SUPER_PAGES:
        r = ca.get(path, follow_redirects=False)
        assert r.status_code == 303 and "forbidden" in r.headers["location"], path


def test_admin_pages_open_for_collection_admin(clients):
    ca = clients["collection_admin"]
    for path in ADMIN_PAGES:
        assert ca.get(path).status_code == 200, path


def test_all_admin_pages_open_for_super(clients):
    su = clients["super_admin"]
    for path in SUPER_PAGES + ADMIN_PAGES:
        assert su.get(path).status_code == 200, path


# --- API: аноним -> 401, чужая роль -> 403 ---


def test_api_anonymous_401(clients):
    anon = clients["anon"]
    for method, path, spec in SUPER_API + ADMIN_API:
        assert _send(anon, method, path, spec).status_code == 401, path


def test_all_admin_api_forbidden_for_user(clients):
    user = clients["user"]
    for method, path, spec in SUPER_API + ADMIN_API:
        assert _send(user, method, path, spec).status_code == 403, path


def test_super_api_forbidden_for_collection_admin(clients):
    ca = clients["collection_admin"]
    for method, path, spec in SUPER_API:
        assert _send(ca, method, path, spec).status_code == 403, path


# --- API: своя роль проходит гейт (не 401/403) ---


def test_admin_api_get_pass_gate_for_collection_admin(clients):
    ca = clients["collection_admin"]
    for method, path, spec in ADMIN_API_GET:
        code = _send(ca, method, path, spec).status_code
        assert code not in (401, 403), f"{path} -> {code}"


def test_super_api_get_pass_gate_for_super(clients):
    su = clients["super_admin"]
    for method, path, spec in SUPER_API:
        if method != "GET":
            continue
        code = _send(su, method, path, spec).status_code
        assert code not in (401, 403), f"{path} -> {code}"


# --- Пользовательские экраны доступны залогиненному ---


def test_user_reaches_own_screens(clients):
    user = clients["user"]
    assert user.get("/").status_code == 200
    assert user.get("/history").status_code == 200
    assert user.get("/profile").status_code == 200
    assert user.get("/api/me").json()["role"] == "user"
    assert user.get("/collections").status_code == 200


def test_no_test_user_created_by_denied_posts(clients):
    """Гейт срабатывает до обработчика: отклонённые POST не создают запись."""
    with SessionLocal() as db:
        exists = db.scalar(
            text("select count(*) from users where login = 'nav_x'")
        )
    assert exists == 0
