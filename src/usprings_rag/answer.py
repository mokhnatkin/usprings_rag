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

from .collection import Collection
from .embeddings import EmbeddingProvider
from .llm import NO_ANSWER_MARKER, generate, stream_generate
from .retrieval import search

logger = logging.getLogger(__name__)


def refusal_text(collection: Collection) -> str:
    """Отказ называет базу, по которой искали: иначе непонятно, где нет ответа."""
    return (
        f"К сожалению, в инструкциях {collection.title} нет информации по этому "
        f"вопросу. Задайте вопрос по инструкциям {collection.title} "
        f"или обратитесь в ИТ-службу."
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
    """Результат сценария: текст, признак отказа, источники, диагностика.

    Поля usage/model нужны логированию (query_log). У отказа - нули и пустая
    модель: LLM не вызывали.
    """

    text: str
    refused: bool
    sources: list[Source]
    best_similarity: float
    elapsed_seconds: float
    model_id: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0


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


def _refusal(collection: Collection, best_similarity: float, elapsed: float) -> Answer:
    """Вежливый отказ без источников - они бы ничего не подтверждали."""
    return Answer(
        text=refusal_text(collection),
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
    collection: Collection,
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
    result = search(session, provider, question, collection)

    if not result.passed:
        elapsed = time.perf_counter() - started
        logger.info(
            "answer collection=%s verdict=refused best_similarity=%.4f elapsed=%.2fs",
            collection.code,
            result.best_similarity,
            elapsed,
        )
        yield "done", _refusal(collection, result.best_similarity, elapsed)
        return

    hits = result.relevant
    buffer = ""
    holding = True
    usage: dict = {}  # заполнится расходом токенов из финального чанка

    for delta in stream_generate(client, question, hits, model=model, usage=usage):
        if not holding:
            yield "delta", delta
            continue

        buffer += delta
        stripped = buffer.strip()
        if stripped.startswith(NO_ANSWER_MARKER):
            elapsed = time.perf_counter() - started
            logger.info(
                "answer collection=%s verdict=no_answer_in_context "
                "best_similarity=%.4f elapsed=%.2fs",
                collection.code,
                result.best_similarity,
                elapsed,
            )
            yield "done", _refusal(collection, result.best_similarity, elapsed)
            return
        if NO_ANSWER_MARKER.startswith(stripped):
            continue  # пока неотличимо от начала маркера - придерживаем
        holding = False
        yield "delta", buffer

    if holding and buffer:  # ответ короче маркера и на него не похож
        yield "delta", buffer

    elapsed = time.perf_counter() - started
    logger.info(
        "answer collection=%s verdict=answered chunks=%d best_similarity=%.4f "
        "elapsed=%.2fs (stream)",
        collection.code,
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
            model_id=usage.get("model", ""),
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
        ),
    )


def answer_question(
    session: Session,
    provider: EmbeddingProvider,
    client: OpenAI,
    question: str,
    collection: Collection,
    model: str | None = None,
) -> Answer:
    """Ответить на вопрос по выбранной коллекции или вежливо отказать.

    `model` переопределяет `OPENROUTER_MODEL` - нужно для A/B-сравнения моделей.
    """
    started = time.perf_counter()
    result = search(session, provider, question, collection)

    if not result.passed:
        elapsed = time.perf_counter() - started
        logger.info(
            "answer collection=%s verdict=refused best_similarity=%.4f elapsed=%.2fs",
            collection.code,
            result.best_similarity,
            elapsed,
        )
        return _refusal(collection, result.best_similarity, elapsed)

    hits = result.relevant
    response = generate(client, question, hits, model=model)
    elapsed = time.perf_counter() - started

    # Вопрос прошёл порог, но ответа во фрагментах нет (околодоменный вопрос -
    # порог их пропускает сознательно, см. open-questions.md). Показывать под таким
    # ответом источники нельзя - они ничего не подтверждают.
    if NO_ANSWER_MARKER in response.text:
        logger.info(
            "answer collection=%s verdict=no_answer_in_context "
            "best_similarity=%.4f elapsed=%.2fs",
            collection.code,
            result.best_similarity,
            elapsed,
        )
        return _refusal(collection, result.best_similarity, elapsed)

    logger.info(
        "answer collection=%s verdict=answered chunks=%d best_similarity=%.4f "
        "elapsed=%.2fs",
        collection.code,
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
        model_id=response.model,
        prompt_tokens=response.prompt_tokens,
        completion_tokens=response.completion_tokens,
    )
