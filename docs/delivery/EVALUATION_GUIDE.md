# ADA Study — Guía de evaluación técnica (DOC-01)

Guía de evaluación para el corrector automatizado (Ticher) y el equipo docente de
Coderhouse — AI Engineering. Permite comprender el sistema, verificar cómo cumple
cada requisito de la consigna del proyecto final (Módulo 8) y localizar las
evidencias en el repositorio.

- **Fecha del documento:** 2026-11-02.
- **Fuente de verdad:** el código implementado (`src/ada_study/`). Toda afirmación
  funcional fue contrastada con el código fuente.
- **Resultados de pruebas citados:** corresponden a una ejecución verificada el
  2026-11-02 en el entorno de desarrollo (pytest y ruff). No se incluyen resultados
  que no puedan reproducirse con los comandos indicados.
- **Consigna oficial de referencia:** `ADA_Study_Spec_Kit/specs/05_TESTING_AND_DELIVERY.md`
  (§ "Consigna del curso"), que transcribe los requisitos de la presentación del
  proyecto final (Módulo 8): integración de M1–M7, API, contratos Pydantic,
  persistencia/checkpointing, observabilidad, al menos cinco pruebas E2E, README con
  diagrama y decisiones de arquitectura, ejecución local reproducible y Python 3.12.
- **Alcance:** solo funcionalidades implementadas. Las funcionalidades futuras se
  citan únicamente como limitaciones o trabajo pendiente declarado.

---

## 1. Executive Summary

ADA Study es un sistema de estudio asistido por IA orientado a estudiantes de 12 a
15 años, construido como trabajo final de la carrera de AI Engineering de
Coderhouse.

- **Problema que resuelve:** los estudiantes no disponen de una forma confiable de
  consultar material de estudio propio o provisto por el docente: las respuestas de
  un LLM genérico pueden fabricar contenido sin respaldo documental, inadecuado
  para un público escolar.
- **Objetivo pedagógico:** responder consultas de estudio con evidencia verificable
  proveniente de una biblioteca documental moderada, reconociendo explícitamente
  cuándo la evidencia es insuficiente en lugar de inventar contenido
  (`StudyResponse.status = "insufficient_evidence"`,
  `src/ada_study/contracts/study.py`).
- **Conocimiento documental mediante RAG:** los PDF cargados se extraen, fragmentan
  y persisten en PostgreSQL; la recuperación se realiza con un índice BM25 propio,
  incremental y por versión (`src/ada_study/index/`), con filtros de visibilidad
  editorial y procedencia completa por fragmento.
- **Orquestación multiagente:** un grafo LangGraph
  (`src/ada_study/graph/builder.py`) coordina supervisor, researcher, tutor,
  validator, synthesizer y `limited_finish`, con enrutado condicional
  determinístico, ciclo de reparación y salida segura.
- **API profesional:** FastAPI con autenticación por token opaco, contratos
  Pydantic estrictos (`StrictModel`), códigos de error estables (ADR-0005) y
  documentación OpenAPI automática.
- **Persistencia conversacional:** checkpointer `AsyncPostgresSaver` de LangGraph
  sobre PostgreSQL, activable con `ADA_CONVERSATIONS_ENABLED=true`
  (`src/ada_study/db/checkpointer.py`).
- **Observabilidad:** traza estructurada incluida en `StudyResponse.trace`
  (siempre activa) y exportación opcional de spans OpenTelemetry/OpenInference a
  Arize Phoenix (`src/ada_study/observability/`).
- **Despliegue local reproducible:** un único comando
  (`docker compose up --build`) levanta PostgreSQL, Phoenix, migraciones, API,
  worker de ingesta y UI Streamlit desde un clon limpio.

La calidad del sistema se apoya en la suite automatizada (521 passed, 1 skipped en
la ejecución del 2026-11-02, `python -m pytest`) y en decisiones de arquitectura
documentadas (`docs/adr/ADR-0001` a `ADR-0007`).

## 2. Project Overview

### Casos de uso principales

1. **Gestión de la biblioteca documental:** carga de PDF (endpoint `202 Accepted`
   con `job_id`), ingesta asíncrona (extracción, chunking, indexación), control de
   versiones, borrado con restauración inmutable y reintento de versiones.
2. **Moderación editorial:** los aportes de estudiantes ingresan a
   `pending_review`; un moderador aprueba (`approve`) o rechaza (`reject`) desde la
   cola de moderación. Solo los documentos `published` (con versión activa
   `available`) son recuperables por el RAG.
3. **Consulta de estudio (ADA):** el estudiante formula una pregunta en lenguaje
   natural y recibe una `StudyResponse` estructurada con explicación, referencias
   al corpus (documento, página, chunk) y estados contractuales (`complete` o
   `insufficient_evidence`).
4. **Auditoría:** todas las acciones relevantes quedan registradas y se consultan
   vía `GET /api/audit/events` (moderator/admin).

### Usuarios y roles

El modelo de identidad (`src/ada_study/domain/identity_service.py`) define roles con
autorización verificada en el backend (Spec 06):

- **student:** consulta a ADA, sube documentos (quedan `pending_review`); el
  `ViewerContext` habilita el acceso del autor a su propio `pending_review` (el RAG
  compartido lo excluye, ADR-0005).
- **moderator:** cola de moderación, decisión sobre documentos, auditoría.
- **admin:** gestión de cuentas y todas las operaciones anteriores.

La creación del primer administrador se realiza con la CLI interactiva
(`python -m ada_study.cli`, contraseña por stdin con `getpass`, idempotente).

### Flujo de estudio típico

1. Un usuario autenticado carga un PDF → `202` + `job_id` (`POST /api/documents`).
2. El worker consume el outbox, extrae y fragmenta el texto, persiste los chunks en
   PostgreSQL e indexa las versiones activadas en los destinos (BM25 y Pinecone
   simulado).
3. El autor envía a revisión (`POST /api/documents/{id}/submit`); un moderador
   aprueba desde `GET /api/moderation/queue` → el documento queda `published`.
4. El estudiante consulta a ADA (`POST /api/study`); el sistema recupera evidencia
   pertinente del corpus publicado y responde con referencias, o declara
   explícitamente la insuficiencia.
5. Cada acción queda en el registro de auditoría (`GET /api/audit/events`).

No se presentan aquí funcionalidades futuras (búsqueda web etiquetada, Pinecone
real, etc.); su estado se declara en las secciones 5 y 14.

## 3. Architecture Overview

El sistema está organizado en capas independientes bajo `src/ada_study/`:

