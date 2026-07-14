"""Строит реестр всех инструкций базы ИТС для отслеживания парсинга.

Скачивает оглавление its.1c.ru/db/<db> (доступно без авторизации), собирает
все ссылки на разделы и пишет registry-<db>.md: номер, название, URL, статусы
и таймстемпы скачивания и формирования PDF (заполняются парсером).

Запуск: uv run --with requests --with beautifulsoup4 python build_registry.py [--db zupcorpdoc]
По умолчанию база erp25ltsdoc (1С:ERP); 1С:ЗУП - zupcorpdoc.
Повторный запуск перезаписывает реестр - запускать только до старта парсинга.
"""

import argparse
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = "https://its.1c.ru"
DEFAULT_DB = "erp25ltsdoc"

# Разделы адресуются по-разному: у erp25ltsdoc это /bookmark/<раздел>/<подраздел>,
# у zupcorpdoc - /content/<N>/hdoc. Прочие ссылки под /db/<db>/ (например /search -
# «Результаты поиска») к оглавлению не относятся и в реестр не попадают.
SECTION_KINDS = ("bookmark", "content")

HEADER = """# Реестр инструкций 1С:ИТС ({db})

База: `{base}` (URL разделов относительные). Всего разделов: {count}.
Сгенерирован `build_registry.py` из оглавления; статусы и таймстемпы
заполняет скрипт парсинга. Формат таймстемпов: `YYYY-MM-DD HH:MM:SS`.

| N | Название | URL | Скачано | Скачивание начало | Скачивание конец | PDF | PDF начало | PDF конец |
|-|-|-|-|-|-|-|-|-|
"""


def registry_path(db: str) -> Path:
    """Путь к реестру базы: реестры разных баз лежат рядом и не перетирают друг друга."""
    return Path(__file__).parent / f"registry-{db}.md"


def main() -> None:
    parser = argparse.ArgumentParser(description="Реестр разделов базы ИТС")
    parser.add_argument("--db", default=DEFAULT_DB,
                        help=f"идентификатор базы ИТС (по умолчанию {DEFAULT_DB})")
    args = parser.parse_args()

    toc_url = f"{BASE}/db/{args.db}"
    prefixes = tuple(f"/db/{args.db}/{kind}/" for kind in SECTION_KINDS)
    out = registry_path(args.db)

    # Браузерному User-Agent сервер отдаёт оболочку с JS-подгрузкой дерева
    # (без ссылок); дефолтный UA requests получает полное статичное оглавление.
    r = requests.get(toc_url)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    seen = set()
    rows = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.startswith(prefixes) or href in seen:
            continue
        seen.add(href)
        title = a.get_text(" ", strip=True).replace("|", "/")
        rows.append((title, href))

    with out.open("w", encoding="utf-8") as f:
        f.write(HEADER.format(base=BASE, db=args.db, count=len(rows)))
        for i, (title, href) in enumerate(rows, 1):
            f.write(f"| {i} | {title} | {href} |  |  |  |  |  |  |\n")

    print(f"registry: {out} ({len(rows)} razdelov)")


if __name__ == "__main__":
    main()
