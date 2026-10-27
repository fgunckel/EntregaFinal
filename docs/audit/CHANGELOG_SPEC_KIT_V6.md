# Changelog — Integración Spec Kit v6 con TASK-01 (2026-10-25)

Registro de la revisión cruzada entre `specs/` (v6), `docs/adr/`, `docs/audit/CONTRACTS_MATRIX.md` y `docs/audit/IMPLEMENTATION_PLAN.md`. Los specs **no se modificaron**: son la fuente de requisitos funcionales. Los ADRs son el registro de decisiones técnicas; el plan es la secuencia de implementación.

## Contradicciones encontradas y resueltas

| # | Tema | Spec Kit v6 decía | TASK-01 (rev. 1) decía | Resolución aplicada | Archivos |
|---|---|---|---|---|---|
| 1 | **Estados de documento** | Spec 06 §59-71: dos ciclos separados — editorial (`draft`…`deleted`) y técnico (`queued`…`deleting`); no sobrecargar un enum | Un único `DocumentStatus` técnico (`queued/processing/available/failed/deleting`) | Se adoptan dos enums: `EditorialStatus` (en `documents`) y `VersionStatus` (en versiones) + `ReviewStatus` | ADR-0001, ADR-0005, CONTRACTS_MATRIX |
| 2 | **Borrado/retirada** | Spec 06 §104: la retirada invalida la recuperación a nivel de aplicación sin depender de la latencia de Pinecone; estados `withdrawn`/`deleted` editoriales | Cortina técnica `deleting` como único mecanismo | Cortina **editorial** (`withdrawn`/`deleted`) + purga física asíncrona vía outbox; `deleting` queda solo como estado técnico de purga | ADR-0004, ADR-0005, ADR-0006 |
| 3 | **Versionado** | Specs 01/02/06: versiones inmutables, `active_version_id` en el documento, restaurar = activar histórica + reconciliar, falla de versión nueva no desactiva la activa | `sha256`/`content_type`/`size_bytes` en `documents`; restauración no definida; sin `active_version_id` | Esquema realineado: identidad técnica en versiones, `active_version_id` + `deleted_at` en `documents`, restauración definida (ADR-0004 §3) | ADR-0001, ADR-0004 |
| 4 | **Consistencia de publicación** | Specs 01/02 + `specs/ADR-001` §19: sin transacción distribuida; outbox/operaciones pendientes, reintentos idempotentes, reconciliación; activación solo con ambos destinos verificados | Reconciliador genérico sin mecanismo concreto | Tabla `pending_index_ops` (outbox por destino `pinecone`/`bm25`), verificación de ambos destinos antes de activar | ADR-0001, ADR-0003, ADR-0004 |
| 5 | **Deduplicación** | Spec 02 §17: mismo documento → respuesta explícita sin duplicar; entre documentos distintos → política a documentar, no asumir | Índice único global por SHA-256 (409) | Mismo documento: respuesta explícita; entre documentos distintos: **política pendiente de aprobación** (propuestas: 409 o compartir contenido) | ADR-0001, ADR-0006 |
| 6 | **Pinecone (metadatos/IDs)** | Spec 01 §40: metadatos mínimos `document_id`, `version_id`, `chunk_id`, `page`, `content_hash` | ID con `chunk_index`; sin `chunk_id` explícito | ID determinista `{document_id}::{version_id}::{chunk_id}`; metadatos mínimos alineados | ADR-0003, CONTRACTS_MATRIX |
| 7 | **BM25** | Spec 01 §41: actualizable por versión, eliminable/reemplazable sin afectar otras; reconstruible | Reconstruible, sin detalle de actualización por versión | BM25 actualizable por versión explícito en ADR-0003 y plan (F1) | ADR-0003, IMPLEMENTATION_PLAN |
| 8 | **RRF** | Specs 01/02: reutilizar M4 si la auditoría confirma compatibilidad | Reutilizar implementación M7 (que es la de M6) | Sin contradicción real: la implementación de M7 deriva de M4/M6; se documenta la verificación previa | ADR-0003 |
| 9 | **Usuarios/roles/moderación** | Spec 06: 4 roles, flujo `pending_review`/`published_pending_review`, moderación con motivo, auditoría append-only, autorización en cada endpoint | **Ausente por completo** | Agregados: endpoints `/api/moderation/*`, estado editorial inicial según rol, visibilidad según rol, fases H1-H3 (identidad, moderación, auditoría) | ADR-0001, ADR-0005, ADR-0006, CONTRACTS_MATRIX, IMPLEMENTATION_PLAN |
| 10 | **Almacenamiento PDF** | Spec 02 §8: `storage_key` que no dependa del nombre como identidad; Spec 06 §92: no ejecutable, no servido | Ruta relativa derivada del hash | `storage_key` opaca + requisitos de seguridad de Spec 06 | ADR-0002 |
| 11 | **Prompt injection** | Spec 06 §94, Spec 04 §21: texto recuperado = dato no confiable | Ausente | Regla transversal en matriz + delimitación en prompts (G2) | CONTRACTS_MATRIX, ADR-0007, IMPLEMENTATION_PLAN |
| 12 | **Seguridad de almacenamiento** | Spec 06 §92-96: validación real del archivo, rate limits, cuotas | Validación básica sin rate limits | Rate limits/cuotas en A2; validación real antes de persistir en C1 | IMPLEMENTATION_PLAN |

