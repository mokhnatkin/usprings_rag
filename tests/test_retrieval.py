"""Проверки логики порога и метаданных выдачи (без БД - на готовых SearchHit)."""

from usprings_rag.retrieval import SearchHit, SearchResult


def make_hit(similarity: float, threshold: float = 0.5, **kwargs) -> SearchHit:
    defaults = dict(
        chunk_id=1,
        document_id=1,
        title="Инструкция",
        source_path="its_erp/x.pdf",
        page_from=1,
        page_to=1,
        content="текст",
    )
    defaults.update(kwargs)
    return SearchHit(
        similarity=similarity,
        above_threshold=similarity >= threshold,
        **defaults,
    )


def test_passed_when_best_above_threshold():
    result = SearchResult("вопрос", [make_hit(0.74), make_hit(0.48)], threshold=0.5)
    assert result.passed
    assert result.best_similarity == 0.74


def test_below_threshold_when_best_is_low():
    result = SearchResult("вопрос", [make_hit(0.29), make_hit(0.26)], threshold=0.5)
    assert not result.passed
    assert result.relevant == []


def test_empty_result_does_not_pass():
    result = SearchResult("вопрос", [], threshold=0.5)
    assert not result.passed
    assert result.best_similarity == 0.0


def test_relevant_keeps_only_hits_above_threshold():
    result = SearchResult(
        "вопрос", [make_hit(0.74), make_hit(0.53), make_hit(0.48)], threshold=0.5
    )
    assert [round(hit.similarity, 2) for hit in result.relevant] == [0.74, 0.53]


def test_pages_ref_single_and_range():
    assert make_hit(0.7, page_from=3, page_to=3).pages_ref == "стр.3"
    assert make_hit(0.7, page_from=3, page_to=5).pages_ref == "стр.3-5"
    assert make_hit(0.7, page_from=None, page_to=None).pages_ref == ""
