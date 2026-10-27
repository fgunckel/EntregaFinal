# ADR-0005 — Códigos de error estables y estados de documento/versión/job

- Estado: Propuesto (espera aprobación del usuario). Alineado con Specs 02/06.
- Fecha: 2026-10-25 (rev. 2 — integración Spec Kit v6)

## Contexto

Specs 02/06 exigen estados observables y errores legibles sin exponer trazas internas. M7 ya tiene un patrón de estados de job en Redis (`app/worker.py`: `PENDING/RUNNING/WAITING_APPROVAL/DONE/FAILED`) y la API devuelve `error` como texto libre (`str(error)`), lo que puede filtrar detalles internos. Spec 06 §59 exige **separar el ciclo editorial del técnico** y evitar sobrecargar un único enum.

## Decisión

### Estados editoriales del documento (`EditorialStatus`, en `documents`)

Ciclo de publicación/moderación (Spec 06 §61-68):

| Estado | Significado |
|---|---|
| `draft` | Carga inicial incompleta o no enviada |
| `pending_review` | Espera revisión previa; **no** aparece en el RAG compartido |
| `published_pending_review` | Publicado automáticamente por colaborador confiable; visible mientras espera moderación posterior |
| `published` | Revisado y aprobado; visible para usuarios autorizados |
| `rejected` | Rechazado; no aparece en búsquedas |
| `withdrawn` | Retirado de la biblioteca compartida; no aparece en búsquedas normales |
| `deleted` | Borrado lógico / retirada definitiva según política de retención; se preserva auditoría mínima |

### Estados técnicos de la versión (`VersionStatus`, en `document_versions`)

Ciclo de ingesta/indexación (Spec 06 §71):

| Estado | Significado |
|---|---|
| `queued` | Recibida, esperando worker |
| `processing` | Extracción/chunking en curso |
| `indexing` | Indexación en Pinecone/BM25 en curso |
| `available` | Indexación completa y verificada en ambos destinos |
| `failed` | Error observable con `error_code`; reintento controlado posible |
| `deleting` | Purga física de índices en curso |

**Regla de visibilidad:** un fragmento es recuperable solo si el documento está en `published`/`published_pending_review` **y** su versión activa está `available` (Spec 06 §72, §104).

### Estados de revisión (`ReviewStatus`, Spec 06 §77)

`not_required`, `pending`, `approved`, `rejected`, `needs_changes` — con quién revisó, cuándo, motivo y versión exacta revisada.

### Estados de job de ingesta (`IngestionJobStatus`)

Alineados con el patrón M7 (minúsculas en contratos nuevos): `queued`, `running`, `done`, `failed`. El estado `waiting_approval` de M7 no aplica a ingesta (no hay HITL en ingesta).

### Códigos de error estables

Formato: `ING_<CAUSA>` para ingesta, `DEL_<CAUSA>` para borrado/retirada, `MOD_<CAUSA>` para moderación. Nunca contienen texto de excepción.

| Código | Causa | HTTP sugerido |
|---|---|---|
| `ING_FILE_EMPTY` | Archivo vacío o sin contenido extraíble | 422 |
| `ING_FILE_TOO_LARGE` | Supera el límite configurable | 413 |
| `ING_UNSUPPORTED_TYPE` | Extensión/MIME real no admitido | 415 |
| `ING_EXTRACT_FAILED` | PDF sin texto seleccionable (escaneado) o extracción corrupta | 422 |
| `ING_INDEX_FAILED` | Fallo de indexación en Pinecone/BM25 (registrado por destino) | 500 |
| `ING_DUPLICATE` | Misma huella SHA-256 ya registrada (política según dedup, ADR-0001) | 409 |
| `ING_INTERNAL` | Error no clasificado (detalle solo en logs/trazas) | 500 |
| `DEL_NOT_FOUND` | Documento inexistente | 404 |
| `DEL_CONFLICT` | Retirada/borrado ya en curso o estado no borrable | 409 |
| `MOD_NOT_ALLOWED` | Usuario sin rol de moderador intenta una acción de moderación | 403 |
| `MOD_INVALID_TRANSITION` | Acción de moderación inválida para el estado editorial actual | 409 |

### Reglas

1. **Nunca exponer `str(exception)` al cliente:** la API mapea excepciones a códigos; el detalle va a logs/trazas (corrigiendo el patrón de texto libre de M7 en `app/main.py`).
2. **Un error por job/versión:** `ingestion_jobs.error_code` y `document_versions.error_code` guardan el código estable; el mensaje humano legible se genera en la capa de presentación (Spec 03).
3. **Reintento controlado:** reintentar la ingesta solo acepta versiones `failed`; repetir no duplica (IDs deterministas, ADR-0004).
4. **Moderación auditable:** cada decisión de moderación registra actor, fecha, acción, motivo y documento/versión (Spec 06 §55, §78); los códigos `MOD_*` complementan, no reemplazan, ese registro.
5. **Compatibilidad M7:** los jobs de estudio (grafo) conservan los estados M7 en Redis; los códigos de esta ADR aplican al flujo documental, que es un flujo separado (Spec 01 §18).

## Consecuencias

- La UI puede mapear códigos a mensajes pedagógicos en español sin conocer la causa interna (Spec 03 §20).
- Los tests E2E pueden asertar sobre códigos estables en lugar de textos frágiles.
- Los tests de permisos (Spec 06 §107) cubren transiciones editoriales válidas/inválidas con los códigos `MOD_*`.
- La API de M7 (`/tasks`) queda intacta; los nuevos endpoints de documentos usan este contrato.

## Historial de revisión

- **Rev. 2 (2026-10-25):** integración Spec Kit v6. Cambios: separación en dos enums (editorial `EditorialStatus` y técnico `VersionStatus`) según Spec 06 §59-71; se agrega `indexing` al ciclo técnico y `ReviewStatus`; se agregan códigos `MOD_*` para moderación; `ING_DUPLICATE` referenciado a la política de dedup de ADR-0001; regla de visibilidad editorial+técnico.
- **Rev. 1 (2026-10-25):** versión inicial de TASK-01 (un único `DocumentStatus` técnico).
