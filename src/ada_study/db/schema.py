"""Esquema PostgreSQL del catálogo documental (ADR-0001, rev. 3).

SQLAlchemy Core async + asyncpg (decisión aprobada en TASK-02).
Tablas: documents, document_versions, document_chunks, ingestion_jobs,
pending_index_ops. Versiones inmutables; outbox por destino.
"""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID

metadata = MetaData()

# Estados editoriales válidos (ADR-0005)
EDITORIAL_VALUES = (
    "draft",
    "pending_review",
    "published_pending_review",
    "published",
    "rejected",
    "withdrawn",
    "deleted",
)

# Estados técnicos de versión válidos (ADR-0005)
VERSION_STATUS_VALUES = (
    "queued",
    "processing",
    "indexing",
    "available",
    "failed",
    "deleting",
)

# Estados de job de ingesta (ADR-0005)
JOB_STATUS_VALUES = ("queued", "running", "done", "failed")

# Estados de operación del outbox
PENDING_OP_STATUS_VALUES = ("pending", "done", "failed")

# Destinos y operaciones del outbox (ADR-0001/0004)
DESTINATION_VALUES = ("pinecone", "bm25")
OPERATION_VALUES = ("index", "delete", "reconcile")


def _check_in(column_name: str, values: tuple[str, ...]) -> str:
    """Genera la expresión CHECK IN para una lista de valores."""
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column_name} IN ({quoted})"


