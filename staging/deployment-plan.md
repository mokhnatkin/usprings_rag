# План: развёртывание usprings_rag на staging + CI/CD

## Контекст

`usprings_rag` (RAG-портал, MVP1 завершён) ещё не развёрнут на общей тестовой
машине УПЗ `195.239.217.102`. Нужно поднять рабочий staging-стенд по образцу
соседей (hr/crm/pps/ncr) и завести GitLab CI/CD (`.gitlab-ci.yml` пока нет).
Итог: портал доступен по `http://195.239.217.102:5285`, корпус ERP+ЗУП
проиндексирован, вход по портальному логину super-admin, каждый push в `main`
проходит lint/test/build.

Решения владельца (2026-07-15): **объём = деплой + CI/CD**; **внешний Basic Auth
не нужен** (полагаемся на портальный логин приложения, как hr/crm/pps);
**корпус заливаем scp + ingest на сервере**.

## Статус выполнения (2026-07-15)

Деплой выполнен, стенд поднят; ingest и внешний доступ — в процессе.

- [x] Артефакты в `main`: `docker-compose.staging.yml`, `.gitlab-ci.yml`, `ruff` в dev.
- [x] Репо клонирован на сервер (`/home/alex/usprings_rag`) по Deploy Token.
- [x] `.env` на сервере (OPENROUTER/SUPERADMIN из dev; свежие SECRET_KEY/POSTGRES_PASSWORD).
- [x] Образ собран, стек поднят, миграции 0001–0008, super-admin `admin`.
- [x] BGE-m3 скачан на сервер (HF доступен), app слушает `8085` (локально 303/вход).
- [x] Корпус залит (erp 413 PDF, zup 194).
- [x] **Ingest завершён**: erp 413/413, zup 194/194 документов (`ALL_INGEST_DONE`).
- [~] Локальный smoke: вход super-admin `admin` — OK, `/collections` (erp,zup) — OK,
  **ретрив работает** (находит чанки выше порога, доходит до LLM). НО ответ падает.
- [~] **БЛОКЕР: исходящий доступ к OpenRouter закрыт сетевой политикой.**
  С сервера `https://openrouter.ai` → `403 {"success":false,"error":"Access denied
  by security policy."}` (так же закрыт `api.github.com`; `huggingface.co` и
  `google.com` — открыты). Это egress-фильтр сети УПЗ, не приложение и не ключ.
  **Заявка провайдеру отправлена 2026-07-15** (whitelist egress `openrouter.ai`).
  Пока не открыт — генерация ответа LLM не работает (retrieval, портал, история,
  админка — работают). Проверка после открытия: `curl -I
  https://openrouter.ai/api/v1/models` с сервера → 200/401.
- [ ] Первый CI-пайплайн на gtl зелёный (lint/test/build).
- [~] Внешний DNAT `5285→8085` — **заявка провайдеру отправлена 2026-07-15**; ждём.
- [x] Финализация docs: раздел про staging в корневом `README.md`, `docs/maintenance.md`
  (раздел 10), отметка в `CLAUDE.md`.

## Проверено на сервере (readonly SSH, 2026-07-15)

| Факт | Значение | Вывод для плана |
|---|---|---|
| RAM | 15 GiB всего, ~11 GiB доступно | BGE-m3 (~2-3 GiB) помещается с запасом |
| Диск `/` | 228 GB, свободно 188 GB | Корпус 766 МБ + HF-кэш 2.3 ГБ + образ + pgdata влезают |
| Docker / Compose | 29.5.3 / v5.1.4 | Актуальны |
| HF-кэш `~/.cache/huggingface` | **отсутствует** (`NO_HF_CACHE`) | Прогрев кэша обязателен, иначе первый старт зависнет |
| `/home/alex/usprings_rag` | **нет** | Чистый первый деплой |
| Host-порт `8085` | **свободен** (заняты 80, 8025, 8081, 8082, 8083) | Целевой порт свободен |
| Host PG18 | слушает `5432` | Порт контейнерной БД наружу НЕ публикуем |
| `/etc/docker/daemon.json` | `registry-mirrors: mirror.gcr.io` | Docker Hub-образы тянутся через зеркало |
| ncr-стенд | dir есть, контейнеры не запущены | На порты не влияет |

