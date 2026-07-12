"""FastAPI: endpoint вопрос-ответ.

Модель эмбеддингов и клиент LLM создаются один раз при старте (lifespan):
иначе первый запрос пользователя платит десятки секунд за загрузку весов BGE-m3.
Прогрев - холостая векторизация, чтобы веса реально легли в память.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from openai import RateLimitError
from pydantic import BaseModel

from .answer import answer_question
from .db import SessionLocal
from .embeddings import BGEEmbeddingProvider
from .llm import create_client

logger = logging.getLogger(__name__)

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
