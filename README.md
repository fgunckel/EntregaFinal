# ADA Study

Biblioteca documental con RAG incremental y centro de estudio para estudiantes de 12–15 años.

## Estado

- **TASK-00** — Auditoría de fuentes (completada, `docs/audit/BASELINE.md`).
- **TASK-01** — Decisiones de arquitectura y contratos (completada, `docs/adr/` + `docs/audit/`).
- **TASK-02** — Catálogo documental y metadatos (completada).
- **TASK-03** — Ingesta incremental (completada).
- **TASK-04** — Identidad mínima y moderación (completada).
- **TASK-05** — Retriever híbrido y grafo pedagógico (completada).
- **TASK-06** — API de documentos, Fase E1 (completada).
- **TASK-07** — Cierre de API: borrado/restauración/reintento, moderación, auditoría, E2E (completada).

## Estructura

- `ADA_Study_Spec_Kit/` — specs, tareas y prompts (fuente de requisitos).
- `docs/adr/` — decisiones de arquitectura (ADR-0001…0007).
- `docs/audit/` — auditoría, matriz de contratos, plan de implementación.
- `src/ada_study/` — código de la aplicación.
- `tests/` — pruebas unitarias e integración.

## Desarrollo

```bash
uv venv --python 3.12 .venv
uv pip install -e ".[dev]" --python .venv
.venv/Scripts/python -m pytest
.venv/Scripts/python -m ruff check .
```

Las pruebas de integración requieren `DATABASE_URL` apuntando a un PostgreSQL real
(p. ej. `postgresql+asyncpg://user:pass@localhost:5432/ada_study`); sin ella se saltan.

## Proveedor LLM (TASK-11)

El nodo **tutor** puede redactar la explicación con un LLM real; el resto del
grafo (supervisor, researcher, validator, synthesizer, `limited_finish`)
permanece determinístico (anti-alucinación y procedencia). Por defecto el
sistema corre en **modo determinístico** (`ADA_LLM_PROVIDER=none`), sin
dependencias de proveedor ni costos.

Para activar el modo LLM:

```bash
pip install -e ".[llm]"   # langchain-openai (extra opcional)
```

```env
ADA_LLM_PROVIDER=openai
ADA_LLM_MODEL=gpt-5.4-mini
ADA_LLM_API_KEY=sk-...    # o OPENAI_API_KEY; nunca versionar la clave real
```

Garantías (decisiones aprobadas TASK-11):

- El modelo se construye **una vez por proceso** (lifespan de la API) y solo
  se inyecta en el tutor; configuración inválida corta el arranque (fail-fast).
- El LLM redacta **únicamente `explanation`**: las referencias se copian
  verbatim de la evidencia recuperada y el `status` se decide determinísticamente.
- Los fragmentos recuperados y la consulta se delimitan como **datos no
  confiables** (escape de etiquetas + reglas anti prompt-injection).
- Límites por llamada: `ADA_LLM_MAX_TOKENS` (800), `ADA_LLM_TIMEOUT_SECONDS`
  (20) y `ADA_LLM_MAX_RETRIES` (2) del proveedor; sin reintentos a nivel de
  grafo (máx. 1 llamada del tutor por intento).
- Fallo del proveedor, salida vacía o excedida → **fallback determinístico**
  con limitación explícita en `StudyResponse.limitations`; sin evidencia no
  se llama al LLM. Errores y uso de tokens se registran sin secretos ni
  contenido sensible.

Prueba optativa contra el proveedor real (realiza una llamada pagada; requiere
autorización explícita y clave):

```bash
ADA_LLM_LIVE=1 OPENAI_API_KEY=sk-... python -m pytest tests/test_study_llm_integration.py -v
```

Las pruebas de integración (PostgreSQL) requieren `DATABASE_URL` apuntando a un
PostgreSQL real (p. ej. `postgresql+asyncpg://user:pass@localhost:5432/ada_study`);
sin ella se saltan.

## API y worker (desarrollo)

