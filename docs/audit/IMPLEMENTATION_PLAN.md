# Plan de implementación por tareas pequeñas — TASK-01 (rev. 2, integración Spec Kit v6)

Desglose de TASK-02+ en tareas pequeñas e independientes, cada una con alcance, archivos esperados y criterio de salida. Orden con dependencias explícitas. Ninguna tarea implementa la UI completa ni todos los endpoints de una vez (restricción TASK-01).

## Fase A — Contratos y esqueleto (sin servicios externos)

### A1. Módulo de contratos documentales
- **Alcance:** `EditorialStatus`, `VersionStatus`, `ReviewStatus`, `DocumentRecord`, `DocumentVersion`, `IngestionJob`, `ChunkRecord`, `PendingIndexOp`, códigos de error (`ING_*`/`DEL_*`/`MOD_*`) como Pydantic `StrictModel` en un módulo nuevo (p. ej. `src/ada_study/contracts/documents.py`).
- **Pruebas:** validación de campos, rechazo de extras, transiciones de estado válidas/inválidas (editoriales y técnicas).
- **Salida:** contratos importables sin dependencias de red ni base de datos.

### A2. Configuración extendida
- **Alcance:** extender `Settings` con `ADA_STORAGE_DIR`, `DATABASE_URL`, `PINECONE_*`, límite de tamaño de upload configurable, rate limits.
- **Pruebas:** defaults, lectura de env, validación de límites.
- **Salida:** configuración tipada lista para las fases siguientes.

## Fase B — Persistencia del catálogo (PostgreSQL)

### B1. Esquema y migraciones
- **Alcance:** tablas `documents`, `document_versions`, `document_chunks`, `ingestion_jobs`, `pending_index_ops` según ADR-0001; migraciones versionadas (Alembic o SQL puro versionado — decidir en la tarea).
- **Dependencia nueva justificada:** driver async (`asyncpg` + SQLAlchemy Core async, o `psycopg` async). Elección mínima: SQLAlchemy Core async + asyncpg.
- **Salida:** base de datos local creable con un comando documentado (Docker Compose).

### B2. Repositorio de catálogo
- **Alcance:** funciones tipadas de acceso (`create_document`, `get_document`, `list_documents`, `update_editorial_status`, `create_version`, `get_active_by_sha256`, `create_job`, `update_job`, `enqueue_pending_op`, `claim_pending_ops`). Sin lógica de negocio.
- **Pruebas:** integración contra PostgreSQL real (docker compose), no solo mocks.
- **Salida:** catálogo operativo con transiciones de estado validadas y outbox operativo.

## Fase C — Almacén de PDF

### C1. Almacén content-addressed
- **Alcance:** `save_pdf(bytes) -> storage_key`, `open_pdf(storage_key)`, `delete_if_orphan(storage_key)` según ADR-0002. Interfaz acotada y tipada; `storage_key` opaca.
- **Pruebas:** unitarias con tmp dir; verificación de hash al guardar.
- **Salida:** archivos persistidos deduplicados por contenido, no servidos por la web.

## Fase D — Ingesta incremental

### D1. Extracción y chunking
- **Alcance:** extracción de texto por página (pypdf o similar — dependencia a justificar), chunking con metadatos estables (`document_id`, `version_id`, `chunk_id`, `page`, `chunk_index`, `content_hash`), detección de PDF sin texto (`ING_EXTRACT_FAILED`).
- **Pruebas:** unitarias con PDFs de fixture (con texto y escaneado).
- **Versiones:** una nueva versión no se activa hasta estar procesada e indexada; la activa anterior queda intacta si la nueva falla (`specs/ADR-001` §20).
- **Salida:** chunks listos para indexar, con páginas conservadas.

### D2. Indexación en Pinecone + BM25 con outbox
- **Alcance:** cliente Pinecone acotado (crear índice si no existe, validar dimensión — patrón M4), upsert con IDs deterministas por namespace de versión, chunks persistidos en PostgreSQL como fuente de verdad. Toda indexación/purga se registra primero en `pending_index_ops` (outbox) y se ejecuta con reintentos idempotentes; estado por destino (`pinecone`/`bm25`).
- **Pruebas:** integración con Pinecone real configurado por env (o skip sin credenciales); unitarias de generación de IDs y del outbox.
- **Salida:** indexación idempotente por documento/versión, con estado por destino.

### D3. Worker de ingesta
- **Alcance:** consumidor de cola (patrón Redis Streams de M7) que ejecuta: validar → extraer → chunking → persistir chunks → indexar (vía outbox) → verificar ambos destinos → `available` → activación editorial según rol. Manejo de errores con códigos estables, aislamiento de parciales (ADR-0004).
- **Pruebas:** integración con fakeredis + PDFs fixture; escenario de fallo a mitad deja `failed` sin parciales consultables y sin desactivar la versión activa anterior.
- **Salida:** ingesta asíncrona completa, separada de la API.

## Fase E — API de documentos

### E1. Endpoints de upload y consulta
- **Alcance:** `POST /api/documents` (202), `GET /api/documents` (visibilidad según rol), `GET /api/documents/{id}`, `GET /api/ingestion-jobs/{job_id}` según ADR-0006. Validación síncrona mínima antes de encolar.
- **Pruebas:** TestClient + fakeredis + PostgreSQL de prueba.
- **Salida:** ciclo upload → polling → `available` funcionando.

