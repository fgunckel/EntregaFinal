"""SELECT-only: estado del documento Historia tras PUBLISH-FLOW-02 (no muta)."""

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from ada_study.config import get_settings


async def main() -> None:
    engine = create_async_engine(get_settings().database_url)
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT document_id, filename, editorial_status, "
                    "active_version_id, uploaded_by, deleted_at "
                    "FROM documents WHERE document_id = 'doc_e156f7ba6e3f'"
                )
            )
        ).fetchone()
        ver = (
            await conn.execute(
                text(
                    "SELECT version_id, status FROM document_versions "
                    "WHERE version_id = 'ver_a096f844b901'"
                )
            )
        ).fetchone()
        events = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM audit_events "
                    "WHERE entity_id = 'doc_e156f7ba6e3f' "
                    "AND action = 'document.submit_for_review'"
                )
            )
        ).scalar()
        print("DOC:", dict(row._mapping) if row else None)
        print("VER:", dict(ver._mapping) if ver else None)
        print("AUDIT submit events:", events)
    await engine.dispose()


asyncio.run(main())
