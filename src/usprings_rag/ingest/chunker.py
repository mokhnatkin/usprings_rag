"""Разбиение текста документа на чанки с привязкой к страницам.

Стратегия (см. mvp-dev-plan.md 4.2): работаем на уровне абзацев - жадно набираем
абзацы в чанк, пока не упрёмся в целевой размер, затем начинаем новый чанк с
небольшим перекрытием (чтобы не рвать пошаговую инструкцию посередине). Каждый
чанк несёт номера страниц и порядковый индекс - на них строится цитирование.

Два инварианта, которые обязаны соблюдаться (оба нарушались до 2026-07-14, см.
журнал прогресса - перекрытие доходило до 460 токенов вместо 64, а половина
корпуса дублировалась):
  - чанк не больше `max_tokens`;
  - перекрытие не больше `overlap` токенов.

Чтобы оба выполнялись одновременно, абзацы режутся до `max_tokens - overlap`:
тогда «хвост предыдущего чанка + очередной абзац» гарантированно влезает в
потолок. Перекрытие набирается по предложениям, а не абзацами целиком - в PDF
из ИТС абзац легко тянет на сотни токенов, и «хотя бы один абзац» означал бы
перенос почти всего чанка.

Размер меряем оценкой токенов: на MVP это грубая эвристика (символы на токен),
достаточная для контроля размера чанка.
"""

import re
from dataclasses import dataclass

from .pdf import Page

# Русский текст в токенизаторе BGE-m3 (XLM-RoBERTa) - порядка 3 символов на токен.
# Оценка для контроля размера чанка, не точный подсчёт.
CHARS_PER_TOKEN = 3

# Граница предложения: точка/воскл./вопр./двоеточие + пробел.
_SENT_BOUNDARY = re.compile(r"(?<=[.!?:])\s+")

# Абзацы в чанке склеиваются "\n\n" - это 2 символа, их тоже считаем в бюджете.
_SEP_CHARS = 2


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


def _hard_split(text: str, max_chars: int) -> list[str]:
    """Крайний случай: резать текст окном по символам (предложение длиннее потолка)."""
    return [text[i : i + max_chars] for i in range(0, len(text), max_chars)]


def _split_long(text: str, max_chars: int) -> list[str]:
    """Разбить абзац длиннее max_chars на части по границам предложений.

    Считаем в символах, а не в оценочных токенах: оценка округляется вниз, и
    сумма оценок частей занижает оценку склейки - потолок чанка «уплывал» вверх.
    """
    if len(text) <= max_chars:
        return [text]
    parts: list[str] = []
    current: list[str] = []
    current_chars = 0
    for sent in _SENT_BOUNDARY.split(text):
        if len(sent) > max_chars:
            if current:
                parts.append(" ".join(current))
                current, current_chars = [], 0
            parts.extend(_hard_split(sent, max_chars))
            continue
        cost = len(sent) + (1 if current else 0)  # пробел-разделитель
        if current and current_chars + cost > max_chars:
            parts.append(" ".join(current))
            current, current_chars = [], 0
            cost = len(sent)
        current.append(sent)
        current_chars += cost
    if current:
        parts.append(" ".join(current))
    return parts


def _split_paragraphs(pages: list[Page], max_chars: int) -> list[_Para]:
    """Разбить страницы на абзацы (пустая строка - граница абзаца).

    Абзац длиннее max_chars дополнительно делим по предложениям, чтобы ни один
    исходный блок не давал чанк заведомо больше потолка.
    """
    paras: list[_Para] = []

    def flush(page: int, buffer: list[str]) -> None:
        if not buffer:
            return
        for part in _split_long(" ".join(buffer), max_chars):
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

    max_tokens - потолок размера чанка; overlap - сколько токенов хвоста
    предыдущего чанка переносим в начало следующего для связности.
    """
    overlap = max(0, min(overlap, max_tokens // 2))  # перекрытие - не полчанка
    max_chars = max_tokens * CHARS_PER_TOKEN
    overlap_chars = overlap * CHARS_PER_TOKEN
    paras = _split_paragraphs(pages, max_chars - overlap_chars)

    chunks: list[Chunk] = []
    current: list[_Para] = []
    current_chars = 0

    for para in paras:
        cost = len(para.text) + (_SEP_CHARS if current else 0)
        if current and current_chars + cost > max_chars:
            chunks.append(_emit(len(chunks), current))
            current, current_chars = _overlap_tail(current, overlap_chars)
            cost = len(para.text) + (_SEP_CHARS if current else 0)
        current.append(para)
        current_chars += cost

    if current:
        chunks.append(_emit(len(chunks), current))
    return chunks


def _tail_by_sentences(text: str, budget: int) -> str:
    """Хвост абзаца не длиннее budget символов, по границам предложений.

    Если в бюджет не влезает даже последнее предложение, режем его по словам:
    пустое перекрытие рвало бы связность, а перенос предложения целиком - это
    ровно тот перенос «сколько получится», от которого мы уходим.
    """
    picked: list[str] = []
    chars = 0
    for sentence in reversed(_SENT_BOUNDARY.split(text)):
        if not picked and len(sentence) > budget:
            words: list[str] = []
            word_chars = 0
            for word in reversed(sentence.split()):
                cost = len(word) + (1 if words else 0)
                if words and word_chars + cost > budget:
                    break
                words.insert(0, word)
                word_chars += cost
            return " ".join(words)
        cost = len(sentence) + (1 if picked else 0)
        if chars + cost > budget:
            break
        picked.insert(0, sentence)
        chars += cost
    return " ".join(picked)


def _overlap_tail(paras: list[_Para], budget: int) -> tuple[list[_Para], int]:
    """Хвост предыдущего чанка не длиннее budget символов.

    Абзац, который в бюджет не влезает, переносим не целиком, а его хвостом по
    предложениям - иначе перекрытие определялось бы размером абзаца, а не
    настройкой (до 2026-07-14 именно так и было).
    """
    if budget <= 0:
        return [], 0
    tail: list[_Para] = []
    chars = 0
    for para in reversed(paras):
        cost = len(para.text) + (_SEP_CHARS if tail else 0)
        if chars + cost <= budget:
            tail.insert(0, para)
            chars += cost
            continue
        snippet = _tail_by_sentences(para.text, budget - chars - (_SEP_CHARS if tail else 0))
        if snippet:
            tail.insert(0, _Para(para.page, snippet))
            chars += len(snippet) + (_SEP_CHARS if len(tail) > 1 else 0)
        break
    return tail, chars
