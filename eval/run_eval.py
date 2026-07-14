"""Прогон eval-набора: recall@k и распределения сходств - по каждой коллекции.

Запуск: uv run --no-sync python eval/run_eval.py [--collection erp]

Порог здесь не применяется - скрипт собирает сырые сходства, по которым порог
выбирается. Решающая величина - лучшее сходство запроса: именно по нему работает
«белый список» в retrieval.py.

Коллекции считаются раздельно: у каждой свой корпус, своя лексика и, как следствие,
своё распределение сходств - общий порог для них смысла не имеет.
"""

import argparse
import logging
from pathlib import Path

import yaml

from usprings_rag.collection import COLLECTIONS, get_collection
from usprings_rag.config import settings
from usprings_rag.db import SessionLocal
from usprings_rag.embeddings import BGEEmbeddingProvider
from usprings_rag.retrieval import search

QUESTIONS_FILE = Path(__file__).parent / "questions.yaml"


def evaluate(only: str | None = None) -> None:
    questions = yaml.safe_load(QUESTIONS_FILE.read_text(encoding="utf-8"))["questions"]
    codes = [only] if only else sorted({item["collection"] for item in questions})

    print(f"Загрузка модели эмбеддингов {settings.embedding_model}...")
    provider = BGEEmbeddingProvider()

    for code in codes:
        collection = get_collection(code)
        subset = [item for item in questions if item["collection"] == code]
        if not subset:
            print(f"\nНет вопросов для коллекции {code}")
            continue
        _evaluate_collection(provider, collection, subset)


def _evaluate_collection(provider, collection, questions) -> None:
    results = []
    with SessionLocal() as session:
        for item in questions:
            result = search(
                session, provider, item["q"], collection, top_k=settings.top_k
            )
            found = [hit.title for hit in result.hits]
            expected = item.get("expected_doc")
            rank = found.index(expected) + 1 if expected in found else None
            results.append(
                {
                    "q": item["q"],
                    "kind": item["kind"],
                    "expected": expected,
                    "best": result.best_similarity,
                    "rank": rank,
                    "top_doc": found[0] if found else None,
                }
            )

    print(f"\n{'=' * 78}")
    print(f"Коллекция {collection.title} ({collection.code}), "
          f"вопросов: {len(results)}, top_k={settings.top_k}\n")
    for r in results:
        if r["expected"]:
            mark = f"rank={r['rank']}" if r["rank"] else "MISS"
        else:
            mark = "-"
        print(f"  [{r['kind']:<11}] best={r['best']:.4f} {mark:<7} {r['q'][:58]}")
        if r["expected"] and r["rank"] != 1:
            print(f"      ожидали: {r['expected']}, первым пришёл: {r['top_doc']}")

    relevant = [r for r in results if r["kind"] in ("covered", "paraphrased")]
    irrelevant = [r for r in results if r["kind"] == "irrelevant"]

    print(f"\nRecall по покрытым вопросам ({len(relevant)} шт.):")
    for kind in ("covered", "paraphrased"):
        group = [r for r in relevant if r["kind"] == kind]
        if not group:
            continue
        at_1 = sum(1 for r in group if r["rank"] == 1)
        at_k = sum(1 for r in group if r["rank"] is not None)
        print(
            f"  {kind:<11}: recall@1 = {at_1}/{len(group)}, "
            f"recall@{settings.top_k} = {at_k}/{len(group)}"
        )

    rel = sorted(r["best"] for r in relevant)
    irr = sorted(r["best"] for r in irrelevant)
    print("\nРаспределение лучших сходств:")
    print(f"  relevant   ({len(rel)}): min={rel[0]:.4f} max={rel[-1]:.4f}  "
          f"{[f'{s:.3f}' for s in rel]}")
    print(f"  irrelevant ({len(irr)}): min={irr[0]:.4f} max={irr[-1]:.4f}  "
          f"{[f'{s:.3f}' for s in irr]}")

    gap = rel[0] - irr[-1]
    print(f"\nЗазор: min(relevant) - max(irrelevant) = {gap:+.4f}")
    if gap > 0:
        print(f"Разделение чистое. Середина зазора: {(rel[0] + irr[-1]) / 2:.4f}")
    else:
        print("Распределения пересекаются. Релевантные вопросы в зоне пересечения:")
        for r in relevant:
            if r["best"] <= irr[-1]:
                print(f"  best={r['best']:.4f}  {r['q'][:58]}")
    print(f"\nТекущий порог коллекции {collection.code}: {collection.threshold}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Eval поиска по коллекциям")
    parser.add_argument(
        "--collection",
        choices=[code.value for code in COLLECTIONS],
        help="считать только одну коллекцию (по умолчанию - все, что есть в наборе)",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    evaluate(args.collection)


if __name__ == "__main__":
    main()
