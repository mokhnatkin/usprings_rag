"""Семантический поиск по чанкам коллекции: вопрос -> эмбеддинг -> top-k по косинусу.

Важно про метрику: pgvector-оператор `<=>` (cosine_distance) возвращает косинусное
РАССТОЯНИЕ, а порог «белого списка» сформулирован как СХОДСТВО. Пересчитываем здесь
(`similarity = 1 - distance`), чтобы дальше по коду, в логах и в конфиге все
говорили на одном языке.

Поиск всегда идёт в границах одной коллекции, и фильтр стоит на `chunks.collection`,
а не на `documents`: с HNSW фильтр применяется после обхода индекса, поэтому по
джойну соседние базы всё равно съедали бы top-k. Условие на секционированной
колонке отсекает чужие секции до входа в индекс (см. миграцию 0003).

Порог применяем к лучшему совпадению: если оно ниже порога коллекции - запрос
считаем непокрытым базой знаний (вежливый отказ, LLM не вызываем).
"""

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .collection import Collection
from .config import settings
from .embeddings import EmbeddingProvider
from .models import Chunk, Document

logger = logging.getLogger(__name__)


@dataclass
class SearchHit:
    """Найденный чанк со сходством и метаданными для цитирования."""

    chunk_id: int
    document_id: int
    title: str
    source_path: str
    page_from: int | None
    page_to: int | None
    content: str
    similarity: float
    above_threshold: bool

    @property
    def pages_ref(self) -> str:
        """Ссылка на страницы для показа пользователю: 'стр.3' или 'стр.3-4'."""
        if self.page_from is None:
            return ""
        if self.page_from == self.page_to:
            return f"стр.{self.page_from}"
        return f"стр.{self.page_from}-{self.page_to}"


@dataclass
class SearchResult:
    """Выдача по одному вопросу: кандидаты (по убыванию сходства) и вердикт порога."""

    query: str
    hits: list[SearchHit]
    threshold: float

    @property
    def best_similarity(self) -> float:
        """Сходство лучшего кандидата (0.0, если ничего не нашлось)."""
        return self.hits[0].similarity if self.hits else 0.0

    @property
    def passed(self) -> bool:
        """Прошёл ли запрос порог «белого списка» (по лучшему совпадению)."""
        return self.best_similarity >= self.threshold

    @property
    def relevant(self) -> list[SearchHit]:
        """Кандидаты выше порога - кандидаты в контекст LLM."""
        return [hit for hit in self.hits if hit.above_threshold]


def search(
    session: Session,
    provider: EmbeddingProvider,
    query: str,
    collection: Collection,
    top_k: int | None = None,
    threshold: float | None = None,
) -> SearchResult:
    """Найти top-k чанков коллекции и пометить их относительно её порога."""
    top_k = top_k if top_k is not None else settings.top_k
    threshold = threshold if threshold is not None else collection.threshold

    vector = provider.embed_query(query)
    distance = Chunk.embedding.cosine_distance(vector).label("distance")

    rows = session.execute(
        select(Chunk, Document, distance)
        .join(Document, Chunk.document_id == Document.id)
        .where(
            Chunk.collection == collection.code,
            Document.archived_at.is_(None),  # архивные документы вне выдачи
        )
        .order_by(distance)
        .limit(top_k)
    ).all()

    hits = [
        SearchHit(
            chunk_id=chunk.id,
            document_id=document.id,
            title=document.title,
            source_path=document.source_path,
            page_from=chunk.page_from,
            page_to=chunk.page_to,
            content=chunk.content,
            similarity=1.0 - dist,
            above_threshold=(1.0 - dist) >= threshold,
        )
        for chunk, document, dist in rows
    ]

    result = SearchResult(query=query, hits=hits, threshold=threshold)
    logger.info(
        "search collection=%s query=%r candidates=%d best_similarity=%.4f "
        "threshold=%.2f verdict=%s",
        collection.code,
        query[:80],
        len(hits),
        result.best_similarity,
        threshold,
        "passed" if result.passed else "below_threshold",
    )
    return result
