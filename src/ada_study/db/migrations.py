"""Migración idempotente del catálogo a ADR-0001 rev. 3.

`metadata.create_all` NO modifica tablas existentes: sobre una base creada
con la rev. 2 del esquema no agrega `documents.sha256` ni retira el índice
heredado (que además es inválido: PostgreSQL rechaza predicados con
subconsulta, `cannot use subquery in index predicate`). Esta migración,
idempotente y NO destructiva, cubre ambos mundos:

1. Agrega `documents.sha256` (nullable, backfill-safe) si falta.
2. Backfill del claim: para cada documento, la huella de su versión
   registrada más reciente NO purgada (`status <> 'deleting'`), que es la
   semántica del claim vigente. No sobrescribe claims ya existentes.
3. Detecta conflictos de huella entre documentos VIVOS antes de crear el
   índice único: si existen, aborta con `MigrationConflictError` SIN crear
   el índice y SIN modificar ni borrar datos (el conflicto se resuelve por
   vía editorial, no automáticamente).
4. Crea `ux_documents_active_sha256` si falta.
5. Retira `ux_versions_active_sha256` si existe.

Uso sobre una base EXISTENTE:

    python -m ada_study.db.migrate "postgresql+asyncpg://ada:pass@localhost:5432/ada_study"

Base limpia: `python -m ada_study.db.create_schema <DATABASE_URL>` crea el
esquema completo (rev. 3) y ejecuta esta migración como verificación final
(todos los pasos quedan en no-op).
"""

import asyncio
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

DEDUP_INDEX_NAME = "ux_documents_active_sha256"
LEGACY_INDEX_NAME = "ux_versions_active_sha256"


class MigrationConflictError(RuntimeError):
    """Documentos vivos distintos reclaman la misma huella; migración abortada.

    NO borra datos ni crea el índice: el conflicto se resuelve por vía
    editorial (retirar/borrar uno de los documentos) y se re-ejecuta.
    """

    def __init__(self, conflicts: dict[str, list[str]]) -> None:
        self.conflicts = conflicts
        detail = "; ".join(
            f"{sha} -> [{', '.join(sorted(docs))}]"
            for sha, docs in sorted(conflicts.items())
        )
        super().__init__(
            "documentos vivos distintos reclaman la misma huella SHA-256 "
            f"({detail}). Migración abortada SIN crear el índice y SIN "
            "modificar datos: resuelva por vía editorial y re-ejecute."
        )


async def _column_exists(conn, table: str, column: str) -> bool:
    row = await conn.execute(
        text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :t AND column_name = :c"
        ),
        {"t": table, "c": column},
    )
    return row.first() is not None


async def _index_exists(conn, index_name: str) -> bool:
    row = await conn.execute(
        text("SELECT 1 FROM pg_indexes WHERE indexname = :i"), {"i": index_name}
    )
    return row.first() is not None


async def run_migrations(engine: AsyncEngine) -> dict[str, int | bool]:
    """Aplica la migración rev. 3 de forma idempotente.

    Devuelve un informe con lo efectivamente hecho en esta corrida:
    `column_added`, `rows_backfilled`, `index_created`, `legacy_index_dropped`.
    Una segunda corrida sobre el mismo esquema devuelve todo en falso/0.
    """
    report: dict[str, int | bool] = {
        "column_added": False,
        "rows_backfilled": 0,
        "index_created": False,
        "legacy_index_dropped": False,
    }
    async with engine.connect() as conn:
        # Sin catálogo, no hay nada que migrar: create_schema/create_all lo
        # crea directamente en rev. 3.
        exists = (await conn.execute(text("SELECT to_regclass('public.documents')"))).first()
        if exists is None or exists[0] is None:
            return report

    # 1) Agregar la columna del claim (nullable: backfill-safe).
    async with engine.begin() as conn:
        if not await _column_exists(conn, "documents", "sha256"):
            await conn.execute(
                text("ALTER TABLE documents ADD COLUMN sha256 VARCHAR(64)")
            )
            report["column_added"] = True

    # 2) Backfill del claim: huella de la versión registrada más reciente
    #    no purgada, SOLO para documentos aún sin claim.
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "UPDATE documents d SET sha256 = v.sha256 "
                "FROM ("
                "  SELECT DISTINCT ON (document_id) document_id, sha256"
                "  FROM document_versions"
                "  WHERE status <> 'deleting'"
                "  ORDER BY document_id, version_number DESC"
                ") v "
                "WHERE v.document_id = d.document_id AND d.sha256 IS NULL"
            )
        )
        report["rows_backfilled"] = result.rowcount or 0

    # 3) Verificación de conflictos + índice único parcial.
    async with engine.begin() as conn:
        conflict_rows = (
            await conn.execute(
                text(
                    "SELECT sha256, array_agg(document_id) FROM documents "
                    "WHERE deleted_at IS NULL AND sha256 IS NOT NULL "
                    "GROUP BY sha256 HAVING count(*) > 1"
                )
            )
        ).all()
        if conflict_rows:
            raise MigrationConflictError({sha: list(docs) for sha, docs in conflict_rows})
        if not await _index_exists(conn, DEDUP_INDEX_NAME):
            await conn.execute(
                text(
                    "CREATE UNIQUE INDEX ux_documents_active_sha256 "
                    "ON documents (sha256) WHERE deleted_at IS NULL"
                )
            )
            report["index_created"] = True

    # 4) Retirar el índice heredado de la rev. 2.
    async with engine.begin() as conn:
        if await _index_exists(conn, LEGACY_INDEX_NAME):
            await conn.execute(text(f"DROP INDEX {LEGACY_INDEX_NAME}"))
            report["legacy_index_dropped"] = True

    return report


async def migrate_database(database_url: str) -> dict[str, int | bool]:
    """Crea un engine efímero, ejecuta la migración y lo dispone."""
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(database_url, echo=False)
    try:
        return await run_migrations(engine)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    """Punto de entrada: python -m ada_study.db.migrate <DATABASE_URL>."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print(
            "Uso: python -m ada_study.db.migrate <DATABASE_URL>\n"
            "Migración idempotente y NO destructiva a ADR-0001 rev. 3.\n"
            "Ejemplo: postgresql+asyncpg://ada:pass@localhost:5432/ada_study"
        )
        return 0 if not args else 1
    try:
        report = asyncio.run(migrate_database(args[0]))
    except MigrationConflictError as exc:
        print(f"CONFLICTO DE MIGRACIÓN: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - CLI: mensaje claro sin traceback
        print(f"ERROR: la migración falló: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(
        "Migración ADR-0001 rev. 3 aplicada: "
        + ", ".join(f"{key}={value}" for key, value in report.items())
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
