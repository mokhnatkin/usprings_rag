"""CLI ingest: обход папки коллекции с PDF -> парсинг, чанкинг, векторизация, БД.

Запуск: uv run ingest --collection erp            полный прогон с записью в БД
        uv run ingest --collection zup --dry-run  только парсинг и чанкинг

Папка берётся из справочника коллекций (коллекция = папка); её можно
переопределить позиционным аргументом - например, чтобы залить в коллекцию
подпапку или отдельный каталог.
"""

import argparse
import sys
from pathlib import Path

from ..collection import get_collection
from ..config import settings
from ..db import SessionLocal
from ..embeddings import BGEEmbeddingProvider
from .chunker import chunk_pages, estimate_tokens
from .pdf import extract_pages
from .pipeline import ensure_partition, ingest_file


def _dry_run(directory: Path) -> None:
    """Пройти по PDF в папке, показать статистику парсинга и чанкинга."""
    pdfs = sorted(directory.glob("*.pdf"))
    if not pdfs:
        print(f"Нет PDF в {directory}")
        return

    print(
        f"Сухой прогон: {len(pdfs)} PDF, "
        f"max_tokens={settings.chunk_max_tokens}, overlap={settings.chunk_overlap}\n"
    )
    for pdf in pdfs:
        pages = extract_pages(pdf)
        chunks = chunk_pages(
            pages, settings.chunk_max_tokens, settings.chunk_overlap
        )
        print("=" * 78)
        print(pdf.name)
        print(f"  страниц: {len(pages)}, чанков: {len(chunks)}")
        for ch in chunks:
            preview = ch.content[:70].replace("\n", " ")
            pages_ref = (
                f"стр.{ch.page_from}"
                if ch.page_from == ch.page_to
                else f"стр.{ch.page_from}-{ch.page_to}"
            )
            print(
                f"  [{ch.chunk_index}] {pages_ref}, "
                f"~{estimate_tokens(ch.content)} ток., {len(ch.content)} симв.: {preview}"
            )


def _ingest(directory: Path, collection_code: str) -> None:
    """Полный прогон: векторизация чанков и запись документов в коллекцию."""
    collection = get_collection(collection_code)
    pdfs = sorted(directory.glob("*.pdf"))
    if not pdfs:
        print(f"Нет PDF в {directory}")
        return

    print(f"Загрузка модели эмбеддингов {settings.embedding_model}...")
    provider = BGEEmbeddingProvider()

    print(f"Ingest: {len(pdfs)} PDF из {directory} -> коллекция {collection.code}\n")
    with SessionLocal() as session:
        ensure_partition(session, collection)
        for pdf in pdfs:
            result = ingest_file(session, provider, pdf, collection)
            print(f"  [{result.status}] {pdf.name} - чанков: {result.chunks}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest PDF в базу знаний RAG")
    parser.add_argument(
        "--collection",
        required=True,
        help="коллекция (база знаний), в которую грузим документы",
    )
    parser.add_argument(
        "directory",
        nargs="?",
        type=Path,
        default=None,
        help="папка с PDF (по умолчанию папка коллекции)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="только парсинг и чанкинг со статистикой, без записи в БД",
    )
    args = parser.parse_args()

    try:
        collection = get_collection(args.collection)
    except ValueError as exc:
        parser.error(str(exc))
    directory = args.directory or Path(settings.manuals_dir) / collection.folder
    if not directory.is_dir():
        print(f"Папка не найдена: {directory}", file=sys.stderr)
        raise SystemExit(1)

    if args.dry_run:
        _dry_run(directory)
    else:
        _ingest(directory, args.collection)


if __name__ == "__main__":
    main()
