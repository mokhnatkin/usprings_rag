"""Калибровка порога коллекции: прогон golden-набора, распределения, рекомендация.

Переиспользует механику `eval/run_eval.py` как библиотеку: те же вопросы
(`eval/questions.yaml`, путь - `EVAL_QUESTIONS_FILE`), тот же `retrieval.search`,
та же решающая величина - лучшее сходство запроса. Порог при прогоне не
применяется: собираются сырые сходства, по которым порог выбирается.

Правило рекомендации - формализация решений MVP0 (open-questions.md, 2026-07-14):
`порог = min(relevant) - 0.03, округлённый вниз до сотых`. Запас под минимумом
релевантных защищает от ложных отказов (они хуже ложных пропусков); негативы выше
порога рекомендацию не двигают - околодоменные вопросы проходят порог сознательно,
их закрывает промпт. Правило воспроизводит оба принятых порога: erp 0.6155 -> 0.58,
zup 0.5857 -> 0.55.

Прогон - фоновая задача со статусом (реестр в памяти): ~20 вопросов x эмбеддинг
занимают десятки секунд, HTTP-запрос столько ждать не должен. Реестр не переживает
рестарт - это отчёт, а не данные; повторный запуск дешёв.
"""

import logging
import threading
import time
from math import floor
from pathlib import Path

import yaml
from sqlalchemy.orm import Session

from ..collection import Collection
from ..config import settings
from ..db import SessionLocal
from ..embeddings import EmbeddingProvider
from ..retrieval import search

logger = logging.getLogger(__name__)

MARGIN = 0.03  # запас под минимумом релевантных (правило MVP0)


def load_questions(code: str) -> list[dict]:
    """Вопросы golden-набора коллекции. Пустой список - набора для неё нет."""
    path = Path(settings.eval_questions_file)
    if not path.is_file():
        raise FileNotFoundError(f"файл golden-набора не найден: {path}")
    questions = yaml.safe_load(path.read_text(encoding="utf-8"))["questions"]
    return [q for q in questions if q["collection"] == code]


def run_calibration(
    session: Session,
    provider: EmbeddingProvider,
    collection: Collection,
    questions: list[dict],
) -> dict:
    """Прогнать вопросы через поиск и собрать отчёт с рекомендованным порогом."""
    rows = []
    for item in questions:
        result = search(
            session, provider, item["q"], collection, top_k=settings.top_k
        )
        found = [hit.title for hit in result.hits]
        expected = item.get("expected_doc")
        rank = found.index(expected) + 1 if expected in found else None
        rows.append(
            {
                "q": item["q"],
                "kind": item["kind"],
                "expected": expected,
                "best": round(result.best_similarity, 4),
                "rank": rank,
                "top_doc": found[0] if found else None,
            }
        )
    return build_report(collection, rows)


def build_report(collection: Collection, rows: list[dict]) -> dict:
    """Отчёт по готовым строкам прогона: recall, распределения, рекомендация.

    Чистая функция (без БД и модели) - правило рекомендации проверяется юнит-тестами
    на числах фактических калибровок MVP0.
    """
    relevant = [r for r in rows if r["kind"] in ("covered", "paraphrased")]
    irrelevant = [r for r in rows if r["kind"] == "irrelevant"]

    recall = {}
    for kind in ("covered", "paraphrased"):
        group = [r for r in relevant if r["kind"] == kind]
        if group:
            recall[kind] = {
                "at_1": sum(1 for r in group if r["rank"] == 1),
                "at_k": sum(1 for r in group if r["rank"] is not None),
                "total": len(group),
            }

    rel = sorted(r["best"] for r in relevant)
    irr = sorted(r["best"] for r in irrelevant)
    gap = round(rel[0] - irr[-1], 4) if rel and irr else None

    recommended, rationale = _recommend(recall, rel, irr)

    return {
        "collection": collection.code,
        "title": collection.title,
        "current_threshold": collection.threshold,
        "top_k": settings.top_k,
        "questions": rows,
        "recall": recall,
        "relevant": {"count": len(rel), "min": rel[0] if rel else None,
                     "max": rel[-1] if rel else None, "values": rel},
        "irrelevant": {"count": len(irr), "min": irr[0] if irr else None,
                       "max": irr[-1] if irr else None, "values": irr},
        "gap": gap,
        "recommended": recommended,
        "rationale": rationale,
    }


