"""Провайдер эмбеддингов за интерфейсом - чтобы позже сменить in-process модель
на внешний сервис/API без переработки вызывающего кода.

MVP: BGE-m3 (multilingual, dim 1024) через sentence-transformers, in-process.
Вектора нормализуем (косинусная близость). BGE-m3 не требует инструкционного
префикса к запросу - вопрос и чанк кодируем одинаково.
"""

from typing import Protocol

from .config import settings


class EmbeddingProvider(Protocol):
    """Контракт провайдера эмбеддингов."""

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Векторизовать батч текстов (чанки при ingest)."""
        ...

    def embed_query(self, text: str) -> list[float]:
        """Векторизовать один запрос (вопрос пользователя)."""
        ...


class BGEEmbeddingProvider:
    """Реализация на sentence-transformers. Модель грузится один раз (лениво)."""

    def __init__(self, model_name: str | None = None):
        self._model_name = model_name or settings.embedding_model
        self._model = None

    @property
    def model(self):
        """Ленивая загрузка модели при первом обращении."""
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self._model_name)
        return self._model

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        vectors = self.model.encode(
            texts, normalize_embeddings=True, show_progress_bar=False
        )
        return vectors.tolist()

    def embed_query(self, text: str) -> list[float]:
        return self.embed_texts([text])[0]
