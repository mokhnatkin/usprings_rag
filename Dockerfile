# Образ приложения USprings RAG (FastAPI + BGE-m3 + ingest CLI).
#
# torch ставим отдельным слоем CPU-колесом: дефолтная установка на Linux тянет
# CUDA-сборку на несколько ГБ, бесполезную без GPU. Слой кэшируется - при правках
# кода torch не перекачивается.
#
# Веса BGE-m3 (2,3 ГБ) в образ не кладём - монтируется HF-кэш хоста (см. compose).

FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN pip install torch --index-url https://download.pytorch.org/whl/cpu

COPY pyproject.toml ./
COPY src ./src
RUN pip install .

COPY alembic.ini ./
COPY alembic ./alembic
# Golden-наборы вопросов - нужны калибровке порогов из UI (этап 9 MVP1).
COPY eval ./eval

EXPOSE 8000

# МИГРАЦИЙ ЗДЕСЬ НЕТ НАМЕРЕННО. До 28.07.2026 CMD был
#   sh -c "alembic upgrade head && uvicorn ..."
# то есть схема менялась при каждом старте контейнера, в любой среде, и снималось
# это только пересборкой образа. При `restart: unless-stopped` на стенде это значило,
# что перезагрузка хоста накатывала pending-миграции сама, ночью и без наблюдения.
# По групповой политике (`usprings/devops_toolkit` → `docs/db_migrations_policy.md`)
# миграции — явный шаг деплоя, перед которым снимается дамп; неудачный дамп деплой
# прерывает. Применяет их `staging/deploy.sh`, шаги 4 и 5.
#
# alembic.ini и alembic/ остаются в образе намеренно: команду выполняет разовый
# контейнер из этого же образа.
CMD ["uvicorn", "usprings_rag.api:app", "--host", "0.0.0.0", "--port", "8000"]
