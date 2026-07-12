"""Стриминг: маркер NO_ANSWER не должен попасть на экран.

Проверяем удержание начала потока на фейковых зависимостях (без БД и сети):
пока текст неотличим от начала маркера, дельты наружу не идут.
"""

from usprings_rag import answer as answer_module
from usprings_rag.answer import REFUSAL_TEXT, stream_answer
from usprings_rag.retrieval import SearchHit, SearchResult


def make_result(similarity: float) -> SearchResult:
    hit = SearchHit(
        chunk_id=1,
        document_id=1,
        title="Отгрузка",
        source_path="IT_1C/Отгрузка.pdf",
        page_from=1,
        page_to=2,
        content="текст",
        similarity=similarity,
        above_threshold=similarity >= 0.53,
    )
    return SearchResult(query="в", hits=[hit], threshold=0.53)


def run_stream(monkeypatch, similarity: float, deltas: list[str]):
    monkeypatch.setattr(answer_module, "search", lambda *a, **k: make_result(similarity))
    monkeypatch.setattr(answer_module, "stream_generate", lambda *a, **k: iter(deltas))
    events = list(stream_answer(None, None, None, "вопрос"))
    text = "".join(payload for kind, payload in events if kind == "delta")
    done = next(payload for kind, payload in events if kind == "done")
    return text, done


def test_marker_never_reaches_client(monkeypatch):
    text, done = run_stream(monkeypatch, 0.55, ["NO_", "ANSWER"])
    assert text == ""
    assert done.refused
    assert done.sources == []
    assert done.text == REFUSAL_TEXT


def test_normal_answer_streams_and_keeps_sources(monkeypatch):
    text, done = run_stream(monkeypatch, 0.74, ["Для отгрузки ", "нажмите ", "кнопку."])
    assert text == "Для отгрузки нажмите кнопку."
    assert not done.refused
    assert [source.title for source in done.sources] == ["Отгрузка"]


def test_short_answer_shorter_than_marker_is_not_swallowed(monkeypatch):
    text, done = run_stream(monkeypatch, 0.74, ["Нет."])
    assert text == "Нет."
    assert not done.refused


def test_below_threshold_refuses_without_calling_llm(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("LLM не должна вызываться ниже порога")

    monkeypatch.setattr(answer_module, "stream_generate", fail)
    monkeypatch.setattr(answer_module, "search", lambda *a, **k: make_result(0.28))
    events = list(stream_answer(None, None, None, "погода"))
    assert [kind for kind, _ in events] == ["done"]
    assert events[0][1].refused
