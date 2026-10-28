# TASK-09 — Diagnóstico de integración y cierre técnico (2026-10-27)

Fase exclusivamente de lectura y análisis. **No se modificó ningún archivo de
producción, contrato, ADR ni commit.** Este documento es el único artefacto.

Estado validado: **229 pruebas colectadas — 222 passed, 7 skipped, Ruff limpio.**

---

## 1. Matriz de componentes

| Componente | Implementado | Integrado | Probado con fakes | Probado con servicio real | Pendiente |
|---|---|---|---|---|---|
| Configuración tipada (`config.py`) | ✅ | ✅ | ✅ | n/a | — |
| Esquema PostgreSQL (`db/schema.py`) | ✅ | ✅ | ✅ | ⚠️ solo 7 pruebas skipped | ejecutar integración real |
| Repositorio (`db/repository.py`, 876 líneas) | ✅ | ✅ | ✅ | ❌ nunca contra asyncpg real | integración real |
| `create_schema` (idempotente) | ✅ | ✅ | ✅ | ❌ | ejecutarlo en PG real |
| Almacén PDF content-addressed | ✅ | ✅ | ✅ | n/a (filesystem) | — |
| Extracción pypdf (detecta escaneados) | ✅ | ✅ | ✅ | n/a | — |
| Worker + outbox + cortina editorial | ✅ | ✅ | ✅ (14 pruebas cortina) | ❌ | integración real |
| BM25 incremental | ✅ | ⚠️ solo en worker | ✅ | n/a | rebuild al arrancar (I2) |
| Pinecone | ❌ (solo `FakePineconeDestination`) | ❌ | ✅ fake | ❌ | destino real + credenciales |
| Retriever híbrido + RRF + ViewerContext | ✅ | ❌ no cableado en producción | ✅ | n/a | cablear con B2 (I1) |
| Identidad (PBKDF2 600k, sesiones SHA-256) | ✅ | ✅ | ✅ | ❌ | integración real |
| API FastAPI (documentos, moderación, auditoría, auth) | ✅ | ✅ | ✅ | ❌ | integración real |
| UI Streamlit (login, home, library, moderation, audit) | ✅ | ✅ vía HTTP | ✅ (16 pruebas cliente) | ⚠️ smoke headless | E2E navegador |
| Grafo pedagógico (nodos) | ✅ | ❌ no cableado | ✅ (11 pruebas por nodo) | n/a | cablear con B2 |
| Grafo compilado (LangGraph) | ✅ `compile_graph` | ❌ | ⚠️ sin prueba de grafo completo | n/a | prueba de topología |
| LLM (proveedor real) | ❌ | ❌ | ✅ (modo determinístico) | ❌ | fábrica de chat model |
| Reconciliador ADR-0004 | ❌ | ❌ | n/a | n/a | deuda diferida |

---

## 2. Bloqueos concretos para ejecutar el sistema completo

**B-1 — PostgreSQL real nunca ejercitado.** Sin `DATABASE_URL`, las 7 pruebas
de `test_catalog_integration.py` se saltan. El SQL real (dialecto asyncpg,
tipos, constraints, transacciones) no está validado. *Cierre: `docker compose
up -d` + `python -m ada_study.db.create_schema "$DATABASE_URL"` + pytest con
`DATABASE_URL` definida.*

**B-2 — Bug en `docker-compose.yml:18`.** El healthcheck usa
`pg_isready -U ada_dev` pero el usuario configurado es `ada`: el healthcheck
falla siempre (el contenedor funciona, pero nunca reporta healthy). Corrección
trivial pendiente de aprobación (no se modificó nada en esta fase).

**B-3 — Sin fábrica de chat model.** El grafo acepta `chat_model` inyectado,
pero no existe código que construya un proveedor real (no hay
`langchain-openai`/`anthropic` en `pyproject.toml`, ni settings de LLM, ni
`init_chat_model`). Hoy el sistema funciona en modo determinístico sin LLM.
Para `/api/study` se requiere: dependencia de proveedor, settings (`ADA_LLM_*`
sin defaults inseguros), y decisión de proveedor.

**B-4 — Retriever y grafo sin cablear en producción.** `HybridRetriever` y
`compile_graph` no se instancian en `api/app.py` ni `worker_cli.py`. El
endpoint `/api/study` no puede implementarse sin este cableado (más
`BM25Index.rebuild` desde `document_chunks` al arrancar, decisión TASK-03).

**B-5 — Sin checkpointer en `compile_graph` (`builder.py:192-194`).**
`compile()` sin argumentos: el estado del grafo vive solo dentro de una
invocación. Para `/api/study` hay que decidir: estado efímero por request
(suficiente para MVP) o checkpointer persistente (requiere decisión).

---

## 3. Decisiones de arquitectura que requieren aprobación

