# BASELINE — Auditoría de fuentes M1–M7 (TASK-00)

Fecha: 2026-10-25 · Modo: solo lectura · Fuentes inspeccionadas, nunca modificadas.

## 0. Rutas validadas

| Marcador | Ruta real | Estado |
|---|---|---|
| `ENTREGA7_PATH` | `C:\Devs\CoderHouse\AI Engineer\Proyectos\Entrega7` | Existe; repositorio principal de M7 (git, `pyproject.toml`, `uv.lock`, Docker Compose) |
| `ENTREGAS_M1_M6_PATH` | `C:\Devs\CoderHouse\AI Engineer\Proyectos\Entregas` | Existe; contiene `Entrega1`…`Entrega6` |
| `ADA_STUDY_PATH` | `C:\Devs\CoderHouse\AI Engineer\Proyectos\ADA-Study` | Workspace editable; spec-kit en `ADA_Study_Spec_Kit/` |

Fuente principal de M7: **`Entrega7`** (es la integración final: API + worker + Redis + checkpointer + HITL + observabilidad). `Entregas/Entrega6` es el antecesor directo: `diff -rq Entrega6/src/ada Entrega7/src/ada` → **sin diferencias** (el paquete `ada` de M7 es idéntico al de M6; M7 añade `app/`).

---

## 1. Mapa de módulos existentes y responsabilidades

### 1.1 Entrega7 (M7) — repositorio de integración

**Paquete `src/ada/` (dominio multi-agente, idéntico a M6):**

| Módulo | Responsabilidad | Símbolos clave |
|---|---|---|
| `src/ada/graph.py` | StateGraph supervisor-jerárquico: START → supervisor → (researcher/analyst/validator/synthesizer/limited_finish) → END. Límites de pasos/reintentos impuestos en Python; invalidación de artefactos obsoletos | `build_graph`, `make_supervisor_node`, `make_researcher_node`, `analyst_node` |
| `src/ada/agents/supervisor.py` | Enrutado con salida estructurada LLM | `route_next_agent` |
| `src/ada/agents/researcher.py` | Recupera evidencia con `HybridRetriever` | `retrieve_evidence` |
| `src/ada/agents/analyst.py` | Análisis con scoring determinístico en Python | `build_analysis`, `derive_scores_from_evidence`, `DEFAULT_CRITERIA` |
| `src/ada/retrieval/hybrid.py` | RAG híbrido: BM25 + vectores + RRF sobre el mismo corpus | `HybridRetriever` |
| `src/ada/retrieval/bm25.py` / `vector.py` / `fusion.py` | Retrieval léxico, semántico y fusión | `BM25Retriever`, `VectorRetriever`, `reciprocal_rank_fusion` |
| `src/ada/retrieval/encoder.py` | Embeddings sentence-transformers (lazy) | `SentenceTransformerEncoder` |
| `src/ada/retrieval/corpus.py` | Carga y validación del corpus JSON estático | `load_corpus`, `get_default_corpus` |
| `src/ada/retrieval/documents.py` | Contratos de documentos | `TechnicalDocument`, `RetrievedDocument` |
| `src/ada/schemas.py` | Contratos Pydantic estrictos (rechazan campos extra) | `StrictModel`, `ResearchArtifact`, `AnalysisArtifact`, `ValidationResult`, `RoutingDecision`, `FinalReport`, `SourceEvidence` |
| `src/ada/state.py` | Estado del grafo | `AgentState(MessagesState)`, `AgentContribution` |
| `src/ada/config.py` | Configuración pydantic-settings (Groq) | `Settings`, `get_settings`, `get_chat_model` |
| `src/ada/decision.py` | Extracción de decisión y criterios | `extract_decision`, `build_criteria` |
| `src/ada/tools/scoring.py` | Scoring ponderado determinístico | `calculate_weighted_score` |
| `src/ada/__main__.py` | CLI interactivo con streaming del grafo | — |

**Capa `app/` (añadida por M7):**

