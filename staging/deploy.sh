#!/usr/bin/env bash
# Развёртывание / передеплой стека usprings_rag на staging-сервере УПЗ
# (195.239.217.102, /home/alex/usprings_rag, host 8085 -> внешний 5285).
# Запускается НА сервере (нужен Docker + склонированный репозиторий и .env).
#
# Шаги: sync с origin (fetch + reset --hard) -> сборка образа -> подъём db ->
# pg_dump -> alembic upgrade head -> up -d приложения и проверка.
#
# ПОЧЕМУ МИГРАЦИИ ЗДЕСЬ, А НЕ В ОБРАЗЕ. До 28.07.2026 `alembic upgrade head` был
# зашит в CMD Dockerfile, то есть выполнялся при каждом старте контейнера. У `app`
# стоит `restart: unless-stopped`, поэтому перезагрузка хоста накатывала pending-
# миграции сама — ночью и без наблюдения. По групповой политике
# (`usprings/devops_toolkit` → `docs/db_migrations_policy.md`) миграции — явный шаг
# деплоя, перед которым снимается дамп; неудачный дамп ПРЕРЫВАЕТ деплой.
#
# СЛЕДСТВИЕ: ручной `docker compose up -d` в обход этого скрипта схему НЕ обновляет.
#
# КОРПУС PDF ЭТОТ СКРИПТ НЕ ТРОГАЕТ. `docs/manuals/**` не в git (~766 МБ), заливается
# отдельно scp + `ingest`. Дамп покрывает БД, включая эмбеддинги; сами PDF бэкапятся
# отдельно. См. docs/maintenance.md.
#
# Использование (из корня репозитория):
#   bash staging/deploy.sh                        # sync main, build, dump, migrate, check
#   RAG_BRANCH=feature/x bash staging/deploy.sh
#   RAG_SKIP_PULL=1 bash staging/deploy.sh        # деплой уже выкаченного кода
#   RAG_SKIP_BACKUP=1 bash staging/deploy.sh      # без дампа (только осознанно, на стенде)
#   RAG_DIR=/home/alex/usprings_rag bash staging/deploy.sh
#
# pipefail обязателен: без него упавший `pg_dump | gzip` вернёт код успеха, и деплой
# продолжится с обрезанным дампом — страховка будет выглядеть сработавшей, не будучи ею.
set -euo pipefail

# Корень репозитория берём от расположения скрипта, чтобы cwd не имел значения.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${RAG_DIR:-$(dirname "$SCRIPT_DIR")}"
BRANCH="${RAG_BRANCH:-main}"
COMPOSE_FILE="docker-compose.staging.yml"
ENV_FILE=".env"
BACKUP_DIR="${RAG_BACKUP_DIR:-$HOME/backups}"
# Дамп rag тяжелее соседских: в БД лежат эмбеддинги 607 документов. Держим меньше копий.
BACKUP_KEEP="${RAG_BACKUP_KEEP:-7}"
HEALTH_URL="${RAG_HEALTH_URL:-http://localhost:8085/login}"

cd "$REPO_DIR"
echo ">>> RAG staging deploy — repo: $REPO_DIR, branch: $BRANCH"

if [ ! -f "$ENV_FILE" ]; then
  echo "!!! нет файла $ENV_FILE в $REPO_DIR — создайте его (см. .env.example)" >&2
  exit 1
fi

compose() { docker compose -f "$COMPOSE_FILE" "$@"; }

# Значение переменной из .env с запасным вариантом.
env_get() {
  local val
  val="$(grep -E "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2- | tr -d '"' | tr -d "'")" || true
  echo "${val:-$2}"
}

# 1. Обновляем исходники (если не пропущено): дерево сервера = зеркало origin.
if [ "${RAG_SKIP_PULL:-0}" = "1" ]; then
  echo ">>> [1/6] skip pull (RAG_SKIP_PULL=1), деплой текущего чекаута"
