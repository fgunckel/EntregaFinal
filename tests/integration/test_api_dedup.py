"""Traducción a 409 (ING_DUPLICATE) en upload y restore — API completa.

Corre contra PostgreSQL real con la app FastAPI y sesión auténtica; el
worker no participa (el claim se establece al registrar la versión).
"""

CONTENT_X = b"%PDF-1.4 contenido API X"
CONTENT_Y = b"%PDF-1.4 contenido API Y"


def _upload_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/pdf",
        "x-filename": "doc.pdf",
    }


class TestApiDedup409:
    async def test_upload_duplicate_maps_to_409_ing_duplicate(self, api):
        client, _, token = api
        first = await client.post(
            "/api/documents", content=CONTENT_X, headers=_upload_headers(token)
        )
        assert first.status_code == 202
        duplicate = await client.post(
            "/api/documents", content=CONTENT_X, headers=_upload_headers(token)
        )
        assert duplicate.status_code == 409
        assert duplicate.json()["error"] == "ING_DUPLICATE"

    async def test_restore_conflict_returns_409_not_500(self, api):
        """Restore con la huella reclamada por OTRO documento vivo → 409.

        El estado cruzado de claims es inalcanzable por los flujos actuales
        (exactamente por eso es seguro) y se simula con SQL directo, como
        podría quedar una base legada tras la migración.
        """
        import hashlib

        from sqlalchemy import text

        client, repo, token = api
        first = (
            await client.post("/api/documents", content=CONTENT_X, headers=_upload_headers(token))
        ).json()
        second = (
            await client.post("/api/documents", content=CONTENT_Y, headers=_upload_headers(token))
        ).json()
        sha_x = hashlib.sha256(CONTENT_X).hexdigest()
        sha_y = hashlib.sha256(CONTENT_Y).hexdigest()
        async with repo._engine.begin() as conn:
            await conn.execute(
                text("UPDATE documents SET sha256 = :t WHERE document_id = :d"),
                {"t": "e" * 64, "d": first["document_id"]},
            )
            await conn.execute(
                text("UPDATE documents SET sha256 = :x WHERE document_id = :d"),
                {"x": sha_x, "d": second["document_id"]},
            )
            await conn.execute(
                text("UPDATE documents SET sha256 = :y WHERE document_id = :d"),
                {"y": sha_y, "d": first["document_id"]},
            )

        response = await client.post(
            f"/api/documents/{first['document_id']}/versions/{first['version_id']}/restore",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 409
        assert response.json()["error"] == "ING_DUPLICATE"

    async def test_restore_without_conflict_returns_202(self, api):
        client, _, token = api
        first = (
            await client.post("/api/documents", content=CONTENT_X, headers=_upload_headers(token))
        ).json()
        response = await client.post(
            f"/api/documents/{first['document_id']}/versions/{first['version_id']}/restore",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 202
        body = response.json()
        assert body["restored_version_id"]
        assert body["status"] == "queued"
