"""Точечные проверки чанкера: упаковка абзацев, перекрытие, деление длинных,
привязка к страницам."""

from usprings_rag.ingest.chunker import CHARS_PER_TOKEN, chunk_pages, estimate_tokens
from usprings_rag.ingest.pdf import Page


def _para(tokens: int, marker: str) -> str:
    """Абзац примерно на заданное число оценочных токенов, с меткой в начале."""
    return marker + "a" * (tokens * CHARS_PER_TOKEN)


def test_short_document_one_chunk():
    pages = [Page(1, "Первый абзац.\n\nВторой абзац.")]
    chunks = chunk_pages(pages, max_tokens=512, overlap=64)
    assert len(chunks) == 1
    assert chunks[0].page_from == 1 and chunks[0].page_to == 1
    assert chunks[0].chunk_index == 0


def test_packs_until_limit_then_splits():
    text = f"{_para(200, 'A. ')}\n\n{_para(200, 'B. ')}\n\n{_para(200, 'C. ')}"
    chunks = chunk_pages([Page(1, text)], max_tokens=512, overlap=0)
    # 3 абзаца по ~200 токенов: A+B в первый чанк, C - во второй (600 > 512).
    assert len(chunks) == 2
    assert "A." in chunks[0].content and "B." in chunks[0].content
    assert "C." in chunks[1].content
    assert [c.chunk_index for c in chunks] == [0, 1]


def test_overlap_carries_tail_into_next_chunk():
    text = f"{_para(200, 'A. ')}\n\n{_para(200, 'B. ')}\n\n{_para(200, 'C. ')}"
    chunks = chunk_pages([Page(1, text)], max_tokens=512, overlap=64)
    # Второй чанк начинается с хвоста первого - иначе шаг инструкции рвётся.
    assert len(chunks) == 2
    assert chunks[1].content[:50] in chunks[0].content


def test_overlap_never_exceeds_budget_on_huge_paragraphs():
    """Абзац много больше overlap не должен переноситься целиком (баг до 2026-07-14).

    В PDF из ИТС абзацы идут на сотни токенов; прежний чанкер переносил такой
    абзац целиком, и перекрытие доходило до 460 токенов вместо 64 - половина
    корпуса дублировалась.
    """
    sentences = " ".join(f"Предложение номер {i} про учёт." for i in range(300))
    chunks = chunk_pages([Page(1, sentences)], max_tokens=512, overlap=64)

    assert len(chunks) > 1
    for previous, following in zip(chunks, chunks[1:]):
        shared = _shared_text(previous.content, following.content)
        assert estimate_tokens(shared) <= 64 + 5  # +допуск на округление оценки


def test_chunk_never_exceeds_max_tokens():
    """Потолок обязан соблюдаться: до фиксa средний чанк был 614-644 при потолке 512."""
    text = "\n\n".join(
        " ".join(f"Абзац {n}, предложение {i}." for i in range(60)) for n in range(10)
    )
    for chunk in chunk_pages([Page(1, text)], max_tokens=512, overlap=64):
        assert estimate_tokens(chunk.content) <= 512


def _shared_text(previous: str, following: str) -> str:
    """Самый длинный префикс following, являющийся суффиксом previous."""
    limit = min(len(previous), len(following))
    for size in range(limit, 0, -1):
        if previous.endswith(following[:size]):
            return following[:size]
    return ""


def test_oversized_paragraph_is_split():
    # Один абзац без пустых строк, крупнее потолка - должен делиться по предложениям.
    big = " ".join(f"Предложение номер {i}." for i in range(200))
    chunks = chunk_pages([Page(1, big)], max_tokens=100, overlap=0)
    assert len(chunks) > 1


def test_page_range_spans_pages():
    pages = [Page(1, "Раз."), Page(2, "Два."), Page(3, "Три.")]
    chunks = chunk_pages(pages, max_tokens=512, overlap=0)
    assert len(chunks) == 1
    assert chunks[0].page_from == 1 and chunks[0].page_to == 3
