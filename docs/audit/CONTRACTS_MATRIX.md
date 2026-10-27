# Matriz de contratos — TASK-01 (rev. 2, integración Spec Kit v6)

Alineación entre los contratos existentes de M7, los sugeridos por `specs/01`/`specs/02`/`specs/06` y los contratos objetivo de ADA Study. Los nombres definitivos se adaptan a los contratos existentes (Spec 01 §35: "evitar duplicar modelos equivalentes").

## 1. Contratos de dominio documental (nuevos)

| Contrato objetivo (Spec) | Estado en M7 | Contrato ADA Study | Campos | ADR |
|---|---|---|---|---|
| `EditorialStatus` (Spec 06 §61-68) | **No existe** | `EditorialStatus` (Literal) | `draft`, `pending_review`, `published_pending_review`, `published`, `rejected`, `withdrawn`, `deleted` | 0005 |
| `VersionStatus` (Spec 06 §71) | **No existe** | `VersionStatus` (Literal) | `queued`, `processing`, `indexing`, `available`, `failed`, `deleting` | 0005 |
| `ReviewStatus` (Spec 06 §77) | **No existe** | `ReviewStatus` (Literal) | `not_required`, `pending`, `approved`, `rejected`, `needs_changes` | 0005 |
| `DocumentRecord` (Spec 01 §31) | **No existe** | `DocumentRecord` | `document_id`, `filename`, `subject?`, `active_version_id?`, `editorial_status`, `review_status`, `uploaded_by?`, `created_at`, `updated_at`, `deleted_at?` | 0001 |
| `DocumentVersion` (Spec 01 §32) | **No existe** | `DocumentVersion` | `version_id`, `document_id`, `version_number`, `sha256`, `storage_key`, `content_type`, `size_bytes`, `status`, `created_at`, `page_count?`, `error_code?` | 0001, 0002 |
| `IngestionJob` (Spec 01 §34) | Parcial: job genérico en Redis sin contrato Pydantic | `IngestionJob` | `job_id`, `document_id`, `status` (`queued/running/done/failed`), `created_at`, `updated_at`, `error_code?` | 0005 |
| `SourceReference` (Spec 01 §33) | Equivalente funcional: `SourceEvidence` (`schemas.py:24`) | `SourceReference` derivado de `SourceEvidence` | `source_type` (`library`/`web`), `document_id?`, `title`, `uri?`, `page?`, `excerpt`, `content_hash?` | 0007 |
| `ChunkRecord` (Spec 02 §18) | **No existe** (chunks solo en memoria) | `ChunkRecord` (fuente de verdad en PostgreSQL) | `chunk_id`, `document_id`, `version_id`, `page`, `chunk_index`, `content_hash`, `text`, `title`, `source` | 0003, 0004 |
| `PendingIndexOp` (Spec 02 §52-54) | **No existe** | `PendingIndexOp` (outbox) | `op_id`, `version_id`, `destination` (`pinecone`/`bm25`), `operation` (`index`/`delete`/`reconcile`), `status`, `attempts`, `last_error_code?` | 0001, 0004 |
| Códigos de error (Spec 02 §33) | **No existen** (texto libre `str(error)`) | `IngestionErrorCode` / `DeletionErrorCode` / `ModerationErrorCode` | `ING_*`, `DEL_*`, `MOD_*` (tabla completa en ADR-0005) | 0005 |
| Identidad/roles/moderación (Spec 06) | **No existe** | `User`, `Role`, `RoleAssignment`, `ModerationDecision`, `AuditEvent` | Detalle de esquema en la tarea de identidad del plan; contratos Pydantic en los límites | 0001, 0006 |

## 2. Contratos del grafo de estudio (existentes, se conservan o adaptan)

| Contrato M7 (`src/ada/schemas.py`) | Acción | Notas |
|---|---|---|
| `StrictModel` | **Conservar** | Base `extra="forbid"` — patrón correcto, se reutiliza para todos los contratos nuevos |
| `SourceEvidence` | **Conservar** | Ya serializable para el checkpointer (usa `str` para URI, no `HttpUrl`); `SourceReference` se deriva de él |
| `ResearchArtifact` | **Conservar** | `evidence_gaps` es la base de la detección de insuficiencia (Spec 04) |
| `ValidationResult` | **Conservar** | Validator determinístico + clasificación de errores por responsable |
| `RoutingDecision` | **Conservar** | Topología del supervisor sin cambios (ADR-0007) |
| `FinalReport` | **Adaptar** → `StudyResponse` | `recommendation` → explicación pedagógica + referencias + etiqueta de fuentes web |
| `SystemProfile` | **Reemplazar** → `StudyContext` | Dominio arquitectura → dominio pedagógico |
| `Criterion`/`CriterionScore`/`AlternativeAnalysis` | **Reemplazar** → `LearningObjective`/`PracticeSet` | Scoring de alternativas → generación de práctica desde evidencia |
| `TechnicalDocument` (`retrieval/documents.py`) | **Adaptar** | De corpus JSON estático a chunks del catálogo; `load_corpus` se reemplaza por consulta al catálogo filtrada por estado editorial |

## 3. Contratos de API (límites externos)

| Contrato M7 (`app/main.py`) | Acción | Contrato nuevo |
|---|---|---|
| `TaskCreate`/`TaskCreated`/`TaskStatus` | **Conservar** | — (flujo de estudio intacto, ADR-0006) |
| `ApprovalRequest` | **Conservar** | — |
| — | **Nuevo** | `DocumentUploadResponse` (202): `document_id`, `job_id`, estado editorial inicial |
| — | **Nuevo** | `DocumentListResponse` / `DocumentDetailResponse` (visibilidad según rol, Spec 06 §106) |
| — | **Nuevo** | `IngestionJobStatusResponse`: `status`, `error_code` |
| — | **Nuevo** | `ModerationActionRequest`: motivo (obligatorio en reject/withdraw/changes) |
| — | **Nuevo** | `ErrorResponse`: `error_code` estable + mensaje humano legible (nunca `str(exception)`) |

## 4. Reglas transversales

1. **Pydantic en todos los límites externos** (API, worker↔store, contratos LLM estructurados) — regla 6 de AGENTS.md.
2. **`StrictModel` como base** de todo contrato nuevo: campos extra rechazados.
3. **IDs deterministas** para chunks y vectores: `{document_id}::{version_id}::{chunk_id}` — idempotencia por diseño (ADR-0003/0004).
4. **URI como `str`**, no `HttpUrl`: el estado del grafo se serializa con msgpack en el checkpointer (lección ya aprendida en M7, `schemas.py:28-30`).
5. **Estados en minúsculas** en los contratos nuevos; los estados M7 de `/tasks` (mayúsculas) no se renombran.
6. **Ningún contrato nuevo duplica un modelo equivalente existente**; `SourceReference` se deriva, no se copia.
7. **Visibilidad = editorial + técnico (Spec 06 §72, §104):** la recuperación solo expone fragmentos de documentos `published`/`published_pending_review` con versión activa `available`. Ningún índice derivado (Pinecone/BM25) decide visibilidad por sí mismo (Spec 01 §54).
8. **Texto recuperado = dato no confiable (Spec 06 §94):** los contratos de generación lo delimitan; nunca instrucciones del sistema/agente.
