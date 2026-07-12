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
from collections.abc import Iterator
from dataclasses import dataclass

from openai import OpenAI
from sqlalchemy.orm import Session

from .embeddings import EmbeddingProvider
from .llm import NO_ANSWER_MARKER, generate, stream_generate
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


def _refusal(best_similarity: float, elapsed: float) -> Answer:
    """Вежливый отказ без источников - они бы ничего не подтверждали."""
    return Answer(
        text=REFUSAL_TEXT,
        refused=True,
        sources=[],
        best_similarity=best_similarity,
        elapsed_seconds=elapsed,
    )


def stream_answer(
    session: Session,
    provider: EmbeddingProvider,
    client: OpenAI,
    question: str,
    model: str | None = None,
) -> Iterator[tuple[str, str | Answer]]:
    """Тот же сценарий, но текст ответа отдаётся по мере генерации.

    Выдаёт пары ("delta", кусок текста) и в конце ("done", Answer) с источниками
    и диагностикой. Отказ по порогу мгновенный - сразу ("done", Answer).

    Начало потока придерживаем: модель может ответить маркером NO_ANSWER (ответа
    во фрагментах нет), и он не должен мелькнуть на экране. Копим текст, пока он
    остаётся возможным началом маркера; как только расходится - отдаём накопленное
    и дальше стримим без задержки.
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
        yield "done", _refusal(result.best_similarity, elapsed)
        return

    hits = result.relevant
    buffer = ""
    holding = True

    for delta in stream_generate(client, question, hits, model=model):
        if not holding:
            yield "delta", delta
            continue

        buffer += delta
        stripped = buffer.strip()
        if stripped.startswith(NO_ANSWER_MARKER):
            elapsed = time.perf_counter() - started
            logger.info(
                "answer verdict=no_answer_in_context best_similarity=%.4f elapsed=%.2fs",
                result.best_similarity,
                elapsed,
            )
            yield "done", _refusal(result.best_similarity, elapsed)
            return
        if NO_ANSWER_MARKER.startswith(stripped):
            continue  # пока неотличимо от начала маркера - придерживаем
        holding = False
        yield "delta", buffer

    if holding and buffer:  # ответ короче маркера и на него не похож
        yield "delta", buffer

    elapsed = time.perf_counter() - started
    logger.info(
        "answer verdict=answered chunks=%d best_similarity=%.4f elapsed=%.2fs (stream)",
        len(hits),
        result.best_similarity,
        elapsed,
    )
    yield (
        "done",
        Answer(
            text="",  # текст уже ушёл дельтами
            refused=False,
            sources=_collect_sources(hits),
            best_similarity=result.best_similarity,
            elapsed_seconds=elapsed,
        ),
    )


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

    # Вопрос прошёл порог, но ответа во фрагментах нет (околодоменный вопрос -
    # порог их пропускает сознательно, см. open-questions.md). Показывать под таким
    # ответом источники нельзя - они ничего не подтверждают.
    if NO_ANSWER_MARKER in response.text:
        logger.info(
            "answer verdict=no_answer_in_context best_similarity=%.4f elapsed=%.2fs",
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
