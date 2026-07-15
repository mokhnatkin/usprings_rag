"""Настройки приложения из окружения (.env). Единый источник конфигурации."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Значения читаются из переменных окружения / .env (регистр не важен)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # OpenRouter (LLM). Модель выбрана по A/B (см. open-questions.md), платный
    # эндпоинт: :free держит общую очередь и часами отдаёт 429.
    openrouter_api_key: str = ""
    openrouter_model: str = "qwen/qwen3-next-80b-a3b-instruct"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    # База данных (обязательный ключ, без дефолта - падаем при отсутствии)
    database_url: str

    # Папка инструкций - корень для относительных source_path и раздачи PDF.
    # Пути в БД храним относительно неё (POSIX), чтобы они совпадали на хосте
    # и в Docker-контейнере.
    manuals_dir: str = "docs/manuals"

    # Эмбеддинги
    embedding_model: str = "BAAI/bge-m3"
    embedding_dim: int = 1024

    # Чанкинг и поиск (стартовые значения, калибруются на прототипе).
    # Порог сходства - НЕ здесь: он свойство коллекции (таблица collections,
    # читается через collection.py), правится super-admin из UI без деплоя.
    chunk_max_tokens: int = 512
    chunk_overlap: int = 64
    top_k: int = 5

    # Генерация ответа
    llm_temperature: float = 0.1
    llm_max_tokens: int = 800

    # Аутентификация и сессии
    # secret_key подписывает cookie сессии. Пусто - dev-режим: генерируем эфемерный
    # ключ при старте (перезапуск разлогинит всех). На проде задать явно.
    secret_key: str = ""
    session_cookie_name: str = "usprings_session"
    session_max_age: int = 86400  # сутки
    # Бутстрап super-admin при первом старте с пустой таблицей users.
    superadmin_login: str = ""
    superadmin_password: str = ""

    # Логирование и история: усечение вопроса/ответа в списках (в самой записи -
    # полный текст). Детальный просмотр показывает полный текст.
    query_log_preview_chars: int = 160

    # Фоновый воркер индексации загруженных PDF (в процессе приложения). Выключить
    # можно на инстансах, которые только отвечают и не должны индексировать.
    index_worker_enabled: bool = True

    # Golden-набор вопросов для калибровки порогов (относительно рабочей папки).
    eval_questions_file: str = "eval/questions.yaml"


settings = Settings()
