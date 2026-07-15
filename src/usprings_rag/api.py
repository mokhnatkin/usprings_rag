"""FastAPI: портал вопрос-ответ (экран, статика, раздача исходных PDF).

Модель эмбеддингов и клиент LLM создаются один раз при старте (lifespan):
иначе первый запрос пользователя платит десятки секунд за загрузку весов BGE-m3.
Прогрев - холостая векторизация, чтобы веса реально легли в память.

Исходные PDF раздаём из папки инструкций по `/manuals/<source_path>` - в БД
`source_path` хранится относительным (см. ingest/pipeline.py), поэтому ссылка
одинаково работает на хосте и в контейнере.
"""

import json
import logging
import secrets
from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from math import ceil
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from openai import RateLimitError
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from .answer import answer_question, stream_answer
from .auth import (
    accessible_codes,
    authenticate,
    bootstrap_super_admin,
    change_password,
    check_collection_access,
    current_user_or_none,
    get_current_user,
    login_user,
    logout_user,
)
from .collection import DEFAULT_COLLECTION, Collection, get_collection, list_collections
from .collections_service import create_collection, update_collection
from .config import settings
from .db import SessionLocal
from .embeddings import BGEEmbeddingProvider
from .admin import calibration as admin_calibration
from .admin import documents as admin_docs
from .admin import logs as admin_logs
from .admin import users as admin_users
from .history import get_owned, paginate, recent
from .indexing import jobs as index_jobs
from .indexing.worker import IndexWorker
from .ingest.pipeline import relative_source_path
from .llm import create_client
from .logging_qa import log_query, set_feedback
from .models import Role, User

logger = logging.getLogger(__name__)

PACKAGE_DIR = Path(__file__).parent