### E2. Borrado, restauración y reintento
- **Alcance:** `DELETE /api/documents/{id}` (202, cortina editorial `withdrawn`/`deleted` + purga vía outbox), `POST .../retry`, `POST .../versions/{version_id}/restore`, purga de namespaces Pinecone y archivos huérfanos.
- **Pruebas:** E2E de Spec 05 escenario 5 (borrar impide recuperar) y restauración (Spec 02 §67).
- **Salida:** retiro inmediato percibido, purga física eventual y observable.

## Fase F — Recuperación híbrida sobre el catálogo

### F1. Retriever híbrido persistente
- **Alcance:** adaptar `HybridRetriever` de M7: BM25 reconstruido desde `document_chunks` de PostgreSQL + Pinecone por namespace + RRF existente. **Filtrado por visibilidad:** solo fragmentos de documentos `published`/`published_pending_review` con versión activa `available` (Spec 06 §72, §104). BM25 actualizable por versión.
- **Pruebas:** unitarias de fusión (existentes adaptadas) + integración con corpus pequeño real; caso de prueba: un documento `pending_review`/`withdrawn` no es recuperable aunque sus vectores sigan en Pinecone.
- **Salida:** consulta híbrida sobre documentos publicados, con provenance documento/página.

## Fase G — Dominio pedagógico (ADR-0007)

### G1. Contratos pedagógicos
- **Alcance:** `StudyContext`, `LearningObjective`, `PracticeSet`, `StudyResponse`, `SourceReference` (derivado de `SourceEvidence`).
- **Pruebas:** validación de contratos.
- **Salida:** contratos del nuevo dominio listos.

### G2. Adaptación del grafo
- **Alcance:** reemplazar `SystemProfile`/criterios/alternativas por los contratos G1; reescribir prompts de supervisor/especialistas; conservar mecánica (validator, límites, invalidación). El texto recuperado se delimita como dato no confiable (Spec 06 §94).
- **Pruebas:** adaptar `test_behavior`/`test_regression` a los contratos nuevos con fake model.
- **Salida:** grafo de estudio pedagógico funcionando con RAG como herramienta.

## Fase H — Identidad, moderación y auditoría (Spec 06)

### H1. Identidad y RBAC
- **Alcance:** tablas `users`, `roles`, `role_assignments` (Spec 06 §35); invitación/activación/suspensión por admin; sesiones/tokens seguros con expiración; sin registro público; sin enumeración de correos (Spec 06 §41); autorización en servidor en cada endpoint.
- **Pruebas:** permisos por rol (Spec 06 §107); cuenta suspendida no accede; sin enumeración de correos.
- **Salida:** comunidad cerrada operativa con los 4 roles.

### H2. Moderación
- **Alcance:** endpoints `/api/moderation/*` (ADR-0006); flujo `pending_review` → approve/reject/withdraw/changes; `published_pending_review` para trusted_contributor con revisión posterior; motivos registrados; el autor ve el motivo de rechazo de sus aportes (Spec 06 §16).
- **Pruebas:** transiciones editoriales válidas/inválidas; visibilidad según rol (Spec 06 §106); aportes privados de otros usuarios inaccesibles.
- **Salida:** flujo de publicación/moderación completo y auditable.

### H3. Auditoría append-only
- **Alcance:** tabla `audit_events` append-only (sin operación ordinaria de edición/borrado); eventos: login, invitaciones, cambios de rol, cargas, dedup, versiones, publicación, rechazo, retirada, restauración, indexación/reconciliación; actor, timestamp UTC, acción, entidad, versión, resultado, motivo/código (Spec 06 §81-86); lectura restringida; sin secretos ni documentos completos en logs.
- **Pruebas:** eventos generados por las acciones clave; append-only verificado; sin secretos en logs.
- **Salida:** auditoría completa de acciones sensibles.

## Fase I — Cierre de calidad

### I1. Los 5 escenarios E2E de Spec 05
- Upload → available → consulta exclusiva con fuente/página; duplicado no duplica; sin evidencia → reconoce insuficiencia; insuficiencia + elección → web etiquetada (requiere proveedor web, tarea aparte); borrar → no recuperable.

### I2. Concurrencia y persistencia real
- Ingesta + consulta concurrentes; estado tras reinicio; Redis/checkpointer reales en integración (Spec 05 §11).

### I3. README, diagrama y comando reproducible
- Diagrama de arquitectura actualizado, `.env.example`, checklist clean clone.

## Reglas del plan

1. Cada tarea es un commit revisable con sus pruebas; no se avanza a la siguiente sin verde.
2. Las dependencias nuevas (asyncpg/SQLAlchemy, extractor PDF, cliente Pinecone) se justifican en su tarea, nunca antes.
3. PostgreSQL y Pinecone requieren servicios locales/externos: docker compose se extiende en B1, nunca se instalan servicios en las fuentes M1–M7.
4. **Visibilidad = editorial + técnico (Spec 06 §72, §104):** toda tarea que toque recuperación o listados aplica el filtro `published`/`published_pending_review` + versión activa `available`.
5. **Autorización en servidor en cada endpoint** (Spec 06 §9): hasta implementar H1, los endpoints privados rechazan sin identidad válida; la UI nunca es control de seguridad.
6. La búsqueda web (Spec 04) queda fuera de este plan hasta aprobar la Fase G; se planificará como tarea propia.
