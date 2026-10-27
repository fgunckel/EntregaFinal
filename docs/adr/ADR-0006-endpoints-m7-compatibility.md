# ADR-0006 — Endpoints finales y compatibilidad con la API M7

- Estado: Propuesto (espera aprobación del usuario). Alineado con Specs 01/02/06.
- Fecha: 2026-10-25 (rev. 2 — integración Spec Kit v6)

## Contexto

Spec 02 propone una API de documentos y aclara: "No crear endpoints duplicados si M7 ya tiene un patrón equivalente". M7 expone `/tasks` (grafo de estudio, 202 + polling + HITL) — patrón validado que se conserva. Spec 06 añade el flujo de publicación/moderación con autorización por rol en cada operación.

## Decisión

### Se conservan intactos (patrón M7)

| Endpoint | Rol |
|---|---|
| `POST /tasks` | Encolar tarea de estudio (grafo) — 202 |
| `GET /tasks/{job_id}` | Polling de tarea de estudio |
| `POST /tasks/{job_id}/approve` | HITL de tareas críticas |
| `GET /health` | Liveness + Redis |

### Endpoints nuevos de documentos (Spec 02, adaptados)

| Endpoint | Método | Comportamiento |
|---|---|---|
| `/api/documents` | POST | Multipart upload; **202** con `document_id`, `job_id`, estado inicial. Validación síncrona mínima (tamaño/MIME real/hash) antes de encolar. El estado editorial inicial depende del rol: `pending_review` (student) o `published_pending_review` tras indexación (trusted_contributor, Spec 06 §52) |
| `/api/documents` | GET | Lista documentos **visibles según rol**: un usuario ve sus propios aportes en cualquier estado; la biblioteca compartida solo muestra `published`/`published_pending_review` (Spec 06 §106) |
| `/api/documents/{document_id}` | GET | Detalle + `page_count`, `size_bytes`, versión activa, estado editorial y técnico |
| `/api/documents/{document_id}` | DELETE | **202** — retirada/borrado lógico inmediato (cortina editorial, ADR-0004) + purga física asíncrona vía outbox; 404 si no existe; 409 si ya retirado/en purga |
| `/api/ingestion-jobs/{job_id}` | GET | Estado del job: `status`, `error_code`, timestamps |
| `/api/documents/{document_id}/retry` | POST | Reintento controlado de una versión `failed`; 409 en otro estado |
| `/api/documents/{document_id}/versions/{version_id}/restore` | POST | Restaurar versión histórica como activa (Spec 02 §49); reconcilia índices vía outbox |

### Endpoints de publicación y moderación (Spec 06)

| Endpoint | Método | Rol mínimo | Comportamiento |
|---|---|---|---|
| `/api/moderation/queue` | GET | moderator | Aportes `pending_review` (y `published_pending_review` para revisión posterior) |
| `/api/moderation/documents/{document_id}/approve` | POST | moderator | `-> published`; registra actor, fecha, motivo opcional |
| `/api/moderation/documents/{document_id}/reject` | POST | moderator | `-> rejected` con motivo obligatorio; el autor ve el motivo (Spec 06 §16) |
| `/api/moderation/documents/{document_id}/withdraw` | POST | moderator | `-> withdrawn` (retirada); excluye de recuperación al instante |
| `/api/moderation/documents/{document_id}/changes` | POST | moderator | `needs_changes` con motivo; el autor puede corregir y reenviar |

### Compatibilidad y convenciones

1. **Prefijo `/api/`** para el flujo documental; `/tasks` de M7 no se mueve (los clientes existentes no se rompen).
2. **Autorización en servidor en cada endpoint** (Spec 06 §9, Spec 01 §24): la UI no es control de seguridad. Sin identidad (Fase de identidad del plan), los endpoints privados rechazan.
3. **Mismas convenciones M7:** Pydantic en los límites, 202 + polling para operaciones largas, error handler central con códigos estables (ADR-0005) que nunca filtra trazas.
4. **Sin endpoints duplicados:** el estado de ingesta NO se consulta por `/tasks`.
5. **`POST /api/documents/{id}/reindex`** (opcional en Spec 02): se pospone; el reconciliador del ADR-0004 cubre la reconstrucción.
6. **Dedup explícita (Spec 02 §17, Spec 06 §50):** re-upload con SHA-256 ya registrado **no crea una segunda versión idéntica silenciosamente**: respuesta explícita con el documento/versión existente (200 con referencia, o 409 con `ING_DUPLICATE`); la elección exacta se fija en TASK-02.
7. **Auditoría:** publicaciones, retiradas, restauraciones y decisiones de moderación generan eventos de auditoría (Spec 06 §105).

## Consecuencias

- El frontend consume un backend con tres flujos separados: estudio (`/tasks`), documentos (`/api/documents`) y moderación (`/api/moderation`).
- Los 5 escenarios E2E de Spec 05 y los criterios de Spec 06 §99-108 pueden escribirse contra esta superficie sin ambigüedades.
- La compatibilidad con la consigna del curso (API + contratos Pydantic + persistencia) queda cubierta por la unión de los flujos.

## Historial de revisión

- **Rev. 2 (2026-10-25):** integración Spec Kit v6. Cambios: endpoints de moderación (`/api/moderation/*`) según Spec 06; restauración de versiones; autorización por rol en cada endpoint; estado editorial inicial según rol; dedup explícita sin duplicación silenciosa; auditoría de acciones.
- **Rev. 1 (2026-10-25):** versión inicial de TASK-01.
