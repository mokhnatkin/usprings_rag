# Вводная: staging usprings_rag + CI/CD

Задача следующего агента: развернуть **usprings_rag** на общей тестовой машине УПЗ
`195.239.217.102` и настроить GitLab CI/CD по аналогии с другими проектами группы.
Ниже — только то, что нужно на старте; детали брать из указанных файлов.

## Что за проект

RAG-портал (FastAPI + PostgreSQL/pgvector + BGE-m3 + OpenRouter, Docker Compose),
**MVP1 реализован**: многопользовательский портал с ролями, логированием, админкой и
калибровкой порогов из UI. Обзор — корневой `README.md` и `../README.md` проекта;
решения и особенности запуска — `../CLAUDE.md`, `../docs/maintenance.md`.

## Целевой стенд

- Машина: `alex@195.239.217.102 -p 5222` (sudo, группа `docker`, docker без sudo).
  Ubuntu 26.04, Docker/Compose, **PostgreSQL 18 на хосте** (порт 5432 занят).
  Docker Hub недоступен → зеркало `mirror.gcr.io`, `ghcr.io` напрямую. За NAT.
  Полный доступ — `IT_strategy_usprings/DevOps/server_access.md`.
- **Внешний порт `5285`** (зарезервирован; см. корневой `README.md`). По схеме
  соседей `5281–5284` → host-порт **`8085`**. Внешние порты пробрасывает провайдер
  (DNAT), лид-тайм большой — **запросить проброс `5285`→`8085` заранее**.
- Размещение и деплой как у соседей: каталог `/home/alex/usprings_rag`, клон по
  **GitLab Deploy Token** (не личный PAT). Обновление — `git fetch` +
  `git reset --hard origin/main` (не редактировать tracked-файлы на сервере;
  секреты — только в gitignored `.env`). Эталон — `../../usprings_ncr/infra/README.md`.
- Общий план и распределение портов — `claude_other_docs/usprings_staging_production_plan.md`.

## Специфика rag (отличия от соседей — учесть до деплоя)

1. **Веса BGE-m3 (2,3 ГБ)** монтируются из HF-кэша хоста (`~/.cache/huggingface`,
   см. `../docker-compose.yml`). На сервере кэша нет → первый старт уйдёт в
   многочасовую перекачку с Hugging Face. **Прогреть кэш заранее** (скачать модель
   на сервере или скопировать кэш), иначе стенд «висит» на старте.
2. **Корпус инструкций не в git** (`docs/manuals/**/*.pdf` в `.gitignore`, ~766 МБ).
   `git reset --hard` его не принесёт. Скопировать на сервер отдельно (scp/rsync),
   затем `docker compose run --rm app ingest --collection erp` и `--collection zup`.
3. **`.env` (gitignored) — заполнить реальными секретами:** `OPENROUTER_API_KEY`
   (платный тир), `SECRET_KEY` (длинная случайная строка — иначе рестарт разлогинит
   всех), `SUPERADMIN_LOGIN`/`SUPERADMIN_PASSWORD` (бутстрап при первом старте).
   Образец и пояснения — `../.env.example`.
4. **Конфликт порта БД:** compose публикует `${POSTGRES_PORT}:5432`, а на хосте уже
   PG18 на 5432. Задать свободный `POSTGRES_PORT` либо не публиковать порт БД наружу
   (внутри сети приложение ходит на `db:5432`). БД проекта — контейнер
   `pgvector/pgvector:pg16` (нужно расширение pgvector), не хостовый PG18.
5. **Нет staging-оверрайда.** Создать `docker-compose.staging.yml` (или `deploy.sh`)
   по образцу соседей: маппинг host-порт `8085`, `restart: unless-stopped`,
   продовые значения `.env`. **Аутентификация уже в приложении** (логин super-admin,
   как у hr/crm/pps) — отдельный Basic Auth, как у mms/ncr, вероятно не нужен;
   решить с владельцем.
6. **Ресурсы:** BGE-m3 в памяти тяжелее соседей; свериться со свободной RAM
   (проверка ресурсов — в плане staging). Старт портала — десятки секунд (прогрев
   весов), готовность по строке `Uvicorn running` в `docker compose logs`.

## CI/CD по аналогии

Пайплайна пока нет (`.gitlab-ci.yml` отсутствует). Эталон — `../../usprings_ncr/.gitlab-ci.yml`
(стадии **lint → test → build:images**, workflow на MR/`main`/тег, registry
`gtl.usteel.ru:5050`, dind для сборки, теги `<short-sha>` + `latest`). Отличия rag:

- **Один Python-сервис** (без отдельных `backend/`/`frontend/`): один job на lint
  (`uv run ruff check .`) и test (`uv run pytest -q`); фронтенд серверный, отдельной
  node-стадии не надо.
- **Сервис БД в тестах — `pgvector/pgvector:pg16`** (не голый `postgres:16`): нужен
  pgvector. Прокинуть `TEST_DATABASE_URL`/миграции.
- **РИСК теста в CI:** проверить, тянет ли `pytest` веса BGE-m3 (2,3 ГБ) или сеть —
  в раннере это недопустимо. Если да — мокать эмбеддер / маркировать тяжёлые тесты.
  Прогнать `uv run --no-sync pytest -q` и посмотреть зависимости до написания job.
- **build:images:** один образ `$CI_REGISTRY_IMAGE:$CI_COMMIT_SHORT_SHA` (+`latest`
  на `main`). Раннер — общий docker-executor на `195.239.217.102`.
- После первого зелёного пайплайна включить push-mirror уже активен (GitHub-бэкап
  создан), CI отдельно синхронизировать не нужно.

## Открытые решения — уточнить у владельца

- Нужен ли внешний Basic Auth поверх портального логина (см. п.5).
- Загрузка корпуса: разовый scp + ingest на сервере vs. заливка PDF из UI.
- Zero-data-retention у OpenRouter перед выходом за пилот (см. `../docs/open-questions.md`).

## Ключевые файлы

- `../docker-compose.yml`, `../.env.example`, `../Dockerfile` — текущая сборка.
- `../../usprings_ncr/infra/README.md` — эталон деплоя (fetch + reset --hard, Basic Auth, бэкап).
- `../../usprings_ncr/.gitlab-ci.yml` — эталон пайплайна.
- `claude_other_docs/usprings_staging_production_plan.md` — общий план, порты, deploy-token.
- `IT_strategy_usprings/DevOps/server_access.md` — доступ к серверу.
