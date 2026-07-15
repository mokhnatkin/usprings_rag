"""Коллекции - базы знаний по продуктам (1С:ERP, 1С:ЗУП).

Справочник живёт в таблице `collections` (см. модель `CollectionRow`). Этот модуль -
тонкая read-model поверх неё: тот же объект `Collection` с полями
`code`/`title`/`folder`/`threshold`, что и раньше, чтобы поиск, генерация и ingest
не меняли сигнатур. Справочник кэшируется в памяти; правки из админки сбрасывают
кэш через `invalidate_cache()`.

Пользователь выбирает коллекцию до вопроса, поиск идёт только по ней (чанки
секционированы по коллекции - см. docs/MVP/MVP0/backlog.md).

Модуль назван в единственном числе, чтобы не затенять stdlib `collections`.
"""

from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import select

from .db import SessionLocal
from .models import CollectionRow


class CollectionCode(StrEnum):
    """Известные коды коллекций. Не источник истины (им стала таблица), но удобны
    как константы для сида миграции, значения по умолчанию и валидации."""

    ERP = "erp"
    ZUP = "zup"


@dataclass(frozen=True)
class Collection:
    """Свойства коллекции: как называется, откуда грузится, каким порогом отсекается."""

    code: str  # код в БД и в API
    title: str  # для UI и текстов ответа
    folder: str  # папка с PDF относительно settings.manuals_dir
    threshold: float  # порог сходства - свой у каждой коллекции
    is_active: bool = True


DEFAULT_COLLECTION = CollectionCode.ERP  # основная база пилота


# Кэш справочника: код -> Collection. Загружается лениво из БД, сбрасывается при
# правках из админки. На пилотном масштабе коллекций единицы - держать в памяти дёшево.
_cache: dict[str, Collection] | None = None


def _load() -> dict[str, Collection]:
    """Прочитать весь справочник из БД (включая неактивные)."""
    with SessionLocal() as session:
        rows = session.scalars(select(CollectionRow)).all()
    return {
        row.code: Collection(
            code=row.code,
            title=row.title,
            folder=row.folder,
            threshold=row.threshold,
            is_active=row.is_active,
        )
        for row in rows
    }


def _ensure_loaded() -> dict[str, Collection]:
    global _cache
    if _cache is None:
        _cache = _load()
    return _cache


def invalidate_cache() -> None:
    """Сбросить кэш - вызывать после правки справочника (создание/изменение)."""
    global _cache
    _cache = None


def get_collection(code: str) -> Collection:
    """Коллекция по коду. Неизвестный код - ValueError (в API превращается в 422)."""
    collections = _ensure_loaded()
    collection = collections.get(str(code))
    if collection is None:
        known = ", ".join(sorted(collections)) or "нет коллекций"
        raise ValueError(f"неизвестная коллекция: {code} (известны: {known})")
    return collection


def list_collections(active_only: bool = True) -> list[Collection]:
    """Все коллекции справочника, по коду. `active_only` скрывает деактивированные."""
    collections = _ensure_loaded().values()
    items = [c for c in collections if c.is_active or not active_only]
    return sorted(items, key=lambda c: c.code)
