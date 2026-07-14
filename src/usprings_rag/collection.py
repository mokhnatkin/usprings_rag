"""Коллекции - базы знаний по продуктам (1С:ERP, 1С:ЗУП).

Единый справочник: код в БД, название для UI, папка инструкций и порог сходства.
Пользователь выбирает коллекцию до вопроса, поиск идёт только по ней (чанки
секционированы по коллекции - см. docs/MVP/MVP0/backlog.md).

На MVP0 справочник - enum в коде; таблица коллекций с админкой - пост-MVP.
Модуль назван в единственном числе, чтобы не затенять stdlib `collections`.
"""

from dataclasses import dataclass
from enum import StrEnum


class CollectionCode(StrEnum):
    """Код коллекции - значение в БД и в API."""

    ERP = "erp"
    ZUP = "zup"


@dataclass(frozen=True)
class Collection:
    """Свойства коллекции: как называется, откуда грузится, каким порогом отсекается."""

    code: CollectionCode
    title: str  # для UI и текстов ответа
    folder: str  # папка с PDF относительно settings.manuals_dir
    threshold: float  # порог сходства - свой у каждой коллекции


COLLECTIONS: dict[CollectionCode, Collection] = {
    CollectionCode.ERP: Collection(
        code=CollectionCode.ERP,
        title="1С:ERP",
        folder="its_erp",
        threshold=0.5,
    ),
    CollectionCode.ZUP: Collection(
        code=CollectionCode.ZUP,
        title="1С:ЗУП",
        folder="its_zup",
        # Калибровка 2026-07-14 на eval ИТС (18 вопросов): релевантные от 0.5835,
        # нерелевантные до 0.5048, кроме одного околодоменного («сколько стоит
        # лицензия ЗУП» - 0.6389, лексически почти корпус). 0.55 держит все
        # релевантные с запасом и режет чужие темы, включая вопрос из соседней
        # коллекции (отгрузка со склада - 0.5048). Околодоменный негатив порог
        # проходит сознательно: его ловит промпт (см. open-questions.md).
        threshold=0.55,
    ),
}

DEFAULT_COLLECTION = CollectionCode.ERP  # основная база пилота


def get_collection(code: str) -> Collection:
    """Коллекция по коду. Неизвестный код - ValueError (в API превращается в 422)."""
    try:
        return COLLECTIONS[CollectionCode(code)]
    except ValueError:
        known = ", ".join(COLLECTIONS)
        raise ValueError(f"неизвестная коллекция: {code} (известны: {known})") from None