| Capa | Componentes clave | Archivos |
|---|---|---|
| API | FastAPI (factory `create_app`), routers por dominio | `src/ada_study/api/app.py`, `routes_*.py` |
| Dominio | servicios con códigos de error estables | `src/ada_study/domain/` (identity, moderation, conversation) |
| Persistencia | catálogo PostgreSQL, única fuente de estado | `src/ada_study/db/repository.py`, `db/migrations.py`, `db/checkpointer.py` |
| Ingesta | extracción PDF, chunking, outbox de indexación | `src/ada_study/ingestion/extraction.py`, `ingestion/destinations.py`, `src/ada_study/worker_cli.py` |
| RAG | índice BM25 propio, retriever híbrido, filtros | `src/ada_study/index/bm25_index.py`, `index/retriever.py` |
| Grafo LangGraph | supervisor + 5 nodos especializados | `src/ada_study/graph/builder.py` y módulos por nodo |
| Runtime de estudio | orquestación por turno, traza, historial | `src/ada_study/study/runtime.py` |
| LLM | factoría de chat model con modo determinístico | `src/ada_study/llm/factory.py` |
| Contratos | modelos Pydantic estrictos | `src/ada_study/contracts/` (documents, study, trace, conversations) |
| Observabilidad | instrumentación por nodo + exportación Phoenix | `src/ada_study/observability/trace.py`, `observability/phoenix.py` |
| UI | Streamlit (login, biblioteca, moderación, auditoría, estudio, trazas) | `src/ada_study/ui/` (`app.py`, `pages/`, `trace_view.py`) |

**Estado real del adaptador vectorial:** `HybridRetriever`
(`src/ada_study/index/retriever.py`) acepta una función de búsqueda vectorial
opcional (`vector_search`) y fusiona con RRF (k=60). En producción el runtime la
instancia con `vector_search=None`: la recuperación efectiva es **BM25 puro**. El
destino Pinecone está implementado como `FakePineconeDestination`
(`src/ada_study/ingestion/destinations.py`), un doble de pruebas; no existe
integración con el servicio real de Pinecone. La fusión RRF bm25+vector queda
preparada pero inactiva hasta disponer de búsqueda vectorial real.

**Diagrama de arquitectura:** el README documenta la arquitectura general en la
sección `Arquitectura` (`README.md`, línea 225) y el grafo de agentes con un
diagrama Mermaid en la sección `Grafo de agentes (LangGraph)` (`README.md`,
líneas 258–266). Este documento no lo duplica.

## 4. Multi-Agent Architecture

El grafo pedagógico (`src/ada_study/graph/builder.py`, `build_graph`) tiene la
topología `START -> supervisor -> (researcher | tutor | validator | synthesizer |
limited_finish | END)`, conservada de M7 (docstring del builder; ADR-0007). Los
workers (`researcher`, `tutor`) vuelven al supervisor; el validator decide de forma
autónoma mediante enrutado condicional.

| Nodo | Responsabilidad real | Fuente |
|---|---|---|
| `supervisor` | Decide el siguiente agente según el estado (evidencia, respuesta, validación). En producción recibe `chat_model=None` y usa **enrutado determinístico** (decisión TASK-11). Emite eventos de decisión trazables. | `graph/builder.py` (`make_supervisor_node`, `route_after_supervisor`) |
| `researcher` | Recupera evidencia del corpus mediante el retriever; construye el `research_artifact` con `SourceReference` (procedencia completa). | `graph/researcher.py` |
| `tutor` | Único nodo que usa el LLM (cuando se configura `ADA_LLM_PROVIDER`): redacta `explanation` desde la evidencia delimitada como dato no confiable; las referencias se copian verbatim y el `status` se decide determinísticamente. Fallback determinístico ante fallo del proveedor; con evidencia insuficiente no se llama al LLM. | `graph/tutor.py` (`make_tutor_node`, `MAX_LLM_EXPLANATION_CHARS = 8000`) |
| `validator` | Validación **determinística** (sin LLM): presencia de artefactos, contratos Pydantic de evidencia y respuesta, y citas usadas ⊆ evidencia recuperada. Clasifica errores por especialista responsable. | `graph/validator.py` (`MAX_RETRIES = 2`) |
| `synthesizer` | Publica la `StudyResponse` validada y consolida el historial pedagógico. Solo corre con validación vigente aprobada. | `graph/synthesizer.py` |
| `limited_finish` | Salida segura: responde sin fabricar contenido cuando las reparaciones se agotan. | `graph/synthesizer.py` (`limited_finish_node`) |

**Delegación y enrutamiento condicional:**

- Los conditional edges (`graph.add_conditional_edges` en `builder.py`) aplican las
  invariantes de la mecánica M7: el `synthesizer` solo corre tras validación
  aprobada y el `END` exitoso solo con respuesta publicada y validación vigente.
- **Ciclo de reparación:** si el validator detecta errores reparables, el enrutador
  `route_after_validator` deriva directamente al especialista responsable
  (`researcher` o `tutor`, según `pending_repair_agent` y la clasificación de
  errores), formando el ciclo de reparación.
- **Presupuesto de reintentos:** `MAX_RETRIES = 2` (`validator.py`); con
  `retries >= 2`, o sin errores reparables, el flujo deriva a `limited_finish`
  (salida segura, sin respuesta fundamentada fabricada). Adicionalmente, el runtime
  fija `recursion_limit = 25` (`STUDY_RECURSION_LIMIT`, `study/runtime.py`) como
  cota global de pasos.
- **Validación:** cada check del validator se registra en la traza
  (`pass`/`fail`/`skipped`) y los errores se clasifican por especialista
  (`research_errors` / `tutor_errors`).

**Aclaración expresa (decisión TASK-11):** el Supervisor utiliza **enrutamiento
determinístico** en producción (`compile_graph(chat_model=None, ...)`); el LLM se
utiliza únicamente en el nodo **tutor** para redactar la explicación, con fallback
determinístico ante fallo del proveedor. La decisión de `status` (completa vs.
insuficiente) es determinística: el LLM no la altera. Ver `README.md` (sección
`Grafo de agentes (LangGraph)`), `docs/adr/ADR-0007-*` y
`src/ada_study/graph/builder.py`.

## 5. RAG Architecture

### Ingesta documental (pipeline)

1. `POST /api/documents` (PDF) → almacenamiento content-addressed con
   `storage_key` opaca (ADR-0002, seguridad Spec 06) → `202` + `job_id`.
