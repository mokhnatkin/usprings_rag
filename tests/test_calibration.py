"""Калибровка порога: правило рекомендации и прогон по синтетической коллекции.

Правило проверяем на числах фактических калибровок MVP0 (open-questions.md,
2026-07-14) - формализация обязана воспроизводить оба принятых порога.

Интеграционный прогон - на одноразовой секции chunks с рукотворными векторами
(известные косинусы). DDL в Postgres транзакционен: секция и данные создаются без
коммита и исчезают при rollback. Нужна БД. Без неё - skip.
"""

import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from usprings_rag import api as api_module
from usprings_rag.admin import calibration as calibration_module
from usprings_rag.admin.calibration import build_report, run_calibration
from usprings_rag.api import app
from usprings_rag.collection import Collection
from usprings_rag.db import SessionLocal
from usprings_rag.models import Chunk, Document, Role, User
from usprings_rag.security import hash_password

ERP = Collection(code="erp", title="1С:ERP", folder="its_erp", threshold=0.58)
ZUP = Collection(code="zup", title="1С:ЗУП", folder="its_zup", threshold=0.55)


def row(kind: str, best: float, rank: int | None = 1) -> dict:
    return {
        "q": f"вопрос {kind} {best}",
        "kind": kind,
        "expected": "док" if kind != "irrelevant" else None,
        "best": best,
        "rank": rank if kind != "irrelevant" else None,
        "top_doc": "док",
    }


# --- Правило рекомендации (чистая логика) ---


def test_rule_reproduces_mvp0_erp():
    """Числа калибровки ERP 2026-07-14: min(rel)=0.6155, негативы до 0.6094 -> 0.58."""
    rows = [
        row("covered", 0.6934), row("covered", 0.6155), row("covered", 0.72),
        row("paraphrased", 0.6300),
        row("irrelevant", 0.29), row("irrelevant", 0.605), row("irrelevant", 0.6094),
    ]
    report = build_report(ERP, rows)
    assert report["recommended"] == 0.58
    rationale = " ".join(report["rationale"])
    # околодоменные негативы (0.605/0.6094) выше порога - зафиксировано, не блокирует
    assert "проходят сознательно" in rationale
    assert "Внимание" not in rationale  # recall@1 полный - предупреждения нет


def test_rule_reproduces_mvp0_zup():
    """Числа калибровки ЗУП: min(rel)=0.5857, негатив-лицензия 0.6324 -> 0.55."""
    rows = [
        row("covered", 0.5857), row("covered", 0.68),
        row("irrelevant", 0.5166), row("irrelevant", 0.6324),
    ]
    report = build_report(ZUP, rows)
    assert report["recommended"] == 0.55
    assert report["gap"] == pytest.approx(0.5857 - 0.6324, abs=1e-4)  # пересечение
    assert "проходят сознательно" in " ".join(report["rationale"])


def test_clean_separation_noted():
    rows = [row("covered", 0.80), row("irrelevant", 0.40)]
    report = build_report(ERP, rows)
    assert report["recommended"] == 0.77
    assert "разделение чистое" in " ".join(report["rationale"])


def test_covered_miss_warns_first():
    """recall@1 < 100% по covered - предупреждение «чинить поиск» (maintenance.md)."""
    rows = [
        row("covered", 0.75), row("covered", 0.70, rank=2),
        row("irrelevant", 0.40),
    ]
    report = build_report(ERP, rows)
    assert report["recall"]["covered"]["at_1"] == 1
    assert report["recommended"] == 0.67  # рекомендация всё равно считается
    assert "recall@1" in report["rationale"][0]


def test_no_relevant_questions_no_recommendation():
    report = build_report(ERP, [row("irrelevant", 0.4)])
    assert report["recommended"] is None


# --- Интеграционный прогон (БД, известные косинусы) ---

DIM = 1024


def vec(*components: tuple[int, float]) -> list[float]:
    v = [0.0] * DIM
    for axis, value in components:
        v[axis] = value
    return v


DOC_A = vec((0, 1.0))
DOC_B = vec((1, 1.0))
Q_COVERED = "как настроить А"          # = вектор А, сходство 1.0
Q_PARA = "а если по-простому про А"    # cos к А ~0.806
Q_IRR = "какая погода в Караганде"     # cos к А ~0.423

QUERY_VECTORS = {
    Q_COVERED: DOC_A,
    Q_PARA: vec((0, 0.806), (2, 0.5918)),
    Q_IRR: vec((0, 0.42), (3, 0.9)),
}


class FakeProvider:
    def embed_query(self, text: str) -> list[float]:
        return QUERY_VECTORS[text]

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError


@pytest.fixture
def session():
    try:
        db = SessionLocal()
        db.execute(text("select 1"))
    except OperationalError:
        pytest.skip("БД недоступна")
    try:
        yield db
    finally:
        db.rollback()  # откатывает и DDL секции, и данные
        db.close()


def _add_doc(session, code: str, title: str, embedding: list[float]) -> None:
    session.add(
        Document(
            collection=code, title=title, source_path=f"x/{title}.pdf",
            content_hash=f"h-{title}",
            chunks=[Chunk(collection=code, chunk_index=0, page_from=1, page_to=1,
                          content=title, embedding=embedding)],
        )
    )
    session.flush()


