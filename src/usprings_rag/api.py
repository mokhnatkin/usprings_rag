"""FastAPI: портал вопрос-ответ (экран, статика, раздача исходных PDF).

Модель эмбеддингов и клиент LLM создаются один раз при старте (lifespan):
иначе первый запрос пользователя платит десятки секунд за загрузку весов BGE-m3.
Прогрев - холостая векторизация, чтобы веса реально легли в память.

Исходные PDF раздаём из папки инструкций по `/manuals/<source_path>` - в БД
`source_path` хранится относительным (см. ingest/pipeline.py), поэтому ссылка
одинаково работает на хосте и в контейнере.
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from openai import RateLimitError
from pydantic import BaseModel

from .answer import answer_question
from .config import settings
from .db import SessionLocal
from .embeddings import BGEEmbeddingProvider
from .llm import create_client

logger = logging.getLogger(__name__)

PACKAGE_DIR = Path(__file__).parent

resources: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Прогрев модели эмбеддингов и создание клиента LLM до первого запроса."""
    logger.info("Загрузка модели эмбеддингов...")
    provider = BGEEmbeddingProvider()
    provider.embed_query("прогрев")
    resources["provider"] = provider
    resources["client"] = create_client()  # валидирует OPENROUTER_API_KEY
    logger.info("Приложение готово")
    yield
    resources.clear()


app = FastAPI(title="USprings RAG", lifespan=lifespan)

app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")
# Исходные PDF: html=False, чтобы отдавались только файлы, без листинга папок.
app.mount(
    "/manuals",
    StaticFiles(directory=settings.manuals_dir),
    name="manuals",
)


@app.get("/")
def index() -> FileResponse:
    """Экран вопрос-ответ."""
    return FileResponse(PACKAGE_DIR / "templates" / "index.html")


class AskRequest(BaseModel):
    question: str


class SourceOut(BaseModel):
    document_id: int
    title: str
    source_path: str
    pages: str


class AskResponse(BaseModel):
    answer: str
    refused: bool
    sources: list[SourceOut]
    best_similarity: float
    elapsed_seconds: float


@app.post("/ask", response_model=AskResponse)
def ask(request: AskRequest) -> AskResponse:
    """Вопрос -> поиск -> порог -> (отказ | ответ LLM) + источники.

    Лимит бесплатного тира OpenRouter (429) - воспроизводимая ситуация пилота,
    отдаём её отдельным понятным статусом, а не общей ошибкой 500.
    """
    with SessionLocal() as session:
        try:
            result = answer_question(
                session, resources["provider"], resources["client"], request.question
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
        sources=[SourceOut(**vars(source)) for source in result.sources],
        best_similarity=round(result.best_similarity, 4),
        elapsed_seconds=round(result.elapsed_seconds, 2),
    )
