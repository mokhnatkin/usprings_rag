"""Строит реестр всех инструкций базы erp25ltsdoc для отслеживания парсинга.

Скачивает оглавление its.1c.ru/db/erp25ltsdoc (доступно без авторизации),
собирает все ссылки на разделы и пишет registry.md: номер, название, URL,
статусы и таймстемпы скачивания и формирования PDF (заполняются парсером).

Запуск: uv run --with requests --with beautifulsoup4 python build_registry.py
Повторный запуск перезаписывает реестр - запускать только до старта парсинга.
"""

from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = "https://its.1c.ru"
TOC_URL = BASE + "/db/erp25ltsdoc"
OUT = Path(__file__).parent / "registry.md"

HEADER = """# Реестр инструкций 1С:ИТС (erp25ltsdoc)

База: `{base}` (URL разделов относительные). Всего разделов: {count}.
Сгенерирован `build_registry.py` из оглавления; статусы и таймстемпы
заполняет скрипт парсинга. Формат таймстемпов: `YYYY-MM-DD HH:MM:SS`.

| N | Название | URL | Скачано | Скачивание начало | Скачивание конец | PDF | PDF начало | PDF конец |
|-|-|-|-|-|-|-|-|-|
"""


def main() -> None:
    # Браузерному User-Agent сервер отдаёт оболочку с JS-подгрузкой дерева
    # (без ссылок); дефолтный UA requests получает полное статичное оглавление.
    r = requests.get(TOC_URL)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    seen = set()
    rows = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.startswith("/db/erp25ltsdoc/bookmark/") or href in seen:
            continue
        seen.add(href)
        title = a.get_text(" ", strip=True).replace("|", "/")
        rows.append((title, href))

    with OUT.open("w", encoding="utf-8") as f:
        f.write(HEADER.format(base=BASE, count=len(rows)))
        for i, (title, href) in enumerate(rows, 1):
            f.write(f"| {i} | {title} | {href} |  |  |  |  |  |  |\n")

    print(f"registry: {OUT} ({len(rows)} razdelov)")


if __name__ == "__main__":
    main()