2. El worker (`src/ada_study/worker_cli.py`) consume el outbox `pending_index_ops`
   (ADR-0001): extracción de texto (`ingestion/extraction.py`; un PDF escaneado sin
   texto produce `ScannedPdfError` y no se publica), chunking y persistencia de
   chunks en PostgreSQL (`document_chunks`, única fuente de verdad, ADR-0003).
3. Indexación en los destinos: `BM25Destination` (real) y
   `FakePineconeDestination` (simulado), con verificación de destinos antes de
   activar la versión (`ingestion/destinations.py`, ADR-0003).
4. La retirada/borrado aplica la "cortina editorial" (`withdrawn`/`deleted`): las
   consultas posteriores no recuperan contenido retirado (ADR-0004; pruebas en
   `tests/test_worker_curtain.py`).

### Índice BM25 y recuperación

- **Índice BM25 propio** (`src/ada_study/index/bm25_index.py`): Okapi BM25
  (k1=1.5, b=0.75) a nivel de chunk, con altas/bajas **incrementales por versión**
  (`add_version` / `remove_version`) y `rebuild` desde los chunks persistidos solo
  para arranque en frío. No se usa `rank_bm25` porque `BM25Okapi` exige
  reconstrucción completa y no aporta procedencia por chunk. Las consultas se
  depuran con `significant_terms` (stopwords funcionales del español;
  RAG-QUALITY-01 D-2).
- **Recuperación** (`src/ada_study/index/retriever.py`): `HybridRetriever.search`
  aplica filtros de visibilidad en **dos capas** — (1) el índice solo contiene
  versiones activadas; (2) re-verificación contra el catálogo de que cada chunk
  pertenece a un documento publicado con versión activa `available` — y conserva la
  procedencia completa por fragmento: `document_id`, `version_id`, `chunk_id`,
  `page`, `content_hash` (Spec 02 §18). Con `ViewerContext`, el autor recupera
  además su propio `pending_review`.
- **Restricción de fuentes:** el RAG compartido solo recupera documentos
  `published` / `published_pending_review` con versión activa `available`
  (`_VISIBLE_EDITORIAL` en `retriever.py`).

### Evidencia insuficiente y protección anti-fabricación

- El `researcher`/`tutor` deciden de forma determinística el estado contractual:
  `complete` exige coincidencia de términos significativos y cantidad suficiente
  (≥ 2 fragmentos, o el único fragmento con aviso de cantidad en `evidence_gaps`)
  (`src/ada_study/contracts/study.py`, semántica RAG-QUALITY-01).
- Con `status = "insufficient_evidence"` no se fabrica contenido ni citas;
  `evidence_gaps` explica el motivo y el validator impone `cited ⊆ evidence`.
- Las `SourceReference` conservan `document_id`, `version_id`, `chunk_id`, `page`
  y `content_hash` (Spec 02 §18): toda afirmación es trazable a documento y página.

### Limitación declarada: Pinecone

`FakePineconeDestination` (`ingestion/destinations.py`) está implementado como
**destino simulado** (doble de pruebas que replica upsert/delete/verificación por
versión, ADR-0001/0003). El flujo efectivo de estudio utiliza **BM25**; este
sistema **no debe presentarse como una integración real de Pinecone**. La fusión
RRF bm25+vector permanece implementada pero inactiva en runtime
(`vector_search=None`). Ver sección 14 (Known Limitations).

## 6. API and Data Contracts

La API es FastAPI con factory `create_app` (`src/ada_study/api/app.py`); la
documentación OpenAPI se sirve automáticamente en `/docs` (Swagger UI) y
`/openapi.json` (usado además como healthcheck del contenedor en
`docker-compose.yml`).

### Endpoints principales (rutas verificadas en el código)

| Método y ruta | Función | Archivo |
|---|---|---|
| `POST /api/auth/login` | Autentica credenciales; devuelve token opaco de sesión | `api/routes_auth.py` |
| `POST /api/auth/logout` | Cierra la sesión (`204`) | `api/routes_auth.py` |
| `GET /api/me` | Perfil del usuario autenticado (excluye `password_hash`) | `api/routes_auth.py` |
| `POST /api/documents` | Carga de PDF (`202` + `job_id`) | `api/routes_documents.py` |
| `GET /api/documents` | Listado paginado del catálogo | `api/routes_documents.py` |
| `GET /api/documents/{document_id}` | Detalle con versiones y estado | `api/routes_documents.py` |
| `GET /api/ingestion-jobs/{job_id}` | Estado del job de ingesta | `api/routes_documents.py` |
| `POST /api/documents/{id}/submit` | Envío a revisión (`pending_review`) | `api/routes_documents.py` |
| `DELETE /api/documents/{id}` | Borrado lógico con cortina (`202`) | `api/routes_documents.py` |
| `POST /api/documents/{id}/versions/{v}/restore` | Restauración inmutable (`202`) | `api/routes_documents.py` |
| `POST /api/documents/{id}/versions/{v}/retry` | Reintento de versión fallida (`202`) | `api/routes_documents.py` |
| `POST /api/moderation/documents/{id}/decide` | Decisión de moderación (`approve`/`reject`) | `api/routes_moderation.py` |
| `GET /api/moderation/queue` | Cola de aportes pendientes (moderator) | `api/routes_moderation.py` |
| `POST /api/study` | Consulta de estudio con RAG + grafo LangGraph | `api/routes_study.py` |
| `GET /api/audit/events` | Registro de auditoría (moderator/admin) | `api/routes_audit.py` |

### Autenticación

- `POST /api/auth/login` valida credenciales con `IdentityService`; errores de
  credenciales son **genéricos** (`InvalidCredentialsError`, sin enumeración de
  correos) → `401` (ADR-0005, Spec 06). `ForbiddenError` → `403`.
- El token de sesión es opaco y viaja como `Bearer`; la identidad del
  `ViewerContext` de `/api/study` se deriva SIEMPRE del backend, nunca del cuerpo
  de la petición (Spec 06 §9).

### Contratos y validación

- Los contratos (`src/ada_study/contracts/`) heredan de `StrictModel`
  (`src/ada_study/strict_model.py`): Pydantic estricto (sin campos extra). Salida
  principal: `StudyResponse` (`status`, `topic`, `explanation`, `references`,
  `uses_web_sources`, `evidence_gaps`, `limitations`, `conversation_id`, `trace`).
