"""SELECT-only: listar conversaciones registradas (sin modificar datos)."""

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from ada_study.config import get_settings


async def main():
    engine = create_async_engine(get_settings().database_url)
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT conversation_id, user_id, created_at "
                    "FROM conversations ORDER BY created_at DESC LIMIT 10"
                )
            )
        ).fetchall()
        print(f"Conversaciones registradas ({len(rows)} últimas):")
        for r in rows:
            print(f"  {r[0]}  user={r[1]}  {r[2]}")
    await engine.dispose()


asyncio.run(main())
