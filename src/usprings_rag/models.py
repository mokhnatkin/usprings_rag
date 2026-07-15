"""ORM-модели: пользователи, справочник коллекций, документы и их чанки."""

from datetime import datetime
from enum import StrEnum

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Sequence,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

EMBEDDING_DIM = 1024


class Base(DeclarativeBase):
    pass


class Role(StrEnum):
    """Роль пользователя. Одна на учётку.

    USER - вопросы по доступным коллекциям; COLLECTION_ADMIN - плюс управление
    своими коллекциями; SUPER_ADMIN - управление порталом (см. MVP1 backlog).
    """

    USER = "user"
    COLLECTION_ADMIN = "collection_admin"
    SUPER_ADMIN = "super_admin"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    login: Mapped[str] = mapped_column(Text, unique=True)
    full_name: Mapped[str] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text, nullable=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text)  # значения - из Role
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class UserCollectionAccess(Base):
    """Доступ пользователя к коллекции.

    Для роли USER - право задавать вопросы; для COLLECTION_ADMIN - право
    администрировать коллекцию (и спрашивать по ней). SUPER_ADMIN грантов не
    требует (видит всё). Составной PK (user_id, collection_id) - без дублей.
    """

    __tablename__ = "user_collection_access"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    collection_id: Mapped[int] = mapped_column(
        ForeignKey("collections.id", ondelete="CASCADE"), primary_key=True
    )


class QueryLog(Base):
    """Журнал вопросов-ответов: привязка к пользователю и коллекции, полный текст,
    расход токенов, диагностика (лучшее сходство, найденные документы, модель) и
    обратная связь. Полный текст - для истории и переиспользования жалоб в eval;
    усечение для списков - на стороне выборки (QUERY_LOG_PREVIEW_CHARS)."""

    __tablename__ = "query_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    collection_id: Mapped[int] = mapped_column(ForeignKey("collections.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(Text)  # answered | refused
    best_similarity: Mapped[float] = mapped_column(Float)
    prompt_tokens: Mapped[int] = mapped_column(Integer)
    completion_tokens: Mapped[int] = mapped_column(Integer)
    total_tokens: Mapped[int] = mapped_column(Integer)
    elapsed_seconds: Mapped[float] = mapped_column(Float)
    model_id: Mapped[str] = mapped_column(Text)
    sources: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    feedback: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    feedback_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    feedback_comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("query_log_user_created_idx", "user_id", "created_at"),
        Index("query_log_collection_created_idx", "collection_id", "created_at"),
    )


class CollectionRow(Base):
    """Справочник коллекций (баз знаний) - источник истины вместо enum.

    `code` - стабильный строковый идентификатор: им же назван раздел секции
    `chunks` (`chunks_erp`, ...), поэтому менять его нельзя (см. миграцию 0003 и
    docs/MVP/MVP1/mvp-dev-plan.md, этап 1). Порог сходства - свойство коллекции,
    правится super-admin из UI без деплоя.
    """

    __tablename__ = "collections"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(Text, unique=True)
    title: Mapped[str] = mapped_column(Text)
    folder: Mapped[str] = mapped_column(Text)
    threshold: Mapped[float] = mapped_column(Float)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    collection: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    source_path: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # Soft-delete: непусто = документ неактуален, исключается из поиска, но остаётся
    # в БД для аудита и старых логов. Возврат из архива - обнулением archived_at.
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    archived_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )

    chunks: Mapped[list["Chunk"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        Index("documents_collection_idx", "collection"),
        Index("documents_archived_idx", "archived_at"),
    )


class Chunk(Base):
    """Чанк документа. Таблица секционирована по `collection` (PARTITION BY LIST).

    `collection` денормализована из документа: фильтр обязан стоять на той же
    таблице, где вектор, иначе HNSW-индекс отработает до фильтра (см. миграцию
    0003). Ключ секционирования обязан входить в PK - отсюда составной ключ.
    """

    __tablename__ = "chunks"

    id: Mapped[int] = mapped_column(
        Integer, Sequence("chunks_id_seq"), primary_key=True, autoincrement=True
    )
    collection: Mapped[str] = mapped_column(Text, primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE")
    )
    chunk_index: Mapped[int]
    page_from: Mapped[int | None]
    page_to: Mapped[int | None]
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM))

    document: Mapped[Document] = relationship(back_populates="chunks")

    __table_args__ = (
        Index(
            "chunks_embedding_hnsw_idx",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        {"postgresql_partition_by": "LIST (collection)"},
    )


class IndexJobStatus(StrEnum):
    """Стадии фоновой индексации загруженного PDF."""

    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"


class IndexJob(Base):
    """Задача фоновой индексации одного PDF.

    Загрузка файла из UI ставит задачу (`queued`); воркер в процессе приложения
    берёт её, гоняет single-file ingest в секцию коллекции и проставляет
    `done`/`error`. `document_id` заполняется после создания документа (`SET NULL`
    при переиндексации, когда старый документ удаляется). Устойчивость: «зависшие»
    `running` после аварийного рестарта помечаются `error` при старте воркера.
    """

    __tablename__ = "index_job"

    id: Mapped[int] = mapped_column(primary_key=True)
    collection_id: Mapped[int] = mapped_column(ForeignKey("collections.id"))
    document_id: Mapped[int | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL"), nullable=True
    )
    source_path: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)  # значения - из IndexJobStatus
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (Index("index_job_status_idx", "status", "created_at"),)
