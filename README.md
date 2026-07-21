# USprings RAG

Внутреннее хранилище инструкций и ИИ-ассистент для группы заводов УПЗ.

Пилот развёртывается для пользователей ИТ-процессов (в первую очередь 1С ERP):
в базу знаний загружаются инструкции, по которым чаще всего задают вопросы,
проводится их векторизация, а пользователь через веб-портал получает от ИИ-ассистента
ответ о том, как выполнить ту или иную операцию. Ответы генерируются через OpenRouter
с опорой на найденные документы (RAG).

Проект соответствует треку **B6 «RAG-базы знаний»** ИТ-стратегии группы (2026-2029).

## Возможности (целевые)

- Портал с ИИ-ассистентом: вопрос -> поиск по базе знаний -> ответ с опорой на инструкции.
- Белый список разрешённых вопросов: ответ выдаётся только при наличии релевантного
  документа выше порога сходства, иначе - вежливый отказ.
- Админский модуль: загрузка новых инструкций, их обработка и добавление в базу знаний.
- Расширяемость: позже подключается документация других служб и процессов
  (другие ИТ-системы, техническая дирекция и др.).

## Стек

- Бэкенд: Python + FastAPI
- Векторное хранилище: PostgreSQL + pgvector
- Эмбеддинги: локальная multilingual-модель (BGE-m3), on-premise
- Фронтенд: простой (серверные шаблоны / HTML+JS), с возможностью нарастить до React
- LLM: OpenRouter
- Инфраструктура: Docker Compose, on-premise

## Статус

MVP собран (этапы 1-8): окружение, схема БД с миграциями (SQLAlchemy + Alembic),
ingest-пайплайн (парсинг PDF, чанкинг, эмбеддинги BGE-m3, запись в БД), семантический
поиск с порогом сходства, генерация ответа через OpenRouter (модель выбрана по A/B:
`qwen/qwen3-next-80b-a3b-instruct`), веб-портал со стримингом ответа и раздачей
исходных PDF, упаковка в Docker Compose. Журнал -
в [`docs/MVP/MVP0/mvp-dev-plan-progress.md`](docs/MVP/MVP0/mvp-dev-plan-progress.md).

**MVP0 завершён:** база знаний разделена на **коллекции** по продуктам -
`erp` (1С:ERP) и `zup` (1С:ЗУП). Пользователь выбирает коллекцию до вопроса,
поиск идёт только по ней: таблица чанков секционирована по коллекции, поэтому
соседние базы не участвуют в обходе индекса. Инструкции лежат по папкам
`docs/manuals/its_erp/` и `docs/manuals/its_zup/` (папка = коллекция).

**MVP1 завершён (этапы 1-12):** многопользовательский портал. Коллекции переехали в
БД; внутренние учётки + cookie-сессии + bootstrap super-admin; три роли
(`user`/`collection_admin`/`super_admin`) и доступ к коллекциям; журнал `query_log`
(полный текст, токены, диагностика) + обратная связь + история; загрузка PDF из UI с
фоновой индексацией и soft-delete (архивация) инструкций; справочники пользователей и
коллекций, журнал вопросов-ответов, аналитика и калибровка порогов из UI (super-admin);
навигация по ролям с серверной защитой; упаковка в Docker (миграции и воркер
индексации стартуют в контейнере). Направления - в
[`docs/MVP/MVP1/backlog.md`](docs/MVP/MVP1/backlog.md), статус по этапам -
в [`docs/MVP/MVP1/mvp-dev-plan-progress.md`](docs/MVP/MVP1/mvp-dev-plan-progress.md).

Качество измерено на корпусе ИТС (eval-наборы по каждой коллекции, 2026-07-14):
прямые вопросы находят нужный документ первым - **recall@1 = 7/7 (ERP) и 8/8 (ЗУП)**;
разговорные формулировки слабее (2/5 и 3/5) - они уходят в соседние главы книги ИТС.
Пороги: **`erp` = 0.58, `zup` = 0.55** (порог - свойство коллекции, не глобальная
константа; хранится в таблице `collections` и правится super-admin из UI без деплоя,
`src/usprings_rag/collection.py` - read-model поверх БД). Прежние `recall@1 = 17/17`
и порог 0.53 относились к удалённому тестовому корпусу и **недействительны**.