Внешний порт `5285` (DNAT провайдера) на момент проверки ещё не заведён —
изнутри сети это не проверяется; запросить у провайдера (см. шаг 0.3).

## Целевая архитектура стенда

- **app** (FastAPI + BGE-m3, образ `build: .`): публикуем `8085:8000`
  (внешний `5285` → host `8085` → контейнер `8000`), `restart: unless-stopped`.
  Веса BGE-m3 — из HF-кэша хоста; корпус — из `./docs/manuals`.
  Миграции (`alembic upgrade head`) и воркер индексации стартуют внутри
  процесса приложения (штатный CMD, `INDEX_WORKER_ENABLED=true`).
- **db** (`pgvector/pgvector:pg16`, том `pgdata`): порт наружу **не публикуем**
  (host PG18 держит 5432; приложение ходит на `db:5432` внутри compose-сети),
  `restart: unless-stopped`. Это контейнерная БД проекта, не хостовый PG18.
- Аутентификация — портальный логин приложения; nginx/Basic Auth не добавляем.

## Артефакты в репозитории (создать, закоммитить в upstream `gtl`, до деплоя)

Правило соседей: рабочее дерево на сервере — зеркало upstream; tracked-файлы
на сервере не редактируем, всё чиним в репозитории и подтягиваем `reset --hard`.
Значит `docker-compose.staging.yml` и `.gitlab-ci.yml` живут в git.

### 1. `docker-compose.staging.yml` (новый, в корне репозитория)

Самодостаточный compose (НЕ override поверх `docker-compose.yml`): у Compose
списки `ports` при слиянии `-f base -f override` **конкатенируются**, и app
получил бы одновременно `8000` и `8085`. Отдельный файл этого избегает и
повторяет паттерн соседей (один compose на стенд).

```yaml
# Staging-стек usprings_rag на 195.239.217.102 (host 8085 -> внешний 5285).
# Запуск на сервере: docker compose -f docker-compose.staging.yml up -d --build
services:
  app:
    build: .
    env_file: .env
    environment:
      DATABASE_URL: postgresql+psycopg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@db:5432/${POSTGRES_DB}
    ports:
      - "8085:8000"
    volumes:
      - ~/.cache/huggingface:/root/.cache/huggingface
      - ./docs/manuals:/app/docs/manuals
    depends_on:
      db:
        condition: service_healthy
    restart: unless-stopped

  db:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_USER: ${POSTGRES_USER}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: ${POSTGRES_DB}
    # Порт наружу не публикуем: host PG18 держит 5432, внешний доступ к БД не нужен.
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER} -d ${POSTGRES_DB}"]
      interval: 5s
      timeout: 3s
      retries: 10
    restart: unless-stopped

volumes:
  pgdata:
```

### 2. `.gitlab-ci.yml` (новый, в корне) — по образцу `usprings_ncr/.gitlab-ci.yml`

Отличия rag: один Python-сервис (нет `backend/`/`frontend/`), фронтенд серверный
(node-стадии не надо), сервис БД в тестах — `pgvector/pgvector:pg16`. Тесты
безопасны для раннера: BGE-m3 в тестах **подменяется** (не тянет 2.3 ГБ), вызовы
LLM/поиска **monkeypatch**-ятся (нет сети OpenRouter), DB-тесты без БД делают
`pytest.skip`. `uv.lock` в репозитории есть → `uv sync --frozen`.

