"""SELECT-only: thread_ids presentes en checkpoints de LangGraph."""

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
                    "SELECT DISTINCT thread_id, checkpoint_ns "
                    "FROM checkpoints ORDER BY thread_id LIMIT 20"
                )
            )
        ).fetchall()
        for r in rows:
            print(f"  thread={r[0]!r} ns={r[1]!r}")
        # Metadatos del último checkpoint de cada thread (fuente, step).
        meta = (
            await conn.execute(
                text(
                    "SELECT thread_id, metadata->>'source' AS src, "
                    "metadata->>'step' AS step FROM checkpoints "
                    "ORDER BY thread_id, metadata->>'step' DESC LIMIT 20"
                )
            )
        ).fetchall()
        for r in meta:
            print(f"  meta thread={r[0]!r} source={r[1]!r} step={r[2]!r}")
    await engine.dispose()


asyncio.run(main())
