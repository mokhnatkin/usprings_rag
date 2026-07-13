"""Тест: авторизация на its.1c.ru (CAS login.1c.ru) и выгрузка одной инструкции в текст.

Запуск: uv run --with requests --with beautifulsoup4 python its_parse_test.py [выходной_файл.md]
Учётные данные читаются из 1C_portal_credentials.txt рядом со скриптом.
Подробности - в parsing-guide.md.
"""

import re
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = "https://its.1c.ru"
CAS_URL = (
    "https://login.1c.ru/login?service="
    "https%3A%2F%2Fits.1c.ru%2Flogin%2F%3Faction%3Daftercheck%26provider%3Dlogin"
)
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}

TEST_PAGE = "/db/erp25ltsdoc/bookmark/1CKassa/1CKassaSettings"
OUT_FILE = sys.argv[1] if len(sys.argv) > 1 else "test_instruction.md"


def read_credentials() -> tuple[str, str]:
    """Читает логин и пароль из 1C_portal_credentials.txt рядом со скриптом."""
    text = (Path(__file__).parent / "1C_portal_credentials.txt").read_text(encoding="utf-8")
    login = re.search(r"Логин:\s*(\S+)", text).group(1)
    password = re.search(r"Пароль:\s*(\S+)", text).group(1)
    return login, password


def login(s: requests.Session) -> None:
    """Авторизация: сначала визит на its.1c.ru (создаёт PHPSESSID), затем CAS-логин.

    Без предварительного визита тикет CAS не привязывается к сессии
    и контент остаётся под paywall.
    """
    user, password = read_credentials()
    s.get(BASE + "/db/erp25ltsdoc", headers=UA)
    r = s.get(CAS_URL, headers=UA)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    execution = soup.find("form", id="loginForm").find("input", {"name": "execution"})["value"]
    r = s.post(
        r.url,
        headers=UA,
        data={
            "username": user,
            "password": password,
            "execution": execution,
            "_eventId": "submit",
            "rememberMe": "on",
        },
    )
    r.raise_for_status()
    if "ticket=" not in r.url:
        sys.exit(f"login failed, final url: {r.url}")
    print("login ok")


def doc_frame_src(s: requests.Session, bookmark_path: str) -> str:
    """Возвращает URL htm-файла с телом документа для страницы-раздела."""
    r = s.get(BASE + bookmark_path, headers=UA)
    r.raise_for_status()
    if "paywall" in r.text:
        sys.exit("page is paywalled - auth did not stick")
    m = re.search(r'id="w_metadata_doc_frame"[^>]*src="([^"#]*)', r.text)
    if not m:
        sys.exit("doc frame src not found")
    return m.group(1)


def extract_text(html: str) -> tuple[str, str]:
    """Извлекает заголовок и текст документа, сохраняя структуру заголовков."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    title = soup.title.get_text(strip=True) if soup.title else ""
    body = soup.body or soup
    lines = []
    for el in body.find_all(["h1", "h2", "h3", "h4", "p", "li", "td"]):
        t = el.get_text(" ", strip=True)
        if not t:
            continue
        if el.name.startswith("h"):
            lines.append("\n" + "#" * int(el.name[1]) + " " + t + "\n")
        else:
            lines.append(t)
    return title, "\n".join(lines)


def main() -> None:
    s = requests.Session()
    login(s)

    src = doc_frame_src(s, TEST_PAGE)
    print(f"doc src: {src}")

    r = s.get(BASE + requests.utils.quote(src), headers=UA)
    r.raise_for_status()
    title, text = extract_text(r.text)
    print(f"title: {title}")
    print(f"text length: {len(text)}")

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        f.write(f"# {title}\n\nИсточник: {BASE}{TEST_PAGE}\n\n{text}\n")
    print(f"saved: {OUT_FILE}")


if __name__ == "__main__":
    main()
