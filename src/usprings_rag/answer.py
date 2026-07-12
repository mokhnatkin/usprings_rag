"""Сценарий вопрос-ответ: поиск -> порог -> (отказ | генерация) -> ответ + источники.

Тонкая оркестрация: поиск в retrieval.py, генерация в llm.py, здесь - развилка
по порогу и сборка источников из метаданных найденных чанков.

Отказ по порогу мгновенный: LLM не вызывается (экономим токены, исключаем
галлюцинации на нерелевантном). Порог отсекает вне-доменные вопросы; на
околодоменных непокрытых вопросах, прошедших порог, честность обеспечивает
системный промпт («в инструкциях этой информации нет») - см. open-questions.md.
"""

import logging
import time
from dataclasses import dataclass

from openai import OpenAI
from sqlalchemy.orm import Session

from .embeddings import EmbeddingProvider
from .llm import generate
from .retrieval import search

logger = logging.getLogger(__name__)

REFUSAL_TEXT = (
    "К сожалению, в доступных инструкциях нет информации по этому вопросу. "
    "Задайте вопрос по инструкциям 1С ERP или обратитесь в ИТ-службу."
)


@dataclass
class Source:
    """Источник ответа - из метаданных чанка, не из текста LLM."""

    document_id: int
    title: str
    source_path: str
    pages: str


@dataclass
class Answer:
    """Результат сценария: текст, признак отказа, источники, диагностика."""

    text: str
    refused: bool
    sources: list[Source]
    best_similarity: float
    elapsed_seconds: float


def _collect_sources(hits) -> list[Source]:
    """Уникальные документы из чанков, в порядке убывания сходства."""
    sources: dict[int, Source] = {}
    for hit in hits:
        existing = sources.get(hit.document_id)
        if existing is None:
            sources[hit.document_id] = Source(
                document_id=hit.document_id,
                title=hit.title,
                source_path=hit.source_path,
                pages=hit.pages_ref,
            )
        elif hit.pages_ref and hit.pages_ref not in existing.pages:
            existing.pages = f"{existing.pages}, {hit.pages_ref}"
    return list(sources.values())


def answer_question(
    session: Session,
    provider: EmbeddingProvider,
    client: OpenAI,
    question: str,
    model: str | None = None,
) -> Answer:
    """Ответить на вопрос по базе знаний или вежливо отказать.

    `model` переопределяет `OPENROUTER_MODEL` - нужно для A/B-сравнения моделей.
    """
    started = time.perf_counter()
    result = search(session, provider, question)

    if not result.passed:
        elapsed = time.perf_counter() - started
        logger.info(
            "answer verdict=refused best_similarity=%.4f elapsed=%.2fs",
            result.best_similarity,
            elapsed,
        )
        return Answer(
            text=REFUSAL_TEXT,
            refused=True,
            sources=[],
            best_similarity=result.best_similarity,
            elapsed_seconds=elapsed,
        )

    hits = result.relevant
    response = generate(client, question, hits, model=model)
    elapsed = time.perf_counter() - started
    logger.info(
        "answer verdict=answered chunks=%d best_similarity=%.4f elapsed=%.2fs",
        len(hits),
        result.best_similarity,
        elapsed,
    )
    return Answer(
        text=response.text,
        refused=False,
        sources=_collect_sources(hits),
        best_similarity=result.best_similarity,
        elapsed_seconds=elapsed,
    )