documents = Table(
    "documents",
    metadata,
    Column("document_id", String(64), primary_key=True),
    Column("filename", String(512), nullable=False),
    Column("subject", String(256), nullable=True),
    Column("active_version_id", String(64), nullable=True),
    Column("editorial_status", String(32), nullable=False, server_default="draft"),
    Column("review_status", String(32), nullable=False, server_default="not_required"),
    Column("uploaded_by", String(64), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("deleted_at", DateTime(timezone=True), nullable=True),
    # Claim de huella (ADR-0001 rev. 3): sha256 que el documento hace valer
    # mientras está vivo (`deleted_at IS NULL`). Se fija en el registro de la
    # primera versión y se mueve en la MISMA transacción que cada alta de
    # versión (restauración). `deleted_at` libera el claim; `withdrawn` y las
    # versiones `failed`/`queued` lo RETIENEN (el dedup no depende del estado
    # técnico de la versión). NULL solo en bases heredadas pendientes de
    # backfill (ver `db/migrations.py`).
    Column("sha256", String(64), nullable=True),
    CheckConstraint(_check_in("editorial_status", EDITORIAL_VALUES), name="ck_documents_editorial"),
    CheckConstraint(
        _check_in(
            "review_status",
            ("not_required", "pending", "approved", "rejected", "needs_changes"),
        ),
        name="ck_documents_review",
    ),
)

document_versions = Table(
    "document_versions",
    metadata,
    Column("version_id", String(64), primary_key=True),
    Column("document_id", String(64), nullable=False),
    Column("version_number", Integer, nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("storage_key", String(128), nullable=False),
    Column("content_type", String(128), nullable=False),
    Column("size_bytes", BigInteger, nullable=False),
    Column("status", String(32), nullable=False, server_default="queued"),
    Column("page_count", Integer, nullable=True),
    Column("error_code", String(64), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(_check_in("status", VERSION_STATUS_VALUES), name="ck_versions_status"),
    CheckConstraint("size_bytes >= 0", name="ck_versions_size"),
)

# Dedup (ADR-0001 rev. 3): la unicidad de huella vive a nivel DOCUMENTO.
# La rev. 2 usaba un índice parcial sobre `document_versions.sha256` con
# predicado `document_id IN (SELECT ...)`; PostgreSQL no soporta subconsultas
# en predicados de índice (`cannot use subquery in index predicate`) y bloqueaba
# `metadata.create_all`. Además era semánticamente incorrecto: las versiones
# históricas de un mismo documento comparten huella legítimamente (restauración)
# y el claim debe existir desde el registro, no desde la activación.
# Solución: claim desnormalizado en `documents.sha256` + índice único parcial
# sobre documentos vivos. La migración idempotente está en `db/migrations.py`.
Index(
    "ux_documents_active_sha256",
    documents.c.sha256,
    unique=True,
    postgresql_where=text("deleted_at IS NULL"),
)

# Unicidad de version_number por documento
Index(
    "ux_versions_document_number",
    document_versions.c.document_id,
    document_versions.c.version_number,
    unique=True,
)

document_chunks = Table(
    "document_chunks",
    metadata,
    Column("chunk_id", String(128), primary_key=True),
    Column("document_id", String(64), nullable=False),
    Column("version_id", String(64), nullable=False),
    Column("page", Integer, nullable=False),
    Column("chunk_index", Integer, nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("text", Text, nullable=False),
    Column("title", String(512), nullable=False),
    Column("source", String(512), nullable=False),
    CheckConstraint("page >= 1", name="ck_chunks_page"),
    CheckConstraint("chunk_index >= 0", name="ck_chunks_index"),
)

Index("ix_chunks_version", document_chunks.c.version_id)

ingestion_jobs = Table(
    "ingestion_jobs",
    metadata,
    Column("job_id", String(64), primary_key=True),
    Column("document_id", String(64), nullable=False),
    Column("version_id", String(64), nullable=True),
    Column("status", String(32), nullable=False, server_default="queued"),
    Column("error_code", String(64), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(_check_in("status", JOB_STATUS_VALUES), name="ck_jobs_status"),
)

Index("ix_jobs_document", ingestion_jobs.c.document_id)

pending_index_ops = Table(
    "pending_index_ops",
    metadata,
    Column("op_id", String(64), primary_key=True),
    Column("version_id", String(64), nullable=False),
    Column("destination", String(16), nullable=False),
    Column("operation", String(16), nullable=False),
    Column("status", String(16), nullable=False, server_default="pending"),
    Column("attempts", Integer, nullable=False, server_default=text("0")),
    Column("last_error_code", String(64), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(_check_in("destination", DESTINATION_VALUES), name="ck_ops_destination"),
    CheckConstraint(_check_in("operation", OPERATION_VALUES), name="ck_ops_operation"),
    CheckConstraint(_check_in("status", PENDING_OP_STATUS_VALUES), name="ck_ops_status"),
)

Index("ix_ops_version_status", pending_index_ops.c.version_id, pending_index_ops.c.status)

# ---------------------------------------------------------------------------
# Identidad, sesiones y auditoría (TASK-04, Spec 06 §35-44, §80-88)
# ---------------------------------------------------------------------------

ROLE_VALUES = ("student", "trusted_contributor", "moderator", "admin")
ACCOUNT_STATUS_VALUES = ("invited", "active", "suspended", "deactivated")
INVITATION_STATUS_VALUES = ("pending", "consumed", "revoked")
SESSION_STATUS_VALUES = ("active", "revoked")
AUDIT_RESULT_VALUES = ("ok", "denied", "error")

users = Table(
    "users",
    metadata,
    Column("user_id", String(64), primary_key=True),
    Column("email", String(320), nullable=False, unique=True),
    Column("display_name", String(256), nullable=False),
    Column("role", String(32), nullable=False, server_default="student"),
    Column("account_status", String(16), nullable=False, server_default="invited"),
    # Hash PBKDF2 "pbkdf2_sha256$iterations$salt_hex$hash_hex"; NULL hasta
    # que la cuenta se active (invitación consumida).
    Column("password_hash", String(512), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(_check_in("role", ROLE_VALUES), name="ck_users_role"),
    CheckConstraint(_check_in("account_status", ACCOUNT_STATUS_VALUES), name="ck_users_status"),
)

Index("ix_users_email", users.c.email, unique=True)

invitations = Table(
    "invitations",
    metadata,
    Column("invitation_id", String(64), primary_key=True),
    Column("email", String(320), nullable=False),
    Column("role", String(32), nullable=False, server_default="student"),
    # Solo el hash del token de invitación; el token en claro nunca se persiste.
    Column("token_hash", String(128), nullable=False),
    Column("status", String(16), nullable=False, server_default="pending"),
    Column("created_by", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("consumed_at", DateTime(timezone=True), nullable=True),
    Column("consumed_by", String(64), nullable=True),
    CheckConstraint(_check_in("role", ROLE_VALUES), name="ck_invitations_role"),
    CheckConstraint(_check_in("status", INVITATION_STATUS_VALUES), name="ck_invitations_status"),
)

Index("ix_invitations_token", invitations.c.token_hash, unique=True)

sessions = Table(
    "sessions",
    metadata,
    Column("session_id", String(64), primary_key=True),
    Column("user_id", String(64), nullable=False),
    # Solo el hash del token opaco; el token en claro vive solo con el cliente.
    Column("token_hash", String(128), nullable=False),
    Column("status", String(16), nullable=False, server_default="active"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("revoked_at", DateTime(timezone=True), nullable=True),
    CheckConstraint(_check_in("status", SESSION_STATUS_VALUES), name="ck_sessions_status"),
)

Index("ix_sessions_token", sessions.c.token_hash, unique=True)
Index("ix_sessions_user", sessions.c.user_id)

# Auditoría append-only (Spec 06 §81-88): la aplicación NUNCA emite
# UPDATE/DELETE sobre esta tabla; solo INSERT y SELECT.
audit_events = Table(
    "audit_events",
    metadata,
    Column("event_id", String(64), primary_key=True),
    Column("actor_id", String(64), nullable=True),
    Column("action", String(64), nullable=False),
    Column("entity_type", String(64), nullable=False),
    Column("entity_id", String(128), nullable=False),
    Column("version_id", String(64), nullable=True),
    Column("result", String(16), nullable=False, server_default="ok"),
    Column("reason", String(512), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(_check_in("result", AUDIT_RESULT_VALUES), name="ck_audit_result"),
)

Index("ix_audit_entity", audit_events.c.entity_type, audit_events.c.entity_id)
Index("ix_audit_created", audit_events.c.created_at)

# Conversaciones multi-turno (CHECKPOINTER-02/03). Una conversación agrupa
# los turnos de estudio de UN alumno; el `thread_id` del checkpointer se
# deriva del `conversation_id` (nunca lo elige el cliente). La tabla vive en
# la misma base del catálogo (ADR-0001: PostgreSQL única fuente de estado);
# las tablas de checkpoint (checkpoints/checkpoint_blobs/...) son gestionadas
# por `AsyncPostgresSaver.setup()`, no por este esquema.
conversations = Table(
    "conversations",
    metadata,
    Column("conversation_id", PGUUID(as_uuid=True), primary_key=True),
    Column("user_id", String(64), nullable=False),
    Column("thread_id", String(128), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("last_turn_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("turn_count", Integer, nullable=False, server_default="0"),
)

Index("ux_conversations_thread_id", conversations.c.thread_id, unique=True)
Index("ix_conversations_user", conversations.c.user_id)
