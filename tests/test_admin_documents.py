"""Админка документов: доступ по ролям, список по коллекциям, архивация, загрузка.

Нужна БД. Без неё - skip. TestClient без lifespan: воркер не запущен, поэтому
загруженная задача остаётся `queued` - проверяем постановку, не саму индексацию.
"""

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from fastapi.testclient import TestClient

from usprings_rag.api import app
from usprings_rag.collection import get_collection
from usprings_rag.db import SessionLocal
from usprings_rag.models import (
    CollectionRow,
    Document,
    IndexJob,
    Role,
    User,
    UserCollectionAccess,
)
from usprings_rag.security import hash_password

ADMIN = "docadmin"
PLAIN = "docplain"
PW = "doc-pass-123"


def _skip_if_no_db():
    try:
        db = SessionLocal()
        db.execute(text("select 1"))
        db.close()
    except OperationalError:
        pytest.skip("БД недоступна")


@pytest.fixture
def ctx():
    _skip_if_no_db()
    db = SessionLocal()
    for login in (ADMIN, PLAIN):
        db.execute(text("delete from users where login = :l"), {"l": login})
    db.commit()

    admin = User(
        login=ADMIN, full_name="Админ", password_hash=hash_password(PW),
        role=Role.COLLECTION_ADMIN,
    )
    plain = User(
        login=PLAIN, full_name="Юзер", password_hash=hash_password(PW), role=Role.USER
    )
    db.add_all([admin, plain])
    db.commit()
    erp_id = db.scalar(select(CollectionRow.id).where(CollectionRow.code == "erp"))
    db.add(UserCollectionAccess(user_id=admin.id, collection_id=erp_id))
    db.commit()

    erp_doc = Document(
        collection="erp", title="erp-doc", source_path="its_erp/erp-doc.pdf",
        content_hash="h1",
    )
    zup_doc = Document(
        collection="zup", title="zup-doc", source_path="its_zup/zup-doc.pdf",
        content_hash="h2",
    )
    db.add_all([erp_doc, zup_doc])
    db.commit()
    ids = {"admin_id": admin.id, "erp_doc": erp_doc.id, "zup_doc": zup_doc.id}
    yield ids

    db.execute(text("delete from index_job where created_by = :u"), {"u": admin.id})
    db.execute(
        text("delete from documents where source_path in "
             "('its_erp/erp-doc.pdf', 'its_zup/zup-doc.pdf', 'its_erp/upload-test.pdf')")
    )
    for login in (ADMIN, PLAIN):
        db.execute(text("delete from users where login = :l"), {"l": login})
    db.commit()
    db.close()


def _login(client, login):
    r = client.post(
        "/login", data={"login": login, "password": PW}, follow_redirects=False
    )
    assert r.status_code == 303


def test_plain_user_denied(ctx):
    client = TestClient(app)
    _login(client, PLAIN)
    assert client.get("/admin/documents").status_code == 403
    assert client.get("/api/admin/documents").status_code == 403


def test_admin_sees_only_own_collections(ctx):
    client = TestClient(app)
    _login(client, ADMIN)
    r = client.get("/api/admin/documents")
    assert r.status_code == 200
    titles = {d["title"] for d in r.json()}
    assert "erp-doc" in titles
    assert "zup-doc" not in titles  # zup admin не администрирует


def test_archive_and_unarchive_own_document(ctx):
    client = TestClient(app)
    _login(client, ADMIN)
    doc_id = ctx["erp_doc"]

    assert client.post(f"/api/admin/documents/{doc_id}/archive").status_code == 200
    with SessionLocal() as s:
        assert s.get(Document, doc_id).archived_at is not None

    assert client.post(f"/api/admin/documents/{doc_id}/unarchive").status_code == 200
    with SessionLocal() as s:
        assert s.get(Document, doc_id).archived_at is None


def test_archive_foreign_collection_forbidden(ctx):
    client = TestClient(app)
    _login(client, ADMIN)
    r = client.post(f"/api/admin/documents/{ctx['zup_doc']}/archive")
    assert r.status_code == 403
    with SessionLocal() as s:
        assert s.get(Document, ctx["zup_doc"]).archived_at is None


def test_upload_rejects_non_pdf(ctx):
    client = TestClient(app)
    _login(client, ADMIN)
    r = client.post(
        "/api/admin/documents/upload",
        data={"collection": "erp"},
        files={"file": ("note.txt", b"hello", "text/plain")},
    )
    assert r.status_code == 422


def test_upload_creates_queued_job(ctx, monkeypatch, tmp_path):
    from usprings_rag.config import settings

    monkeypatch.setattr(settings, "manuals_dir", str(tmp_path))
    client = TestClient(app)
    _login(client, ADMIN)
    r = client.post(
        "/api/admin/documents/upload",
        data={"collection": "erp"},
        files={"file": ("upload-test.pdf", b"%PDF-1.4 stub", "application/pdf")},
    )
    assert r.status_code == 200
    job_id = r.json()["job_id"]
    erp_id = get_collection("erp").id
    with SessionLocal() as s:
        job = s.get(IndexJob, job_id)
        assert job.status == "queued"
        assert job.collection_id == erp_id
        assert job.source_path == "its_erp/upload-test.pdf"
    assert (tmp_path / "its_erp" / "upload-test.pdf").is_file()


def test_upload_foreign_collection_forbidden(ctx, monkeypatch, tmp_path):
    from usprings_rag.config import settings

    monkeypatch.setattr(settings, "manuals_dir", str(tmp_path))
    client = TestClient(app)
    _login(client, ADMIN)
    r = client.post(
        "/api/admin/documents/upload",
        data={"collection": "zup"},
        files={"file": ("x.pdf", b"%PDF", "application/pdf")},
    )
    assert r.status_code == 403
