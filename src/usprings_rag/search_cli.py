"""CLI для ручной проверки выдачи поиска в выбранной коллекции.

Запуск: uv run --no-sync search "вопрос" --collection erp
        uv run --no-sync search "вопрос" --collection zup --top-k 10 --threshold 0.4

Показывает top-k чанков со сходством и пометкой относительно порога - чтобы
глазами оценить качество поиска и накопить картину распределения близостей
перед калибровкой порога коллекции.
"""

import argparse
import logging

from .collection import DEFAULT_COLLECTION, get_collection
from .config import settings
from .db import SessionLocal
from .embeddings import BGEEmbeddingProvider
from .retrieval import search


def main() -> None:
    parser = argparse.ArgumentParser(description="Проверка семантического поиска")
    parser.add_argument("query", help="вопрос пользователя")
    parser.add_argument(
        "--collection",
        default=str(DEFAULT_COLLECTION),
        help=f"коллекция (по умолчанию {DEFAULT_COLLECTION})",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=settings.top_k,
        help=f"число кандидатов (по умолчанию {settings.top_k})",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="порог сходства (по умолчанию - порог коллекции)",
    )
    parser.add_argument(
        "--full", action="store_true", help="показать текст чанков целиком"
    )
    args = parser.parse_args()
    try:
        collection = get_collection(args.collection)
    except ValueError as exc:
        parser.error(str(exc))

    # INFO только для своих логгеров: на root он тянет шумные логи httpx/huggingface.
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    logging.getLogger("usprings_rag").setLevel(logging.INFO)

    print(f"Загрузка модели эмбеддингов {settings.embedding_model}...")
    provider = BGEEmbeddingProvider()

    with SessionLocal() as session:
        result = search(
            session, provider, args.query, collection, args.top_k, args.threshold
        )

    verdict = "выше порога" if result.passed else "НИЖЕ ПОРОГА (отказ)"
    print(f"\nКоллекция: {collection.title}")
    print(f"Вопрос: {result.query}")
    print(
        f"Лучшее сходство: {result.best_similarity:.4f} "
        f"при пороге {result.threshold:.2f} -> {verdict}\n"
    )

    for position, hit in enumerate(result.hits, start=1):
        mark = "+" if hit.above_threshold else "-"
        body = hit.content if args.full else hit.content[:200].replace("\n", " ")
        print(f"[{mark}] {position}. {hit.similarity:.4f}  {hit.title} {hit.pages_ref}")
        print(f"    {body}\n")


if __name__ == "__main__":
    main()