resources: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Bootstrap super-admin, прогрев модели эмбеддингов, клиент LLM и воркер индексации."""
    with SessionLocal() as session:
        bootstrap_super_admin(session)
    logger.info("Загрузка модели эмбеддингов...")
    provider = BGEEmbeddingProvider()
    provider.embed_query("прогрев")
    resources["provider"] = provider
    resources["client"] = create_client()  # валидирует OPENROUTER_API_KEY
    worker = None
    if settings.index_worker_enabled:
        worker = IndexWorker(provider)  # переиспользует уже прогретую модель
        worker.start()
        resources["worker"] = worker
    logger.info("Приложение готово")
    yield
    if worker is not None:
        worker.stop()
    resources.clear()


app = FastAPI(title="USprings RAG", lifespan=lifespan)

# Подпись cookie-сессии. Пустой SECRET_KEY - dev-режим: эфемерный ключ, перезапуск
# разлогинит всех. На проде задать SECRET_KEY в .env.
_secret = settings.secret_key
if not _secret:
    logger.warning("SECRET_KEY не задан - генерирую эфемерный (сессии не переживут рестарт)")
    _secret = secrets.token_urlsafe(32)
app.add_middleware(
    SessionMiddleware,
    secret_key=_secret,
    session_cookie=settings.session_cookie_name,
    max_age=settings.session_max_age,
    same_site="lax",
    https_only=False,  # on-premise может работать по http; за TLS включить True
)

app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")
# Исходные PDF: html=False, чтобы отдавались только файлы, без листинга папок.
app.mount(
    "/manuals",
    StaticFiles(directory=settings.manuals_dir),
    name="manuals",
)


@app.get("/")
def index(request: Request):
    """Экран вопрос-ответ. Аноним - на форму входа."""
    if current_user_or_none(request) is None:
        return RedirectResponse("/login", status_code=303)
    return FileResponse(PACKAGE_DIR / "templates" / "index.html")


@app.get("/login")
def login_page(request: Request):
    """Форма входа. Уже авторизованного - на портал."""
    if current_user_or_none(request) is not None:
        return RedirectResponse("/", status_code=303)
    return FileResponse(PACKAGE_DIR / "templates" / "login.html")


@app.post("/login")
def login_submit(
    request: Request,
    login: str = Form(...),
    password: str = Form(...),
):
    """Проверить учётные данные и открыть сессию."""
    with SessionLocal() as session:
        user = authenticate(session, login, password)
    if user is None:
        return RedirectResponse("/login?error=1", status_code=303)
    login_user(request, user)
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
def logout(request: Request):
    """Завершить сессию."""
    logout_user(request)
    return RedirectResponse("/login", status_code=303)


@app.get("/profile")
def profile_page(user: User = Depends(get_current_user)) -> FileResponse:
    """Профиль: смена своего пароля."""
    return FileResponse(PACKAGE_DIR / "templates" / "profile.html")


@app.post("/profile/password")
def profile_change_password(
    old_password: str = Form(...),
    new_password: str = Form(...),
    user: User = Depends(get_current_user),
):
    """Сменить свой пароль (нужен верный старый)."""
    with SessionLocal() as session:
        ok = change_password(session, user.id, old_password, new_password)
    return RedirectResponse(
        "/profile?changed=1" if ok else "/profile?error=1", status_code=303
    )


class FeedbackRequest(BaseModel):
    log_id: int
    comment: str | None = None


@app.post("/feedback")
def feedback(
    request: FeedbackRequest, user: User = Depends(get_current_user)
) -> dict:
    """Пометить свой ответ неверным (+опциональный комментарий)."""
    with SessionLocal() as session:
        ok = set_feedback(session, user.id, request.log_id, request.comment)
    if not ok:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    return {"ok": True}


# --- История вопросов ---


class HistoryItemOut(BaseModel):
    id: int
    question: str  # усечён до QUERY_LOG_PREVIEW_CHARS
    created_at: datetime
    refused: bool
    collection: str


class HistoryListOut(BaseModel):
    items: list[HistoryItemOut]
    total: int
    page: int
    page_size: int
    pages: int


class HistoryEntryOut(BaseModel):
    id: int
    question: str
    answer: str
    refused: bool
    collection: str
    created_at: datetime
    best_similarity: float
    sources: list[SourceOut]


def _preview(text: str) -> str:
    limit = settings.query_log_preview_chars
    return text if len(text) <= limit else text[:limit] + "…"


def _collection_titles() -> dict[int, str]:
    return {c.id: c.title for c in list_collections(active_only=False)}


def _to_item(row, titles: dict[int, str]) -> HistoryItemOut:
    return HistoryItemOut(
        id=row.id,
        question=_preview(row.question),
        created_at=row.created_at,
        refused=row.outcome == "refused",
        collection=titles.get(row.collection_id, ""),
    )


@app.get("/api/history/recent", response_model=list[HistoryItemOut])
def history_recent(user: User = Depends(get_current_user)) -> list[HistoryItemOut]:
    """Последние три вопроса текущего пользователя (для главной)."""
    titles = _collection_titles()
    with SessionLocal() as session:
        rows = recent(session, user.id, 3)
    return [_to_item(row, titles) for row in rows]


@app.get("/api/history", response_model=HistoryListOut)
def history_list(
    page: int = 1, page_size: int = 20, user: User = Depends(get_current_user)
) -> HistoryListOut:
    """Страница истории текущего пользователя."""
    page = max(1, page)
    page_size = min(max(1, page_size), 100)
    titles = _collection_titles()
    with SessionLocal() as session:
        items, total = paginate(session, user.id, page, page_size)
    return HistoryListOut(
        items=[_to_item(row, titles) for row in items],
        total=total,
        page=page,
        page_size=page_size,
        pages=max(1, ceil(total / page_size)),
    )


@app.get("/api/history/{entry_id}", response_model=HistoryEntryOut)
def history_entry(
    entry_id: int, user: User = Depends(get_current_user)
) -> HistoryEntryOut:
    """Полная запись (только своя)."""
    titles = _collection_titles()
    with SessionLocal() as session:
        row = get_owned(session, user.id, entry_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Запись не найдена")
        sources = [
            SourceOut(
                document_id=s["document_id"],
                title=s["title"],
                source_path=s.get("source_path", ""),
                pages=s.get("pages", ""),
            )
            for s in (row.sources or [])
        ]
        return HistoryEntryOut(
            id=row.id,
            question=row.question,
            answer=row.answer,
            refused=row.outcome == "refused",
            collection=titles.get(row.collection_id, ""),
            created_at=row.created_at,
            best_similarity=round(row.best_similarity, 4),
            sources=sources,
        )


@app.get("/history")
def history_page(user: User = Depends(get_current_user)) -> FileResponse:
    """Страница полной истории с пагинацией."""
    return FileResponse(PACKAGE_DIR / "templates" / "history.html")


@app.get("/history/{entry_id}")
def history_entry_page(
    entry_id: int, user: User = Depends(get_current_user)
) -> FileResponse:
    """Просмотр одного вопроса-ответа. Чужая/несуществующая запись - 404."""
    with SessionLocal() as session:
        if get_owned(session, user.id, entry_id) is None:
            raise HTTPException(status_code=404, detail="Запись не найдена")
    return FileResponse(PACKAGE_DIR / "templates" / "history_entry.html")


class CollectionOut(BaseModel):
    code: str
    title: str


@app.get("/collections", response_model=list[CollectionOut])
def collections(user: User = Depends(get_current_user)) -> list[CollectionOut]:
    """Коллекции, доступные пользователю (super_admin - все активные)."""
    with SessionLocal() as session:
        codes = accessible_codes(session, user)
    return [
        CollectionOut(code=item.code, title=item.title)
        for item in list_collections()
        if item.code in codes
    ]


class MeOut(BaseModel):
    login: str
    full_name: str
    role: str


@app.get("/api/me", response_model=MeOut)
def me(user: User = Depends(get_current_user)) -> MeOut:
    """Текущий пользователь - для навигации по роли на клиенте."""
    return MeOut(login=user.login, full_name=user.full_name, role=user.role)


def _resolve_collection(code: str) -> Collection:
    """Коллекция по коду или 422: неизвестная коллекция не уходит в молчаливый поиск."""
    try:
        return get_collection(code)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


# --- Админка: документы (collection_admin, super_admin) ---


def require_admin(user: User = Depends(get_current_user)) -> User:
    """Зависимость: только администратор коллекций или super_admin, иначе 403."""
    if user.role not in (Role.COLLECTION_ADMIN, Role.SUPER_ADMIN):
        raise HTTPException(status_code=403, detail="Недостаточно прав")
    return user


def _admin_page_redirect(request: Request, need_super: bool) -> RedirectResponse | None:
    """Гейт для HTML-страниц админки: аноним -> вход, нехватка прав -> портал с
    уведомлением (а не «сырой» JSON 403, как у API-эндпоинтов для fetch)."""
    user = current_user_or_none(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    allowed = user.role == Role.SUPER_ADMIN or (
        not need_super and user.role == Role.COLLECTION_ADMIN
    )
    if not allowed:
        return RedirectResponse("/?forbidden=1", status_code=303)
    return None


class DocumentOut(BaseModel):
    id: int
    collection: str
    title: str
    source_path: str
    created_at: datetime
    archived: bool
    chunks: int


class JobOut(BaseModel):
    id: int
    collection: str
    source_path: str
    status: str
    error: str | None
    created_at: datetime
    finished_at: datetime | None


@app.get("/admin/documents")
def admin_documents_page(request: Request):
    """Экран управления документами (загрузка, статус индексации, архивация)."""
    return _admin_page_redirect(request, need_super=False) or FileResponse(
        PACKAGE_DIR / "templates" / "admin" / "documents.html"
    )


@app.get("/api/admin/documents", response_model=list[DocumentOut])
def admin_documents_list(
    collection: str | None = None, user: User = Depends(require_admin)
) -> list[DocumentOut]:
    """Документы доступных админу коллекций (опционально - одной)."""
    with SessionLocal() as session:
        codes = accessible_codes(session, user)
        if collection is not None:
            codes = codes & {collection}
        items = admin_docs.list_documents(session, codes)
    return [DocumentOut(**vars(item)) for item in items]


@app.get("/api/admin/jobs", response_model=list[JobOut])
def admin_jobs_list(
    collection: str | None = None, user: User = Depends(require_admin)
) -> list[JobOut]:
    """Последние задачи индексации по доступным админу коллекциям (для опроса статуса)."""
    with SessionLocal() as session:
        codes = accessible_codes(session, user)
        if collection is not None:
            codes = codes & {collection}
        items = admin_docs.recent_jobs(session, codes)
    return [JobOut(**vars(item)) for item in items]


@app.post("/api/admin/documents/upload")
async def admin_documents_upload(
    collection: str = Form(...),
    file: UploadFile = File(...),
    user: User = Depends(require_admin),
) -> dict:
    """Загрузить PDF в коллекцию и поставить задачу индексации. Возвращает job_id."""
    selected = _resolve_collection(collection)
    name = Path(file.filename or "").name
    if not name.lower().endswith(".pdf"):
        raise HTTPException(status_code=422, detail="Допустимы только файлы PDF")
    with SessionLocal() as session:
        check_collection_access(session, user, selected.code, need_admin=True)
        dest_dir = Path(settings.manuals_dir) / selected.folder
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / name
        dest.write_bytes(await file.read())
        job_id = index_jobs.enqueue(
            session, selected.id, relative_source_path(dest), user.id
        )
    return {"job_id": job_id}


def _set_document_archived(
    doc_id: int, archived: bool, user: User
) -> None:
    with SessionLocal() as session:
        code = admin_docs.collection_of(session, doc_id)
        if code is None:
            raise HTTPException(status_code=404, detail="Документ не найден")
        check_collection_access(session, user, code, need_admin=True)
        admin_docs.set_archived(session, doc_id, archived, user.id)


@app.post("/api/admin/documents/{doc_id}/archive")
def admin_document_archive(
    doc_id: int, user: User = Depends(require_admin)
) -> dict:
    """Пометить документ неактуальным (исключить из поиска)."""
    _set_document_archived(doc_id, True, user)
    return {"ok": True}


@app.post("/api/admin/documents/{doc_id}/unarchive")
def admin_document_unarchive(
    doc_id: int, user: User = Depends(require_admin)
) -> dict:
    """Вернуть документ из архива (снова в поиске, переиндексация не нужна)."""
    _set_document_archived(doc_id, False, user)
    return {"ok": True}


# --- Админка: справочник пользователей (super_admin) ---


def require_super_admin(user: User = Depends(get_current_user)) -> User:
    """Зависимость: только super_admin, иначе 403."""
    if user.role != Role.SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="Требуются права super-admin")
    return user


class UserInfoOut(BaseModel):
    id: int
    login: str
    full_name: str
    email: str | None
    role: str
    is_active: bool
    created_at: datetime
    collection_codes: list[str]


class UserCreateIn(BaseModel):
    login: str
    full_name: str
    email: str | None = None
    role: str
    password: str


class AccessIn(BaseModel):
    collection_ids: list[int]


@app.get("/admin/users")
def admin_users_page(request: Request):
    """Экран справочника пользователей."""
    return _admin_page_redirect(request, need_super=True) or FileResponse(
        PACKAGE_DIR / "templates" / "admin" / "users.html"
    )


@app.get("/api/admin/users", response_model=list[UserInfoOut])
def admin_users_list(user: User = Depends(require_super_admin)) -> list[UserInfoOut]:
    """Все пользователи с их доступами."""
    with SessionLocal() as session:
        items = admin_users.list_users(session)
    return [UserInfoOut(**vars(item)) for item in items]


@app.post("/api/admin/users")
def admin_users_create(
    payload: UserCreateIn, user: User = Depends(require_super_admin)
) -> dict:
    """Создать пользователя с ролью (user - автогрант на активные коллекции)."""
    with SessionLocal() as session:
        try:
            user_id = admin_users.create_user(
                session,
                payload.login.strip(),
                payload.full_name.strip(),
                payload.email,
                payload.role,
                payload.password,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
    return {"id": user_id}


@app.post("/api/admin/users/{user_id}/active")
def admin_users_set_active(
    user_id: int, active: bool, user: User = Depends(require_super_admin)
) -> dict:
    """Включить/выключить учётку. Себя деактивировать нельзя (защита от самоблокировки)."""
    if user_id == user.id and not active:
        raise HTTPException(
            status_code=422, detail="Нельзя деактивировать собственную учётку"
        )
    with SessionLocal() as session:
        if not admin_users.set_active(session, user_id, active):
            raise HTTPException(status_code=404, detail="Пользователь не найден")
    return {"ok": True}


@app.post("/api/admin/users/{user_id}/reset-password")
def admin_users_reset_password(
    user_id: int, user: User = Depends(require_super_admin)
) -> dict:
    """Сбросить пароль на временный и вернуть его (показать один раз)."""
    with SessionLocal() as session:
        temp = admin_users.reset_password(session, user_id)
    if temp is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    return {"temp_password": temp}


@app.put("/api/admin/users/{user_id}/access")
def admin_users_set_access(
    user_id: int, payload: AccessIn, user: User = Depends(require_super_admin)
) -> dict:
    """Задать полный набор доступов к коллекциям (для user и collection_admin)."""
    with SessionLocal() as session:
        if not admin_users.set_access(session, user_id, payload.collection_ids):
            raise HTTPException(status_code=404, detail="Пользователь не найден")
    return {"ok": True}


# --- Админка: справочник коллекций (super_admin) ---


class CollectionAdminOut(BaseModel):
    id: int
    code: str
    title: str
    folder: str
    threshold: float
    is_active: bool


class CollectionCreateIn(BaseModel):
    code: str
    title: str
    folder: str
    threshold: float


class CollectionUpdateIn(BaseModel):
    title: str
    threshold: float
    is_active: bool


def _collection_admin_out(c: Collection) -> CollectionAdminOut:
    return CollectionAdminOut(
        id=c.id,
        code=c.code,
        title=c.title,
        folder=c.folder,
        threshold=c.threshold,
        is_active=c.is_active,
    )


@app.get("/admin/collections")
def admin_collections_page(request: Request):
    """Экран справочника коллекций."""
    return _admin_page_redirect(request, need_super=True) or FileResponse(
        PACKAGE_DIR / "templates" / "admin" / "collections.html"
    )


@app.get("/api/admin/collections", response_model=list[CollectionAdminOut])
def admin_collections_list(
    user: User = Depends(require_super_admin),
) -> list[CollectionAdminOut]:
    """Все коллекции, включая деактивированные."""
    return [_collection_admin_out(c) for c in list_collections(active_only=False)]


@app.post("/api/admin/collections", response_model=CollectionAdminOut)
def admin_collections_create(
    payload: CollectionCreateIn, user: User = Depends(require_super_admin)
) -> CollectionAdminOut:
    """Создать коллекцию: строка + секция chunks + папка (сразу пригодна для ingest)."""
    with SessionLocal() as session:
        try:
            created = create_collection(
                session,
                payload.code.strip(),
                payload.title.strip(),
                payload.folder.strip(),
                payload.threshold,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
    return _collection_admin_out(created)


@app.patch("/api/admin/collections/{collection_id}", response_model=CollectionAdminOut)
def admin_collections_update(
    collection_id: int,
    payload: CollectionUpdateIn,
    user: User = Depends(require_super_admin),
) -> CollectionAdminOut:
    """Изменить title/threshold/is_active (code и folder неизменяемы)."""
    with SessionLocal() as session:
        try:
            updated = update_collection(
                session,
                collection_id,
                payload.title.strip(),
                payload.threshold,
                payload.is_active,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
    return _collection_admin_out(updated)


# --- Админка: калибровка порогов (super_admin) ---


@app.get("/admin/calibration")
def admin_calibration_page(request: Request):
    """Экран калибровки порогов."""
    return _admin_page_redirect(request, need_super=True) or FileResponse(
        PACKAGE_DIR / "templates" / "admin" / "calibration.html"
    )


@app.post("/api/admin/calibration/{code}")
def admin_calibration_start(
    code: str, user: User = Depends(require_super_admin)
) -> dict:
    """Запустить прогон golden-набора коллекции фоновой задачей."""
    collection = _resolve_collection(code)
    try:
        questions = admin_calibration.load_questions(collection.code)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if not questions:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Для коллекции {collection.code} нет golden-набора "
                f"(файл {settings.eval_questions_file}). Добавьте вопросы и повторите."
            ),
        )
    provider = resources.get("provider")
    if provider is None:
        raise HTTPException(status_code=503, detail="Модель эмбеддингов не загружена")
    if not admin_calibration.start_calibration(provider, collection, questions):
        raise HTTPException(
            status_code=409, detail="Другая калибровка ещё выполняется"
        )
    return {"status": "running", "questions": len(questions)}


@app.get("/api/admin/calibration/{code}")
def admin_calibration_status(
    code: str, user: User = Depends(require_super_admin)
) -> dict:
    """Статус и результат последнего прогона по коллекции."""
    job = admin_calibration.get_job(_resolve_collection(code).code)
    if job is None:
        raise HTTPException(status_code=404, detail="Калибровка не запускалась")
    return job


# --- Админка: просмотр журнала вопросов-ответов (collection_admin, super_admin) ---


class LogRowOut(BaseModel):
    id: int
    created_at: datetime
    user_login: str
    collection: str
    question: str  # усечён до QUERY_LOG_PREVIEW_CHARS
    outcome: str
    best_similarity: float
    total_tokens: int
    feedback: bool


class LogListOut(BaseModel):
    items: list[LogRowOut]
    total: int
    page: int
    page_size: int
    pages: int


class LogEntryOut(BaseModel):
    id: int
    created_at: datetime
    user_login: str
    collection: str
    question: str
    answer: str
    outcome: str
    best_similarity: float
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    elapsed_seconds: float
    model_id: str
    sources: list[SourceOut]
    feedback: bool
    feedback_at: datetime | None
    feedback_comment: str | None


def _log_allowed_ids(session, user: User) -> set[int] | None:
    """id коллекций, чьи логи доступны: None - весь портал (super_admin)."""
    if user.role == Role.SUPER_ADMIN:
        return None
    codes = accessible_codes(session, user)
    return {c.id for c in list_collections(active_only=False) if c.code in codes}


def _parse_day(value: str | None, *, end: bool = False) -> datetime | None:
    """YYYY-MM-DD -> datetime. Для верхней границы берём начало следующего дня
    (включительный день). Некорректная дата - 422."""
    if not value:
        return None
    try:
        day = datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Некорректная дата: {value}")
    return day + timedelta(days=1) if end else day


def _log_row_out(row) -> LogRowOut:
    ql, login, code = row
    return LogRowOut(
        id=ql.id,
        created_at=ql.created_at,
        user_login=login,
        collection=code,
        question=_preview(ql.question),
        outcome=ql.outcome,
        best_similarity=round(ql.best_similarity, 4),
        total_tokens=ql.total_tokens,
        feedback=bool(ql.feedback),
    )


@app.get("/admin/logs")
def admin_logs_page(request: Request):
    """Экран журнала вопросов-ответов."""
    return _admin_page_redirect(request, need_super=False) or FileResponse(
        PACKAGE_DIR / "templates" / "admin" / "logs.html"
    )


@app.get("/api/admin/logs", response_model=LogListOut)
def admin_logs_list(
    collection: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    page: int = 1,
    page_size: int = 20,
    user: User = Depends(require_admin),
) -> LogListOut:
    """Журнал по правам и фильтрам (коллекция, период), новые сверху."""
    page = max(1, page)
    page_size = min(max(1, page_size), 100)
    df = _parse_day(date_from)
    dt = _parse_day(date_to, end=True)
    collection_id = _resolve_collection(collection).id if collection else None
    with SessionLocal() as session:
        allowed = _log_allowed_ids(session, user)
        rows, total = admin_logs.list_logs(
            session, allowed, collection_id, df, dt, page, page_size
        )
    return LogListOut(
        items=[_log_row_out(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
        pages=max(1, ceil(total / page_size)),
    )


@app.get("/api/admin/logs/{log_id}", response_model=LogEntryOut)
def admin_logs_entry(
    log_id: int, user: User = Depends(require_admin)
) -> LogEntryOut:
    """Полная запись журнала (с проверкой прав по коллекции)."""
    with SessionLocal() as session:
        allowed = _log_allowed_ids(session, user)
        row = admin_logs.get_log(session, allowed, log_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    ql, login, code = row
    sources = [
        SourceOut(
            document_id=s["document_id"],
            title=s["title"],
            source_path=s.get("source_path", ""),
            pages=s.get("pages", ""),
        )
        for s in (ql.sources or [])
    ]
    return LogEntryOut(
        id=ql.id,
        created_at=ql.created_at,
        user_login=login,
        collection=code,
        question=ql.question,
        answer=ql.answer,
        outcome=ql.outcome,
        best_similarity=round(ql.best_similarity, 4),
        prompt_tokens=ql.prompt_tokens,
        completion_tokens=ql.completion_tokens,
        total_tokens=ql.total_tokens,
        elapsed_seconds=round(ql.elapsed_seconds, 2),
        model_id=ql.model_id,
        sources=sources,
        feedback=bool(ql.feedback),
        feedback_at=ql.feedback_at,
        feedback_comment=ql.feedback_comment,
    )


class AskRequest(BaseModel):
    question: str
    collection: str = DEFAULT_COLLECTION


class SourceOut(BaseModel):
    document_id: int
    title: str
    source_path: str
    pages: str


class AskResponse(BaseModel):
    answer: str
    refused: bool
    collection: str
    sources: list[SourceOut]
    best_similarity: float
    elapsed_seconds: float
    log_id: int | None = None  # id записи лога - для отметки «ответ неверный»


@app.post("/ask", response_model=AskResponse)
def ask(request: AskRequest, user: User = Depends(get_current_user)) -> AskResponse:
    """Вопрос -> поиск по коллекции -> порог -> (отказ | ответ LLM) + источники.

    Лимит бесплатного тира OpenRouter (429) - воспроизводимая ситуация пилота,
    отдаём её отдельным понятным статусом, а не общей ошибкой 500.
    """
    collection = _resolve_collection(request.collection)
    with SessionLocal() as session:
        check_collection_access(session, user, collection.code)
        try:
            result = answer_question(
                session,
                resources["provider"],
                resources["client"],
                request.question,
                collection,
            )
        except RateLimitError:
            logger.warning("LLM rate limit (429) - бесплатный тир OpenRouter занят")
            raise HTTPException(
                status_code=503,
                detail=(
                    "Модель временно недоступна (лимит бесплатного тира). "
                    "Повторите вопрос через минуту."
                ),
            )
        log_id = log_query(session, user.id, collection, request.question, result)
    return AskResponse(
        answer=result.text,
        refused=result.refused,
        collection=collection.title,
        sources=[SourceOut(**vars(source)) for source in result.sources],
        best_similarity=round(result.best_similarity, 4),
        elapsed_seconds=round(result.elapsed_seconds, 2),
        log_id=log_id,
    )


def _sse(event: dict) -> str:
    """Один кадр SSE. ensure_ascii=False - в тексте кириллица."""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@app.get("/ask/stream")
def ask_stream(
    question: str,
    collection: str = DEFAULT_COLLECTION,
    user: User = Depends(get_current_user),
) -> StreamingResponse:
    """То же, что /ask, но ответ идёт по мере генерации (SSE).

    Генерация LLM доминирует в задержке, поэтому первые слова появляются через
    секунду-две вместо ожидания всего ответа. Отказ по порогу остаётся мгновенным:
    приходит сразу событием `done`, LLM не вызывается.

    GET (а не POST) - чтобы на клиенте работал штатный EventSource.
    """
    selected = _resolve_collection(collection)
    # Доступ проверяем до начала стрима: 403 отдаём обычным ответом, не в SSE.
    with SessionLocal() as session:
        check_collection_access(session, user, selected.code)

    def events() -> Iterator[str]:
        parts: list[str] = []  # накопленный текст ответа - для записи в лог
        with SessionLocal() as session:
            try:
                for kind, payload in stream_answer(
                    session,
                    resources["provider"],
                    resources["client"],
                    question,
                    selected,
                ):
                    if kind == "delta":
                        parts.append(payload)
                        yield _sse({"type": "delta", "text": payload})
                    else:
                        # текст ответа при стриме ушёл дельтами (payload.text пуст),
                        # для лога собираем его из накопленных кусков; у отказа -
                        # payload.text (дельт не было). Логируем до события done,
                        # чтобы вложить log_id для отметки «ответ неверный».
                        full = "".join(parts) if parts else payload.text
                        log_id = log_query(
                            session, user.id, selected, question, payload, answer_text=full
                        )
                        yield _sse(
                            {
                                "type": "done",
                                "answer": payload.text,
                                "refused": payload.refused,
                                "collection": selected.title,
                                "sources": [vars(s) for s in payload.sources],
                                "best_similarity": round(payload.best_similarity, 4),
                                "elapsed_seconds": round(payload.elapsed_seconds, 2),
                                "log_id": log_id,
                            }
                        )
            except RateLimitError:
                logger.warning("LLM rate limit (429) - бесплатный тир OpenRouter занят")
                yield _sse(
                    {
                        "type": "error",
                        "message": (
                            "Модель временно недоступна (лимит бесплатного тира). "
                            "Повторите вопрос через минуту."
                        ),
                    }
                )

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