| Módulo | Responsabilidad | Símbolos clave |
|---|---|---|
| `app/main.py` | FastAPI asíncrona: `POST /tasks` (202, encola), `GET /tasks/{id}` (polling), `POST /tasks/{id}/approve` (HITL, 409 en doble aprobación), `GET /health`. Nunca ejecuta el grafo en el request | `app`, `create_task`, `get_task`, `approve_task`, `unhandled_exception_handler` |
| `app/worker.py` | Worker independiente: consume Redis Streams (`XADD`/`XREADGROUP`/`XACK`), ejecuta el grafo en thread, persiste `PENDING→RUNNING→(WAITING_APPROVAL)→DONE/FAILED`. Recuperación de procesos muertos con `XAUTOCLAIM` (60 s idle) | `JobStore`, `run_worker`, `process_new_job`, `process_resume`, `claim_resume` (WATCH/MULTI/EXEC), `make_store` |
| `app/graph.py` | Orquestador con checkpointer Redis (`RedisSaver`) + nodo HITL entre START y supervisor. Falla ruidosamente si Redis está configurado pero no inicializable | `get_checkpointer`, `build_checkpointer`, `build_graph` |
| `app/hitl.py` | Nodo de aprobación humana con `interrupt()` real de LangGraph | `make_hitl_node`, `is_critical_task`, `get_cost_threshold` |
| `app/observability.py` | Trazas OpenInference/OTel → Phoenix; span por nodo incluso en modo fake | `init_observability`, `set_job_context`, `traced` |
| `app/cost.py` | Registro de tokens/costo estimado desde `usage_metadata` | `UsageRecorder`, `estimate_cost_usd` |
| `app/fake_model.py` | Modelo determinístico sin red (`ADA_LLM_FAKE=1`) | `FakeChatModel` |

**Infraestructura:** `docker-compose.yml` (redis:7 + phoenix + app + worker), `Dockerfile`, `pyproject.toml` (Python ≥3.12, uv, extras `dev`/`retrieval`), `data/corpus/corpus.json` (corpus estático), `tests/unit/` (137 tests) y `tests/integration/test_api_worker.py` (25 tests, fakeredis + fake model).

### 1.2 Entregas M1–M6 (`Entregas/`)

| Entrega | Contenido relevante | Símbolos clave | Reutilizable para ADA-Study |
|---|---|---|---|
| `Entrega1` | Clientes LLM multi-proveedor (OpenAI/Anthropic/Gemini) con `BaseLLMClient` ABC, streaming y `LLMClientManager` factory | `BaseLLMClient`, `OpenAIClient`, `AnthropicClient`, `GeminiClient`, `manager.py` | Parcial: el patrón ABC/factory; M7 ya usa `langchain-groq` |
| `Entrega2` | Cadena LangChain con prompt + salida Pydantic | `construir_cadena`, `process_text`, `AnalisisTecnico` | Bajo: patrón cubierto por M7 |
| `Entrega3` | Ingesta básica: carga de `.txt`, split, embeddings HF, Chroma persistente | `load_documents`, `split_documents`, `build_vectorstore` | Bajo: sin PDF, sin dedup, sin estados |
| `Entrega4` | RAG híbrido (BM25+Pinecone+EnsembleRetriever) sobre `.docx`, evaluación Precision@5/Recall@5 con golden set | `RAGSystem`, `build_hybrid_retriever`, `ingestion_pipeline.ingest`, `evaluation/evaluate.py`, `golden_set.json` | **Alta**: metodología de evaluación con golden set |
| `Entrega5` | Agente LangGraph con herramientas (OpenF1), checkpointer **AsyncSqliteSaver** persistente, trazas serializadas, tests | `compilar_grafo`, `nodo_modelo`, `tools.buscar_*`, `trace.serializar_traza`, `tests/test_agent.py` | Media: patrón checkpointer persistente (M7 ya lo supera con Redis) |
| `Entrega6` | Paquete `ada` completo (idéntico a M7): grafo supervisor + RAG híbrido + CLI + 137 tests unit | Igual que `src/ada` de M7 | Ya integrada en M7 |

---

## 2. Matriz por capacidad

