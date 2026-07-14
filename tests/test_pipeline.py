"""Проверка нормализации source_path: относительный путь с POSIX-разделителями.

Абсолютный путь хоста не переживает перенос в контейнер и не годится для URL
раздачи PDF - поэтому в БД кладём путь относительно папки инструкций.
"""

from pathlib import Path

from usprings_rag.config import settings
from usprings_rag.ingest.pipeline import relative_source_path


def test_relative_to_manuals_root_with_posix_separators():
    path = Path(settings.manuals_dir) / "its_erp" / "Оформление трудозатрат.pdf"
    assert relative_source_path(path) == "its_erp/Оформление трудозатрат.pdf"


def test_absolute_path_normalized_the_same_way():
    absolute = (Path(settings.manuals_dir) / "its_zup" / "Отгрузка.pdf").resolve()
    assert relative_source_path(absolute) == "its_zup/Отгрузка.pdf"