def test_run_calibration_on_synthetic_partition(session):
    code = "caltst"
    collection = Collection(code=code, title="Тест", folder="x", threshold=0.5)
    session.execute(
        text(f"CREATE TABLE chunks_{code} PARTITION OF chunks FOR VALUES IN ('{code}')")
    )
    _add_doc(session, code, "докА", DOC_A)
    _add_doc(session, code, "докБ", DOC_B)

    questions = [
        {"q": Q_COVERED, "collection": code, "kind": "covered", "expected_doc": "докА"},
        {"q": Q_PARA, "collection": code, "kind": "paraphrased", "expected_doc": "докА"},
        {"q": Q_IRR, "collection": code, "kind": "irrelevant"},
    ]
    report = run_calibration(session, FakeProvider(), collection, questions)

    assert report["recall"]["covered"] == {"at_1": 1, "at_k": 1, "total": 1}
    assert report["recall"]["paraphrased"] == {"at_1": 1, "at_k": 1, "total": 1}
    assert report["relevant"]["max"] == pytest.approx(1.0, abs=1e-3)
    assert report["relevant"]["min"] == pytest.approx(0.806, abs=1e-3)
    assert report["irrelevant"]["max"] == pytest.approx(0.4229, abs=1e-3)
    assert report["recommended"] == 0.77  # floor((0.806 - 0.03) * 100) / 100
    assert report["gap"] > 0.3
    assert "разделение чистое" in " ".join(report["rationale"])


# --- Эндпоинты (super-admin, фоновый запуск со статусом) ---

SUPER = "cal9super"
PLAIN = "cal9plain"
PW = "cal9-pass-123"


@pytest.fixture
def users_ctx():
    try:
        db = SessionLocal()
        db.execute(text("select 1"))
    except OperationalError:
        pytest.skip("БД недоступна")
    db.execute(
        text("delete from users where login in (:a, :b)"), {"a": SUPER, "b": PLAIN}
    )
    db.commit()
    db.add_all([
        User(login=SUPER, full_name="s", password_hash=hash_password(PW),
             role=Role.SUPER_ADMIN),
        User(login=PLAIN, full_name="p", password_hash=hash_password(PW),
             role=Role.USER),
    ])
    db.commit()
    calibration_module._jobs.clear()
    yield
    calibration_module._jobs.clear()
    api_module.resources.pop("provider", None)
    db.execute(
        text("delete from users where login in (:a, :b)"), {"a": SUPER, "b": PLAIN}
    )
    db.commit()
    db.close()


def _login(client, login):
    r = client.post(
        "/login", data={"login": login, "password": PW}, follow_redirects=False
    )
    assert r.status_code == 303


def _questions_file(tmp_path, monkeypatch, codes: list[str]):
    """Подменить golden-набор временным yaml с вопросами для указанных коллекций."""
    from usprings_rag.config import settings

    content = "questions:\n" + "".join(
        f'  - q: "вопрос {c}"\n    collection: {c}\n    kind: covered\n'
        f'    expected_doc: "док"\n'
        for c in codes
    )
    path = tmp_path / "questions.yaml"
    path.write_text(content, encoding="utf-8")
    monkeypatch.setattr(settings, "eval_questions_file", str(path))


def test_non_super_denied(users_ctx):
    client = TestClient(app)
    _login(client, PLAIN)
    page = client.get("/admin/calibration", follow_redirects=False)
    assert page.status_code == 303 and "forbidden" in page.headers["location"]
    assert client.post("/api/admin/calibration/erp").status_code == 403
    assert client.get("/api/admin/calibration/erp").status_code == 403


def test_unknown_collection_422(users_ctx):
    client = TestClient(app)
    _login(client, SUPER)
    assert client.post("/api/admin/calibration/nope").status_code == 422


def test_no_golden_set_422(users_ctx, tmp_path, monkeypatch):
    _questions_file(tmp_path, monkeypatch, ["zup"])  # для erp вопросов нет
    client = TestClient(app)
    _login(client, SUPER)
    r = client.post("/api/admin/calibration/erp")
    assert r.status_code == 422
    assert "golden" in r.json()["detail"]


def test_busy_409(users_ctx, tmp_path, monkeypatch):
    _questions_file(tmp_path, monkeypatch, ["erp"])
    api_module.resources["provider"] = object()
    calibration_module._jobs["zup"] = {"status": "running"}
    client = TestClient(app)
    _login(client, SUPER)
    assert client.post("/api/admin/calibration/erp").status_code == 409


def test_run_flow_and_status(users_ctx, tmp_path, monkeypatch):
    """POST -> running -> done с результатом; GET без прогона -> 404."""
    _questions_file(tmp_path, monkeypatch, ["erp"])
    api_module.resources["provider"] = object()
    canned = {"recommended": 0.58, "rationale": ["ok"], "collection": "erp"}
    monkeypatch.setattr(
        calibration_module, "run_calibration", lambda *args: canned
    )

    client = TestClient(app)
    _login(client, SUPER)
    assert client.get("/api/admin/calibration/zup").status_code == 404

    r = client.post("/api/admin/calibration/erp")
    assert r.status_code == 200
    assert r.json() == {"status": "running", "questions": 1}

    deadline = time.time() + 5
    while time.time() < deadline:
        job = client.get("/api/admin/calibration/erp").json()
        if job["status"] != "running":
            break
        time.sleep(0.1)
    assert job["status"] == "done"
    assert job["result"] == canned
    assert job["elapsed_seconds"] is not None