def _recommend(
    recall: dict, rel: list[float], irr: list[float]
) -> tuple[float | None, list[str]]:
    """Рекомендованный порог и его обоснование (по-русски, для экрана калибровки)."""
    rationale: list[str] = []

    if not rel:
        return None, ["В наборе нет покрытых вопросов - рекомендовать порог не по чему."]

    covered = recall.get("covered")
    if covered and covered["at_1"] < covered["total"]:
        rationale.append(
            f"Внимание: recall@1 по covered = {covered['at_1']}/{covered['total']} "
            "(не 100%) - сначала разбираться с поиском, порог этого не чинит."
        )

    min_rel = rel[0]
    value = floor((min_rel - MARGIN) * 100) / 100
    if value <= 0:
        return None, rationale + [
            f"Минимум релевантных ({min_rel:.4f}) слишком низкий - "
            "рекомендация не имеет смысла, разбираться с поиском."
        ]

    rationale.append(
        f"Порог = min(relevant) - {MARGIN:.2f}, вниз до сотых: "
        f"{min_rel:.4f} - {MARGIN:.2f} -> {value:.2f}. Запас под минимумом "
        "релевантных: ложный отказ хуже ложного пропуска."
    )

    if not irr:
        rationale.append(
            "В наборе нет негативов - нижняя граница порога не проверена."
        )
    else:
        above = [s for s in irr if s >= value]
        if above:
            rationale.append(
                f"Негативов выше порога: {len(above)} (max {irr[-1]:.4f}) - "
                "проходят сознательно, околодоменные вопросы закрывает промпт "
                "(см. open-questions.md)."
            )
        else:
            rationale.append(
                f"Порог выше максимума негативов ({irr[-1]:.4f}) - разделение чистое."
            )

    return value, rationale


# --- Фоновый запуск со статусом (реестр в памяти) ---

_jobs: dict[str, dict] = {}  # код коллекции -> {status, result, error, ...}
_lock = threading.Lock()


def start_calibration(
    provider: EmbeddingProvider, collection: Collection, questions: list[dict]
) -> bool:
    """Запустить калибровку в фоне. False - другая калибровка ещё выполняется
    (одна за раз: прогон делит модель эмбеддингов с ответами пользователям)."""
    with _lock:
        if any(job["status"] == "running" for job in _jobs.values()):
            return False
        _jobs[collection.code] = {
            "status": "running",
            "result": None,
            "error": None,
            "elapsed_seconds": None,
        }
    threading.Thread(
        target=_run_job,
        args=(provider, collection, questions),
        name=f"calibration-{collection.code}",
        daemon=True,
    ).start()
    logger.info("Калибровка %s запущена (%d вопросов)", collection.code, len(questions))
    return True


def get_job(code: str) -> dict | None:
    """Статус последнего запуска по коллекции. None - не запускалась."""
    return _jobs.get(code)


def _run_job(
    provider: EmbeddingProvider, collection: Collection, questions: list[dict]
) -> None:
    started = time.perf_counter()
    job = _jobs[collection.code]
    try:
        with SessionLocal() as session:
            job["result"] = run_calibration(session, provider, collection, questions)
        job["status"] = "done"
        logger.info(
            "Калибровка %s: рекомендовано %s (%.1fс)",
            collection.code,
            job["result"]["recommended"],
            time.perf_counter() - started,
        )
    except Exception as exc:
        job["status"] = "error"
        job["error"] = str(exc)
        logger.exception("Калибровка %s упала", collection.code)
    finally:
        job["elapsed_seconds"] = round(time.perf_counter() - started, 1)
