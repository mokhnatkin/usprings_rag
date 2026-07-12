"""Прогон вопросов через полный сценарий ответа (этап 6): поиск -> порог ->
(отказ | генерация LLM). Служит и приёмкой 6.2, и A/B-сравнением моделей (6.4).

Запуск: uv run --no-sync python eval/run_answers.py
        uv run --no-sync python eval/run_answers.py --model google/gemma-4-31b-it:free

По умолчанию берёт вопросы из questions.yaml: несколько покрытых, один
околодоменный непокрытый (проверка честности промпта: ожидаем «информации нет»,
он проходит порог сознательно - см. open-questions.md) и вне-доменные
(проверка отказа без вызова LLM).
"""

import argparse
import logging

from usprings_rag.answer import answer_question
from usprings_rag.config import settings
from usprings_rag.db import SessionLocal
from usprings_rag.embeddings import BGEEmbeddingProvider
from usprings_rag.llm import create_client

# Компактный набор для приёмки этапа 6: покрытые, околодоменный, вне-доменные.
QUESTIONS = [
    "Можно ли изменить направление деятельности в действующем договоре?",
    "Как запланировать отгрузку продукции, если на складе нет остатков?",
    "Кто имеет право провести документ с лицензируемым товаром?",
    "Как указать ключевые параметры при формировании техпроцесса?",
    "Как провести инвентаризацию склада в 1С?",  # околодоменный: ждём "информации нет"
    "Какая погода в Астане на выходных?",  # вне-доменный: ждём отказ без LLM
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Прогон сценария ответа")
    parser.add_argument(
        "--model",
        default=settings.openrouter_model,
        help=f"ID модели OpenRouter (по умолчанию {settings.openrouter_model})",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    logging.getLogger("usprings_rag").setLevel(logging.INFO)

    print(f"Загрузка модели эмбеддингов {settings.embedding_model}...")
    provider = BGEEmbeddingProvider()
    client = create_client()

    print(f"LLM: {args.model}\n")
    with SessionLocal() as session:
        for question in QUESTIONS:
            print("=" * 78)
            print(f"ВОПРОС: {question}")
            result = answer_question(
                session, provider, client, question, model=args.model
            )
            verdict = "ОТКАЗ (без LLM)" if result.refused else "ОТВЕТ"
            print(
                f"[{verdict}] сходство={result.best_similarity:.4f}, "
                f"{result.elapsed_seconds:.1f} c"
            )
            print(f"\n{result.text}\n")
            if result.sources:
                print("Источники:")
                for source in result.sources:
                    print(f"  - {source.title} ({source.pages})")
            print()


if __name__ == "__main__":
    main()