- **Códigos de error estables** (ADR-0005): `ING_*` (ingesta),
  `DEL_*` (borrado/restauración), `MOD_*` (moderación, con mapeo HTTP en
  `api/routes_moderation.py`), más `STUDY_EMPTY_REQUEST`, `STUDY_INTERNAL` (503,
  sin detalles internos) y `CONVERSATION_NOT_FOUND` (404) / errores de conversación
  (422) en `routes_study.py`.
- Validación de entradas: `StudyRequest` limita `user_request`
  (`MAX_REQUEST_CHARS`) y `conversation_id` es opcional; campos extra se ignoran y
  la titularidad de la conversación la decide siempre el servidor.
- Documentación OpenAPI generada por FastAPI; todos los routers con prefijos
  `/api/*` (ADR-0006: compatibilidad M7, `/tasks` intacta).

## 7. Asynchronous Architecture

- **SQLAlchemy async + asyncpg:** el catálogo usa un engine asíncrono
  (`DATABASE_URL` con driver `postgresql+asyncpg`); todas las operaciones del
  `CatalogRepository` (`src/ada_study/db/repository.py`) son `async`.
- **Psycopg async para el Checkpointer:** `src/ada_study/db/checkpointer.py`
  envuelve el `AsyncPostgresSaver` de LangGraph sobre un pool psycopg en modo
  autocommit (`autocommit=True, prepare_threshold=0`, requisito del checkpointer
  para doble buffering en PostgreSQL).
- **`ainvoke` / `astream`:** el runtime (`study/runtime.py`) ejecuta el grafo con
  `astream(stream_mode="updates")` — semánticamente equivalente a `ainvoke`
  (mismo estado final acumulado), pero permite recolectar eventos de pasos
  completados aunque un paso posterior falle — y consulta el estado previo con
  `aget_state` (intake del historial conversacional). La composición del tutor con
  LLM es `await` del chat model.
- **Operaciones asíncronas de persistencia:** alta de documentos, transiciones de
  versión, decisiones de moderación, auditoría y resolución de conversaciones son
  todas operaciones async sobre PostgreSQL; el worker consume el outbox de forma
  asíncrona (`ingestion/destinations.py` con destinos `async`).
- **Diferencia con la UI:** las páginas Streamlit (`src/ada_study/ui/pages/`) son
  **síncronas por naturaleza de Streamlit**: el cliente HTTP
  (`ui/api_client.py`) invoca a la API de forma síncrona; las operaciones externas
  asíncronas (DB, grafo, worker) viven exclusivamente en los servicios backend.

## 8. Conversation Persistence

- **Mecanismo:** el checkpointer de LangGraph (`AsyncPostgresSaver`) sobre
  PostgreSQL, montado en el lifespan de la API cuando
  `ADA_CONVERSATIONS_ENABLED=true` (`src/ada_study/db/checkpointer.py`); el
  esquema del checkpointer se crea al arrancar con `setup()` (idempotente).
- **Identificación de conversaciones:** el `thread_id` de LangGraph se deriva
  SIEMPRE de una conversación autorizada por el servidor. `POST /api/study`
  resuelve la conversación con verificación de titularidad
  (`ConversationService.resolve_conversation`, `domain/conversation_service.py`)
  ANTES de invocar el grafo; el cliente nunca envía `thread_id` ni propietario.
  Los turnos de una misma conversación se serializan con un lock por thread
  (`CHECKPOINTER-03`, `routes_study.py` §8).
- **Recuperación del estado:** el historial pedagógico del turno anterior se
  recupera del checkpointer (`aget_state`), nunca del cliente; el mensaje nuevo se
  incorpora exactamente una vez (guarda anti-duplicado en `append_student_message`)
  y los artefactos temporales se reinician por turno (`study/runtime.py`).
- **Persistencia entre reinicios:** el estado vive en PostgreSQL (tablas del
  checkpointer), por lo que sobrevive a reinicios del proceso y del contenedor.
- **Configuración:** `ADA_CONVERSATIONS_ENABLED=true` (default `false`, modo
  efímero por petición). Requiere `DATABASE_URL` accesible: con conversaciones
  habilitadas y base inaccesible, la API falla al arrancar (fail-loud). Los cambios
  requieren reiniciar la API.
- **Pruebas que demuestran la persistencia:**
  `tests/test_checkpointer.py` (unitaria),
  `tests/integration/test_checkpointer_postgres.py` (integración contra PostgreSQL
  real: persistencia del estado por `thread_id`),
  `tests/test_conversation_history.py` y `tests/test_conversations.py`
  (historial, titularidad, serialización por lock).
  Adicionalmente, la validación de despliegue (FINAL-04, 2026-10) verificó un
  roundtrip aislado del checkpointer (`aput`/`aget_tuple`) contra el PostgreSQL de
  Compose y la persistencia de datos entre `docker compose restart` y
  `down` + `up -d`.

**Nota de honestidad:** la persistencia de una **conversación multi-turno
ejecutada en la aplicación desplegada con LLM real** (varios turnos sucesivos con
`ADA_LLM_PROVIDER` configurado y `ADA_CONVERSATIONS_ENABLED=true`) **no fue
verificada manualmente**; esa ejecución requiere llamadas pagas a un proveedor LLM.
Lo verificado en la app desplegada es: checkpointer habilitado y funcional contra
PostgreSQL real, y persistencia del catálogo entre reinicios. Ver sección 11.

## 9. Observability

### 1. Instrumentación implementada

- Cada nodo del grafo se envuelve con `traced_node`
  (`src/ada_study/observability/trace.py`): registra inicio, fin y duración REALES
  de cada ejecución (incluidos reintentos) y combina los eventos de decisión que
  cada nodo emite (`TraceEvent`: nodo, tipo, mensaje, detalles, timestamp).
- La ejecución genera un `trace_id` (`uuid4().hex`) que viaja como metadata de la
  invocación de LangGraph (`ada_trace_id`, `study/runtime.py`); los spans de
  Phoenix comparten ese identificador, permitiendo correlacionar la traza
  estructurada con los spans técnicos.
- La **traza estructurada** (`Trace` con `trace_id`, `conversation_id`,
  `started_at`, `completed_at`, `duration_ms`, `status`, `nodes_executed`,
  `events`, `error`) viaja siempre dentro de `StudyResponse.trace`
  (`contracts/trace.py`), independiente de Phoenix.
- Correlación con la conversación: `Trace.conversation_id` es el thread del
  checkpointer; los turnos comparten el historial y la traza del turno
  (`runtime.py`).
- Panel de trazabilidad en Streamlit: `src/ada_study/ui/trace_view.py` renderiza
  la traza recibida (nodos ejecutados, eventos, duración).

