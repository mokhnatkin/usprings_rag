"""Окружение Alembic: метаданные из моделей, URL из настроек приложения."""

from logging.config import fileConfig

from alembic import context

from usprings_rag.config import settings
from usprings_rag.db import engine
from usprings_rag.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def include_object(obj, name, type_, reflected, compare_to) -> bool:
    """Не считать секции chunks_<code> изменениями схемы.

    Таблица chunks секционирована PARTITION BY LIST (миграция 0003); дочерние
    секции (chunks_erp, chunks_zup, ...) создаёт ingest под каждую коллекцию,
    в ORM-моделях их нет. Без этого фильтра autogenerate на каждую секцию
    предлагает drop_table - ложный дифф, который засорял бы будущие миграции.
    """
    if type_ == "table" and reflected and name.startswith("chunks_"):
        return False
    if type_ == "index" and name.startswith("chunks_") and name.endswith("_idx"):
        return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
