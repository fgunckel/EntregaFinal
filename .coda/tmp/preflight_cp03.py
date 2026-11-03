"""Preflight CHECKPOINTER-03 (solo lectura / acciones autorizadas).

1. Verifica conexión a PostgreSQL (sin imprimir credenciales).
2. Lista existencia de tablas: conversations, checkpoints, checkpoint_blobs,
   checkpoint_writes, checkpoint_migrations.
3. Si falta `conversations` pero la conexión funciona, crea SOLO la tabla
   `conversations` (migración CHECKPOINTER-02), idempotente.
"""

import asyncio
import re
import sys

REQUIRED = [
    "conversations",
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
    "checkpoint_migrations",
]

CONVERSATIONS_DDL = """
CREATE TABLE IF NOT EXISTS conversations (
    conversation_id UUID PRIMARY KEY,
    user_id VARCHAR(64) NOT NULL,
    thread_id VARCHAR(128) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_turn_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    turn_count INTEGER NOT NULL DEFAULT 0
)
"""


def load_database_url() -> str:
    env = open(".env", encoding="utf-8").read()
    raw = re.search(r"^DATABASE_URL=(.+)$", env, re.MULTILINE).group(1).strip()
    return raw.replace("postgresql+asyncpg://", "postgresql://")


async def main() -> int:
    import asyncpg

    url = load_database_url()
    conn = await asyncpg.connect(url)
    try:
        rows = await conn.fetch(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' ORDER BY table_name"
        )
        names = {r["table_name"] for r in rows}
        print("tablas public:", sorted(names))
        for t in REQUIRED:
            print(f"  {t}: {'OK' if t in names else 'FALTA'}")

        if "conversations" not in names:
            print("aplicando migración CHECKPOINTER-02 (solo tabla conversations)...")
            await conn.execute(CONVERSATIONS_DDL)
            print("tabla conversations creada.")
        else:
            cols = await conn.fetch(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name='conversations' ORDER BY ordinal_position"
            )
            print("columnas conversations:")
            for c in cols:
                print(f"  {c['column_name']}: {c['data_type']}")
            n = await conn.fetchval("SELECT count(*) FROM conversations")
            print("filas conversations:", n)
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