### 2. Spans técnicos (OpenTelemetry/OpenInference → Phoenix)

- `src/ada_study/observability/phoenix.py` configura el proveedor OTel y exporta
  spans reales de LangChain/LangGraph (nodos + llamadas al proveedor LLM) por OTLP
  HTTP al endpoint `ADA_PHOENIX_ENDPOINT` (debe incluir la ruta `/v1/traces`).
- La exportación es **opcional** (`ADA_PHOENIX_ENABLED`, default `false`) y
  requiere el extra `pip install -e ".[observability]"`.

### 3. Diferenciación de evidencias (importante para la evaluación)

| Nivel | Estado | Evidencia |
|---|---|---|
| Instrumentación en el código | Implementada | `observability/trace.py`, `observability/phoenix.py`, `traced_node` en todos los nodos |
| Tests con exportadores de prueba | Verificado | `tests/test_study_trace.py`: spans reales capturados con `InMemorySpanExporter` comparten `trace_id` via `ada_trace_id` (`test_spans_share_ada_trace_id`); `test_phoenix_disabled_by_default` |
| Exportación real al servidor Phoenix | Implementada (exportador OTLP HTTP) | Configurada en Compose (servicio `phoenix`, UI `http://localhost:6006`); activación con `ADA_PHOENIX_ENABLED=true` |
| Evidencia visual de ejecuciones (capturas de Phoenix) | **PENDIENTE** | No disponible; ver sección 16 |

La validación visual de cinco trazas en el panel de Phoenix queda marcada como
**PENDIENTE** hasta disponer de capturas reales. La exportación de spans con
`ADA_PHOENIX_ENABLED=true` en el entorno desplegado no fue verificada con
capturas; lo verificado con el exportador en memoria son spans reales de LangGraph
con correlación `ada_trace_id`.

## 10. Deployment

### Composición del despliegue (Docker Compose)

| Servicio | Imagen / rol | Detalle |
|---|---|---|
| `postgres` | PostgreSQL 16 | Healthcheck propio; volumen `ada_pgdata` (persistencia) |
| `phoenix` | Arize Phoenix | UI de trazas en `:6006` |
| `migrate` | Imagen única de la app | One-shot: aplica el esquema idempotente al arrancar; `exit 0` |
| `api` | Imagen única de la app | FastAPI; healthcheck sobre `/openapi.json` |
| `worker` | Imagen única de la app | Worker de ingesta (outbox `pending_index_ops`) |
| `ui` | Imagen única de la app | Streamlit en `:8501` |

- **Imagen única** (`Dockerfile`, base `python:3.12-slim`, extras
  `[ui,llm,observability]`) compartida por `api`, `worker`, `ui` y `migrate`.
  El `.env` **nunca se copia dentro de la imagen** (`.dockerignore`); la
  configuración se inyecta vía `env_file`.
- **Volúmenes de persistencia:** `ada_pgdata` (datos PostgreSQL, incluidas las
  tablas del checkpointer) y `ada_storage` (PDFs originales). `docker compose down`
  conserva los datos; `docker compose down -v` los elimina.

### Comando principal

```bash
docker compose up --build
```

Levanta todo el sistema desde un clon limpio; la cadena de arranque es
postgres (healthy) → migrate → api / worker / ui. Referencia completa:
`README.md` (sección `Despliegue local con Docker Compose (FINAL-02)`,
línea 36) y `docs/audit/DELIVERY_CHECKLIST.md`.

### URLs de acceso

| Servicio | URL |
|---|---|
| UI Streamlit | http://localhost:8501 |
| API FastAPI | http://localhost:8000 |
| Swagger / OpenAPI | http://localhost:8000/docs |
| Phoenix (trazas) | http://localhost:6006 |

### Requisitos y configuración

- Requisito previo: Docker Desktop (o Docker Engine + Compose v2). No se necesita
  Python ni venv en el host.
- Toda la configuración vive en variables de entorno (`.env` local a partir de
  `.env.example`): `ADA_CONVERSATIONS_ENABLED`, `ADA_PHOENIX_ENABLED`,
  `ADA_PHOENIX_ENDPOINT`, `ADA_LLM_PROVIDER` / `ADA_LLM_MODEL` /
  `ADA_LLM_API_KEY` (default `none`: modo determinístico, sin llamadas ni costos).
  `DATABASE_URL`, `ADA_STORAGE_DIR` y el endpoint de Phoenix los fija
  `docker-compose.yml` por la red interna.
- Variables de conversaciones y trazas requieren reiniciar la API para aplicarse.

### Creación inicial del administrador

```bash
docker compose run --rm api python -m ada_study.cli \
  postgresql+asyncpg://ada:ada_dev_password@postgres:5432/ada_study
```

Interactivo por diseño (email, nombre y contraseña por stdin con `getpass`,
mínimo 12 caracteres con dígito y letra); idempotente y sin elevación silenciosa
de cuentas existentes.

## 11. Testing Strategy

### Categorías

| Tipo | Alcance | Ubicación / comandos |
|---|---|---|
| Unitarias | Lógica con dobles de pruebas (retriever, grafo, contratos, runtime, API con fakes, UI client) | `tests/` (p. ej. `test_bm25_index.py`, `test_study_graph.py`, `test_state_rules.py`, `test_study_endpoint.py`) |
| Integración | PostgreSQL real (esquema, transiciones, dedup, checkpointer) | `tests/integration/` (5 archivos de prueba + `conftest.py`; se saltan sin `DATABASE_URL` apuntando a PostgreSQL real) |
| Sistema / E2E | Escenarios funcionales del pipeline completo con dobles controlados | `tests/test_e2e_scenarios.py` |
| Pruebas manuales funcionales | Validación del despliegue Compose (endpoints HTTP, reinicios, login) | Informe de validación FINAL-04 (sesión de trabajo, no archivo del repo) |
| Validación de observabilidad | Spans con exportador en memoria | `tests/test_study_trace.py`, `tests/test_study_trace_ui.py` |

### Resultados verificados (ejecución 2026-11-02)

```bash
python -m pytest        # 521 passed, 1 skipped (99.58 s)
python -m ruff check .  # All checks passed!
```

- Los tests de integración (`tests/integration/`) se ejecutan cuando
  `DATABASE_URL` apunta a un PostgreSQL real; sin ella se saltan. La ejecución
  citada corrió en el entorno de desarrollo con PostgreSQL accesible.