| Capacidad | Estado | Evidencia |
|---|---|---|
| Grafo multi-agente supervisor con límites determinísticos | **implemented+tested** | `ada/graph.py`; 8 tests en `test_graph.py`, 31 en `test_behavior.py`, 26 en `test_regression.py` |
| RAG híbrido BM25+vector+RRF con provenance | **implemented+tested** | `ada/retrieval/hybrid.py`; `test_hybrid.py` (7), `test_retrieval.py` (12) |
| Contratos Pydantic estrictos | **implemented+tested** | `ada/schemas.py`; `test_schemas.py` |
| API asíncrona no bloqueante (202 + polling) | **implemented+tested** | `app/main.py`; `test_api_worker.py::test_create_task_returns_202_and_job_id` |
| Worker separado con Redis Streams + ACK + XAUTOCLAIM | **implemented+tested** | `app/worker.py`; `test_message_is_acked_after_processing`, `test_five_jobs_processed_concurrently` |
| Checkpointer persistente (RedisSaver) | **implemented+not tested (real)** | `app/graph.py::build_checkpointer`; los tests usan `MemorySaver`/fakeredis; no hay prueba contra Redis real en el repo |
| HITL con `interrupt()` real + reanudación | **implemented+tested (con fakes)** | `app/hitl.py`; `test_critical_task_pauses_and_resumes`, `test_double_resume_is_prevented` |
| Idempotencia de ejecución (doble job / doble resume) | **implemented+tested (con fakes)** | `worker.py::process_new_job` (guard de estado); `test_double_execution_is_prevented` |
| Concurrencia (5 jobs paralelos) | **implemented+tested (con fakes)** | `test_five_jobs_processed_concurrently` |
| Recuperación de errores de worker caído | **implemented+tested (con fakes)** | `claim_pending`/XAUTOCLAIM; `test_worker_crash_marks_failed` |
| Observabilidad Phoenix/OTel | **implemented+not tested (automatizada)** | `app/observability.py`; evidencia manual: `artifacts/`, `*.jsonl` de trazas, `screenshots/` |
| Costo/tokens estimado | **implemented+tested** | `app/cost.py`; `test_cost.py` (13) |
| CLI interactivo | **implemented+tested** | `ada/__main__.py`; `test_cli.py` (17) |
| **Ingesta de PDF** (upload, extracción, chunking por página) | **missing** | No existe endpoint ni módulo de documentos; el corpus es JSON estático (`data/corpus/corpus.json`) |
| **Catálogo de documentos / estados** (`queued/processing/available/failed`) | **missing** | Solo estados de *job* genérico en Redis; no hay `DocumentRecord` |
| **Dedup por SHA-256** | **missing** | No hay cálculo de hash de archivos en ninguna entrega |
| **Borrado/actualización por documento** (índices BM25/vector) | **missing** | `HybridRetriever` es in-memory sobre lista fija; no soporta eliminación |
| **Índices persistentes** (vector store / BM25 en disco) | **missing** | M7: en memoria; M3/M4 usaban Chroma/Pinecone pero no están integrados en M7 |
| **Búsqueda web complementaria** (proveedor, política, etiquetado) | **missing** | No hay proveedor ni herramienta web en M1–M7 |
| **Web UI** (biblioteca + centro de estudio) | **missing** | Solo CLI y API; sin frontend |
| **Flujos Estudiar/Practicar/Preparar evaluación** | **missing** | El grafo es de decisiones de arquitectura, no pedagógico |
| **5 escenarios E2E de Spec 05** | **partial** | Existen pruebas de ciclo de vida API+worker con fakes; no cubren PDF→available→consulta ni borrado (no existen esas capacidades) |
| **Persistencia real verificada en ejecución** (Redis/checkpointer) | **unknown** | El código exige RedisSaver si `REDIS_URL` está seteado y falla ruidosamente; hay evidencia manual (trazas, `5concurrents.png`), pero no fue verificada en esta auditoría |

---

## 3. Comandos de inspección/tests ejecutados y resultados reales

| Comando | Resultado |
|---|---|
| `ls -d` de las 3 rutas | Las 3 existen (exit 0) |
| `find src app tests scripts -type f` (Entrega7) | Árbol real: 18 módulos `ada`, 8 `app`, 13 archivos de tests |
| `cat pyproject.toml`, `cat docker-compose.yml` | Python ≥3.12, uv, FastAPI/Redis/LangGraph; compose con redis+phoenix+app+worker |
| `grep -n "def \|class "` en retrieval, schemas, app | Mapa de símbolos de la sección 1 |
| `diff -rq Entrega6/src/ada Entrega7/src/ada` | **Sin diferencias** — M7 reutiliza M6 íntegro |
| `grep -rn "def test_" tests \| wc -l` (Entrega7) | **162 tests** (137 unit + 25 integration) |
| `python -c "import langgraph, fastapi, ..."` (global) | Falla: `No module named 'fastapi'` |
| `ls .venv` (Entrega7, Entrega6) | **No existe** en ninguna fuente |