## Archivos modificados

| Archivo | Cambio |
|---|---|
| `docs/adr/ADR-0001-postgresql-catalog-versions.md` | Rev. 2: dos ciclos de estado, `active_version_id`/`subject`/`uploaded_by`/`deleted_at`, `storage_key`, outbox `pending_index_ops`, dedup como política pendiente, restauración, responsabilidad Spec 06 |
| `docs/adr/ADR-0002-pdf-storage.md` | Rev. 2: `storage_key` opaca, seguridad Spec 06, raíz fuera de `src/` |
| `docs/adr/ADR-0003-pinecone-bm25-rrf.md` | Rev. 2: verificación de destinos antes de activar, metadatos mínimos Spec 01, ID con `chunk_id`, BM25 por versión |
| `docs/adr/ADR-0004-deletion-restoration-eventual-consistency.md` | Rev. 2: cortina editorial, outbox, restauración inmutable, falla no desactiva activa |
| `docs/adr/ADR-0005-error-codes-and-states.md` | Rev. 2: enums editorial/técnico/revisión, códigos `MOD_*`, regla de visibilidad |
| `docs/adr/ADR-0006-endpoints-m7-compatibility.md` | Rev. 2: endpoints de moderación, restauración, autorización por rol, dedup explícita, auditoría |
| `docs/adr/ADR-0007-minimal-domain-change-ada-to-ada-study.md` | Rev. 2: recuperación filtrada por editorial, prompt injection |
| `docs/adr/README.md` | Índice actualizado + relación con el Spec Kit |
| `docs/audit/CONTRACTS_MATRIX.md` | Rev. 2: contratos nuevos (`EditorialStatus`, `VersionStatus`, `ReviewStatus`, `PendingIndexOp`, identidad/moderación), reglas de visibilidad y prompt injection |
| `docs/audit/IMPLEMENTATION_PLAN.md` | Rev. 2: outbox en D2, cortina editorial en E2, filtro de visibilidad en F1, nuevas fases H (identidad/moderación/auditoría) e I (calidad), reglas 4-5 |
| `docs/audit/BASELINE.md` | Nota de rev. 2 en la sección TASK-01 |

## Archivos NO modificados

- `specs/00`–`specs/06` y `specs/ADR-001`: fuente de requisitos funcionales; sin contradicciones internas detectadas entre ellos.
- `docs/audit/BASELINE.md` (secciones 1-7, 9): auditoría de fuentes; sigue válida.
- Carpetas `Entrega7` y `Entregas`: intactas (solo lectura).

## Decisiones que siguen pendientes de aprobación

1. **Los 7 ADRs** (estado Propuesto) — ahora alineados con el Spec Kit v6.
2. **Política de dedup entre documentos distintos** (Spec 02 §17): 409 con `ING_DUPLICATE` vs. compartir contenido/referencia. Se resuelve en TASK-02.
3. **Restauración de versiones** (Spec 06 §74): reactivar la versión histórica directamente vs. crear nueva versión que la referencie. Se resuelve en TASK-02.
4. **Driver PostgreSQL:** SQLAlchemy Core async + asyncpg (propuesta; se confirma en B1).
5. **Extractor PDF:** pypdf (propuesta; se justifica en D1).
6. **Búsqueda web (Spec 04):** fuera del plan hasta aprobar la Fase G; se planificará como tarea propia.
7. **Alcance de identidad en el MVP local** (Spec 03 §51 vs Spec 06): Spec 03 dice "no introducir autenticación compleja en el MVP local" y Spec 06 exige comunidad cerrada con autorización en cada endpoint. Propuesta: implementar H1 (identidad mínima con sesiones simples) antes de exponer endpoints privados; requiere confirmación del usuario.
