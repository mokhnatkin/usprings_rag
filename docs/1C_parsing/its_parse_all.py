"""Пакетный парсер инструкций ИТС по реестру базы: текст + картинки -> PDF.

Запуск: uv run --with requests --with beautifulsoup4 python its_parse_all.py [--db zupcorpdoc] [--limit N]
(из папки docs/1C_parsing; рекомендуется PYTHONIOENCODING=utf-8)

База задаётся `--db` (по умолчанию erp25ltsdoc); от неё зависят реестр
`registry-<db>.md`, рабочая папка `work/<db>/` и папка PDF - она же папка
коллекции (`docs/manuals/its_erp/`, `docs/manuals/its_zup/`).

Идёт по реестру, для каждого раздела скачивает документ с картинками и
собирает PDF. Статусы и таймстемпы пишет обратно в реестр после каждого
раздела - прерванный прогон продолжается с места остановки. Сетевые запросы
с ретраями; ошибки помечаются в реестре (err: причина), прогон продолжается.
Разделы, ссылающиеся на уже скачанный документ (глава = несколько
подразделов), помечаются как "дубль".

Требуются pandoc и typst в PATH. Учётные данные - 1C_portal_credentials.txt.
"""

import argparse
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urljoin

import requests
from bs4 import BeautifulSoup

BASE = "https://its.1c.ru"
CAS_URL = (
    "https://login.1c.ru/login?service="
    "https%3A%2F%2Fits.1c.ru%2Flogin%2F%3Faction%3Daftercheck%26provider%3Dlogin"
)
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}

HERE = Path(__file__).parent
PAUSE = 0.7  # сек между HTTP-запросами (DDoS-Guard)
RETRY_WAITS = [5, 30]  # паузы между 3 попытками

DEFAULT_DB = "erp25ltsdoc"
# База ИТС -> папка коллекции в docs/manuals (коллекция = папка, см. backlog B1)
DB_DIRS = {"erp25ltsdoc": "its_erp", "zupcorpdoc": "its_zup"}


@dataclass
class Target:
    """Пути и URL, производные от идентификатора базы ИТС."""

    db: str
    registry: Path
    work: Path
    out_dir: Path


def target_for(db: str) -> Target:
    if db not in DB_DIRS:
        raise SystemExit(f"неизвестная база: {db} (известны: {', '.join(DB_DIRS)})")
    return Target(
        db=db,
        registry=HERE / f"registry-{db}.md",
        work=HERE / "work" / db,
        out_dir=HERE.parent / "manuals" / DB_DIRS[db],
    )


class FetchError(Exception):
    pass


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def read_credentials() -> tuple[str, str]:
    text = (HERE / "1C_portal_credentials.txt").read_text(encoding="utf-8")
    return (
        re.search(r"Логин:\s*(\S+)", text).group(1),
        re.search(r"Пароль:\s*(\S+)", text).group(1),
    )


def login(s: requests.Session, db: str) -> None:
    """Авторизация: визит за PHPSESSID, затем CAS-логин (см. parsing-guide.md)."""
    user, password = read_credentials()
    s.get(f"{BASE}/db/{db}", headers=UA, timeout=60)
    r = s.get(CAS_URL, headers=UA, timeout=60)
    form = BeautifulSoup(r.text, "html.parser").find("form", id="loginForm")
    if form is None:
        # CAS помнит сессию (TGT) и сразу редиректит с тикетом - формы нет
        if "ticket=" in r.url:
            print(f"[{now()}] login ok (SSO)")
            return
        raise FetchError(f"no login form: {r.url}")
    r = s.post(r.url, headers=UA, timeout=60, data={
        "username": user, "password": password,
        "execution": form.find("input", {"name": "execution"})["value"],
        "_eventId": "submit", "rememberMe": "on",
    })
    if "ticket=" not in r.url:
        raise FetchError(f"login failed: {r.url}")
    print(f"[{now()}] login ok")


def get_retry(s: requests.Session, url: str) -> requests.Response:
    """GET с 3 попытками; между попытками паузы RETRY_WAITS."""
    last = ""
    for attempt in range(len(RETRY_WAITS) + 1):
        try:
            r = s.get(url, headers=UA, timeout=60)
            if r.status_code == 200:
                return r
            if r.status_code == 401:  # нет авторизации - ретраи бесполезны
                raise FetchError("HTTP 401")
            last = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            last = type(e).__name__
        if attempt < len(RETRY_WAITS):
            time.sleep(RETRY_WAITS[attempt])
    raise FetchError(last)


