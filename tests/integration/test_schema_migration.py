"""Esquema rev. 3 en PostgreSQL REAL (ADR-0001 rev. 3).

- Creación limpia con `create_all` (regresión del bug: el índice heredado
  con subconsulta en el predicado rompía `metadata.create_all`).
- Migración idempotente sobre esquema heredado, backfill del claim,
  detección de conflictos y retiro del índice inválido.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from ada_study.db.migrations import (
    DEDUP_INDEX_NAME,
    LEGACY_INDEX_NAME,
    MigrationConflictError,
    run_migrations,
)
from ada_study.db.schema import metadata

# Esquema "heredado" (rev. 2 simplificada): documents SIN sha256 y sin
# índices de dedup (el índice heredado nunca pudo existir en PostgreSQL).
LEGACY_DDL = """
CREATE TABLE documents (
  document_id      VARCHAR(64) PRIMARY KEY,
  filename         VARCHAR(512) NOT NULL,
  subject          VARCHAR(256),
  active_version_id VARCHAR(64),
  editorial_status VARCHAR(32) NOT NULL DEFAULT 'draft',
  review_status    VARCHAR(32) NOT NULL DEFAULT 'not_required',
  uploaded_by      VARCHAR(64),
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  deleted_at       TIMESTAMPTZ
);
CREATE TABLE document_versions (
  version_id     VARCHAR(64) PRIMARY KEY,
  document_id    VARCHAR(64) NOT NULL,
  version_number INTEGER NOT NULL,
  sha256         VARCHAR(64) NOT NULL,
  storage_key    VARCHAR(128) NOT NULL,
  content_type   VARCHAR(128) NOT NULL,
  size_bytes     BIGINT NOT NULL,
  status         VARCHAR(32) NOT NULL DEFAULT 'queued',
  page_count     INTEGER,
  error_code     VARCHAR(64),
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ux_versions_document_number
    ON document_versions (document_id, version_number);
"""

SHA_A = "a" * 64
SHA_B = "b" * 64


async def _index_exists(engine, name: str) -> bool:
    async with engine.connect() as conn:
        row = await conn.execute(
            text("SELECT 1 FROM pg_indexes WHERE indexname = :i"), {"i": name}
        )
        return row.first() is not None


async def _legacy_engine(test_database_url: str):
    """Engine con tablas de la rev. 2 (sin claim, sin índice de dedup)."""
    engine = create_async_engine(test_database_url)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.drop_all)
        # asyncpg NO admite múltiples comandos en un prepared statement: el
        # DDL se ejecuta sentencia por sentencia (infra de test).
        for statement in LEGACY_DDL.split(";"):
            if statement.strip():
                await conn.execute(text(statement))
    return engine


async def _dispose_and_clean(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(metadata.drop_all)
    await engine.dispose()


class TestCleanSchema:
    async def test_create_all_produces_rev3_schema(self, db_engine):
        """metadata.create_all funciona y deja la rev. 3 completa."""
        assert await _index_exists(db_engine, DEDUP_INDEX_NAME)
        assert not await _index_exists(db_engine, LEGACY_INDEX_NAME)
        async with db_engine.connect() as conn:
            row = await conn.execute(
                text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = 'documents' AND column_name = 'sha256'"
                )
            )
            assert row.first() is not None

    async def test_migration_on_fresh_schema_is_noop(self, db_engine):
        report = await run_migrations(db_engine)
        assert report == {
            "column_added": False,
            "rows_backfilled": 0,
            "index_created": False,
            "legacy_index_dropped": False,
        }


class TestMigrationOnLegacySchema:
    async def _insert_versions(self, engine, rows: list[tuple[str, str, bool]]):
        """(document_id, sha, deleted) simplificado: doc + 1 versión."""
        async with engine.begin() as conn:
            for document_id, sha, deleted in rows:
                deleted_clause = "now()" if deleted else "NULL"
                await conn.execute(
                    text(
                        "INSERT INTO documents (document_id, filename, deleted_at) "
                        f"VALUES (:d, 'legado.pdf', {deleted_clause})"
                    ),
                    {"d": document_id},
                )
                await conn.execute(
                    text(
                        "INSERT INTO document_versions (version_id, document_id, "
                        "version_number, sha256, storage_key, content_type, "
                        "size_bytes, status) VALUES (:v, :d, 1, :s, 'k', "
                        "'application/pdf', 1, 'available')"
                    ),
                    {"v": f"ver_{document_id}", "d": document_id, "s": sha},
                )

    async def test_adds_column_and_backfills_latest_non_purged_version(self, test_database_url):
        engine = await _legacy_engine(test_database_url)
        try:
            await self._insert_versions(engine, [("doc1", SHA_A, False)])
            # Segunda versión del mismo documento con otra huella.
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO document_versions (version_id, document_id, "
                        "version_number, sha256, storage_key, content_type, "
                        "size_bytes, status) VALUES ('v2', 'doc1', 2, :s, 'k2', "
                        "'application/pdf', 1, 'available')"
                    ),
                    {"s": SHA_B},
                )
            report = await run_migrations(engine)
            assert report["column_added"] is True
            assert report["rows_backfilled"] == 1
            assert report["index_created"] is True
            async with engine.connect() as conn:
                claim = (
                    await conn.execute(
                        text("SELECT sha256 FROM documents WHERE document_id = 'doc1'")
                    )
                ).scalar()
            assert claim == SHA_B  # la versión registrada más reciente
        finally:
            await _dispose_and_clean(engine)

    async def test_migration_is_idempotent(self, test_database_url):
        engine = await _legacy_engine(test_database_url)
        try:
            await self._insert_versions(engine, [("doc1", SHA_A, False)])
            first = await run_migrations(engine)
            assert first["column_added"] is True
            assert first["index_created"] is True
            second = await run_migrations(engine)
            assert second == {
                "column_added": False,
                "rows_backfilled": 0,
                "index_created": False,
                "legacy_index_dropped": False,
            }
        finally:
            await _dispose_and_clean(engine)

    async def test_conflicting_live_shas_abort_migration_without_side_effects(
        self, test_database_url
    ):
        engine = await _legacy_engine(test_database_url)
        try:
            await self._insert_versions(
                engine, [("docA", SHA_A, False), ("docB", SHA_A, False)]
            )
            with pytest.raises(MigrationConflictError) as excinfo:
                await run_migrations(engine)
            assert set(excinfo.value.conflicts) == {SHA_A}
            # Sin índice creado y SIN borrar/modificar datos.
            assert not await _index_exists(engine, DEDUP_INDEX_NAME)
            async with engine.connect() as conn:
                count = (await conn.execute(text("SELECT count(*) FROM documents"))).scalar()
            assert count == 2

            # El conflicto se resuelve por vía editorial (retiro): la
            # siguiente corrida converge.
            async with engine.begin() as conn:
                await conn.execute(
                    text("UPDATE documents SET deleted_at = now() WHERE document_id = 'docB'")
                )
            report = await run_migrations(engine)
            assert report["index_created"] is True
            assert await _index_exists(engine, DEDUP_INDEX_NAME)
        finally:
            await _dispose_and_clean(engine)

    async def test_deleted_document_claims_do_not_block_migration(self, test_database_url):
        engine = await _legacy_engine(test_database_url)
        try:
            # Misma huella pero uno de los documentos está borrado: no hay
            # conflicto (el claim liberado queda fuera del índice parcial).
            await self._insert_versions(engine, [("docA", SHA_A, False), ("docB", SHA_A, True)])
            report = await run_migrations(engine)
            assert report["index_created"] is True
            assert await _index_exists(engine, DEDUP_INDEX_NAME)
        finally:
            await _dispose_and_clean(engine)

