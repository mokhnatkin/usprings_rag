# Образ приложения USprings RAG (FastAPI + BGE-m3 + ingest CLI).
#
# torch ставим отдельным слоем CPU-колесом: дефолтная установка на Linux тянет
# CUDA-сборку на несколько ГБ, бесполезную без GPU. Слой кэшируется - при правках
# кода torch не перекачивается.
#
# Веса BGE-m3 (2,3 ГБ) в образ не кладём - монтируется HF-кэш хоста (см. compose).

FROM python:3.12-slim

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

# Миграции перед стартом портала: к этому моменту db уже healthy (см. compose).
CMD ["sh", "-c", "alembic upgrade head && uvicorn usprings_rag.api:app --host 0.0.0.0 --port 8000"]
