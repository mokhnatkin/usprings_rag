"""Оркестрация ingest: PDF -> чанки -> эмбеддинги -> БД.

Идемпотентность по паре (source_path, content_hash): файл без изменений
пропускаем; изменившийся - удаляем старый документ (каскадом чанки) и вставляем
заново. Документ и его чанки пишем в одной транзакции.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..embeddings import EmbeddingProvider
from ..models import Chunk, Document
from .chunker import chunk_pages
from .pdf import extract_pages


@dataclass
class IngestResult:
    """Исход обработки одного файла."""

    path: Path
    status: str  # "inserted" | "updated" | "skipped"
    chunks: int


def _file_hash(path: Path) -> str:
    """SHA-256 содержимого файла - для детекции изменений."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def relative_source_path(path: Path) -> str:
    """Путь относительно папки инструкций, POSIX-разделители.

    Абсолютный путь хоста не переживёт перенос в контейнер и не годится для
    URL раздачи PDF, поэтому в БД храним относительный (напр. `IT_1C/Отгрузка.pdf`).
    """
    manuals_root = Path(settings.manuals_dir).resolve()
    return path.resolve().relative_to(manuals_root).as_posix()


def ingest_file(
    session: Session, provider: EmbeddingProvider, path: Path
) -> IngestResult:
    """Обработать один PDF: парсинг, чанкинг, векторизация, запись в БД."""
    source_path = relative_source_path(path)
    content_hash = _file_hash(path)

    existing = session.scalars(
        select(Document).where(Document.source_path == source_path)
    ).first()
    if existing and existing.content_hash == content_hash:
        return IngestResult(path, "skipped", len(existing.chunks))

    status = "inserted"
    if existing:
        session.delete(existing)  # каскад удалит старые чанки
        session.flush()
        status = "updated"

    pages = extract_pages(path)
    chunks = chunk_pages(pages, settings.chunk_max_tokens, settings.chunk_overlap)
    vectors = provider.embed_texts([c.content for c in chunks])

    document = Document(
        title=path.stem,
        source_path=source_path,
        content_hash=content_hash,
        chunks=[
            Chunk(
                chunk_index=c.chunk_index,
                page_from=c.page_from,
                page_to=c.page_to,
                content=c.content,
                embedding=vector,
            )
            for c, vector in zip(chunks, vectors)
        ],
    )
    session.add(document)
    session.commit()
    return IngestResult(path, status, len(chunks))
