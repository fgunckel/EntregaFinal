"""SELECT-only: columnas y conteo de la tabla conversations."""

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from ada_study.config import get_settings


async def main():
    engine = create_async_engine(get_settings().database_url)
    async with engine.connect() as conn:
        cols = (
            await conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='conversations'"
                )
            )
        ).scalars().all()
        total = (await conn.execute(text("SELECT count(*) FROM conversations"))).scalar()
        print("columnas:", cols)
        print("total filas:", total)
    await engine.dispose()


asyncio.run(main())
