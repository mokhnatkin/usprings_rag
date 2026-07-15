"""Очередь и воркер индексации: смена статусов, привязка документа, ошибки.

Оркестрацию воркера проверяем в изоляции от ingest (парсинг PDF и модель BGE-m3
подменяем): здесь важны переходы статусов и обработка ошибок, а не сам ingest.
Нужна БД. Без неё - skip.
"""

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from usprings_rag.collection import get_collection
from usprings_rag.db import SessionLocal
from usprings_rag.indexing import jobs, worker
from usprings_rag.models import Document, IndexJob, IndexJobStatus, Role, User
from usprings_rag.security import hash_password

LOGIN = "idxtest"


@pytest.fixture
def ctx():
    try:
        db = SessionLocal()
        db.execute(text("select 1"))
    except OperationalError:
        pytest.skip("БД недоступна")
    db.execute(text("delete from users where login = :l"), {"l": LOGIN})
    db.commit()
    user = User(
        login=LOGIN, full_name="Индексация", password_hash=hash_password("x"),
        role=Role.COLLECTION_ADMIN,
    )
    db.add(user)
    db.commit()
    erp = get_collection("erp")
    yield {"db": db, "user_id": user.id, "collection_id": erp.id}
    db.execute(text("delete from index_job where created_by = :u"), {"u": user.id})
    db.execute(text("delete from users where login = :l"), {"l": LOGIN})
    db.commit()
    db.close()


def test_enqueue_creates_queued(ctx):
    db = ctx["db"]
    job_id = jobs.enqueue(db, ctx["collection_id"], "its_erp/x.pdf", ctx["user_id"])
    job = db.get(IndexJob, job_id)
    assert job.status == IndexJobStatus.QUEUED
    assert job.started_at is None


def test_claim_next_flips_to_running_then_empty(ctx):
    db = ctx["db"]
    jobs.enqueue(db, ctx["collection_id"], "its_erp/x.pdf", ctx["user_id"])
    job = jobs.claim_next(db)
    assert job is not None
    assert job.status == IndexJobStatus.RUNNING
    assert job.started_at is not None
    # больше нет queued - claim пустой (single worker не берёт задачу дважды)
    assert jobs.claim_next(db) is None


def test_reset_stale_running_to_error(ctx):
    db = ctx["db"]
    jobs.enqueue(db, ctx["collection_id"], "its_erp/x.pdf", ctx["user_id"])
    job = jobs.claim_next(db)  # -> running
    count = jobs.reset_stale(db)
    assert count >= 1
    db.refresh(job)
    assert job.status == IndexJobStatus.ERROR
    assert "рестарт" in job.error


def _stub_manuals(monkeypatch, tmp_path, rel: str):
    """Временная папка инструкций с пустым файлом по относительному пути `rel`.

    Воркер проверяет наличие файла перед ingest; сам ingest здесь подменён, поэтому
    содержимое файла не важно - важно, чтобы путь существовал под папкой инструкций.
    """
    from usprings_rag.config import settings

    monkeypatch.setattr(settings, "manuals_dir", str(tmp_path))
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-stub")
    return rel


def test_process_job_success_links_document(ctx, monkeypatch, tmp_path):
    db = ctx["db"]
    rel = _stub_manuals(monkeypatch, tmp_path, "its_erp/proc-ok.pdf")
    jobs.enqueue(db, ctx["collection_id"], rel, ctx["user_id"])
    job = jobs.claim_next(db)

    # Подменяем ingest: вместо парсинга PDF создаём документ, как это сделал бы ingest.
    def fake_ingest(session, provider, path, collection):
        session.add(
            Document(
                collection=collection.code,
                title="proc-ok",
                source_path=rel,
                content_hash="h",
            )
        )
        session.commit()

    monkeypatch.setattr(worker, "ingest_file", fake_ingest)
    monkeypatch.setattr(worker, "ensure_partition", lambda s, c: None)

    try:
        worker.process_job(db, provider=None, job=job)
        db.refresh(job)
        assert job.status == IndexJobStatus.DONE
        doc_id = db.scalar(select(Document.id).where(Document.source_path == rel))
        assert job.document_id == doc_id
    finally:
        db.execute(text("delete from documents where source_path = :p"), {"p": rel})
        db.commit()


def test_process_job_error_records_message(ctx, monkeypatch, tmp_path):
    db = ctx["db"]
    rel = _stub_manuals(monkeypatch, tmp_path, "its_erp/broken.pdf")
    jobs.enqueue(db, ctx["collection_id"], rel, ctx["user_id"])
    job = jobs.claim_next(db)

    def boom(session, provider, path, collection):
        raise ValueError("битый PDF")

    monkeypatch.setattr(worker, "ingest_file", boom)
    monkeypatch.setattr(worker, "ensure_partition", lambda s, c: None)

    worker.process_job(db, provider=None, job=job)
    db.refresh(job)
    assert job.status == IndexJobStatus.ERROR
    assert "битый PDF" in job.error


def test_process_job_missing_file_errors(ctx, monkeypatch, tmp_path):
    """Файла нет на диске - задача падает в error, а не молча пропадает."""
    from usprings_rag.config import settings

    db = ctx["db"]
    monkeypatch.setattr(settings, "manuals_dir", str(tmp_path))
    jobs.enqueue(db, ctx["collection_id"], "its_erp/absent.pdf", ctx["user_id"])
    job = jobs.claim_next(db)

    worker.process_job(db, provider=None, job=job)
    db.refresh(job)
    assert job.status == IndexJobStatus.ERROR
    assert "не найден" in job.error