## Запуск (Docker Compose)

Требуется: Docker Desktop, заполненный `.env` (скопировать с `.env.example`, вписать
`OPENROUTER_API_KEY` - ключ получить на <https://openrouter.ai/keys>; задать
`SECRET_KEY` и `SUPERADMIN_LOGIN`/`SUPERADMIN_PASSWORD` - см. ниже про вход).

```bash
docker compose up -d                                    # БД + приложение (миграции на старте)
docker compose run --rm app ingest --collection erp     # наполнить коллекцию 1С:ERP
docker compose run --rm app ingest --collection zup     # наполнить коллекцию 1С:ЗУП
```

Ingest идемпотентен и грузит папку коллекции (`--collection erp` -> `docs/manuals/its_erp/`).

Портал открывается на <http://localhost:8000>. **Вход обязателен** (MVP1): при первом
старте с пустой таблицей пользователей создаётся super-admin из `SUPERADMIN_LOGIN` и
`SUPERADMIN_PASSWORD` (`.env`); `SECRET_KEY` подписывает cookie-сессии (пустой - dev-режим
с эфемерным ключом). После входа: вопрос -> ответ со ссылками на исходные инструкции
(текст печатается по мере генерации) либо вежливый отказ, если релевантной инструкции нет.

**Управление из UI** (super-admin/collection-admin): загрузка отдельных PDF с фоновой
индексацией и архивация инструкций - `/admin/documents`; пользователи и коллекции -
`/admin/users`, `/admin/collections`; калибровка и правка порогов - `/admin/calibration`;
журнал и аналитика - `/admin/logs`, `/admin/analytics`. Массовая заливка корпуса
остаётся за CLI-ingest (выше). Порог сходства правится из UI без деплоя (свойство
коллекции, не `.env`).

Нюансы:

- **Веса BGE-m3 (2,3 ГБ)**: в контейнер монтируется HF-кэш хоста
  (`~/.cache/huggingface`). Если модель на хосте ещё не скачана, первый старт уйдёт
  в загрузку с Hugging Face (на медленном канале - часы), дальше кэш переживает
  пересборки образа.
- **Старт приложения** занимает десятки секунд (загрузка весов, прогрев в lifespan) -
  готовность видна в `docker compose logs app` по строке `Uvicorn running`.
- **Калибруемые параметры** (`TOP_K`, `CHUNK_*`, `OPENROUTER_MODEL`, ...) - в `.env`,
  описание - в `.env.example`; когда и что перекалибровывать - `docs/maintenance.md`.
  **Порог сходства - не в `.env`**: он свой у каждой коллекции, хранится в таблице
  `collections` и правится super-admin из UI (`collection.py` - read-model поверх БД).
- Порт БД наружу пробрасывается как `${POSTGRES_PORT}` (нужен только для локальной
  разработки; внутри compose-сети приложение ходит на `db:5432`).

## Локальный запуск без Docker (разработка)

Требуется: `uv`, Docker Desktop (для БД), заполненный `.env`.

```bash
docker compose up -d db                          # только БД (Postgres + pgvector)
uv run --no-sync alembic upgrade head            # миграции схемы
uv run --no-sync ingest --collection erp         # наполнить коллекцию (erp | zup)
uv run --no-sync uvicorn usprings_rag.api:app    # портал -> http://127.0.0.1:8000
```

Полезное:

```bash
uv run --no-sync search "вопрос" --collection erp  # выдача поиска по коллекции, без LLM
uv run --no-sync python eval/run_eval.py         # recall@k и распределения сходств
uv run --no-sync python eval/run_answers.py      # прогон вопросов через полный сценарий
uv run --no-sync pytest -q                       # тесты

# A/B-сравнение LLM (регламент - docs/maintenance.md, раздел 5): секунды, не часы
uv run --no-sync python eval/build_contexts.py                       # кэш поиска, 1 раз
uv run --no-sync python eval/run_ab.py <модель-A> <модель-B>         # -> eval/ab-report.md
```

