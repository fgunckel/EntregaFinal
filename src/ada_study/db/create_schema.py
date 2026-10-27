"""Creación idempotente del esquema en PostgreSQL (TASK-06, cierra B1).

Aplica `metadata.create_all` desde `db/schema.py` (única definición del
esquema; no duplica ni diverge). Idempotente: `create_all` solo crea lo que
falta (checkfirst por defecto). Uso:

    python -m ada_study.db.create_schema "postgresql+asyncpg://ada:pass@localhost:5432/ada_study"
"""

import asyncio
import sys

from ada_study.db.schema import metadata


async def create_schema(database_url: str) -> int:
    """Crea el esquema si falta; devuelve código de salida (0 ok, 1 error)."""
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
        print("Esquema verificado/creado (idempotente).")
        return 0
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