**Tests NO ejecutados — decisión deliberada:** correr los tests requeriría `uv run pytest` dentro de `Entrega7`, lo que crearía `.venv` y modificaría la carpeta fuente (prohibido), o instalar `fastapi`/`fakeredis` en el Python global (efecto externo). Se registra como limitación: la calidad de los tests se evaluó por inspección de código, no por ejecución.

---

## 4. Brechas frente a `specs/00_PRODUCT.md`–`specs/05_TESTING_AND_DELIVERY.md`

| Spec | Brecha principal |
|---|---|
| **00 Producto** | Historias 1 (cargar PDF y saber cuándo está disponible), 6 (dedup/actualizar/retirar documento) y la web "Mi centro de estudio" no existen. El RAG incremental —capacidad central— no está implementado |
| **01 Arquitectura** | Faltan: ingesta documental, knowledge store con índices persistentes, `DocumentStatus`/`DocumentRecord`/`SourceReference`/`IngestionJob`, Web UI. Existen y son reutilizables: FastAPI+Pydantic, worker+Redis, checkpointer, observabilidad, LangGraph |
| **02 RAG incremental** | Casi toda la spec es brecha: upload multipart, SHA-256, extracción PDF con páginas, chunking con metadatos estables, ciclo de estados, dedup, borrado de índices, reintentos seguros. El retriever híbrido existe pero es in-memory y estático |
| **03 Centro de estudio** | Todo: navegación, biblioteca, Estudiar/Practicar/Preparar. El grafo actual es de dominio incorrecto (decisiones de arquitectura, no pedagógico) |
| **04 Búsqueda web** | Todo: no hay proveedor, detección de insuficiencia, política de consentimiento ni etiquetado de fuentes web |
| **05 Testing** | Los 5 escenarios E2E exigidos dependen de capacidades inexistentes (PDF, borrado). Existen pruebas de integración sólidas con fakes; faltan pruebas contra Redis real y los escenarios 1, 4 y 5 de la spec |

---

## 5. Reutilización recomendada de M1–M7 (sin copiar aún)

| Componente | Origen | Acción recomendada |
|---|---|---|
| Paquete `src/ada/` completo (grafo, retrieval, schemas, state, config) | M7 (= M6) | **Copiar** como base del dominio; adaptar el grafo al dominio pedagógico |
| `app/main.py` (patrón API 202+polling, error handler central) | M7 | **Copiar y extender** con endpoints de documentos (`/api/documents`, `/api/ingestion-jobs`) |
| `app/worker.py` (JobStore, streams, XAUTOCLAIM, claim_resume) | M7 | **Copiar** casi íntegro; añadir cola de ingesta paralela a la de estudio |
| `app/graph.py` (checkpointer Redis con fail-loud) | M7 | **Copiar** tal cual |
| `app/hitl.py`, `app/observability.py`, `app/cost.py`, `app/fake_model.py` | M7 | **Copiar**; reutilizar spans por nodo y UsageRecorder |
| Tests de integración (patrones fakeredis + fake model) | M7 | **Copiar** como plantilla de los nuevos E2E |
| Metodología de evaluación con golden set (Precision@5/Recall@5) | M4 | **Adaptar** para evaluar el RAG incremental |
| Patrón ABC de clientes LLM | M1 | Solo como referencia; M7 ya resuelve el proveedor con `langchain-groq` |
| Chroma/Pinecone de M3/M4 | M3, M4 | **No copiar**: decidir el knowledge store en TASK-01/02 tras evaluar necesidades de borrado por documento |
| AsyncSqliteSaver de M5 | M5 | **No copiar**: RedisSaver de M7 lo reemplaza |

---

## 6. Riesgos de migración y decisiones que requieren aprobación

