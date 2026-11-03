"""Auditoría READ-ONLY de publicación (PUBLISH-FLOW-01).

Solo ejecuta SELECT. Nunca imprime la DATABASE_URL ni secretos.
Uso: .venv\\Scripts\\python.exe .coda\\tmp\\audit_publish_flow_readonly.py
"""

import asyncio

DOC_ID = "doc_e156f7ba6e3f"
UPLOADER = "usr_86123da82670"
ACTIVE_VERSION = "ver_a096f844b901"

QUERIES = [
    (
        "DOCUMENTO",
        f"SELECT document_id, filename, subject, active_version_id, "
        f"editorial_status, review_status, uploaded_by, created_at, updated_at "
        f"FROM documents WHERE document_id = '{DOC_ID}'",
    ),
    (
        "USUARIO (uploader)",
        f"SELECT user_id, role FROM users WHERE user_id = '{UPLOADER}'",
    ),
    (
        "VERSIONES DEL DOCUMENTO",
        f"SELECT version_id, version_number, status, page_count, error_code "
        f"FROM document_versions WHERE document_id = '{DOC_ID}' ORDER BY version_number",
    ),
    (
        "CHUNKS DE LA VERSION ACTIVA",
        f"SELECT count(*) AS chunks FROM document_chunks WHERE version_id = '{ACTIVE_VERSION}'",
    ),
    (
        "JOBS DE INGESTA",
        f"SELECT job_id, status, error_code, created_at, updated_at FROM ingestion_jobs "
        f"WHERE document_id = '{DOC_ID}' ORDER BY created_at",
    ),
    (
        "OUTBOX (pending_index_ops) DE LA VERSION ACTIVA",
        f"SELECT destination, operation, status, count(*) AS n FROM pending_index_ops "
        f"WHERE version_id = '{ACTIVE_VERSION}' GROUP BY 1, 2, 3 ORDER BY 1, 2, 3",
    ),
    (
        "COLA DE MODERACION (estados que la API incluye)",
        "SELECT editorial_status, count(*) AS n FROM documents "
        "WHERE editorial_status IN ('pending_review', 'published_pending_review') "
        "GROUP BY 1",
    ),
    (
        "EVENTOS DE AUDITORIA DEL DOCUMENTO",
        f"SELECT action, result, reason, created_at FROM audit_events "
        f"WHERE entity_type = 'document' AND entity_id = '{DOC_ID}' ORDER BY created_at",
    ),
]


async def main() -> None:
    from ada_study.config import get_settings

    database_url = get_settings().database_url
    if not database_url:
        print("DATABASE_URL no configurada: no se puede verificar la BD.")
        return
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(database_url, echo=False)
    try:
        for title, query in QUERIES:
            print(f"\n=== {title} ===")
            try:
                # Conexión por consulta: un error no aborta las demás.
                async with engine.connect() as conn:
                    result = await conn.execute(__import__("sqlalchemy").text(query))
                    rows = result.fetchall()
                    if not rows:
                        print("(sin filas)")
                    for row in rows:
                        print(" | ".join(f"{v}" for v in row))
            except Exception as exc:  # noqa: BLE001 (diagnóstico)
                print(f"ERROR: {type(exc).__name__}: {exc}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
