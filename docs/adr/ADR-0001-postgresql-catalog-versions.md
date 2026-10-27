# ADR-0001 — Catálogo y versiones de documentos en PostgreSQL

- Estado: Propuesto (espera aprobación del usuario). Alineado con `specs/ADR-001` (aprobada) y `specs/06`.
- Fecha: 2026-10-25 (rev. 2 — integración Spec Kit v6)
- Decisión aprobada previamente: PostgreSQL como fuente de verdad del catálogo y del historial de versiones.

## Contexto

Specs 01/02 definen el catálogo y el versionado inmutable; Spec 06 añade identidad, roles, moderación y auditoría sobre la misma base. La auditoría (BASELINE §2) confirmó que M7 no tiene catálogo: solo estados de *job* en Redis con TTL de 24 h (`app/worker.py::JOB_TTL_SECONDS`), insuficiente como registro permanente.

## Decisión

PostgreSQL es la **única fuente de verdad** para:

1. **Catálogo de documentos** (`documents`): identidad lógica, ciclo **editorial** (publicación/moderación), metadatos y borrado lógico.
2. **Historial de versiones** (`document_versions`): versiones inmutables con ciclo **técnico** de ingesta; el contenido viejo no se mezcla con el nuevo.
3. **Fragmentos** (`document_chunks`): fuente de verdad de chunks; BM25 y Pinecone son derivables.
4. **Trabajos de ingesta** (`ingestion_jobs`): rastreabilidad persistente de cada tarea.
5. **Operaciones pendientes de indexación** (`pending_index_ops`, patrón outbox): una fila por destino (`pinecone`, `bm25`) con estado, para reintento y reconciliación (Specs 01/02; `specs/ADR-001` §Consistencia).
6. **Identidad, roles, moderación y auditoría** (Spec 06): tablas de usuarios/roles/asignaciones, decisiones de moderación y eventos de auditoría append-only. Detalle de esquema en la tarea correspondiente del plan; este ADR solo fija la responsabilidad.

## Modelo de dos ciclos de estado (Spec 06)

El ciclo **editorial** (qué es visible y para quién) vive en `documents`; el ciclo **técnico** (progreso de ingesta/indexación) vive en `document_versions`. No se sobrecarga un único enum.

## Esquema lógico

```sql
documents (
  document_id       UUID PK,
  filename          TEXT NOT NULL,            -- nombre visible declarado por el usuario
  subject           TEXT NULL,                -- materia/tema declarada (Spec 02)
  active_version_id UUID NULL FK -> document_versions,
  editorial_status  TEXT NOT NULL DEFAULT 'pending_review',
      -- draft|pending_review|published_pending_review|published|rejected|withdrawn|deleted
  review_status     TEXT NOT NULL DEFAULT 'not_required',
      -- not_required|pending|approved|rejected|needs_changes
  uploaded_by       UUID NULL,                -- FK a usuarios (Spec 06); NULL hasta existir identidad
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at        TIMESTAMPTZ NULL          -- borrado lógico (Spec 01)
)

document_versions (
  version_id      UUID PK,
  document_id     UUID FK -> documents,
  version_number  INT NOT NULL,               -- 1, 2, 3...
  sha256          CHAR(64) NOT NULL,          -- huella de ESA versión
  storage_key     TEXT NOT NULL,              -- clave opaca del original (ADR-0002)
  content_type    TEXT NOT NULL,
  size_bytes      BIGINT NOT NULL,
  status          TEXT NOT NULL,              -- queued|processing|indexing|available|failed|deleting
  page_count      INT NULL,
  chunk_count     INT NULL,
  error_code      TEXT NULL,                  -- código estable, ver ADR-0005
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  superseded_at   TIMESTAMPTZ NULL,           -- cuándo dejó de ser la activa
  UNIQUE (document_id, version_number)
)
-- Las versiones procesadas son inmutables: nunca UPDATE de contenido/sha/storage_key.

pending_index_ops (
  op_id           UUID PK,
  version_id      UUID FK -> document_versions,
  destination     TEXT NOT NULL,              -- 'pinecone' | 'bm25'
  operation       TEXT NOT NULL,              -- 'index' | 'delete' | 'reconcile'
  status          TEXT NOT NULL,              -- pending|running|done|failed
  attempts        INT NOT NULL DEFAULT 0,
  last_error_code TEXT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (version_id, destination, operation)
)

-- ingestion_jobs: igual a la rev. 1 (job_id, document_id, status, error_code, timestamps).
-- document_chunks: igual a la rev. 1 (chunk_id, document_id, version_id, page,
--   chunk_index, content_hash, text, title, source) — fuente de verdad de fragmentos.
```

### Deduplicación (Spec 02 §17)

- **Mismo documento:** una carga cuyo SHA-256 coincide con una versión ya registrada de ese documento **no crea duplicados**: respuesta explícita con el documento/versión existente.
- **Entre documentos distintos:** la política (rechazar con 409, o crear referencia/compartir contenido) **queda como propuesta pendiente de aprobación**; no se asume silenciosamente. La rev. 1 proponía índice único global; se mantiene como propuesta, no como decisión.

## Restauración (Spec 06 §74, Spec 02 §49)

- Las versiones son inmutables: restaurar **no reescribe el pasado**.
- Restaurar = apuntar `active_version_id` a una versión histórica + reconciliar sus fragmentos en Pinecone/BM25 si ya no están indexados (vía `pending_index_ops`). Si la política exige no reactivar el contenido histórico directamente, se registra como nueva versión que lo referencia; la elección se resuelve en TASK-02.

## Consecuencias

- **Visibilidad = editorial + técnico:** un fragmento es recuperable solo si `editorial_status IN ('published','published_pending_review')` **y** la versión activa está `available` (Spec 06 §72, Spec 01 §25).
- **Activación segura:** una versión nueva no se vuelve activa hasta que extracción, chunks, Pinecone y BM25 terminaron y se verificaron (Spec 02 §47); una falla de la versión nueva **no desactiva** la activa anterior (`specs/ADR-001` §20).
- **Consistencia con índices:** sin transacción distribuida; `pending_index_ops` + reintentos idempotentes + reconciliación (Spec 02 §52).
- **Redis sigue siendo operativo, no fuente de verdad:** estados de ejecución y colas viven en Redis (patrón M7 validado).
- **Nueva dependencia justificada:** `asyncpg` + SQLAlchemy Core async (o `psycopg` async); se justifica en el plan de implementación.
- **Extensión Spec 06:** las tablas de usuarios/roles/moderación/auditoría se añaden en su propia tarea; `uploaded_by` queda preparado desde el inicio para evitar migración destructiva posterior.

## Historial de revisión

- **Rev. 2 (2026-10-25):** integración Spec Kit v6. Cambios: modelo de dos ciclos de estado (editorial en `documents`, técnico en versiones); `active_version_id`, `subject`, `uploaded_by`, `deleted_at` en `documents`; `storage_key`, `content_type`, `error_code` en versiones; tabla `pending_index_ops` (outbox por destino); dedup entre documentos distintos pasa de "índice único global" a "política pendiente de aprobación"; restauración alineada con Spec 06 (inmutabilidad, reconciliación); se agrega responsabilidad de identidad/moderación/auditoría (Spec 06).
- **Rev. 1 (2026-10-25):** versión inicial de TASK-01.
