"""Создание и правка коллекций (баз знаний).

Создание коллекции - многошаговая операция: строка в справочнике + секция
`chunks` (её HNSW-индекс Postgres заводит сам, индекс объявлен на родителе) +
папка инструкций. DDL в Postgres транзакционен, поэтому строку и секцию пишем
в одной транзакции - частичная «полу-коллекция» в БД невозможна. Папку создаём
до коммита; пустая папка при откате безвредна.
"""

import re
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .collection import Collection, invalidate_cache
from .config import settings
from .models import CollectionRow

# Код становится именем секции (chunks_<code>) и подставляется в DDL, где параметры
# невозможны, поэтому набор символов ограничиваем жёстко.
CODE_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def create_collection(
    session: Session, code: str, title: str, folder: str, threshold: float
) -> Collection:
    """Завести коллекцию: справочник + секция chunks + папка инструкций.

    Идемпотентности нет: повторный код - ошибка (ValueError), чтобы не затереть
    существующую базу знаний.
    """
    if not CODE_RE.match(code):
        raise ValueError(
            f"недопустимый код коллекции: {code!r} "
            "(строчные латинские буквы, цифры и подчёркивание, начинается с буквы)"
        )
    exists = session.scalar(select(CollectionRow).where(CollectionRow.code == code))
    if exists:
        raise ValueError(f"коллекция {code!r} уже существует")

    (Path(settings.manuals_dir) / folder).mkdir(parents=True, exist_ok=True)

    row = CollectionRow(code=code, title=title, folder=folder, threshold=threshold)
    session.add(row)
    session.flush()
    session.execute(
        text(
            f"CREATE TABLE IF NOT EXISTS chunks_{code} "
            f"PARTITION OF chunks FOR VALUES IN ('{code}')"
        )
    )
    session.commit()
    invalidate_cache()
    return Collection(
        code=row.code,
        title=row.title,
        folder=row.folder,
        threshold=row.threshold,
        is_active=row.is_active,
        id=row.id,
    )