**D-1 — Proveedor y modo de LLM.** ¿Qué proveedor (vía qué paquete LangChain),
qué modelo, y cómo se configura (env vars `ADA_LLM_PROVIDER`, `ADA_LLM_MODEL`,
clave por variable de entorno dedicada, nunca default)? Alternativa MVP:
mantener el modo determinístico sin LLM para el trabajo final y dejar el
proveedor real como incremento posterior.

**D-2 — Estado del grafo para `/api/study`.** Opción A: request síncrono con
estado efímero (simple, sin persistencia; el grafo termina en ≤25 pasos por
`recursion_limit` default de LangGraph). Opción B: checkpointer (Memory/Postgres)
para sesiones multi-turno. Recomiendo A para MVP-2.

**D-3 — Límite de pasos explícito.** El grafo tiene `MAX_RETRIES=2` (validator)
pero no `MAX_STEPS` propio: solo el `recursion_limit` implícito de LangGraph
(25) evita bucles del supervisor con LLM. Recomiendo configurar
`recursion_limit` explícito y un corte determinístico por `steps` en el
supervisor con LLM.

**D-4 — HITL heredado de M7 sin cablear.** Los campos `hitl_*`,
`estimated_cost_usd` y `pending_tools` existen en `AgentState` pero ningún
nodo los usa. Recomiendo declararlos fuera de alcance para MVP-2 (no
eliminarlos: son parte del contrato conservado de M7).

---

## 4. Deudas técnicas priorizadas

1. **Reconciliador ADR-0004 (mayor).** Solo existen ops `reconcile` del outbox
   (`worker.py:281-283`); no hay verificación activa de estado real vs deseado
   del índice. Mitigación actual: `_is_visible` re-verifica contra el catálogo
   en cada recuperación. Diferible; documentar como limitación.
2. **Pinecone real (mayor).** Solo fake informado. Requiere credenciales y
   decisión de índice/namespaces. Diferible para el trabajo final.
3. **Prueba de integración del grafo compilado (media).** Las 11 pruebas
   cubren nodos individuales; ninguna invoca `compile_graph().invoke()` con la
   topología completa (edges condicionales, protección synthesizer, corte por
   reintentos). Riesgo bajo pero real de regresión de topología.
4. **`BM25Index.rebuild` al arrancar (media, I2).** El worker crea `BM25Index()`
   vacío (`worker_cli.py:31`); la decisión TASK-03 era reconstruir desde
   PostgreSQL.
5. **E2E de navegador (menor).** UI cubierta por cliente API puro + smoke
   headless; sin Playwright/Selenium.
6. **Healthcheck de compose (menor, B-2).** Una línea.

---

## 5. Propuesta de TASK-10 (alcance acotado)

**Objetivo:** dejar el sistema integrado ejecutable y validado contra
PostgreSQL real, y cablear el grafo pedagógico en modo determinístico detrás
de `/api/study` (sin proveedor LLM real — queda para TASK-11 tras aprobar D-1).

**Alcance:**
1. Corregir healthcheck de `docker-compose.yml` (una línea).
2. Ejecutar las 7 pruebas de integración con PostgreSQL real y dejarlas en el
   flujo estándar (documentar `DATABASE_URL` en README).
3. Cablear en `api/app.py`: `BM25Index.rebuild(chunks)` al arrancar +
   `HybridRetriever` + `compile_graph()` (modo determinístico) en `AdaState`.
4. Implementar `POST /api/study` (MVP-2, Opción A de D-2): request → grafo →
   `StudyResponse` validado; errores con códigos ADR-0005; `recursion_limit`
   explícito (D-3); sin HITL (D-4).
5. Pruebas: unitarias del endpoint con fakes + 1 prueba de integración del
   grafo compilado (topología completa) + smoke E2E: API+worker+UI contra PG
   real.

**Criterios de aceptación:**
- 7/7 pruebas de integración PostgreSQL pasando (no skipped) en entorno con
  Docker.
- `/api/study` responde `StudyResponse` con referencias verificadas contra el
  catálogo; sin evidencia → `insufficient_evidence` sin citas inventadas.
- Solo documentos `published` (y acceso privado TASK-07 vía ViewerContext)
  alimentan la recuperación; `withdrawn`/`deleted`/`rejected` excluidos.
- Suite completa verde + Ruff limpio; ningún fake presentado como servicio
  real en la documentación.
- Sin secretos en código ni defaults inseguros de LLM.

**Fuera de alcance:** proveedor LLM real (TASK-11), Pinecone real,
reconciliador, E2E navegador, UI de estudio (página Streamlit de `/api/study`
puede incluirse si el alcance lo permite, o diferirse a TASK-12).

---

## 6. Confirmación

No se modificó ningún archivo de código, contrato, ADR ni configuración.
No se crearon commits. No se imprimieron secretos ni valores de `.env`.
Todo el trabajo se realizó dentro de `ADA-Study`.

**Esperando aprobación explícita para implementar TASK-10.**
