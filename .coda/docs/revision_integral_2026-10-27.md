# Revisión integral del proyecto — ADA Study (2026-10-27)

Revisión de código de solo lectura, sin modificar archivos de producción.
Estado validado al momento de la revisión: **222 passed, 7 skipped, Ruff limpio**.

---

## 1. Resumen ejecutivo

El proyecto está en buen estado para el trabajo final de Coderhouse: la API
FastAPI, el worker de ingesta con cortina editorial (ADR-0004), la identidad
con sesiones seguras y la UI Streamlit (TASK-08) están implementados y
cubiertos por pruebas unitarias con fakes.

No se encontraron hallazgos bloqueantes ni de seguridad. Existen **tres deudas
arquitectónicas conocidas y documentadas** (reconciliador, Pinecone real,
integración PostgreSQL real) y **un hallazgo de cableado** (el retriever
híbrido no está construido en producción, coherente con B2 pospuesto).

---

## 2. Hallazgos por severidad

### 🔴 Bloqueantes
Ninguno.

### 🟠 Mayores (deuda con impacto funcional)

**M1 — Reconciliador de ADR-0004 no existe como componente independiente.**
El worker procesa ops `reconcile` del outbox (`worker.py:281-283`, re-indexa
los chunks de la versión), pero no hay un proceso que compare el estado real
del índice (Pinecone/BM25) contra el estado deseado del catálogo para detectar
residuos o faltantes fuera del outbox. Si una purga en Pinecone falla
silenciosamente, solo la verificación en recuperación (`_is_visible`) evita
fugas. Impacto: consistencia eventual del índice vectorial no verificada.

**M2 — Pinecone real sin integrar.** Solo existe
`FakePineconeDestination` (`ingestion/destinations.py:60`). El `worker_cli`
lo usa explícitamente con la limitación informada (`worker_cli.py:9-10`).
Sin Pinecone real, la búsqueda vectorial no está validada.

**M3 — 7 pruebas de integración con PostgreSQL real sin ejecutar.** Todas en
`tests/test_catalog_integration.py`, saltadas por `DATABASE_URL` no definida.
El repositorio (`db/repository.py`, 876 líneas) solo está probado contra
fakes; el SQL real (dialecto asyncpg, tipos, constraints) no está validado.

### 🟡 Menores / informativos

**I1 — Retriever híbrido no cableado en producción.** `HybridRetriever` y
`BM25Index.rebuild` no se instancian ni en `api/app.py` ni en `worker_cli.py`;
solo se usan en tests. Coherente con B2 (`/api/study`) pospuesto por decisión
del usuario, pero debe cablearse antes de exponer el estudio.

**I2 — BM25 del worker no se reconstruye desde PostgreSQL al arrancar.**
`worker_cli.py:31` crea `BM25Index()` vacío. La decisión de TASK-03 era
reconstruir desde `document_chunks` al arrancar; hoy el índice BM25 del
worker arranca en frío y solo crece con ops nuevas. Para el RAG compartido
futuro (B2) hace falta `BM25Index.rebuild(chunks)` en el arranque.

**I3 — Sin pruebas E2E de navegador para la UI.** La UI está cubierta a
nivel de cliente API puro (16 pruebas nuevas en TASK-08) y smoke test
headless; no hay prueba de flujo completo en navegador.

---

## 3. Invariantes verificados (sin hallazgos)

| Invariante | Evidencia |
|---|---|
| Cortina editorial revalidada (inicio, pre-ops, pre-activación) | `worker.py:165,176`, `_run_outbox_ops` revalida por op |
| Bajo cortina solo ops delete; recover() no reintroduce contenido | `worker.py:188-203,214` |
| Transacciones por operación con commit/rollback automático | `repository.py:207-210` (`engine.begin()`) |
| Índices y unicidades correctas (email, token_hash, invitations) | `schema.py:203,223,239` |
| Tokens de sesión: solo SHA-256 en BD; PBKDF2 600k para passwords | `identity_service.py:7-8,34-35` |
| `/api/me` sin `password_hash` (fuga corregida en TASK-08) | `routes_auth.py` + test |
| Aislamiento en recuperación con ViewerContext (TASK-07) | `retriever.py:146-167` |
| UI: token solo en `st.session_state`, identidad del backend | `ui/auth.py` |
| Grafo: inyección de dependencias, sin secretos en código | `graph/builder.py:146` |
| Config: sin secretos por defecto, `DATABASE_URL` obligatoria | `config.py` |

---

## 4. Clasificación de evidencia (honestidad de validación)

- **Unitarias con fakes: 222** — cubren dominio, API, worker, UI cliente.
- **Integración PostgreSQL real: 0 ejecutadas** (7 skipped sin `DATABASE_URL`).
- **Pinecone real: 0 pruebas** (no existe el destino real).
- **E2E navegador: 0** (solo smoke test headless de Streamlit).

**No se declara validada ninguna integración real.**

---

## 5. Propuesta de priorización

Para el **trabajo final (prioridad absoluta)**:
1. Ejecutar las 7 pruebas de integración con PostgreSQL real (docker compose
   ya documentado en `.env.example`) — cierra M3 con bajo esfuerzo.
2. Cablear el retriever + `BM25Index.rebuild` cuando se implemente B2
   (`/api/study`) — cierra I1/I2 en su contexto natural.

Deuda diferible (no bloquea el trabajo final):
- Reconciliador independiente (M1) — documentar como limitación.
- Pinecone real (M2) — requiere credenciales; el fake está informado.
- E2E de navegador (I3) — opcional para la entrega.
