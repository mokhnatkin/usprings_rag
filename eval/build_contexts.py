"""Подготовка контекстов для A/B моделей: поиск выполняется ОДИН раз и кэшируется.

Запуск: uv run --no-sync python eval/build_contexts.py   (-> eval/contexts.json)

Зачем: A/B сравнивает LLM, а не поиск. Вопросы и найденные чанки для всех моделей
одинаковы, поэтому гонять BGE-m3 (2,3 ГБ, ~минута на загрузку) под каждую модель -
чистая потеря времени. Кэш решает сразу три задачи:
  - `run_ab.py` не тянет torch: прогон стартует мгновенно и не оставляет висящих
    процессов (известная беда проекта - см. журнал прогресса);
  - контекст у сравниваемых моделей гарантированно идентичен;
  - вопросы, не прошедшие порог, отмечены сразу - LLM на них не тратится.

Кэш пересобирать после ingest, смены модели эмбеддингов, порога или questions.yaml.
"""

import json
from dataclasses import asdict
from pathlib import Path

import yaml

from usprings_rag.config import settings
from usprings_rag.db import SessionLocal
from usprings_rag.embeddings import BGEEmbeddingProvider
from usprings_rag.retrieval import search

EVAL_DIR = Path(__file__).parent
QUESTIONS_FILE = EVAL_DIR / "questions.yaml"
CONTEXTS_FILE = EVAL_DIR / "contexts.json"


def main() -> None:
    questions = yaml.safe_load(QUESTIONS_FILE.read_text(encoding="utf-8"))["questions"]

    print(f"Загрузка модели эмбеддингов {settings.embedding_model}...")
    provider = BGEEmbeddingProvider()

    contexts = []
    with SessionLocal() as session:
        for item in questions:
            result = search(session, provider, item["q"])
            contexts.append(
                {
                    "q": item["q"],
                    "kind": item["kind"],
                    "expected_doc": item.get("expected_doc"),
                    "must_contain": item.get("must_contain", []),
                    "passed": result.passed,
                    "best_similarity": round(result.best_similarity, 4),
                    "hits": [asdict(hit) for hit in result.relevant],
                }
            )

    payload = {
        "embedding_model": settings.embedding_model,
        "threshold": settings.similarity_threshold,
        "top_k": settings.top_k,
        "contexts": contexts,
    }
    CONTEXTS_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    passed = sum(1 for c in contexts if c["passed"])
    print(
        f"\nСохранено: {CONTEXTS_FILE}\n"
        f"  вопросов: {len(contexts)}, прошли порог: {passed}, "
        f"отказ по порогу: {len(contexts) - passed}\n"
        f"  порог: {settings.similarity_threshold}, top_k: {settings.top_k}"
    )


if __name__ == "__main__":
    main()
