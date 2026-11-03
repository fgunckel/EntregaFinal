import asyncio
import re

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

url = re.search(r"^DATABASE_URL=(.+)$", open(".env", encoding="utf-8").read(), re.M)
url = url.group(1).strip()


async def m():
    e = create_async_engine(url)
    async with e.connect() as c:
        idx = (
            await c.execute(
                text("SELECT indexname FROM pg_indexes WHERE tablename = 'conversations'")
            )
        ).scalars().all()
        n = (await c.execute(text("SELECT count(*) FROM conversations"))).scalar()
        print("indices conversations:", sorted(idx))
        print("filas conversations:", n)
    await e.dispose()


asyncio.run(m())
