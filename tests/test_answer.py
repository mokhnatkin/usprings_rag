"""Проверки сборки источников и промпта (без БД и без вызовов LLM)."""

from usprings_rag.answer import _collect_sources
from usprings_rag.llm import build_user_prompt
from usprings_rag.retrieval import SearchHit


def make_hit(document_id: int, title: str, page_from: int, page_to: int) -> SearchHit:
    return SearchHit(
        chunk_id=document_id * 10 + page_from,
        document_id=document_id,
        title=title,
        source_path=f"docs/manuals/IT_1C/{title}.pdf",
        page_from=page_from,
        page_to=page_to,
        content=f"текст {title}",
        similarity=0.7,
        above_threshold=True,
    )


def test_sources_deduplicate_by_document():
    hits = [
        make_hit(1, "Отгрузка", 2, 3),
        make_hit(1, "Отгрузка", 5, 6),
        make_hit(2, "Договор", 1, 1),
    ]
    sources = _collect_sources(hits)
    assert [source.document_id for source in sources] == [1, 2]
    assert sources[0].pages == "стр.2-3, стр.5-6"
    assert sources[1].pages == "стр.1"


def test_sources_keep_similarity_order():
    sources = _collect_sources([make_hit(3, "Б", 1, 1), make_hit(1, "А", 1, 1)])
    assert [source.title for source in sources] == ["Б", "А"]


def test_user_prompt_numbers_fragments_with_metadata():
    prompt = build_user_prompt(
        "Как отгрузить?", [make_hit(1, "Отгрузка", 2, 3), make_hit(2, "Договор", 1, 1)]
    )
    assert "[Фрагмент 1] Документ: Отгрузка, стр.2-3" in prompt
    assert "[Фрагмент 2] Документ: Договор, стр.1" in prompt
    assert "Вопрос: Как отгрузить?" in prompt
