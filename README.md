# ADA Study

Biblioteca documental con RAG incremental y centro de estudio para estudiantes de 12–15 años.

## Estado

- **TASK-00** — Auditoría de fuentes (completada, `docs/audit/BASELINE.md`).
- **TASK-01** — Decisiones de arquitectura y contratos (completada, `docs/adr/` + `docs/audit/`).
- **TASK-02** — Catálogo documental y metadatos (en curso).

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
