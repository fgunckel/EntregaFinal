# MEMORY

This file stores durable learned context for the coding agent and the user.
Do not duplicate instructions already captured in `AGENTS.md`.

## Stable User Preferences

## Stable Project Preferences

## Important Decisions
- TASK-02 aprobada (2026-10-25): dedup entre documentos distintos = 409 ING_DUPLICATE (sin fusionar autoría/permisos/historial; reutilizar archivo físico permitido); restauración = nueva versión basada en histórica (historial inmutable); PostgreSQL = SQLAlchemy Core async + asyncpg; PDF = pypdf (detectar escaneados y no publicarlos); identidad mínima aprobada antes de endpoints privados (invitaciones, sesiones seguras, roles, autorización en backend); búsqueda web fuera de TASK-02.
- TASK-03 aprobada (2026-10-26): PostgreSQL única fuente de estado; BM25 se reconstruye desde document_chunks al arrancar, sin persistencia JSON del índice. Pendientes explícitas: 7 pruebas de integración con PostgreSQL real, destino Pinecone real con credenciales; no declarar validadas mientras se usen fakes. TASK-04 aprobada para planificar: presentar objetivo/alcance/archivos/dependencias/criterios antes de implementar; solo ADA-Study.
- TASK-07 alcance aprobado para propuesta (2026-10-26): E2 (borrado/restauración/reintento) + rutas HTTP de moderación/auditoría + I1/I3; UI queda para TASK-08. Condición: mantener TASK-07 acotada, separar bloques si hace falta. Prioridad absoluta: trabajo final Coderhouse (UI usable + flujos E2E); Evidence Pack Globant secundario, sin tareas específicas, excluido de Git.
- Regla de acceso privado aprobada (2026-10-26, TASK-07): el autor puede recuperar chunks de su propio documento `pending_review` con versión `available` (uso pedagógico privado); otros estudiantes no lo ven; withdrawn/deleted/rejected excluidos absolutamente incluso para el autor. Implementado con ViewerContext en HybridRetriever (aislamiento en recuperación, no filtro final). Upload de student nace pending_review (Spec 06 §51); moderator/admin nacen draft. RAG compartido sigue excluyendo
- pending_review (ADR-0005 sin cambios).
- TASK-07 completada (2026-10-26): E2 (DELETE 202 cortina+purga, retry idempotente solo failed, restore versión nueva) + rutas moderación (decide/queue) y auditoría (GET events, append-only) + 4 escenarios E2E + regla acceso privado + .env.example + diagrama en README + DELIVERY_CHECKLIST.md. 192 passed, 7 skipped, ruff limpio. Bugs preexistentes corregidos: ModerationService usaba retorno None de update_editorial_status y operation="deindex" inválida (ahora "delete"); firma
- update_editorial_status unificada a (document_id, target). Pendientes: Pinecone real, 7 pruebas integración con PostgreSQL real, UI TASK-08.
- Corrección consistencia eventual (2026-10-26, post-revisión ZIP): cortina editorial revalidada en worker al inicio, antes de cada op index/reconcile y antes de activar; bajo cortina solo se ejecutan ops delete (index/reconcile no se ejecutan ni completan). delete_document y withdraw encolan purga también para versiones en curso (queued/processing/indexing). recover() bajo cortina reactiva solo ops delete (nuevo repo.reset_failed_delete_ops). 206 passed, 7 skipped, ruff limpio; 14 pruebas nuevas
- en tests/test_worker_curtain.py.
- TASK-08 MVP-1+B1+B3 completada (2026-10-27): UI Streamlit (src/ada_study/ui: api_client puro con inyección de http client para tests, auth.py session_state, app.py + páginas login/home/library/moderation/audit) + POST /api/auth/login, /api/auth/logout y GET /api/me (routes_auth.py, reutiliza IdentityService; /api/me excluye password_hash — fuga detectada por test y corregida). B2 (/api/study) pospuesto por decisión del usuario. 222 passed, 7 skipped, ruff limpio; smoke test Streamlit 1.66.1
- headless OK (health 200). Evidencia: todo unitario con fakes; PostgreSQL real (7 skipped sin DATABASE_URL) y Pinecone real siguen sin validar.
- TASK-10 completada (2026-10-27): Fase 1 PostgreSQL real NO ejecutada (sin Docker ni PG en la máquina; healthcheck compose corregido a -U ada -d ada_study; 7 pruebas integración siguen skipped, NO validadas). Fase 2: paquete src/ada_study/study (runtime.py) — BM25Index.rebuild por request, HybridRetriever con ViewerContext, compile_graph determinístico (sin LLM, D-1), recursion_limit=25 explícito, estado efímero (D-2), HITL fuera de alcance (D-4). Fase 3: POST /api/study (routes_study.py) con
- auth Bearer, ViewerContext del backend, 422/503 con códigos STUDY_* nuevos (no en ADR-0005 aún). 21 pruebas nuevas; 243 passed, 7 skipped, ruff limpio; smoke API→grafo OK. Pendientes: PG real, Pinecone real, LLM real (TASK-11), UI estudio (TASK-12), códigos STUDY_* en ADR-0005 si se aprueba.
## Learned Facts

## Known Gotchas

## Source-of-Truth Pointers

## Things To Re-Validate
- Deuda técnica BM25 (registrada 2026-10-27, ratificado por usuario): la reconstrucción por petición (BM25Index.rebuild en run_study) queda ratificada para el MVP. Deuda futura: evaluar índice incremental de larga vida en el proceso (add_version/remove_version ya existen y el worker ya los usa vía BM25Destination; PostgreSQL seguiría siendo la única fuente de estado, índice = caché derivada). No se han medido mejoras de rendimiento: la motivación es escalabilidad con el corpus, no un beneficio
- demostrado. Factor dominante esperado con PostgreSQL real: patrón N+1 en _collect_visible_chunks (1 + D + V queries).