```yaml
stages: [lint, test, build]

workflow:
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
    - if: $CI_COMMIT_BRANCH == "main"
    - if: $CI_COMMIT_TAG

variables:
  UV_CACHE_DIR: .uv-cache

.uv-base:
  image: python:3.14-slim
  before_script:
    - pip install uv
    - uv sync --frozen
  cache:
    key:
      files: [uv.lock]
    paths: [.uv-cache]

lint:
  extends: .uv-base
  stage: lint
  script:
    - uv run ruff check .

test:
  extends: .uv-base
  stage: test
  services:
    - name: pgvector/pgvector:pg16
      alias: db
  variables:
    POSTGRES_USER: usprings
    POSTGRES_PASSWORD: usprings
    POSTGRES_DB: usprings_rag
    DATABASE_URL: "postgresql+psycopg://usprings:usprings@db:5432/usprings_rag"
  script:
    - uv run alembic upgrade head   # создаёт схему и сидит коллекции erp/zup (миграция 0004)
    - uv run pytest -q

build:images:
  stage: build
  image: docker:27
  services: [docker:27-dind]
  variables:
    DOCKER_TLS_CERTDIR: "/certs"
  rules:
    - if: $CI_COMMIT_BRANCH == "main"
    - if: $CI_COMMIT_TAG
  script:
    - docker login -u "$CI_REGISTRY_USER" -p "$CI_REGISTRY_PASSWORD" "$CI_REGISTRY"
    - docker build -t "$CI_REGISTRY_IMAGE:$CI_COMMIT_SHORT_SHA" .
    - docker push "$CI_REGISTRY_IMAGE:$CI_COMMIT_SHORT_SHA"
    - |
      if [ "$CI_COMMIT_BRANCH" = "main" ]; then
        docker tag  "$CI_REGISTRY_IMAGE:$CI_COMMIT_SHORT_SHA" "$CI_REGISTRY_IMAGE:latest"
        docker push "$CI_REGISTRY_IMAGE:latest"
      fi
```

Registry (`gtl.usteel.ru:5050`) и логин — через предопределённые `$CI_REGISTRY*`
(ничего не хардкодим). Раннер — общий docker-executor на `195.239.217.102` (уже
зарегистрирован, привилегированный dind). Стадия `build` только собирает и пушит
образ; деплой — ручной (`reset --hard`), CI на сервер сам не выкатывает.

### 3. Мелкая правка `pyproject.toml`: добавить `ruff` в dev-группу

Сейчас `ruff` не в зависимостях → `uv run ruff` в CI не найдёт его. Добавить:

```toml
[dependency-groups]
dev = [
    "pytest>=9.1.1",
    "ruff>=0.9",
]
```

Первый прогон `ruff check .` может найти замечания на существующем коде —
разобрать (поправить или добавить минимальный `[tool.ruff]` с нужными исключениями)
локально до включения пайплайна, чтобы `main` был зелёным.

### 4. Обновить документацию

`staging/README.md` (снять расхождение: п.20 ссылается на резервацию порта 5285 в
корневом `README.md`, которой там нет — либо добавить строку в корневой README,
либо поправить ссылку), корневой `README.md`/`docs/maintenance.md` — добавить
раздел про staging (URL, порт, обновление, бэкап). `CLAUDE.md` — короткая отметка,
что стенд развёрнут.

## Пошаговый деплой

Подключение к серверу (Windows, без интерактива) — по паттерну
`IT_strategy_usprings/DevOps/server_access.md` (SSH_ASKPASS, порт 5222, `alex`).
Секреты в лог не выводить.

### Шаг 0. Предпосылки (часть — заранее, лид-тайм)

- **0.1** Получить у владельца **платный** `OPENROUTER_API_KEY`.
- **0.2** Создать в GitLab (`gtl.usteel.ru`) **Deploy Token** для `usprings/usprings_rag`
  (scope `read_repository`) — клон по токену, не по личному PAT.
- **0.3** **Запросить у провайдера DNAT внешнего `5285` → host `8085`** (как
  `5281–5284 → 8081–8084`; шаблон — `claude_other_docs/port_forwarding_request_195.239.217.102.md`).
  Лид-тайм большой — заказать в первую очередь; деплой стенда параллелится, но
  внешняя проверка (шаг 8) ждёт этот проброс.
- **0.4** Закоммитить артефакты 1-3 в `main`, запушить в `gtl` (и GitHub-зеркало).

