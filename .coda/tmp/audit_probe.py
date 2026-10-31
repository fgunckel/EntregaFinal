"""Sonda READ-ONLY de audit_events (no inserta, no actualiza)."""
import asyncio

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine

from ada_study.config import get_settings
from ada_study.db import schema


async def main() -> None:
    engine = create_async_engine(get_settings().database_url, echo=False)
    try:
        async with engine.begin() as conn:
            total = (
                await conn.execute(select(func.count()).select_from(schema.audit_events))
            ).scalar()
            print(f"TOTAL eventos: {total}")
            rows = (
                await conn.execute(
                    select(
                        schema.audit_events.c.action,
                        schema.audit_events.c.entity_type,
                        schema.audit_events.c.entity_id,
                        func.count(),
                    )
                    .group_by(
                        schema.audit_events.c.action,
                        schema.audit_events.c.entity_type,
                        schema.audit_events.c.entity_id,
                    )
                    .order_by(schema.audit_events.c.action)
                )
            ).all()
            print("--- accion | entity_type | entity_id | cantidad ---")
            for r in rows:
                print(f"  {r[0]} | {r[1]} | {r[2]} | {r[3]}")
            target = (
                await conn.execute(
                    select(func.count())
                    .select_from(schema.audit_events)
                    .where(schema.audit_events.c.entity_id == "doc_b9d9f675e861")
                )
            ).scalar()
            print(f"Eventos con entity_id=doc_b9d9f675e861: {target}")
    finally:
        await engine.dispose()


asyncio.run(main())
