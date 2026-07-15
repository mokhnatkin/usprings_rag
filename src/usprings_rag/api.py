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
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
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
from .config import settings
from .db import SessionLocal
from .embeddings import BGEEmbeddingProvider
from .llm import create_client
from .models import User

logger = logging.getLogger(__name__)

PACKAGE_DIR = Path(__file__).parent

resources: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Bootstrap super-admin, прогрев модели эмбеддингов и клиент LLM до запросов."""
    with SessionLocal() as session:
        bootstrap_super_admin(session)
    logger.info("Загрузка модели эмбеддингов...")
    provider = BGEEmbeddingProvider()
    provider.embed_query("прогрев")
    resources["provider"] = provider
    resources["client"] = create_client()  # валидирует OPENROUTER_API_KEY
    logger.info("Приложение готово")
    yield
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


def _resolve_collection(code: str) -> Collection:
    """Коллекция по коду или 422: неизвестная коллекция не уходит в молчаливый поиск."""
    try:
        return get_collection(code)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


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
    return AskResponse(
        answer=result.text,
        refused=result.refused,
        collection=collection.title,
        sources=[SourceOut(**vars(source)) for source in result.sources],
        best_similarity=round(result.best_similarity, 4),
        elapsed_seconds=round(result.elapsed_seconds, 2),
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
                        yield _sse({"type": "delta", "text": payload})
                    else:
                        yield _sse(
                            {
                                "type": "done",
                                "answer": payload.text,
                                "refused": payload.refused,
                                "collection": selected.title,
                                "sources": [vars(s) for s in payload.sources],
                                "best_similarity": round(payload.best_similarity, 4),
                                "elapsed_seconds": round(payload.elapsed_seconds, 2),
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