else
  echo ">>> [1/6] git fetch + reset --hard origin/$BRANCH"
  git fetch --prune origin
  # Только tracked-файлы: untracked (корпус PDF, .env) переживают reset --hard.
  dirty="$(git status --porcelain --untracked-files=no)"
  [ -n "$dirty" ] && { echo ">>> [!] локальные правки tracked-файлов будут отброшены:"; echo "$dirty"; }
  git reset --hard "origin/$BRANCH"
fi

# 2. Собираем образ, но стек НЕ поднимаем: сначала дамп и миграции (шаги 4-5).
echo ">>> [2/6] docker compose build"
compose build

# 3. Поднимаем только БД и ждём healthcheck: снять дамп и мигрировать без неё нельзя,
#    а app до миграций подниматься не должен.
echo ">>> [3/6] docker compose up -d db (ждём healthy)"
compose up -d --wait db

# 4. Дамп ДО того, как что-либо способно изменить схему (миграции — шаг 5).
if [ "${RAG_SKIP_BACKUP:-0}" = "1" ]; then
  echo ">>> [4/6] skip backup (RAG_SKIP_BACKUP=1)"
else
  echo ">>> [4/6] pg_dump перед миграциями (с эмбеддингами — небыстро)"
  db_user="$(env_get POSTGRES_USER usprings)"
  db_name="$(env_get POSTGRES_DB usprings_rag)"
  mkdir -p "$BACKUP_DIR"
  dump="$BACKUP_DIR/${db_name}_before_deploy_$(date +%Y%m%d_%H%M%S).sql.gz"
  compose exec -T db pg_dump -U "$db_user" -d "$db_name" | gzip >"$dump"

  # Две проверки, обе про целостность, а не про факт создания файла.
  # gzip -t ловит оборванный поток; хвостовой маркер pg_dump — дамп, который
  # заархивировался целиком, но был выгружен не до конца.
  gzip -t "$dump" || { echo "!!! дамп повреждён: $dump" >&2; exit 1; }
  if ! gzip -dc "$dump" | tail -5 | grep -q 'PostgreSQL database dump complete'; then
    echo "!!! в дампе нет маркера завершения — выгрузка неполная: $dump" >&2
    exit 1
  fi
  echo ">>> дамп: $dump ($(du -h "$dump" | cut -f1))"

  # Ретенция: дамп на каждый деплой иначе однажды доест диск.
  ls -1t "$BACKUP_DIR"/${db_name}_before_deploy_*.sql.gz 2>/dev/null \
    | tail -n "+$((BACKUP_KEEP + 1))" | xargs -r rm -f
fi

# 5. Миграции — явным шагом. В образе нет ENTRYPOINT, только CMD, поэтому команда
#    разового контейнера подменяется без --entrypoint. --no-deps: db поднята шагом 3.
#    Контейнер запускает alembic, а не приложение, — веса BGE-m3 не грузятся.
echo ">>> [5/6] alembic upgrade head"
compose run --rm --no-deps app alembic upgrade head

# 6. Поднимаем приложение и ждём готовности.
echo ">>> [6/6] docker compose up -d + проверка $HEALTH_URL"
compose up -d

# Старт портала — десятки секунд: прогреваются веса BGE-m3. Поэтому ждём до 5 минут,
# а не «проверяем сразу»: проверка через секунду после `up -d` ничего не доказывает —
# контейнер, который упадёт, ещё не успел упасть.
ok=0
for _ in $(seq 1 60); do
  if curl -fsS -o /dev/null "$HEALTH_URL"; then
    ok=1
    break
  fi
  sleep 5
done

if [ "$ok" != "1" ]; then
  echo "!!! портал не поднялся за 5 минут — последние логи app:" >&2
  compose logs --tail 60 app >&2 || true
  exit 1
fi

echo ">>> портал OK: $HEALTH_URL отвечает"
compose ps
echo ">>> deploy done."
