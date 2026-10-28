# TASK-10 — Informe de implementación (2026-10-27)

**Validación: 243 passed, 7 skipped, Ruff limpio. Smoke test del flujo
completo API → retrieval → grafo → respuesta: OK (200, `complete`,
referencia real con procedencia).**

---

## Fase 1 — PostgreSQL real: NO EJECUTADA (entorno sin servicio)

- Healthcheck de `docker-compose.yml` corregido: `pg_isready -U ada -d ada_study`
  (antes usaba el usuario inexistente `ada_dev` y nunca reportaba healthy).
- Docker y PostgreSQL **no están disponibles en esta máquina** (verificado:
  sin binario docker, sin servicio Windows de PostgreSQL, puerto 5432 sin
  escuchar). Con autorización del usuario se continuó con las Fases 2 y 3.
- **Las 7 pruebas de integración con PostgreSQL real siguen skipped y NO se
  declaran validadas.** No se simuló la integración.

## Fase 2 — Cableado de retrieval y grafo

**Nuevos archivos:**
- `src/ada_study/study/__init__.py` — paquete de estudio; exporta límites y
  `run_study`.
- `src/ada_study/study/runtime.py` — runtime determinístico por petición:
  1. `_collect_visible_chunks`: chunks de versiones activas `available` de
     documentos `published`/`published_pending_review` + acceso privado del
     autor a su propio `pending_review` (regla TASK-07);
     `withdrawn`/`deleted`/`rejected` excluidos absolutamente.
  2. `BM25Index.rebuild(chunks)` desde el catálogo (decisión TASK-03, cierra I2).
  3. `HybridRetriever(catalog=repository, viewer=...)` — capa 2 de
     visibilidad contra el catálogo real; `vector_search=None` (Pinecone
     real pendiente; no se simula).
  4. `compile_graph(chat_model=None, retriever=...)` — supervisor
     determinístico; **no simula proveedor LLM** (D-1).
  5. `ainvoke` con `recursion_limit=25` explícito (D-3) y estado efímero por
     petición (D-2, sin checkpointer ni persistencia conversacional).
- Errores: `StudyRuntimeError` con códigos `STUDY_EMPTY_REQUEST`,
  `STUDY_REQUEST_TOO_LONG`, `STUDY_RETRIEVAL_UNAVAILABLE`,
  `STUDY_GRAPH_FAILED`, `STUDY_NO_RESPONSE` (nuevos, propios del endpoint;
  no alteran ADR-0005 — reportado para eventual inclusión).
- HITL heredado de M7: fuera de alcance (D-4); el runtime no lo expone.

## Fase 3 — Endpoint POST /api/study

**Nuevos archivos:**
- `src/ada_study/api/routes_study.py` — `POST /api/study`:
  - Autenticación con la frontera existente (`get_current_user`, Bearer
    contra sesión real).
  - `ViewerContext` derivado de la identidad autenticada en el backend.
  - `StudyRequest` (pydantic, 1..2000 chars); salida `StudyResponse`.
  - 422 para consultas vacías/largas; 503 con código estable para fallos de
    retrieval/grafo, sin trazas internas.
  - Sin respuestas ficticias: sin evidencia → 200 con
    `insufficient_evidence`/`limited` y `references: []`.

**Modificados:**
- `src/ada_study/api/app.py` — registro del router de estudio.
- `docker-compose.yml` — healthcheck corregido (una línea).
- `README.md` — endpoint documentado con su limitación (sin LLM real).

## Pruebas

**Nuevo archivo:** `tests/test_study_endpoint.py` — 21 pruebas, todas pasan:
- Runtime: flujo completo con evidencia real y procedencia; insuficiencia
  sin citas; consulta vacía/oversize rechazadas.
- Aislamiento: withdrawn/deleted excluidos incluso para el autor;
  pending_review privado solo para el autor; versión no activa excluida.
- Endpoint: 401 sin token; flujo E2E en app; 422 límites; insuficiencia como
  200 (no error); cortina vía endpoint; acceso privado propio/ajeno.
- Topología: compilación sin modelo; `recursion_limit` explícito;
  `limited_finish` con reintentos agotados; protección del synthesizer.

**Suite completa: 243 passed, 7 skipped (integración PostgreSQL, sin
DATABASE_URL), Ruff limpio.**

**Smoke test (en proceso, sin red):** API → retrieval → grafo → respuesta:
200, `status=complete`, referencia a `doc_s` página 1 con procedencia
completa y `evidence_gaps` reconocido.

## Limitaciones y deudas restantes

1. **PostgreSQL real sin validar** (Fase 1): las 7 pruebas de integración y
   el SQL real (asyncpg, transacciones) siguen sin ejercitar. Requiere
   Docker o PostgreSQL local.
2. **Pinecone real**: el runtime usa solo BM25 (`vector_search=None`);
   no se afirma integración vectorial.
3. **Proveedor LLM real**: el grafo corre en modo determinístico (D-1);
   TASK-11.
4. **Reconciliador ADR-0004**: no implementado (fuera de alcance).
5. **BM25 por petición**: reconstruir el índice en cada consulta es correcto
   para el volumen actual; para escalar convendrá caché con invalidación por
   activación de versión (deuda futura, no bloqueante).
   **Actualización (2026-10-27, ajuste aprobado):** el modelo por petición
   queda **ratificado para el MVP**. Deuda técnica registrada: evaluar un
   índice incremental de larga vida en el proceso (`add_version`/
   `remove_version` ya existen y el worker ya los usa vía `BM25Destination`;
   PostgreSQL seguiría siendo la única fuente de estado, índice = caché
   derivada). No se han medido mejoras de rendimiento: la motivación es
   escalabilidad con el corpus, no un beneficio demostrado. Factor dominante
   esperado con PostgreSQL real: patrón N+1 en `_collect_visible_chunks`
   (1 + D + V queries).
6. **Códigos `STUDY_*`**: **Actualización (2026-10-27, ADR-0005 rev. 3):**
   documentados en el ADR. `STUDY_GRAPH_FAILED` y `STUDY_NO_RESPONSE`
   consolidados en `STUDY_INTERNAL`; la extensión tipada de `StudyErrorCode`
   con los códigos de frontera HTTP queda pospuesta a TASK-11.
7. **UI de estudio**: no se implementó página Streamlit para `/api/study`
   (candidato a TASK-12).

## Confirmación de restricciones

Sin commits. Sin cambios en ADRs ni contratos existentes. Sin proveedor LLM
real. Sin reconciliador. Sin secretos impresos. Todo dentro de `ADA-Study`;
no se tocó `Entrega7` ni `Entregas`. Fakes e integraciones reales siguen
separados y las 7 pruebas de PostgreSQL real NO se declaran validadas.