1. **Knowledge store (decisión bloqueante):** el retriever de M7 es in-memory y no soporta borrado por documento. Spec 02 exige eliminar de BM25 y del índice vectorial. Requiere aprobación: elegir tecnología (p. ej. Chroma persistente ya conocido de M3, u otra) y estrategia de borrado/reconstrucción acotada.
2. **Dominio del grafo:** el grafo de M7 es de decisiones de arquitectura. Adaptarlo a Estudiar/Practicar/Preparar implica reescribir prompts, schemas y criterios del analyst. Riesgo de subestimar el esfuerzo; requiere aprobación del alcance.
3. **Tests no ejecutados en la auditoría:** la calidad de los 162 tests se verificó por inspección. La primera tarea post-aprobación debería ejecutar la suite en un entorno propio de `ADA-Study` para confirmar el punto de partida real.
4. **Python 3.14 global vs 3.12 del proyecto:** el proyecto exige ≥3.12; el global es 3.14. `ADA-Study` deberá fijar su propio intérprete/lockfile (uv) sin tocar las fuentes.
5. **Corpus estático → dinámico:** pasar de `corpus.json` a documentos subidos cambia el contrato de `TechnicalDocument`/`load_corpus`; los tests de corpus/hybrid necesitarán dobles o fixtures de catálogo.
6. **Seguridad de archivos:** ninguna entrega valida MIME real ni límite de tamaño en upload (no existe upload). Debe diseñarse desde cero en la tarea de catálogo/ingesta.
7. **Secretos:** `.env.example` presente en M7; no se leyó ningún `.env` real. Mantener secretos fuera de Git en `ADA-Study`.

---

## 7. Propuesta de orden de tareas actualizada

1. **TASK-01 — Contratos** (ya definida): `DocumentStatus`, `DocumentRecord`, `SourceReference`, `IngestionJob` en Pydantic, alineados con `ada/schemas.py`; decidir knowledge store (riesgo 1).
2. **TASK-02 — Catálogo de documentos:** persistencia del catálogo (Redis o store elegido), estados, dedup SHA-256, API de documentos.
3. **TASK-03 — Ingesta incremental:** upload multipart, validación de archivo, extracción PDF con páginas, chunking con metadatos estables, indexación solo del documento nuevo, reintentos seguros, borrado de índices.
4. **Nueva TASK-04 — Migración del esqueleto M7:** copiar `app/` + `src/ada/` a `ADA-Study`, ejecutar la suite en el entorno propio, adaptar config.
5. **Nueva TASK-05 — Adaptación del grafo al dominio pedagógico:** Estudiar/Practicar/Preparar con contratos validados.
6. **Nueva TASK-06 — Búsqueda web complementaria:** proveedor configurable, política de consentimiento, etiquetado, límites.
7. **Nueva TASK-07 — Web UI:** biblioteca + centro de estudio conectados a FastAPI.
8. **Nueva TASK-08 — Testing y entrega:** 5 escenarios E2E de Spec 05, pruebas de concurrencia/persistencia contra Redis real, README con diagrama, comando reproducible.

---

## 8. Actualización TASK-01 (2026-10-25)

Tras la aprobación de esta auditoría, TASK-01 definió las decisiones de arquitectura y contratos:

- **Decisiones aprobadas por el usuario:** PostgreSQL (catálogo/versiones), PDF en almacén separado, Pinecone + BM25 propio + RRF, sin ChromaDB.
- **ADRs:** `docs/adr/ADR-0001`…`ADR-0007` (índice en `docs/adr/README.md`), todos en estado Propuesto.
- **Matriz de contratos:** `docs/audit/CONTRACTS_MATRIX.md`.
- **Plan de implementación por tareas pequeñas:** `docs/audit/IMPLEMENTATION_PLAN.md` (fases A–I).

**Rev. 2 — integración Spec Kit v6 (2026-10-25):** los ADRs, la matriz de contratos y el plan fueron realineados con el Spec Kit v6 (nuevo `specs/06_COMMUNITY_ACCESS_AND_MODERATION.md` y `specs/ADR-001-storage-and-hybrid-retrieval.md`, esta última aprobada). Cambios principales: modelo de dos ciclos de estado (editorial/técnico), outbox `pending_index_ops`, cortina editorial de borrado, endpoints de moderación, fases de identidad/moderación/auditoría. Detalle completo en `docs/audit/CHANGELOG_SPEC_KIT_V6.md`.

## 9. Limitaciones de esta auditoría

- **No se ejecutaron tests** (motivo en sección 3): la evaluación de pruebas es por inspección de código y nombres de tests, no por resultados de ejecución.
- **No se verificó persistencia real en ejecución** (Redis, Phoenix): requiere levantar servicios, lo que excede el modo solo lectura.
- **No se leyó ningún `.env` real**; solo `.env.example` fue visto en el listado.
- Los README de M1–M6 no se usaron como fuente de verdad: todas las afirmaciones provienen del árbol de archivos y del código.
