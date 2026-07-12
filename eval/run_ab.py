"""A/B-сравнение LLM на готовых контекстах: параллельно, с автопроверками.

Запуск:
  uv run --no-sync python eval/build_contexts.py            # один раз (кэш поиска)
  uv run --no-sync python eval/run_ab.py МОДЕЛЬ_A МОДЕЛЬ_B  # сравнение

Пример:
  uv run --no-sync python eval/run_ab.py \\
      qwen/qwen3-next-80b-a3b-instruct google/gemma-4-31b-it

Почему так, а не «прогнать сценарий на каждой модели»: поиск уже посчитан
(`contexts.json`), поэтому здесь не грузится BGE-m3, а все вызовы LLM идут
параллельно - сравнение занимает секунды. Промпт берём из `llm.py` - единый
источник, иначе сравнивали бы не то, что работает в проде.

Проверки автоматические (глазами читать не нужно):
  - маркер NO_ANSWER там, где ответа в контексте нет, и его отсутствие там, где есть;
  - must_contain из questions.yaml - факты, без которых ответ бесполезен;
  - отсутствие markdown-разметки (портал показывает текст как есть);
  - ответ не односложный.
Сравнивать модели на бесплатных (`:free`) эндпоинтах бессмысленно - они часами
отдают 429; используйте платные (стоят доли цента, см. maintenance.md).
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from openai import AsyncOpenAI

from usprings_rag.config import settings
from usprings_rag.llm import NO_ANSWER_MARKER, SYSTEM_PROMPT, build_user_prompt
from usprings_rag.retrieval import SearchHit

EVAL_DIR = Path(__file__).parent
CONTEXTS_FILE = EVAL_DIR / "contexts.json"
REPORT_FILE = EVAL_DIR / "ab-report.md"

MARKDOWN_TOKENS = ("**", "##", "```")
MIN_ANSWER_CHARS = 25


async def ask(client: AsyncOpenAI, model: str, context: dict) -> dict:
    """Один вопрос к одной модели. Контекст готов - остаётся только вызов LLM."""
    hits = [SearchHit(**hit) for hit in context["hits"]]
    started = time.perf_counter()
    completion = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(context["q"], hits)},
        ],
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_tokens,
    )
    return {
        "text": completion.choices[0].message.content or "",
        "elapsed": time.perf_counter() - started,
        "completion_tokens": completion.usage.completion_tokens if completion.usage else 0,
    }


def check(context: dict, answer: str) -> list[str]:
    """Что не так с ответом. Пустой список - всё в порядке."""
    problems = []
    said_no_answer = NO_ANSWER_MARKER in answer
    expected_no_answer = context["expected_doc"] is None  # прошёл порог, но не покрыт

    if expected_no_answer and not said_no_answer:
        # Обычно не галлюцинация: модель пересказывает смежный чанк, лексически
        # похожий на вопрос. Для «белого списка» это всё равно плохо - пользователь
        # получает уверенный ответ на вопрос, который база не покрывает.
        problems.append("ответил по смежному материалу вместо «информации нет»")
    if not expected_no_answer and said_no_answer:
        problems.append("не нашёл ответ, хотя документ покрывает вопрос")
    if said_no_answer:
        return problems  # остальные проверки к отказу неприменимы

    for fact in context["must_contain"]:
        if fact.lower() not in answer.lower():
            problems.append(f"пропущен факт: «{fact}»")
    if any(token in answer for token in MARKDOWN_TOKENS):
        problems.append("markdown-разметка (портал покажет символы как есть)")
    if len(answer.strip()) < MIN_ANSWER_CHARS:
        problems.append("односложный ответ")
    return problems


async def run_model(client: AsyncOpenAI, model: str, contexts: list[dict]) -> dict:
    """Все вопросы одной модели - параллельно."""
    live = [c for c in contexts if c["passed"]]  # ниже порога LLM не вызывается
    answers = await asyncio.gather(*(ask(client, model, c) for c in live))

    results = []
    for context, answer in zip(live, answers):
        problems = check(context, answer["text"])
        results.append({"context": context, "answer": answer, "problems": problems})
    return {"model": model, "results": results}


def report(runs: list[dict], skipped: int) -> str:
    """Сводка со счётом + разбор проблемных ответов."""
    lines = ["# A/B-сравнение LLM", ""]
    lines.append(
        f"Вопросов с контекстом: {len(runs[0]['results'])} "
        f"(ещё {skipped} отсекается порогом - LLM не вызывается).\n"
    )
    lines.append("| Модель | Ответов без замечаний | Замечаний | Токенов | Среднее время |")
    lines.append("|-|-|-|-|-|")
    for run in runs:
        results = run["results"]
        clean = sum(1 for r in results if not r["problems"])
        problems = sum(len(r["problems"]) for r in results)
        tokens = sum(r["answer"]["completion_tokens"] for r in results)
        avg = sum(r["answer"]["elapsed"] for r in results) / len(results)
        lines.append(
            f"| `{run['model']}` | **{clean}/{len(results)}** | {problems} | "
            f"{tokens} | {avg:.1f} с |"
        )

    lines.append("\n## Замечания по вопросам\n")
    any_problem = False
    for run in runs:
        for result in run["results"]:
            if not result["problems"]:
                continue
            any_problem = True
            lines.append(f"- **{run['model']}** - «{result['context']['q']}»")
            for problem in result["problems"]:
                lines.append(f"  - {problem}")
    if not any_problem:
        lines.append("Замечаний нет: модели неразличимы по автопроверкам, "
                     "выбирайте по цене/скорости или добавьте must_contain.")

    lines.append("\n## Ответы\n")
    for index, context in enumerate(runs[0]["results"]):
        lines.append(f"### {context['context']['q']}\n")
        for run in runs:
            answer = run["results"][index]["answer"]["text"].strip()
            lines.append(f"**{run['model']}**\n\n```\n{answer}\n```\n")
    return "\n".join(lines)


async def main() -> None:
    parser = argparse.ArgumentParser(description="A/B-сравнение LLM на готовых контекстах")
    parser.add_argument("models", nargs="+", help="ID моделей OpenRouter (2 и более)")
    args = parser.parse_args()

    if not CONTEXTS_FILE.exists():
        print(
            f"Нет {CONTEXTS_FILE}. Сначала: "
            f"uv run --no-sync python eval/build_contexts.py",
            file=sys.stderr,
        )
        raise SystemExit(1)

    payload = json.loads(CONTEXTS_FILE.read_text(encoding="utf-8"))
    contexts = payload["contexts"]
    skipped = sum(1 for c in contexts if not c["passed"])

    if any(model.endswith(":free") for model in args.models):
        print(
            "Внимание: бесплатные (:free) эндпоинты часами отдают 429 - "
            "сравнение на них ненадёжно. См. maintenance.md.\n"
        )

    client = AsyncOpenAI(
        api_key=settings.openrouter_api_key, base_url=settings.openrouter_base_url
    )
    started = time.perf_counter()
    runs = await asyncio.gather(
        *(run_model(client, model, contexts) for model in args.models)
    )
    elapsed = time.perf_counter() - started

    text = report(list(runs), skipped)
    REPORT_FILE.write_text(text, encoding="utf-8")

    print(text.split("## Ответы")[0])
    print(f"Прогон занял {elapsed:.1f} с. Полный отчёт с ответами: {REPORT_FILE}")


if __name__ == "__main__":
    asyncio.run(main())
