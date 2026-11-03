"""Diagnóstico READ-ONLY: estados documentales (solo SELECT, sin migraciones)."""

import asyncio
import re

import asyncpg

_env = open(".env", encoding="utf-8").read()
_url = re.search(r"DATABASE_URL=(.+)", _env).group(1).strip()
_url = re.sub(r"^postgresql\+asyncpg://", "postgresql://", _url)


async def main() -> None:
    conn = await asyncpg.connect(_url)
    print("--- documents (sin datos sensibles) ---")
    rows = await conn.fetch(
        "SELECT document_id, filename, editorial_status, review_status, "
        "uploaded_by, created_at, updated_at, deleted_at "
        "FROM documents ORDER BY created_at"
    )
    for r in rows:
        print(dict(r))
    print("--- document_versions ---")
    rows = await conn.fetch(
        "SELECT version_id, document_id, version_number, status, error_code, "
        "created_at FROM document_versions ORDER BY created_at"
    )
    for r in rows:
        print(dict(r))
    print("--- users (sin hash) ---")
    rows = await conn.fetch(
        "SELECT user_id, email, role, account_status FROM users"
    )
    for r in rows:
        print(dict(r))
    print("--- audit_events recientes ---")
    rows = await conn.fetch(
        "SELECT event_id, actor_id, action, entity_type, entity_id, result, "
        "created_at FROM audit_events ORDER BY created_at DESC LIMIT 10"
    )
    for r in rows:
        print(dict(r))
    print("--- audit_events de documentos ---")
    rows = await conn.fetch(
        "SELECT actor_id, action, entity_type, entity_id, result, created_at "
        "FROM audit_events WHERE entity_type='document' ORDER BY created_at"
    )
    print("cantidad:", len(rows))
    for r in rows:
        print(dict(r))
    print("--- ingestion_jobs ---")
    rows = await conn.fetch(
        "SELECT job_id, document_id, status, error_code FROM ingestion_jobs"
    )
    for r in rows:
        print(dict(r))
    print("--- pending_index_ops ---")
    rows = await conn.fetch(
        "SELECT op_id, version_id, destination, operation, status, attempts "
        "FROM pending_index_ops ORDER BY created_at"
    )
    for r in rows:
        print(dict(r))
    await conn.close()

    # Parte 2 (sin BD): JSON REAL del endpoint /api/moderation/queue
    # usando el repo fake de los tests (TestClient, sin red ni Postgres).
    # ---------------------------------------------------------------
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # raíz proyecto
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))
    from test_ui_api_client import (  # noqa: E402
        _build_app,
        _document,
        _session_token,
        _user,
    )

    from ada_study.ui.api_client import ApiClient  # noqa: E402

    repo = _make_repo_with_state()
    admin = _user("usr_admin_diag", "admin")
    repo.users[admin.user_id] = admin
    repo.documents["doc_pend"] = _document(
        "doc_pend", admin.user_id, status="pending_review"
    )
    repo.documents["doc_draft"] = _document(
        "doc_draft", admin.user_id, status="draft"
    )
    from fastapi.testclient import TestClient  # noqa: E402

    token = _session_token(repo, admin)
    with TestClient(_build_app(repo)) as http:
        client = ApiClient("http://testserver", token, http=http)
        raw = http.get(
            "/api/moderation/queue", headers={"Authorization": f"Bearer {token}"}
        )
        print("--- HTTP crudo del endpoint ---")
        print(f"status: {raw.status_code}")
        print(f"body: {raw.text[:1200]}")
        try:
            client.moderation_queue()
            print("moderation_queue(): OK (sin KeyError)")
        except Exception as exc:  # noqa: BLE001
            print(f"moderation_queue(): {type(exc).__name__}: {exc}")


def _make_repo_with_state():
    from test_ui_api_client import FakeCatalogRepository  # type: ignore

    return FakeCatalogRepository()


asyncio.run(main())