def sanitize(name: str) -> str:
    """Имя файла для Windows: убрать запрещённые символы, ограничить длину.

    Обрезка идёт до strip: хвостовой пробел (или точка) после обрезки Windows
    не переваривает - каталог "имя " не находится как промежуточный компонент
    пути, и mkdir падает с WinError 3.
    """
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    return name[:100].strip(" .")


def load_registry(registry: Path) -> tuple[list[str], list[dict]]:
    """Читает реестр базы: шапка (до разделителя таблицы включительно) и строки."""
    lines = registry.read_text(encoding="utf-8").splitlines()
    sep = next(i for i, ln in enumerate(lines) if ln.startswith("|-"))
    rows = []
    for ln in lines[sep + 1:]:
        if not ln.startswith("|"):
            continue
        f = [c.strip() for c in ln.strip().strip("|").split("|")]
        rows.append(dict(zip(
            ["n", "title", "url", "dl", "dl_start", "dl_end", "pdf", "pdf_start", "pdf_end"], f
        )))
    return lines[:sep + 1], rows


def save_registry(registry: Path, header: list[str], rows: list[dict]) -> None:
    """Атомарная запись реестра (tmp + replace) - переживает прерывание."""
    tmp = registry.with_suffix(".md.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write("\n".join(header) + "\n")
        for r in rows:
            f.write("| {n} | {title} | {url} | {dl} | {dl_start} | {dl_end} "
                    "| {pdf} | {pdf_start} | {pdf_end} |\n".format(**r))
    os.replace(tmp, registry)


def resolve_src(s: requests.Session, row: dict) -> tuple[str, str]:
    """По странице раздела определяет адрес тела документа. Возвращает (src, stem).

    Отдельный шаг: по stem видно, скачан ли уже этот документ (глава = несколько
    подразделов-якорей), и дубль отсеивается до скачивания тела и картинок.
    """
    r = get_retry(s, BASE + row["url"])
    m = re.search(r'id="w_metadata_doc_frame"[^>]*src="([^"#]*)', r.text)
    if not m:
        raise FetchError("no doc frame")
    src = m.group(1)
    return src, sanitize(Path(unquote(src)).stem)


def fetch_document(s: requests.Session, target: Target, src: str, stem: str) -> Path:
    """Скачивает тело документа с картинками. Возвращает папку work."""
    doc_dir = target.work / stem
    (doc_dir / "img").mkdir(parents=True, exist_ok=True)
    time.sleep(PAUSE)
    try:
        r = get_retry(s, BASE + requests.utils.quote(src))
    except FetchError as e:  # 401 = сессия протухла: перелогин и одна попытка
        if str(e) != "HTTP 401":
            raise
        login(s, target.db)
        r = get_retry(s, BASE + requests.utils.quote(src))

    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup(["script", "style", "link"]):
        tag.decompose()
    for a in soup.find_all("a"):
        # ссылки на якоря других глав (<a href="#_refNNN">) ведут в typst
        # к label, которого в документе нет - сборка PDF падает. Текст оставляем.
        a.unwrap()
    for i, img in enumerate(soup.find_all("img")):
        img_src = img.get("src")
        if not img_src:
            continue
        time.sleep(PAUSE)
        try:
            rimg = get_retry(s, urljoin(BASE + src, img_src))
        except FetchError as e:
            print(f"    img skip {img_src}: {e}")
            img.decompose()
            continue
        ext = Path(unquote(img_src.split("?")[0])).suffix or ".png"
        fname = f"img/{i:03d}{ext}"
        (doc_dir / fname).write_bytes(rimg.content)
        img["src"] = fname
        for attr in ("srcset", "style", "width", "height"):
            if img.has_attr(attr):
                del img[attr]

    title = soup.title.get_text(strip=True) if soup.title else stem
    (doc_dir / "instruction.html").write_text(str(soup), encoding="utf-8")
    (doc_dir / "title.txt").write_text(title, encoding="utf-8")
    return doc_dir


def build_pdf(doc_dir: Path, out_dir: Path, stem: str) -> Path:
    """Собирает PDF из скачанного документа в папку коллекции."""
    breakable = doc_dir / "breakable.typ"
    breakable.write_text("#show figure: set block(breakable: true)\n")
    title = (doc_dir / "title.txt").read_text(encoding="utf-8")
    pdf = out_dir / f"{stem}.pdf"
    res = subprocess.run(
        ["pandoc", "instruction.html", "-o", str(pdf), "--pdf-engine=typst",
         "-V", "mainfont=Times New Roman", "--metadata", f"title={title}",
         "--include-in-header=breakable.typ"],
        cwd=doc_dir, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if res.returncode != 0:
        raise FetchError(f"pandoc rc={res.returncode}: {res.stderr[:120]}")
    return pdf


def keep_awake(on: bool) -> None:
    """Запрещает Windows усыплять систему на время прогона (SetThreadExecutionState).

    Без этого ноутбук уходит в сон по бездействию и процесс замирает вместе с ним.
    Флаг действует, пока процесс жив; при выходе система возвращается к обычному
    режиму сама. На не-Windows - ничего не делает.
    """
    if sys.platform != "win32":
        return
    import ctypes

    ES_CONTINUOUS = 0x80000000
    ES_SYSTEM_REQUIRED = 0x00000001
    state = ES_CONTINUOUS | ES_SYSTEM_REQUIRED if on else ES_CONTINUOUS
    ctypes.windll.kernel32.SetThreadExecutionState(state)


def main() -> None:
    parser = argparse.ArgumentParser(description="Пакетный парсинг базы ИТС по её реестру")
    parser.add_argument("--db", default=DEFAULT_DB,
                        help=f"идентификатор базы ИТС (по умолчанию {DEFAULT_DB})")
    parser.add_argument("--limit", type=int, default=0,
                        help="обработать не более N разделов, включая дубли (0 - все)")
    parser.add_argument("--new", type=int, default=0,
                        help="остановиться после N новых PDF (дубли не считаются; 0 - все)")
    args = parser.parse_args()

    target = target_for(args.db)
    keep_awake(True)
    target.out_dir.mkdir(parents=True, exist_ok=True)
    header, rows = load_registry(target.registry)
    pending = [r for r in rows if r["pdf"] != "ok" and r["dl"] != "дубль"]
    print(f"[{now()}] база {target.db}, разделов в реестре: {len(rows)}, "
          f"к обработке: {len(pending)}, PDF -> {target.out_dir}")

    s = requests.Session()
    login(s, target.db)

    done = errors = dups = 0
    for row in rows:
        if row["pdf"] == "ok" or row["dl"] == "дубль":
            continue
        if args.limit and done + errors + dups >= args.limit:
            break
        if args.new and done >= args.new:
            break
        label = f"[{row['n']}/{len(rows)}]"
        try:
            row["dl"], row["dl_start"] = "", now()
            src, stem = resolve_src(s, row)
            if (target.out_dir / f"{stem}.pdf").exists():  # уже собран - не качаем
                row["dl"] = row["pdf"] = "дубль"
                row["dl_start"] = ""
                dups += 1
                print(f"{label} дубль: {stem}")
            else:
                doc_dir = fetch_document(s, target, src, stem)
                row["dl"], row["dl_end"] = "ok", now()
                row["pdf_start"] = now()
                build_pdf(doc_dir, target.out_dir, stem)
                row["pdf"], row["pdf_end"] = "ok", now()
                done += 1
                print(f"{label} ok: {stem}.pdf")
        except (FetchError, OSError) as e:
            stage = "pdf" if row["dl"] == "ok" else "dl"
            row["pdf" if stage == "pdf" else "dl"] = f"err: {e}"[:80].replace("|", "/")
            errors += 1
            print(f"{label} {stage} err: {e}")
        save_registry(target.registry, header, rows)
        time.sleep(PAUSE)

    keep_awake(False)
    print(f"\n[{now()}] готово: pdf {done}, дублей {dups}, ошибок {errors}")
    if errors:
        print("строки с ошибками остаются в работе - повторный запуск обработает их снова")


if __name__ == "__main__":
    main()
