"""Тест: выгрузка инструкции ИТС с картинками и конвертация в PDF (pandoc + typst).

Запуск: uv run --with requests --with beautifulsoup4 python its_parse_pdf_test.py [папка_вывода]
Учётные данные читаются из 1C_portal_credentials.txt рядом со скриптом.
Требуются pandoc и typst в PATH. Подробности и замеры - в parsing-guide.md.
"""

import re
import subprocess
import sys
import time
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

TEST_PAGE = "/db/erp25ltsdoc/bookmark/1CKassa/1CKassaSettings"
WORK = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("pdf_out")


def read_credentials() -> tuple[str, str]:
    """Читает логин и пароль из 1C_portal_credentials.txt рядом со скриптом."""
    text = (Path(__file__).parent / "1C_portal_credentials.txt").read_text(encoding="utf-8")
    login = re.search(r"Логин:\s*(\S+)", text).group(1)
    password = re.search(r"Пароль:\s*(\S+)", text).group(1)
    return login, password


def login(s: requests.Session) -> None:
    """Авторизация: визит на its.1c.ru за PHPSESSID, затем CAS-логин (см. parsing-guide.md)."""
    user, password = read_credentials()
    s.get(BASE + "/db/erp25ltsdoc", headers=UA)
    r = s.get(CAS_URL, headers=UA)
    soup = BeautifulSoup(r.text, "html.parser")
    execution = soup.find("form", id="loginForm").find("input", {"name": "execution"})["value"]
    r = s.post(r.url, headers=UA, data={
        "username": user, "password": password, "execution": execution,
        "_eventId": "submit", "rememberMe": "on",
    })
    if "ticket=" not in r.url:
        sys.exit(f"login failed: {r.url}")


def main() -> None:
    (WORK / "img").mkdir(parents=True, exist_ok=True)
    s = requests.Session()
    timings = {}

    t = time.perf_counter()
    login(s)
    timings["login"] = time.perf_counter() - t

    t = time.perf_counter()
    r = s.get(BASE + TEST_PAGE, headers=UA)
    src = re.search(r'id="w_metadata_doc_frame"[^>]*src="([^"#]*)', r.text).group(1)
    r = s.get(BASE + requests.utils.quote(src), headers=UA)
    r.raise_for_status()
    timings["fetch html"] = time.perf_counter() - t

    t = time.perf_counter()
    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup(["script", "style", "link"]):
        tag.decompose()
    n_img, total_bytes = 0, 0
    for i, img in enumerate(soup.find_all("img")):
        img_src = img.get("src")
        if not img_src:
            continue
        rimg = s.get(urljoin(BASE + src, img_src), headers=UA)
        if rimg.status_code != 200:
            print(f"  skip {img_src}: HTTP {rimg.status_code}")
            img.decompose()
            continue
        ext = Path(unquote(img_src.split("?")[0])).suffix or ".png"
        fname = f"img/{i:03d}{ext}"
        (WORK / fname).write_bytes(rimg.content)
        img["src"] = fname
        for attr in ("srcset", "style", "width", "height"):
            if img.has_attr(attr):
                del img[attr]
        n_img += 1
        total_bytes += len(rimg.content)
    timings[f"fetch {n_img} images"] = time.perf_counter() - t

    title = soup.title.get_text(strip=True) if soup.title else "instruction"
    html_file = WORK / "instruction.html"
    html_file.write_text(str(soup), encoding="utf-8")

    t = time.perf_counter()
    # figure в typst по умолчанию неразрывный: длинная таблица на границе страниц
    # рисуется поверх текста; разрешаем перенос
    (WORK / "breakable.typ").write_text("#show figure: set block(breakable: true)\n")
    pdf_file = WORK / "instruction.pdf"
    res = subprocess.run(
        ["pandoc", html_file.name, "-o", pdf_file.name, "--pdf-engine=typst",
         "-V", "mainfont=Times New Roman", "--metadata", f"title={title}",
         "--include-in-header=breakable.typ"],
        cwd=WORK, capture_output=True, text=True, encoding="utf-8",
    )
    timings["pandoc+typst -> pdf"] = time.perf_counter() - t
    if res.returncode != 0:
        print("PANDOC STDERR:", res.stderr[:2000])
        sys.exit(1)

    print(f"\nimages: {n_img}, {total_bytes // 1024} KiB")
    print(f"pdf: {pdf_file} ({pdf_file.stat().st_size // 1024} KiB)")
    total = sum(timings.values())
    print("\ntimings:")
    for k, v in timings.items():
        print(f"  {k:24s} {v:6.2f} s")
    print(f"  {'TOTAL':24s} {total:6.2f} s")


if __name__ == "__main__":
    main()
