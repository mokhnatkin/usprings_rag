"""Фоновый воркер индексации: берёт задачи `index_job` и гоняет single-file ingest.

Живёт в процессе приложения (без внешнего брокера) - для пилота достаточно. Один
поток обрабатывает задачи по одной: тяжёлый ingest (модель BGE-m3 уже в памяти,
переиспользуем из lifespan) не конкурирует с ответами пользователю. Опрос очереди
интервальный; событие остановки будит поток сразу при выключении приложения.
"""

import logging
import threading
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..collection import get_collection
from ..config import settings
from ..db import SessionLocal
from ..embeddings import EmbeddingProvider
from ..ingest.pipeline import ensure_partition, ingest_file, relative_source_path
from ..models import CollectionRow, Document, IndexJob
from . import jobs

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 2.0


def process_job(session: Session, provider: EmbeddingProvider, job: IndexJob) -> None:
    """Обработать одну захваченную задачу: ingest файла в секцию коллекции.

    Успех - `done` с привязкой к документу; любая ошибка (нет файла, битый PDF) -
    `error` с текстом. Транзакцию ingest при ошибке откатывает `mark_error`.
    """
    try:
        code = session.scalar(
            select(CollectionRow.code).where(
                CollectionRow.id == job.collection_id
            )
        )
        collection = get_collection(code)
        ensure_partition(session, collection)
        path = Path(settings.manuals_dir) / job.source_path
        if not path.is_file():
            raise FileNotFoundError(f"файл не найден: {job.source_path}")
        ingest_file(session, provider, path, collection)
        rel = relative_source_path(path)
        document_id = session.scalar(
            select(Document.id).where(Document.source_path == rel)
        )
        jobs.mark_done(session, job.id, document_id)
        logger.info("index_job %d done (%s)", job.id, job.source_path)
    except Exception as exc:
        jobs.mark_error(session, job.id, str(exc))
        logger.warning("index_job %d error: %s", job.id, exc)


class IndexWorker:
    """Фоновый поток: цикл claim -> process. Останавливается через stop()."""

    def __init__(self, provider: EmbeddingProvider):
        self._provider = provider
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        with SessionLocal() as session:
            jobs.reset_stale(session)
        self._thread = threading.Thread(
            target=self._run, name="index-worker", daemon=True
        )
        self._thread.start()
        logger.info("Воркер индексации запущен")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=POLL_INTERVAL_SECONDS + 1.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            with SessionLocal() as session:
                job = jobs.claim_next(session)
                if job is not None:
                    process_job(session, self._provider, job)
            if job is None:
                self._stop.wait(POLL_INTERVAL_SECONDS)
