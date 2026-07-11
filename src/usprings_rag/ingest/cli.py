"""CLI ingest. На этапе 3 - сухой прогон: парсинг и чанкинг со статистикой,
без записи в БД. Запись подключим на этапе 4.

Запуск: uv run ingest <dir>  (по умолчанию docs/manuals/IT_1C)
"""

import argparse
import sys
from pathlib import Path

from ..config import settings
from .chunker import chunk_pages, estimate_tokens
from .pdf import extract_pages

DEFAULT_DIR = Path("docs/manuals/IT_1C")


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


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest PDF в базу знаний RAG")
    parser.add_argument(
        "directory",
        nargs="?",
        type=Path,
        default=DEFAULT_DIR,
        help=f"папка с PDF (по умолчанию {DEFAULT_DIR})",
    )
    args = parser.parse_args()

    if not args.directory.is_dir():
        print(f"Папка не найдена: {args.directory}", file=sys.stderr)
        raise SystemExit(1)

    _dry_run(args.directory)


if __name__ == "__main__":
    main()