### Шаг 1. Прогреть HF-кэш на сервере (иначе первый старт зависнет)

Веса BGE-m3 в образ не кладутся — монтируются из `~/.cache/huggingface` хоста,
которого на сервере нет.

**Проверено 2026-07-15: `huggingface.co` доступен с сервера** (`resolve/main/config.json`
→ 307 на CDN за ~0.4 с). Значит прогрев = **скачивание на самом сервере** уже
поднятым образом (offload на быстрый линк сервера, без выгрузки с dev-машины):

```bash
docker compose -f docker-compose.staging.yml run --rm app \
    python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-m3')"
```

Модель (~2.3 ГБ) ляжет в смонтированный `~/.cache/huggingface` хоста; последующие
старты берут её из кэша. Выполнять после сборки образа (шаг 5, `--build`) и до/вместо
первого прогрева при `up`. Локальный кэш dev-машины (`~/.cache/huggingface/hub/models--BAAI--bge-m3`,
~5.5 ГБ — несколько форматов) как источник scp — запасной вариант, если доступ к
HF с сервера пропадёт.

### Шаг 2. Клонировать репозиторий по Deploy Token

```bash
git clone https://<deploy-token-user>:<deploy-token>@gtl.usteel.ru/usprings/usprings_rag.git \
    /home/alex/usprings_rag
cd /home/alex/usprings_rag
```

Обновления в дальнейшем — `git fetch --prune origin && git reset --hard origin/main`.
Tracked-файлы на сервере не править.

### Шаг 3. Заполнить `.env` (gitignored, только на сервере)

`cp .env.example .env`, задать реальные значения:

- `OPENROUTER_API_KEY` — платный ключ (0.1).
- `SECRET_KEY` — длинная случайная строка
  (`python3 -c "import secrets; print(secrets.token_urlsafe(48))"`); иначе рестарт
  разлогинит всех.
- `POSTGRES_PASSWORD` — сильный пароль (не дефолтный `usprings`).
- `SUPERADMIN_LOGIN` / `SUPERADMIN_PASSWORD` — бутстрап super-admin при первом
  старте (после создания учётки пароль сменить в профиле).
- `POSTGRES_USER=usprings`, `POSTGRES_DB=usprings_rag` — можно дефолтные.
- `POSTGRES_PORT` — оставить любым валидным (в staging БД наружу не публикуется,
  переменная не используется).
- Остальное (`OPENROUTER_MODEL`, `EMBEDDING_*`, `CHUNK_*`, `TOP_K`, `INDEX_WORKER_ENABLED=true`) — дефолты.
- Порог сходства НЕ в `.env` — свойство коллекции (erp=0.58, zup=0.55), правится из UI.

`chmod 600 .env`.

### Шаг 4. Залить корпус инструкций (не в git, ~766 МБ)

```bash
# с dev-машины: корпус в docs/manuals/{its_erp,its_zup}
scp -P 5222 -r docs/manuals/its_erp docs/manuals/its_zup \
    alex@195.239.217.102:/home/alex/usprings_rag/docs/manuals/
```

Проверить на сервере, что PDF на месте: `ls docs/manuals/its_erp | wc -l`
(ожидаемо ~411), `ls docs/manuals/its_zup | wc -l` (~194).
(Если на dev-машине есть rsync/WSL — предпочесть `rsync -avz --progress` как
возобновляемый.)

### Шаг 5. Поднять стек (миграции применятся на старте app)

```bash
cd /home/alex/usprings_rag
docker compose -f docker-compose.staging.yml up -d --build
docker compose -f docker-compose.staging.yml logs -f app   # ждать "Uvicorn running"
```

Первый старт: сборка образа (torch CPU-колесо + зависимости), затем `alembic
upgrade head` (создаёт схему, сидит коллекции erp/zup) и прогрев BGE-m3 (десятки
секунд). Образы тянутся через `mirror.gcr.io` (Docker Hub закрыт).

### Шаг 6. Проиндексировать корпус

