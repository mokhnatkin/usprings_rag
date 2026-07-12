"""Клиент LLM через OpenRouter (OpenAI SDK + base_url): промпт, вызов, разбор.

Промпт строится из найденных чанков: LLM отвечает строго по ним и честно
признаёт отсутствие информации. Источники в ответе собираются из метаданных
чанков (см. answer.py), а не выдумываются моделью.
"""

import logging
from dataclasses import dataclass

from openai import OpenAI

from .config import settings
from .retrieval import SearchHit

logger = logging.getLogger(__name__)

NO_ANSWER_MARKER = "НЕТ_ОТВЕТА"

SYSTEM_PROMPT = f"""\
Ты - ассистент по внутренним инструкциям завода (1С ERP и смежные ИТ-процессы).

Правила:
- Отвечай СТРОГО по предоставленным фрагментам инструкций. Не добавляй знания
  извне и ничего не домысливай.
- Если во фрагментах нет ответа на вопрос - ответь РОВНО одним словом:
  {NO_ANSWER_MARKER}
  Без пояснений и без попыток ответить по общим знаниям о 1С.
- Отвечай кратко и по существу, пошагово, если инструкция описывает шаги.
- Язык ответа - русский.
- Не перечисляй источники в конце - их подставит система.\
"""


@dataclass
class LLMResponse:
    """Ответ модели с расходом токенов (usage пригодится для логирования)."""

    text: str
    model: str
    prompt_tokens: int
    completion_tokens: int


def build_user_prompt(question: str, hits: list[SearchHit]) -> str:
    """Вопрос + пронумерованные фрагменты с указанием документа и страниц."""
    fragments = "\n\n".join(
        f"[Фрагмент {i}] Документ: {hit.title}"
        f"{', ' + hit.pages_ref if hit.pages_ref else ''}\n{hit.content}"
        for i, hit in enumerate(hits, start=1)
    )
    return f"Фрагменты инструкций:\n\n{fragments}\n\nВопрос: {question}"


def create_client() -> OpenAI:
    """Клиент OpenRouter. Ключ проверяем здесь - понятная ошибка сразу."""
    if not settings.openrouter_api_key:
        raise RuntimeError(
            "OPENROUTER_API_KEY не задан. Укажите ключ в .env "
            "(получить: https://openrouter.ai/keys)"
        )
    return OpenAI(
        api_key=settings.openrouter_api_key,
        base_url=settings.openrouter_base_url,
    )


def generate(
    client: OpenAI, question: str, hits: list[SearchHit], model: str | None = None
) -> LLMResponse:
    """Сгенерировать ответ по вопросу и найденным фрагментам."""
    model = model or settings.openrouter_model
    completion = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(question, hits)},
        ],
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_tokens,
    )

    usage = completion.usage
    response = LLMResponse(
        text=completion.choices[0].message.content or "",
        model=completion.model,
        prompt_tokens=usage.prompt_tokens if usage else 0,
        completion_tokens=usage.completion_tokens if usage else 0,
    )
    logger.info(
        "llm model=%s prompt_tokens=%d completion_tokens=%d",
        response.model,
        response.prompt_tokens,
        response.completion_tokens,
    )
    return response