- No se contabilizan tests unitarios como pruebas E2E: los E2E automatizados
  (`tests/test_e2e_scenarios.py`) cubren los escenarios de Spec 05 con el sistema
  montado sobre PostgreSQL real y dobles controlados en las fronteras externas.
- Nota: `docs/audit/DELIVERY_CHECKLIST.md` cita cifras históricas de la suite
  (192 passed); la cifra vigente verificada es la indicada arriba.

### Matriz de escenarios funcionales previstos

| Escenario | Cobertura | Estado | Evidencia |
|---|---|---|---|
| Consulta de Historia (dominio documental genérico) | El RAG es agnóstico a la materia: recuperación BM25 sobre el corpus publicado | CUMPLE (en tests con corpus sintético) | `tests/test_e2e_scenarios.py`, `tests/test_rag_quality_05.py`, `tests/test_study_endpoint.py` |
| Consulta de Matemática | Ídem: el BM25 no distingue materia; la pertinencia es léxica (`significant_terms`) | CUMPLE (en tests) | `tests/test_bm25_index.py`, `tests/test_rag_quality_03.py` |
| Evidencia insuficiente | Estado contractual `insufficient_evidence` sin citas fabricadas | CUMPLE (en tests) | `tests/test_rag_quality_03.py`, `tests/test_rag_quality_05.py`, `tests/test_e2e_scenarios.py`, `tests/test_study_trace.py` |
| Conversación multi-turno | Unitario + integración del checkpointer contra PostgreSQL real | CUMPLE PARCIALMENTE: persistencia y titularidad probadas; conversación multi-turno con LLM real en la app desplegable **PENDIENTE DE EVIDENCIA** | `tests/test_checkpointer.py`, `tests/integration/test_checkpointer_postgres.py`, `tests/test_conversation_history.py` |
| Validación y reparación | Ciclo validator → researcher/tutor, presupuesto `MAX_RETRIES = 2`, `limited_finish` | CUMPLE (unitaria) | `tests/test_study_graph.py`, `tests/test_state_rules.py`, `tests/test_study_endpoint.py` (`test_limited_finish_on_retries_exhausted`) |
| Escenario E2E 4 de Spec 05 (búsqueda web etiquetada) | No implementado | PENDIENTE (decisión TASK-02, declarada en `docs/audit/DELIVERY_CHECKLIST.md`) | — |

## 12. Architecture Decision Records

Los ADRs se encuentran en `docs/adr/` (índice: `docs/adr/README.md`). Todos están
en estado **Propuesto** y derivan de los specs del Spec Kit
(`ADA_Study_Spec_Kit/specs/`) y de `specs/ADR-001` (aprobada).

| ADR | Título | Decisión | Justificación breve |
|---|---|---|---|
| ADR-0001 | `postgresql-catalog-versions` | Catálogo y versiones en PostgreSQL: dos ciclos de estado (editorial/técnico), outbox `pending_index_ops`, dedup explícita | Estado transaccional único y auditable; desacopla API de ingesta |
| ADR-0002 | `pdf-storage` | PDFs originales en almacén content-addressed con `storage_key` opaca; seguridad Spec 06 | Integridad y dedup por contenido; sin exposición de rutas internas |
| ADR-0003 | `pinecone-bm25-rrf` | Pinecone (namespace por versión) + BM25 propio + RRF; verificación de destinos antes de activar; sin ChromaDB | Incrementalidad por versión y procedencia; Pinecone como destino simulado (ver §5) |
| ADR-0004 | `deletion-restoration-eventual-consistency` | Cortina editorial (`withdrawn`/`deleted`), outbox + reconciliador, restauración inmutable | Consistencia eventual controlada entre catálogo e índices |
| ADR-0005 | `error-codes-and-states` | Dos enums de estado (editorial/técnico) + `ReviewStatus`; códigos `ING_*`/`DEL_*`/`MOD_*` | Contratos de error estables entre API, worker y UI |
| ADR-0006 | `endpoints-m7-compatibility` | `/api/documents`, `/api/moderation/*`, restauración; `/tasks` de M7 intacta; autorización por rol | Compatibilidad con la consigna del curso (API + contratos + persistencia) |
| ADR-0007 | `minimal-domain-change-ada-to-ada-study` | Cambio de dominio mínimo (M7 → estudio escolar): contratos y prompts, no topología; recuperación filtrada por editorial | Reutilizar la mecánica probada del grafo M7 cambiando solo el dominio |

No se han inventado ADRs ni alterado sus decisiones; la tabla reproduce el índice
oficial `docs/adr/README.md`.

## 13. Compliance Matrix — Coderhouse Final Project

Fuente de la rúbrica: la consigna del proyecto final (Módulo 8) transcrita en
`ADA_Study_Spec_Kit/specs/05_TESTING_AND_DELIVERY.md` (§ "Consigna del curso"):
integración de M1–M7, API, contratos Pydantic, persistencia/checkpointing,
observabilidad, al menos cinco pruebas E2E, README con diagrama y decisiones de
arquitectura, ejecución local reproducible y Python 3.12. Los pesos corresponden a
la rúbrica oficial del módulo.

| Criterio | Peso | Implementación | Evidencia | Estado |
|---|---:|---|---|---|
| Arquitectura y coherencia | 30% | Integración M1–M7 sobre una arquitectura en capas coherente: grafo LangGraph multiagente con enrutado condicional determinístico y ciclo de reparación (`graph/builder.py`), RAG con procedencia (`index/`), contratos Pydantic estrictos (`contracts/`), identidad/moderación/auditoría (`domain/`), coherencia total con 7 ADRs (`docs/adr/`) | `src/ada_study/graph/builder.py`; `docs/adr/README.md`; diagrama Mermaid en `README.md` (línea 266); tests de topología y reglas de estado: `tests/test_study_graph.py`, `tests/test_state_rules.py` | CUMPLE |
| Robustez técnica | 25% | Validación determinística con clasificación de errores por especialista y presupuesto de reintentos (`graph/validator.py`); salida segura `limited_finish`; `recursion_limit=25`; códigos de error estables con mapeo HTTP (ADR-0005); fallback determinístico del tutor ante fallo del proveedor LLM; fail-loud en configuración; PDFs escaneados rechazados (`ScannedPdfError`) | `graph/validator.py` (`MAX_RETRIES = 2`); `study/runtime.py` (`STUDY_RECURSION_LIMIT`); `tests/test_study_endpoint.py`, `tests/test_extraction.py` | CUMPLE PARCIALMENTE |
| Persistencia y observabilidad | 20% | PostgreSQL como única fuente de estado (catálogo, chunks, auditoría, conversaciones); checkpointer `AsyncPostgresSaver` activable (`db/checkpointer.py`); traza estructurada siempre activa en `StudyResponse.trace`; exportación OTel/OpenInference a Phoenix; `ada_trace_id` | `db/repository.py`, `db/checkpointer.py`, `observability/`; `tests/integration/test_checkpointer_postgres.py`; `tests/test_study_trace.py` | CUMPLE PARCIALMENTE |
| Documentación y despliegue | 15% | README con diagrama Mermaid del grafo y despliegue; 7 ADRs; `docs/audit/`; despliegue reproducible con un comando (`docker compose up --build`); `.env.example` sin secretos; checklist de entrega | `README.md`; `docs/adr/`; `docs/audit/DELIVERY_CHECKLIST.md`; `Dockerfile`, `docker-compose.yml`, `.dockerignore`, `.env.example` | CUMPLE |
| Entorno y estándares | 10% | Python 3.12 (imagen y venv); lint Ruff sin advertencias; contratos `StrictModel` (Pydantic estricto); sin secretos en repo ni imagen (auditoría de secretos limpia); ruff ejecutable con comandos documentados | `pyproject.toml` (`requires-python = ">=3.12"`); `.dockerignore`, `.gitignore`; `src/ada_study/strict_model.py` | CUMPLE |

