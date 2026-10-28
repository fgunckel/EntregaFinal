# Checklist de entrega reproducible (TASK-07, Fase I3)

Checklist para reproducir la entrega de ADA Study desde cero (trabajo final Coderhouse).

## 1. Requisitos previos

- [ ] Python 3.12 instalado (`python --version`).
- [ ] Docker disponible (`docker --version`).
- [ ] Puerto 5432 libre para PostgreSQL.

## 2. Entorno

```bash
uv venv --python 3.12 .venv
uv pip install -e ".[dev]" --python .venv
cp .env.example .env   # ajustar credenciales si hace falta
```

## 3. Infraestructura

```bash
docker compose up -d          # PostgreSQL 16
export DATABASE_URL="postgresql+asyncpg://ada:ada_dev_password@localhost:5432/ada_study"
python -m ada_study.db.create_schema "$DATABASE_URL"   # esquema idempotente
python -m ada_study.cli "$DATABASE_URL"                # admin inicial (interactivo)
```

## 4. Verificación de calidad

```bash
.venv/Scripts/python -m pytest          # 192 passed, 7 skipped (sin DATABASE_URL real)
.venv/Scripts/python -m ruff check .
```

- [ ] Suite completa en verde (las 7 pruebas de integración se saltan sin
      PostgreSQL real; con `DATABASE_URL` deben correr y pasar).
- [ ] Ruff sin advertencias.

## 5. Servicios en marcha

```bash
python -m ada_study.worker_cli "$DATABASE_URL" &        # worker (outbox)
uvicorn ada_study.api.app:create_app --factory --reload # API en :8000
```

- [ ] `GET /api/documents` responde 401 sin token y 200 con token válido.
- [ ] `POST /api/documents` con un PDF devuelve 202 + `job_id`.
- [ ] El worker procesa el job; `GET /api/documents/{id}` muestra `active_version_id`.
- [ ] `GET /api/moderation/queue` (moderator) lista el aporte pendiente.
- [ ] `POST /api/moderation/documents/{id}/decide` con `approve` publica.
- [ ] `DELETE /api/documents/{id}` devuelve 202 y el detalle pasa a 404.
- [ ] `GET /api/audit/events` (moderator) registra las acciones anteriores.

## 6. Pendientes declarados (no validados con servicios reales)

- Pinecone real: el worker usa `FakePineconeDestination`; falta integración
  con credenciales reales (decisión TASK-03).
- Las 7 pruebas de integración requieren PostgreSQL real; no declarar
  validadas mientras se ejecuten con fakes (decisión TASK-03).
- Escenario E2E 4 (búsqueda web etiquetada): fuera de TASK-07 (decisión TASK-02).
- UI: TASK-08.