```bash
docker compose -f docker-compose.staging.yml run --rm app ingest --collection erp
docker compose -f docker-compose.staging.yml run --rm app ingest --collection zup
```

Ingest идемпотентен (по `source_path`/хешу), портал во время прогона работает.
`erp` → `docs/manuals/its_erp`, `zup` → `docs/manuals/its_zup` (папка = коллекция).

### Шаг 7. Локальный smoke на сервере

```bash
curl -s http://localhost:8085/ -o /dev/null -w "%{http_code}\n"   # 200 или редирект на вход
```

Через SSH-туннель или браузер: войти super-admin'ом, выбрать коллекцию 1С:ERP,
задать вопрос по инструкции — ответ со ссылками на источники; задать
заведомо непокрытый вопрос — вежливый отказ (порог).

### Шаг 8. Внешний проброс и проверка (после 0.3)

Когда провайдер подтвердит `5285 → 8085`, с внешней машины:

```powershell
Test-NetConnection 195.239.217.102 -Port 5285          # TcpTestSucceeded=True
Invoke-WebRequest http://195.239.217.102:5285/ -UseBasicParsing   # 200 / страница входа
```

## CI/CD: включение

1. Артефакт 2 (`.gitlab-ci.yml`) + 3 (`ruff`) уже в `main` (шаг 0.4).
2. Первый пайплайн на push в `main`: убедиться, что `lint`, `test`, `build:images`
   зелёные. Если `test` не видит БД — проверить alias `db` и `DATABASE_URL`.
   Если `lint` красный — разобрать замечания ruff (шаг «правка pyproject»).
3. Container Registry проекта включён (`gtl.usteel.ru:5050`) — если нет, включить
   в настройках проекта GitLab. Push-mirror на GitHub уже активен, отдельной
   синхронизации CI не требует.

## Обновление и бэкап (эксплуатация)

- **Обновление кода:** на сервере из `/home/alex/usprings_rag` —
  `git fetch --prune origin && git reset --hard origin/main &&
  docker compose -f docker-compose.staging.yml up -d --build`. `.env`, корпус и
  `pgdata` (gitignored/тома) не затрагиваются.
- **Бэкап БД:** у rag своего скрипта нет (в отличие от ncr). Завести по образцу
  `usprings_ncr/scripts/backup.sh` (ежедневный `pg_dump` контейнерной БД в
  отдельную папку, ротация): `docker compose -f docker-compose.staging.yml exec -T
  db pg_dump -U usprings -d usprings_rag --clean --if-exists | gzip > dump.sql.gz`,
  cron `0 2 * * *`. Корпус PDF бэкапить отдельно (он не в git). Это добавить в
  `docs/maintenance.md`.

## Проверка (end-to-end)

1. **Инфраструктура:** `docker compose -f docker-compose.staging.yml ps` — `app`
   и `db` в `running`/`healthy`; `docker compose logs app` содержит
   `Uvicorn running` и старт воркера индексации; `ss -tln | grep 8085` — слушает.
2. **Данные:** после ingest — вопрос в UI по 1С:ERP и по 1С:ЗУП возвращает ответ
   с источниками; непокрытый вопрос → отказ.
3. **Роли/логин:** вход super-admin из `.env`; доступны экраны админки
   (Документы/Журнал/Аналитика/Пользователи/Коллекции/Калибровка).
4. **Персистентность:** `docker compose restart app` — пользователь остаётся
   залогинен (стабильный `SECRET_KEY`); данные на месте (том `pgdata`).
5. **Внешний доступ:** `http://195.239.217.102:5285/` открывается снаружи (после 0.3).
6. **CI:** пайплайн на `main` зелёный (lint/test/build), образ появился в registry.

## Открытые вопросы (за рамками деплоя)

- Zero-data-retention у OpenRouter перед выходом за пилот (`docs/open-questions.md`).
- Приватность LLM / соответствие политике безопасности перед продом.
- Промоушен staging → prod (отдельные IP/субдомены) — вне текущего объёма.