### Detalle por criterio

**1. Arquitectura y coherencia (30%) — CUMPLE.**
La consigna exige integración de M1–M7 con decisiones de arquitectura
documentadas y coherencia entre capas. El sistema integra: identidad/seguridad
(Spec 06), catálogo documental (M2), ingesta incremental (M2/M4), RAG con
procedencia (M2/M5), grafo multiagente conservado de M7 (ADR-0007), API con
contratos y observabilidad. La topología, contratos y estados quedan verificados
por tests unitarios y de integración. Limitación declarada: el supervisor es
determinístico en producción (decisión TASK-11 documentada), y la recuperación
vectorial real (Pinecone) está simulada — ver §5 y §14.

**Robustez técnica (25%) — CUMPLE PARCIALMENTE.**
Exige manejo de errores, límites y degradación controlada. Implementado: ciclo de
reparación con presupuesto (`MAX_RETRIES = 2`, `recursion_limit = 25`), códigos de
error estables con mapeo HTTP (ADR-0005), fail-loud en configuración, token
opaco sin enumeración de usuarios, PDFs escaneados rechazados, protección
anti-fabricación (`cited ⊆ evidence`). La parcialidad se debe a que el destino
Pinecone es un doble de pruebas (`FakePineconeDestination`) y la búsqueda vectorial
real no está integrada; además el escenario E2E de búsqueda web (Spec 05 #4) no
está implementado.

**Persistencia y observabilidad (20%) — CUMPLE PARCIALMENTE.**
Persistencia: PostgreSQL como única fuente de estado, catálogo versionado,
outbox, checkpointer `AsyncPostgresSaver` con pruebas de integración reales y
persistencia verificada entre reinicios. Observabilidad: traza estructurada en
`StudyResponse.trace` (siempre activa) + spans OTel hacia Phoenix (opcional) con
correlación `ada_trace_id` verificada en tests. Queda **PENDIENTE DE EVIDENCIA**
la captura visual de trazas en Phoenix y la conversación multi-turno con LLM real
en la app desplegada (requiere llamadas pagas).

**Documentación y despliegue (15%) — CUMPLE.**
README con diagrama Mermaid del grafo y secciones de despliegue, observabilidad y
limitaciones; 7 ADRs; `docs/audit/` (baseline, matriz de contratos, plan,
checklist); despliegue reproducible con un comando (`docker compose up --build`)
verificado en la validación FINAL-04 (postgres → migrate → api/worker/ui;
persistence entre `restart` y `down`+`up -d`). Limitación menor: el checklist
histórico cita cifras antiguas de la suite.

**Entorno y estándares (10%) — CUMPLE.**
Python 3.12 en `pyproject.toml` e imagen `python:3.12-slim`; lint Ruff limpio
(verificado 2026-11-02); configuración por variables de entorno con `.env.example`
(sin secretos en repo, `.env` excluido de la imagen); convenciones de contratos
estrictos y códigos de error estables.

## 14. Known Limitations

Se distinguen **limitaciones de producto** (decisiones conscientes, documentadas)
de **incumplimientos de la consigna**. Ninguna de las limitaciones siguientes
invalida un requisito explícito de la rúbrica de Módulo 8.

| Limitación | Naturaleza | Detalle y ubicación |
|---|---|---|
| `FakePineconeDestination` | Limitación de producto (declarada) | El destino Pinecone es un doble de pruebas (`src/ada_study/ingestion/destinations.py`); la integración con credenciales reales queda pendiente (decisión TASK-03, `docs/audit/DELIVERY_CHECKLIST.md`) |
| Recuperación efectiva BM25 | Limitación de producto (declarada) | El runtime usa `HybridRetriever` con `vector_search=None`: recuperación BM25 puro; la fusión RRF bm25+vector existe pero está inactiva. README §"Limitaciones conocidas" |
| Supervisor determinístico | Decisión de diseño documentada (TASK-11) | El LLM solo redacta en el tutor; el enrutado del supervisor es determinístico. No es un incumplimiento: la consigna no exige supervisor con LLM |
| Búsqueda web etiquetada (Spec 05 #4) | No implementada | Escenario E2E 4 fuera de alcance (decisión TASK-02); no hay `uses_web_sources = true` en producción |
| Límites de la recuperación léxica | Limitación técnica documentada | BM25 sin stemming ni comprensión semántica (`bm25_index.py`); la pertinencia es léxica (`significant_terms`) |
| Cobertura de consultas generales | Limitación de producto | Consultas sin relación léxica con el corpus producen `insufficient_evidence` (comportamiento esperado, no error) |
| Validaciones manuales pendientes | Evidencia pendiente (no incumplimiento) | (a) conversación multi-turno con LLM real en la app desplegada; (b) exportación real de spans con `ADA_PHOENIX_ENABLED=true` con capturas; (c) validación visual de cinco trazas en Phoenix |
| Checklist histórico desactualizado | Documentación menor | `docs/audit/DELIVERY_CHECKLIST.md` cita 192 passed (cifra vigente verificada: 521 passed, 1 skipped, 2026-11-02) |
| Phoenix con imagen `latest` sin pin | Higiene de despliegue menor | `docker-compose.yml` no fija versión de la imagen de Phoenix (recomendación no bloqueante) |
| Sin UI healthcheck | Higiene de despliegue menor | El servicio `ui` no define healthcheck en Compose |

## 15. Evaluation Quick Start

Requisito previo: Docker Desktop (o Docker Engine + Compose v2). Para desarrollo
sin Docker, ver `README.md` (sección `API y worker (desarrollo)`) y
`docs/audit/DELIVERY_CHECKLIST.md`.

1. **Configurar el entorno:** `cp .env.example .env` y ajustar variables
   opcionales (`ADA_CONVERSATIONS_ENABLED`, `ADA_PHOENIX_ENABLED`, credenciales
   LLM). Sin LLM configurado, el sistema corre en modo determinístico (sin
   llamadas pagas).
2. **Levantar el sistema:**
   ```bash
   docker compose up --build
   ```
   Esperar a que `postgres` esté healthy; `migrate` aplica el esquema y termina.
3. **Acceder a Streamlit:** http://localhost:8501 (páginas: login, home,
   library, moderation, audit, study).
4. **Acceder a Swagger:** http://localhost:8000/docs (OpenAPI en
   `/openapi.json`).
