"""Contratos documentales de ADA Study (TASK-01 rev. 2, aprobados en TASK-02).

Fuente de requisitos: specs/01, specs/02, specs/06 y ADR-0001/0005.
Convenciones: Pydantic en los límites, base StrictModel (extra="forbid"),
estados en minúsculas, códigos de error estables sin texto de excepción.
"""

from typing import Literal

from pydantic import Field

from ada_study.strict_model import StrictModel

# ---------------------------------------------------------------------------
# Estados (ADR-0005)
# ---------------------------------------------------------------------------

EditorialStatus = Literal[
    "draft",
    "pending_review",
    "published_pending_review",
    "published",
    "rejected",
    "withdrawn",
    "deleted",
]

VersionStatus = Literal[
    "queued",
    "processing",
    "indexing",
    "available",
    "failed",
    "deleting",
]

ReviewStatus = Literal[
    "not_required",
    "pending",
    "approved",
    "rejected",
    "needs_changes",
]

IngestionJobStatus = Literal["queued", "running", "done", "failed"]

# Destinos de indexación y operaciones del outbox (ADR-0001, ADR-0004)
IndexDestination = Literal["pinecone", "bm25"]
PendingOperation = Literal["index", "delete", "reconcile"]
PendingOpStatus = Literal["pending", "done", "failed"]

# ---------------------------------------------------------------------------
# Códigos de error estables (ADR-0005). Nunca contienen texto de excepción.
# ---------------------------------------------------------------------------

IngestionErrorCode = Literal[
    "ING_FILE_EMPTY",
    "ING_FILE_TOO_LARGE",
    "ING_UNSUPPORTED_TYPE",
    "ING_EXTRACT_FAILED",
    "ING_INDEX_FAILED",
    "ING_DUPLICATE",
    "ING_INTERNAL",
]

DeletionErrorCode = Literal[
    "DEL_NOT_FOUND",
    "DEL_CONFLICT",
]

ModerationErrorCode = Literal[
    "MOD_NOT_ALLOWED",
    "MOD_INVALID_TRANSITION",
]

# ---------------------------------------------------------------------------
# Contratos de catálogo
# ---------------------------------------------------------------------------


class DocumentRecord(StrictModel):
    """Registro lógico de un documento en el catálogo (tabla `documents`)."""

    document_id: str
    filename: str
    subject: str | None = None
    active_version_id: str | None = None
    editorial_status: EditorialStatus = "draft"
    review_status: ReviewStatus = "not_required"
    uploaded_by: str | None = None
    created_at: str
    updated_at: str
    deleted_at: str | None = None


class SubmitForReviewResponse(StrictModel):
    """Respuesta del envío a revisión del autor (PUBLISH-FLOW-02).

    Contrato Pydantic del endpoint `POST /api/documents/{id}/submit`:
    identifica el documento, su versión activa y la transición aplicada.
    """

    document_id: str
    active_version_id: str
    previous_status: EditorialStatus
    editorial_status: EditorialStatus


class DocumentVersion(StrictModel):
    """Versión inmutable de un documento (tabla `document_versions`)."""

    version_id: str
    document_id: str
    version_number: int
    sha256: str = Field(min_length=64, max_length=64)
    storage_key: str
    content_type: str
    size_bytes: int = Field(ge=0)
    status: VersionStatus = "queued"
    created_at: str
    page_count: int | None = None
    error_code: IngestionErrorCode | None = None


class IngestionJob(StrictModel):
    """Trabajo de ingesta (tabla `ingestion_jobs`)."""

    job_id: str
    document_id: str
    version_id: str | None = None
    status: IngestionJobStatus = "queued"
    created_at: str
    updated_at: str
    error_code: IngestionErrorCode | None = None


class ChunkRecord(StrictModel):
    """Fragmento con metadatos estables; fuente de verdad en PostgreSQL (ADR-0003)."""

    chunk_id: str
    document_id: str
    version_id: str
    page: int = Field(ge=1)
    chunk_index: int = Field(ge=0)
    content_hash: str = Field(min_length=64, max_length=64)
    text: str
    title: str
    source: str


class PendingIndexOp(StrictModel):
    """Operación de indexación pendiente por destino (outbox, ADR-0001/0004)."""

    op_id: str
    version_id: str
    destination: IndexDestination
    operation: PendingOperation
    status: PendingOpStatus = "pending"
    attempts: int = Field(default=0, ge=0)
    last_error_code: str | None = None
