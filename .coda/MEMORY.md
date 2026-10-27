# MEMORY

This file stores durable learned context for the coding agent and the user.
Do not duplicate instructions already captured in `AGENTS.md`.

## Stable User Preferences

## Stable Project Preferences

## Important Decisions
- TASK-02 aprobada (2026-10-25): dedup entre documentos distintos = 409 ING_DUPLICATE (sin fusionar autoría/permisos/historial; reutilizar archivo físico permitido); restauración = nueva versión basada en histórica (historial inmutable); PostgreSQL = SQLAlchemy Core async + asyncpg; PDF = pypdf (detectar escaneados y no publicarlos); identidad mínima aprobada antes de endpoints privados (invitaciones, sesiones seguras, roles, autorización en backend); búsqueda web fuera de TASK-02.
- TASK-03 aprobada (2026-10-26): PostgreSQL única fuente de estado; BM25 se reconstruye desde document_chunks al arrancar, sin persistencia JSON del índice. Pendientes explícitas: 7 pruebas de integración con PostgreSQL real, destino Pinecone real con credenciales; no declarar validadas mientras se usen fakes. TASK-04 aprobada para planificar: presentar objetivo/alcance/archivos/dependencias/criterios antes de implementar; solo ADA-Study.
## Learned Facts

## Known Gotchas

## Source-of-Truth Pointers

## Things To Re-Validate