```bash
# 1. PostgreSQL 16 (Docker)
docker compose up -d

# 2. Crear el esquema (idempotente)
export DATABASE_URL="postgresql+asyncpg://ada:ada_dev_password@localhost:5432/ada_study"
python -m ada_study.db.create_schema "$DATABASE_URL"

# 3. Crear la cuenta admin inicial (interactivo, contraseña por stdin)
python -m ada_study.cli "$DATABASE_URL"

# 4. Iniciar el worker de ingesta (separado de la API; consume el outbox)
python -m ada_study.worker_cli "$DATABASE_URL" --once   # un ciclo
python -m ada_study.worker_cli "$DATABASE_URL"          # bucle (2 s)

# 5. Iniciar la API
uvicorn ada_study.api.app:create_app --factory --reload

# 6. (Opcional) Iniciar la UI Streamlit (TASK-08)
pip install -e ".[ui]"
streamlit run src/ada_study/ui/app.py
```

Endpoints (ADR-0006): `POST /api/documents` (202 + polling),
`GET /api/documents`, `GET /api/documents/{id}`,
`GET /api/ingestion-jobs/{job_id}`,
`DELETE /api/documents/{id}` (202, cortina + purga eventual),
`POST /api/documents/versions/{id}/retry` (solo failed, idempotente),
`POST /api/documents/{id}/versions/{vid}/restore` (versión nueva, historial intacto),
`POST /api/moderation/documents/{id}/decide` (moderator/admin),
`GET /api/moderation/queue` (moderator/admin),
`GET /api/audit/events` (moderator/admin),
`POST /api/auth/login` / `POST /api/auth/logout` (TASK-08, B1),
`GET /api/me` (perfil de la sesión validada, TASK-08 B3),
`POST /api/study` (TASK-10/11: consulta pedagógica con evidencia verificada
del corpus; el supervisor conserva el enrutado determinístico y el chat
model opcional solo redacta la explicación del tutor, con fallback
determinístico ante fallo del proveedor — ver "Proveedor LLM" abajo).
Autenticación: `Authorization: Bearer <token>`
(token opaco de sesión; se valida contra PostgreSQL en cada petición).

## Arquitectura

```text
┌─────────────┐   202+job   ┌──────────────────┐   outbox   ┌─────────────────┐
│   Cliente   │ ──────────► │  API FastAPI     │ ─────────► │  PostgreSQL     │
│ (UI TASK-08)│ ◄────────── │  + Identity      │ ◄───────── │  (ADR-0001)     │
└─────────────┘   polling   └──────────────────┘            └──────┬──────────┘
                                                                   │ outbox
                     ┌─────────────────────────────────────────────┘
                     ▼
             ┌───────────────┐   index/deindex   ┌──────────────────────┐
             │ Worker ingesta│ ────────────────► │ BM25 (rebuild desde  │
             │ (separado)    │                   │ chunks) + Pinecone   │
             └──────┬────────┘                   └──────────┬───────────┘
                    │ extrae/chunkea (pypdf)                │
                    ▼                                       ▼
             ┌───────────────┐                    ┌──────────────────────┐
             │ Almacén PDF   │                    │ HybridRetriever      │
             │ content-addr. │                    │ (RRF: bm25 + vector) │
             │ (ADR-0002)    │                    └──────────┬───────────┘
             └───────────────┘                               ▼
                                                    ┌──────────────────────┐
                                                    │ Grafo pedagógico     │
                                                    │ (supervisor/…/synth) │
                                                    └──────────────────────┘
```

Flujo editorial: upload de student nace `pending_review` (visible en privado
para su autor; el RAG compartido solo expone `published*`). El moderator decide
en `/api/moderation/queue`; toda acción sensible queda en `audit_events`
(append-only). Borrado/retiro aplican cortina inmediata y encolan la purga
física (eventual, ADR-0004).

Nota: sin Pinecone real, el worker usa `FakePineconeDestination` (doble de
pruebas); la integración real de Pinecone queda pendiente.
