"""Извлечение текста из PDF с привязкой к номерам страниц.

Основной путь - pypdf (текстовый слой). Возвращаем страницы по отдельности,
чтобы чанкинг мог проставить точную ссылку на страницы.
"""

from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader


@dataclass
class Page:
    """Одна страница PDF: номер (с 1) и извлечённый текст."""

    number: int
    text: str


def extract_pages(path: str | Path) -> list[Page]:
    """Извлечь текст постранично из PDF через pypdf."""
    reader = PdfReader(str(path))
    pages: list[Page] = []
    for i, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        pages.append(Page(number=i, text=text.strip()))
    return pages
