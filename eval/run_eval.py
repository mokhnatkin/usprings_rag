"""Прогон eval-набора (подшаг 5.5): recall@k по покрытым вопросам и
распределения лучших сходств relevant vs irrelevant для выбора порога.

Запуск: uv run --no-sync python eval/run_eval.py

Порог здесь не применяется - скрипт собирает сырые сходства, по которым
порог выбирается (подшаг 5.6). Решающая величина - лучшее сходство запроса:
именно по нему работает «белый список» в retrieval.py.
"""

import logging
from pathlib import Path

import yaml

from usprings_rag.config import settings
from usprings_rag.db import SessionLocal
from usprings_rag.embeddings import BGEEmbeddingProvider
from usprings_rag.retrieval import search

QUESTIONS_FILE = Path(__file__).parent / "questions.yaml"


def evaluate() -> None:
    data = yaml.safe_load(QUESTIONS_FILE.read_text(encoding="utf-8"))
    questions = data["questions"]

    print(f"Загрузка модели эмбеддингов {settings.embedding_model}...")
    provider = BGEEmbeddingProvider()

    results = []
    with SessionLocal() as session:
        for item in questions:
            result = search(session, provider, item["q"], top_k=settings.top_k)
            found_docs = [hit.title for hit in result.hits]
            expected = item.get("expected_doc")
            rank = found_docs.index(expected) + 1 if expected in found_docs else None
            results.append(
                {
                    "q": item["q"],
                    "kind": item["kind"],
                    "expected": expected,
                    "best": result.best_similarity,
                    "rank": rank,
                    "top_doc": found_docs[0] if found_docs else None,
                }
            )

    print(f"\n{'=' * 78}\nПо вопросам (top_k={settings.top_k}):\n")
    for r in results:
        if r["expected"]:
            mark = f"rank={r['rank']}" if r["rank"] else "MISS"
        else:
            mark = "-"
        print(f"  [{r['kind']:<11}] best={r['best']:.4f} {mark:<7} {r['q'][:60]}")
        if r["expected"] and r["rank"] != 1:
            print(f"      ожидали: {r['expected']}, первым пришёл: {r['top_doc']}")

    relevant = [r for r in results if r["kind"] in ("covered", "paraphrased")]
    irrelevant = [r for r in results if r["kind"] == "irrelevant"]

    print(f"\n{'=' * 78}\nRecall по покрытым вопросам ({len(relevant)} шт.):")
    for kind in ("covered", "paraphrased"):
        group = [r for r in relevant if r["kind"] == kind]
        at_1 = sum(1 for r in group if r["rank"] == 1)
        at_k = sum(1 for r in group if r["rank"] is not None)
        print(
            f"  {kind:<11}: recall@1 = {at_1}/{len(group)}, "
            f"recall@{settings.top_k} = {at_k}/{len(group)}"
        )

    rel_sims = sorted(r["best"] for r in relevant)
    irr_sims = sorted(r["best"] for r in irrelevant)
    print("\nРаспределение лучших сходств:")
    print(f"  relevant   ({len(rel_sims)}): "
          f"min={rel_sims[0]:.4f} max={rel_sims[-1]:.4f}  {[f'{s:.3f}' for s in rel_sims]}")
    print(f"  irrelevant ({len(irr_sims)}): "
          f"min={irr_sims[0]:.4f} max={irr_sims[-1]:.4f}  {[f'{s:.3f}' for s in irr_sims]}")

    gap = rel_sims[0] - irr_sims[-1]
    print(f"\nЗазор: min(relevant) - max(irrelevant) = {gap:+.4f}")
    if gap > 0:
        midpoint = (rel_sims[0] + irr_sims[-1]) / 2
        print(f"Разделение чистое. Середина зазора: {midpoint:.4f}")
    else:
        overlap_rel = [r for r in relevant if r["best"] <= irr_sims[-1]]
        print("Распределения пересекаются. Релевантные вопросы в зоне пересечения:")
        for r in overlap_rel:
            print(f"  best={r['best']:.4f}  {r['q'][:60]}")
    print(f"\nТекущий SIMILARITY_THRESHOLD={settings.similarity_threshold}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    evaluate()
