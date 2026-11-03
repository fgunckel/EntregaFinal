"""SELECT-only: conteo de tablas de checkpoint de LangGraph."""

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from ada_study.config import get_settings


async def main():
    engine = create_async_engine(get_settings().database_url)
    async with engine.connect() as conn:
        tables = (
            await conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_name LIKE '%checkpoint%'"
                )
            )
        ).scalars().all()
        print("tablas checkpoint:", tables)
        for t in tables:
            n = (await conn.execute(text(f"SELECT count(*) FROM {t}"))).scalar()
            print(f"  {t}: {n} filas")
    await engine.dispose()


asyncio.run(main())