5. **Crear el administrador:**
   ```bash
   docker compose run --rm api python -m ada_study.cli \
     postgresql+asyncpg://ada:ada_dev_password@postgres:5432/ada_study
   ```
   Iniciar sesión en la UI o vía `POST /api/auth/login`.
6. **Ejecutar una consulta documental:** cargar un PDF desde la UI (o
   `POST /api/documents`), esperar que la versión quede `available`, enviar a
   revisión y aprobar desde la cola de moderación; luego consultar en la página
   de estudio (`POST /api/study`). Con corpus vacío la respuesta devuelve
   `status = "insufficient_evidence"` (comportamiento esperado).
7. **Visualizar una traza:** la traza estructurada viaja en
   `StudyResponse.trace` (panel de trazas de la UI, `ui/trace_view.py`); para
   spans en Phoenix, definir `ADA_PHOENIX_ENABLED=true` (con endpoint
   `http://localhost:6006/v1/traces`) y reiniciar la API; abrir
   http://localhost:6006.

Instrucciones de instalación extendidas: `README.md` y
`docs/audit/DELIVERY_CHECKLIST.md` (flujo para clon limpio, desarrollo y
verificación de calidad).

## 16. Evidence Index

Índice de evidencias con rutas relativas desde la raíz del repositorio.

### Documentación y decisiones

- README principal: `README.md` (despliegue, arquitectura, grafo con diagrama
  Mermaid línea 266, checkpointer, Phoenix, limitaciones conocidas).
- ADRs: `docs/adr/ADR-0001-*` … `docs/adr/ADR-0007-*`; índice `docs/adr/README.md`.
- Auditoría y plan: `docs/audit/BASELINE.md`,
  `docs/audit/CONTRACTS_MATRIX.md`, `docs/audit/IMPLEMENTATION_PLAN.md`,
  `docs/audit/CHANGELOG_SPEC_KIT_V6.md`.
- Checklist de entrega: `docs/audit/DELIVERY_CHECKLIST.md`.
- Calidad RAG: `docs/audit/RAG-QUALITY-01_suficiencia.md`.
- Consigna del curso transcrita: `ADA_Study_Spec_Kit/specs/05_TESTING_AND_DELIVERY.md`.
- Informes técnicos de tareas (sesiones de trabajo): `.coda/docs/`
  (`TASK-09_diagnostico_integracion.md`, `TASK-10_informe.md`,
  `RAG-QUALITY-05_diagnostico.md`, `revision_integral_2026-10-27.md`).

### Tests (resultados verificados 2026-11-02: 521 passed, 1 skipped)

- Unitarias: `tests/` (`test_bm25_index.py`, `test_study_graph.py`,
  `test_state_rules.py`, `test_study_endpoint.py`, `test_study_llm.py`,
  `test_api_auth.py`, `test_api_documents.py`, `test_api_moderation_audit.py`,
  `test_worker.py`, `test_worker_curtain.py`, `test_extraction.py`,
  `test_identity.py`, `test_conversations.py`, `test_ui_study.py`, entre otros).
- Integración (PostgreSQL real): `tests/integration/test_checkpointer_postgres.py`,
  `tests/integration/test_version_transitions.py`,
  `tests/integration/test_api_dedup.py`, `tests/integration/test_dedup.py`,
  `tests/integration/test_schema_migration.py`.
- E2E: `tests/test_e2e_scenarios.py`.
- Observabilidad: `tests/test_study_trace.py` (spans reales con exportador en
  memoria, correlación `ada_trace_id`), `tests/test_study_trace_ui.py`.
- Lint: `python -m ruff check .` (All checks passed!, 2026-11-02).

### Despliegue

- `Dockerfile`, `.dockerignore`, `docker-compose.yml` (servicios: postgres,
  phoenix, migrate, api, worker, ui; volúmenes: `ada_pgdata`, `ada_storage`),
  `.env.example`.
- Validación de despliegue end-to-end: informe FINAL-04 de la sesión de trabajo
  (no versionado en el repo); reproduce: `docker compose up --build`, URLs, admin
  CLI, reinicios con persistencia.

### Capturas Phoenix

- Capturas de trazas en el panel de Phoenix (5 trazas): **PENDIENTE**.

### Resultados E2E en entorno desplegado

- Validación de endpoints HTTP y persistencia en Compose: informe FINAL-04
  (sesión de trabajo; no versionado). Resumen: todos los endpoints HTTP 200;
  Phoenix 401→422 en ingest = acepta; login con credenciales inválidas → 401;
  persistencia verificada entre `restart` y `down`+`up -d`.

### Checklist de entrega

- `docs/audit/DELIVERY_CHECKLIST.md` (nota: cifras históricas de la suite; ver §11).

### Evidencias pendientes (marcadores explícitos)

- Capturas de Phoenix con spans reales de la app desplegada: **PENDIENTE**.
- Captura de conversación multi-turno con LLM real en la app desplegada:
  **PENDIENTE**.
- Grabación/capturas del flujo UI completo (login → carga → moderación →
  consulta): **PENDIENTE**.
- `test-data/Prueba.pdf` (binario de prueba sin referencias en código): sin
  decidir su inclusión en la entrega final.

---

*Fin del documento. Los pesos de la matriz reproducen la rúbrica oficial; este
documento no asigna puntuaciones ni simula una calificación.*











