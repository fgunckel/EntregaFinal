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
```

Endpoints (ADR-0006): `POST /api/documents` (202 + polling),
`GET /api/documents`, `GET /api/documents/{id}`,
`GET /api/ingestion-jobs/{job_id}`. Autenticación: `Authorization: Bearer <token>`
(token opaco de sesión; se valida contra PostgreSQL en cada petición).

Nota: sin Pinecone real, el worker usa `FakePineconeDestination` (doble de
pruebas); la integración real de Pinecone queda pendiente.
