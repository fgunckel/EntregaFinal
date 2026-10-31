"""Creación y migración del esquema en PostgreSQL (TASK-06, ADR-0001 rev. 3).

Aplica `metadata.create_all` desde `db/schema.py` (única definición del
esquema; no duplica ni diverge) y luego ejecuta la migración idempotente de
`db/migrations.py` (columna claim `documents.sha256` + índice
`ux_documents_active_sha256` + retiro del índice heredado).

- Base limpia: `create_all` crea todo en rev. 3 y la migración queda en no-op.
- Base existente (rev. 2): `create_all` no toca las tablas ya creadas; la
  migración agrega la columna, hace backfill del claim, detecta conflictos
  de huella y crea el índice único parcial SIN borrar datos.

Uso:

    python -m ada_study.db.create_schema "postgresql+asyncpg://ada:pass@localhost:5432/ada_study"
    # equivalente y suficiente para bases existentes:
    python -m ada_study.db.migrate "postgresql+asyncpg://ada:pass@localhost:5432/ada_study"
"""

import asyncio
import sys

from ada_study.db.migrations import (
    MigrationConflictError,
    run_migrations,
)
from ada_study.db.schema import metadata


async def create_schema(database_url: str) -> int:
    """Crea el esquema y aplica la migración; devuelve código de salida."""
    try:
        from sqlalchemy.ext.asyncio import create_async_engine
    except ImportError:
        print(
            "ERROR: falta sqlalchemy (instalá requirements o el paquete dev).",
            file=sys.stderr,
        )
        return 1
    engine = create_async_engine(database_url, echo=False)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(metadata.create_all)
        report = await run_migrations(engine)
        print("Esquema verificado/creado (idempotente).")
        print("Migración rev. 3: " + ", ".join(f"{k}={v}" for k, v in report.items()))
        return 0
    except MigrationConflictError as exc:
        print(f"CONFLICTO DE MIGRACIÓN: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - CLI: mensaje claro sin traceback
        print(f"ERROR: no se pudo crear el esquema: {type(exc).__name__}", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    """Punto de entrada: python -m ada_study.db.create_schema <DATABASE_URL>."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print(
            "Uso: python -m ada_study.db.create_schema <DATABASE_URL>\n"
            'Ejemplo: postgresql+asyncpg://ada:pass@localhost:5432/ada_study'
        )
        return 0 if not args else 1
    return asyncio.run(create_schema(args[0]))


if __name__ == "__main__":
    sys.exit(main())
