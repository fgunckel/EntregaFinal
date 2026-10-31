# ADR-0001 — Catálogo y versiones de documentos en PostgreSQL

- Estado: Aceptado (rev. 3 aprobada por el usuario el 2026-10-30). Alineado con `specs/ADR-001` (aprobada) y `specs/06`.
- Fecha: 2026-10-30 (rev. 3 — claim de huella en `documents`)
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
  sha256            CHAR(64) NULL,            -- claim de huella (rev. 3, ver "Deduplicación")
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

### Deduplicación (Spec 02 §17) — rev. 3: claim en `documents`

**Limitación de PostgreSQL que motivó el cambio:** la rev. 2 colocaba la
unicidad de huella en `document_versions` con un índice parcial cuyo
predicado contenía una subconsulta
(`document_id IN (SELECT document_id FROM documents WHERE deleted_at IS NULL)`).
PostgreSQL **no soporta subconsultas en predicados de índice**
(`cannot use subquery in index predicate`): ese DDL hace fallar
`metadata.create_all` y no puede existir en la base.

**Decisión (rev. 3):** el claim se **desnormaliza** a `documents.sha256` y la
unicidad se garantiza con un índice único parcial a nivel DOCUMENTO:

```sql
CREATE UNIQUE INDEX ux_documents_active_sha256
    ON documents (sha256) WHERE deleted_at IS NULL;
```

- **Justificación de la desnormalización:** la huella se reclama a nivel
  documento lógico; un índice parcial sobre `document_versions` no puede
  referenciar `documents` (limitación anterior) y además era semánticamente
  incorrecto: el claim existe **desde el registro de la versión**, no desde
  su activación (una versión `queued`/`failed` ya ocupa la huella), y las
  versiones históricas de un mismo documento comparten huella
  legítimamente (restauración). Duplicar la huella en `documents` es el
  único punto donde "un documento vivo" es una sola fila indexable.
- **Ciclo de vida del claim** (`documents.sha256`):
  - Nace en el **registro** del documento, en la MISMA transacción que el
    INSERT de la versión inicial, el job y las ops del outbox
    (`create_document_atomic`).
  - Se **traslada** a la huella de cada versión nueva en la MISMA
    transacción que su alta (restauración: `create_version_with_claim`).
  - Lo **retienen** los estados editoriales intermedios
    (`pending_review`, `withdrawn`, `published*`, `rejected`) y los estados
    técnicos de la versión (`queued`, `failed`): el dedup no depende del
    progreso de ingesta ni de la moderación.
  - Lo **libera** solo el borrado (`deleted_at` NOT NULL → la fila sale del
    índice parcial); `deleted` es terminal, no hay restauración del claim.
- **Flujo de detección (invariante):** el dedup se resuelve al **registrar**
  la versión (pre-check de servicio para mensaje amable + garantía del
  índice único en la misma transacción del repositorio). El worker NO
  resuelve deduplicación: solo mapea fallos a `ING_DUPLICATE` cuando los hay.
- **Concurrencia:** el índice único parcial es la garantía final; el
  repositorio traduce a `DuplicateDocumentError` (409 `ING_DUPLICATE`)
  **únicamente** las violaciones cuyo constraint sea
  `ux_documents_active_sha256`; cualquier otra `IntegrityError` se propaga
  sin traducir (p. ej. `ux_versions_document_number`).
- **Contratos:** `DocumentRecord` NO expone `sha256` (el claim es un
  detalle del repositorio, se pasa como parámetro); el `status` HTTP de
  `ING_DUPLICATE` (409) no cambia y ahora también aplica al endpoint de
  restore.

### Migración (rev. 3, idempotente y no destructiva)

`metadata.create_all` NO modifica tablas existentes. La migración vive en
`src/ada_study/db/migrations.py` (CLI: `python -m ada_study.db.migrate
<DATABASE_URL>`; `create_schema` la ejecuta como verificación final):

1. `ALTER TABLE documents ADD COLUMN sha256` si falta (nullable: seguro en
   bases pobladas).
2. Backfill del claim: huella de la versión registrada más reciente NO
   purgada (`status <> 'deleting'`) de cada documento que aún no tenga claim.
3. **Detección de conflictos**: si dos o más documentos vivos reclaman la
   misma huella, aborta SIN crear el índice y SIN modificar datos
   (`MigrationConflictError`); el conflicto se resuelve por vía editorial.
4. `CREATE UNIQUE INDEX ux_documents_active_sha256 ... WHERE deleted_at IS NULL`.
5. `DROP INDEX ux_versions_active_sha256` (nunca pudo existir en PostgreSQL:
   predicado con subconsulta).

Cada paso es idempotente (`IF NOT EXISTS`/verificación previa); ejecutar la
migración N veces converge al mismo esquema sin pérdida de datos.

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

- **Rev. 3 (2026-10-30):** deduplicación aprobada y corregida. Cambios: el claim de huella se traslada de `document_versions` a `documents.sha256` (desnormalización); índice único parcial `ux_documents_active_sha256 WHERE deleted_at IS NULL` reemplaza a `ux_versions_active_sha256` (inválido: PostgreSQL no soporta subconsultas en predicados de índice); el claim nace en el registro y se traslada en la misma transacción de cada alta de versión (registro y restauración atómicas: documento/claim + versión + job + outbox en una sola transacción); `deleted_at` libera el claim; ciclo de vida documentado; migración idempotente no destructiva (`db/migrations.py`) con backfill y detección de conflictos previa a la creación del índice.
- **Rev. 2 (2026-10-25):** integración Spec Kit v6. Cambios: modelo de dos ciclos de estado (editorial en `documents`, técnico en versiones); `active_version_id`, `subject`, `uploaded_by`, `deleted_at` en `documents`; `storage_key`, `content_type`, `error_code` en versiones; tabla `pending_index_ops` (outbox por destino); dedup entre documentos distintos pasa de "índice único global" a "política pendiente de aprobación"; restauración alineada con Spec 06 (inmutabilidad, reconciliación); se agrega responsabilidad de identidad/moderación/auditoría (Spec 06).
- **Rev. 1 (2026-10-25):** versión inicial de TASK-01.