Особенности запуска (проверено на Windows):

- **Всегда `--no-sync`.** Без него `uv run` делает авто-sync и уходит перекачивать torch
  (несколько ГБ).
- **Русский вывод CLI** - с `PYTHONUTF8=1` (иначе `UnicodeEncodeError` при
  перенаправлении вывода в файл).
- **Первый старт портала - десятки секунд**: грузятся веса BGE-m3 (2,3 ГБ, прогрев в
  lifespan-хуке). Дальше запросы идут быстро.
- Если команда падает с «файл занят» или пропала точка входа (`ingest`/`search`) -
  проверить незавершившиеся процессы:
  `Get-Process | Where-Object { $_.Path -like "*usprings_rag*" } | Stop-Process -Force`,
  затем `uv pip install -e . --no-deps` (перегенерирует консольные скрипты).
- **Python 3.14 и в образе, и на dev** (выровнено 2026-07-15: `Dockerfile` -
  `python:3.14-slim`, `pyproject` - `>=3.14`). При смене базового образа проверять
  импорт в контейнере после правок аннотаций/импортов:
  `docker compose build app && docker compose run --rm --no-deps --entrypoint sh app
  -c "python -c 'import usprings_rag.api'"`. Подробнее - `docs/maintenance.md`, раздел 9.

## Staging (тестовый стенд)

Развёрнут на общей тестовой машине УПЗ `195.239.217.102` (2026-07-15): каталог
`/home/alex/usprings_rag`, стек `docker-compose.staging.yml` (host-порт **8085**,
внешний `http://195.239.217.102:5285` через DNAT провайдера). Корпус ERP+ЗУП
проиндексирован (413 и 194 документа). Обновление - `git fetch --prune origin &&
git reset --hard origin/main && docker compose -f docker-compose.staging.yml up -d --build`
(секреты - только в gitignored `.env`, tracked-файлы на сервере не править).

Стенд работоспособен полностью (проверено 2026-07-21): исходящий доступ к
`openrouter.ai` открыт по заявке в ИТ/провайдер, генерация ответов LLM работает;
внешний DNAT `5285→8085` заведён, портал доступен снаружи.

Детали и runbook - [`staging/deployment-plan.md`](staging/deployment-plan.md) и
[`staging/README.md`](staging/README.md); эксплуатация - `docs/maintenance.md`, раздел 10.

## Документация

Проектная документация - в каталоге [`docs/`](docs/):

- [`docs/overview.md`](docs/overview.md) - обзор проекта и архитектура пилота
- [`docs/how-it-works.md`](docs/how-it-works.md) - механика работы базы знаний (RAG) для команды
- [`docs/open-questions.md`](docs/open-questions.md) - открытые вопросы и решения
- [`docs/maintenance.md`](docs/maintenance.md) - регламенты развития (новые инструкции и подразделения, калибровка порога, мониторинг)
- [`docs/MVP/MVP0/mvp-plan.md`](docs/MVP/MVP0/mvp-plan.md) - план развёртывания MVP0 (стек и этапы)
- [`docs/MVP/MVP0/mvp-dev-plan.md`](docs/MVP/MVP0/mvp-dev-plan.md) - детальный план разработки MVP0
- [`docs/MVP/MVP1/backlog.md`](docs/MVP/MVP1/backlog.md) - направления MVP1 (аутентификация, роли, логирование, история, админка)
- [`docs/MVP/MVP1/mvp-dev-plan.md`](docs/MVP/MVP1/mvp-dev-plan.md) - детальный план разработки MVP1
- [`docs/MVP/MVP1/mvp-dev-plan-progress.md`](docs/MVP/MVP1/mvp-dev-plan-progress.md) - журнал прогресса MVP1

## Репозиторий

- Источник правды (`origin`): GitLab - https://gtl.usteel.ru/usprings/usprings_rag
- Бэкап-зеркало: GitHub - https://github.com/mokhnatkin/usprings_rag.git
  (создан; GitLab push-mirror активен - GitHub синхронизируется автоматически)
- Ветка: `main`.
