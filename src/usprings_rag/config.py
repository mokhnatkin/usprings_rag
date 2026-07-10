"""Настройки приложения из окружения (.env). Единый источник конфигурации."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Значения читаются из переменных окружения / .env (регистр не важен)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # OpenRouter (LLM)
    openrouter_api_key: str = ""
    openrouter_model: str = "qwen/qwen3-next-80b-a3b-instruct:free"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    # База данных (обязательный ключ, без дефолта - падаем при отсутствии)
    database_url: str

    # Эмбеддинги
    embedding_model: str = "BAAI/bge-m3"
    embedding_dim: int = 1024

    # Чанкинг и поиск (стартовые значения, калибруются на прототипе)
    chunk_max_tokens: int = 512
    chunk_overlap: int = 64
    top_k: int = 5
    similarity_threshold: float = 0.5

    # Генерация ответа
    llm_temperature: float = 0.1
    llm_max_tokens: int = 800


settings = Settings()
