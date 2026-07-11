"""Разбиение текста документа на чанки с привязкой к страницам.

Стратегия (см. mvp-dev-plan.md 4.2): документы короткие и структурные, поэтому
работаем на уровне абзацев - жадно набираем абзацы в чанк, пока не упрёмся в
целевой размер, затем начинаем новый чанк с небольшим перекрытием (чтобы не рвать
пошаговую инструкцию посередине). Каждый чанк несёт номера страниц и порядковый
индекс - на них строится цитирование.

Размер меряем оценкой токенов: на MVP это грубая эвристика (символы на токен),
достаточная для контроля размера чанка. Точный токенизатор BGE-m3 подключим при
необходимости на калибровке.
"""

import re
from dataclasses import dataclass

from .pdf import Page

# Русский текст в токенизаторе BGE-m3 (XLM-RoBERTa) - порядка 3 символов на токен.
# Оценка для контроля размера чанка, не точный подсчёт.
CHARS_PER_TOKEN = 3

# Граница предложения: точка/воскл./вопр./двоеточие + пробел.
_SENT_BOUNDARY = re.compile(r"(?<=[.!?:])\s+")


@dataclass
class Chunk:
    """Готовый чанк: порядковый индекс, диапазон страниц и текст."""

    chunk_index: int
    page_from: int
    page_to: int
    content: str


@dataclass
class _Para:
    """Абзац с номером страницы, на которой он находится."""

    page: int
    text: str


def estimate_tokens(text: str) -> int:
    """Грубая оценка числа токенов по длине текста."""
    return len(text) // CHARS_PER_TOKEN


def _hard_split(text: str, max_tokens: int) -> list[str]:
    """Крайний случай: резать текст окном по символам (предложение длиннее потолка)."""
    window = max_tokens * CHARS_PER_TOKEN
    return [text[i : i + window] for i in range(0, len(text), window)]


def _split_long(text: str, max_tokens: int) -> list[str]:
    """Разбить абзац крупнее max_tokens на части по границам предложений."""
    if estimate_tokens(text) <= max_tokens:
        return [text]
    parts: list[str] = []
    current: list[str] = []
    current_tokens = 0
    for sent in _SENT_BOUNDARY.split(text):
        if estimate_tokens(sent) > max_tokens:
            if current:
                parts.append(" ".join(current))
                current, current_tokens = [], 0
            parts.extend(_hard_split(sent, max_tokens))
            continue
        if current and current_tokens + estimate_tokens(sent) > max_tokens:
            parts.append(" ".join(current))
            current, current_tokens = [], 0
        current.append(sent)
        current_tokens += estimate_tokens(sent)
    if current:
        parts.append(" ".join(current))
    return parts


def _split_paragraphs(pages: list[Page], max_tokens: int) -> list[_Para]:
    """Разбить страницы на абзацы (пустая строка - граница абзаца).

    Абзац крупнее max_tokens дополнительно делим по предложениям, чтобы ни один
    исходный блок не давал чанк заведомо больше потолка.
    """
    paras: list[_Para] = []

    def flush(page: int, buffer: list[str]) -> None:
        if not buffer:
            return
        for part in _split_long(" ".join(buffer), max_tokens):
            paras.append(_Para(page, part))

    for page in pages:
        buffer: list[str] = []
        for line in page.text.splitlines():
            if line.strip():
                buffer.append(line.strip())
            elif buffer:
                flush(page.number, buffer)
                buffer = []
        flush(page.number, buffer)
    return paras


def _emit(chunk_index: int, paras: list[_Para]) -> Chunk:
    """Собрать чанк из накопленных абзацев."""
    return Chunk(
        chunk_index=chunk_index,
        page_from=min(p.page for p in paras),
        page_to=max(p.page for p in paras),
        content="\n\n".join(p.text for p in paras),
    )


def chunk_pages(pages: list[Page], max_tokens: int, overlap: int) -> list[Chunk]:
    """Разбить документ на чанки по абзацам с перекрытием.

    max_tokens - целевой потолок размера чанка; overlap - сколько токенов
    хвоста предыдущего чанка переносим в начало следующего для связности.
    """
    paras = _split_paragraphs(pages, max_tokens)
    chunks: list[Chunk] = []
    current: list[_Para] = []
    current_tokens = 0

    for para in paras:
        para_tokens = estimate_tokens(para.text)
        if current and current_tokens + para_tokens > max_tokens:
            chunks.append(_emit(len(chunks), current))
            current, current_tokens = _overlap_tail(current, overlap)
        current.append(para)
        current_tokens += para_tokens

    if current:
        chunks.append(_emit(len(chunks), current))
    return chunks


def _overlap_tail(paras: list[_Para], overlap: int) -> tuple[list[_Para], int]:
    """Взять хвостовые абзацы предыдущего чанка на ~overlap токенов."""
    if overlap <= 0:
        return [], 0
    tail: list[_Para] = []
    tokens = 0
    for para in reversed(paras):
        para_tokens = estimate_tokens(para.text)
        if tokens + para_tokens > overlap and tail:
            break
        tail.insert(0, para)
        tokens += para_tokens
    return tail, tokens
