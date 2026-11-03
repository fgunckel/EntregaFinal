"""Pruebas del envío a revisión (PUBLISH-FLOW-02).

Cubren:
- POST /api/documents/{id}/submit: draft -> pending_review (ruta de AUTORÍA).
- Autorización: solo el propietario, con identidad de la sesión autenticada;
  moderator/admin NO pueden enviar documentos de terceros (403).
- 404 (inexistente / no visible / cortina de borrado) y 409 (no-draft,
  versión no `available`); sin publicación directa (ADR-0005 intacto).
- Auditoría append-only: exactamente UN evento con actor, documento,
  versión, estado anterior y nuevo, fecha y acción.
- Moderación: el documento enviado aparece en la cola y el `approve`
  existente lo publica (sin excepciones de permisos nuevas).
- Cliente UI: ApiClient.submit_for_review contra la app ASGI (sin red).
- Visibilidad del botón y errores controlados (funciones puras de la UI).

Usan la app FastAPI con repositorio fake en memoria; CatalogService y
ModerationService son los REALES. Sin LLM ni PostgreSQL real.
"""

from datetime import UTC, datetime

from ada_study.contracts.documents import DocumentRecord, DocumentVersion
from ada_study.ui.api_client import ApiClient, ApiError
from ada_study.ui.pages.library import can_submit_for_review, describe_submit_error

# Reutiliza el fake de catálogo + auditoría + identidad del test de moderación
# (misma técnica que test_ui_api_client reutiliza el fake de test_api_auth).
from tests.test_api_moderation_audit import (
    FakeModerationRepository,
    _auth,
    _client,
    _document,
    _session_token,
    _user,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _version(
    version_id: str, document_id: str, status: str = "available"
) -> DocumentVersion:
    now = datetime.now(UTC).isoformat()
    return DocumentVersion(
        version_id=version_id,
        document_id=document_id,
        version_number=1,
        sha256="a" * 64,
        storage_key=f"{document_id}/{version_id}.pdf",
        content_type="application/pdf",
        size_bytes=10,
        status=status,  # type: ignore[arg-type]
        created_at=now,
    )


def _setup_draft(
    repo: FakeModerationRepository,
    *,
    owner_id: str = "usr_author",
    role: str = "admin",
    status: str = "draft",
    version_status: str = "available",
) -> str:
    """Usuario + documento draft con versión activa; devuelve el token."""
    user = _user(owner_id, role)
    repo.users[user.user_id] = user
    document = _document("doc_1", user.user_id, status=status)
    document.active_version_id = "ver_1"
    repo.documents["doc_1"] = document
    repo.versions["ver_1"] = _version("ver_1", "doc_1", status=version_status)
    return _session_token(repo, user)


# ---------------------------------------------------------------------------
# Backend: POST /api/documents/{id}/submit
# ---------------------------------------------------------------------------


class TestSubmitForReviewEndpoint:
    def test_owner_submits_own_draft(self):
        """Admin autorizado envía su propio draft a revisión (200)."""
        repo = FakeModerationRepository()
        token = _setup_draft(repo, owner_id="usr_author", role="admin")
        with _client(repo) as client:
            response = client.post("/api/documents/doc_1/submit", headers=_auth(token))
        assert response.status_code == 200
        body = response.json()
        assert body["document_id"] == "doc_1"
        assert body["active_version_id"] == "ver_1"
        assert body["previous_status"] == "draft"
        assert body["editorial_status"] == "pending_review"
        assert repo.documents["doc_1"].editorial_status == "pending_review"

    def test_owner_trusted_contributor_can_submit(self):
        """Cualquier rol con autoría puede enviar su PROPIO draft."""
        repo = FakeModerationRepository()
        token = _setup_draft(repo, owner_id="usr_author", role="trusted_contributor")
        with _client(repo) as client:
            response = client.post("/api/documents/doc_1/submit", headers=_auth(token))
        assert response.status_code == 200
        assert response.json()["editorial_status"] == "pending_review"

    def test_moderator_cannot_submit_third_party_403(self):
        """No se permiten envíos de terceros por la ruta de autoría (403)."""
        repo = FakeModerationRepository()
        _setup_draft(repo, owner_id="usr_author", role="admin")
        moderator = _user("usr_mod", "moderator")
        repo.users[moderator.user_id] = moderator
        mod_token = _session_token(repo, moderator)
        with _client(repo) as client:
            response = client.post(
                "/api/documents/doc_1/submit", headers=_auth(mod_token)
            )
        assert response.status_code == 403
        assert response.json()["error"] == "DEL_NOT_ALLOWED"
        # El documento no cambia de estado.
        assert repo.documents["doc_1"].editorial_status == "draft"

    def test_admin_cannot_submit_third_party_403(self):
        """Admin tampoco puede enviar un draft ajeno (reglas de autoría)."""
        repo = FakeModerationRepository()
        _setup_draft(repo, owner_id="usr_author", role="student")
        admin = _user("usr_admin", "admin")
        repo.users[admin.user_id] = admin
        admin_token = _session_token(repo, admin)
        with _client(repo) as client:
            response = client.post(
                "/api/documents/doc_1/submit", headers=_auth(admin_token)
            )
        assert response.status_code == 403
        assert response.json()["error"] == "DEL_NOT_ALLOWED"

    def test_missing_document_404(self):
        repo = FakeModerationRepository()
        token = _setup_draft(repo, owner_id="usr_author", role="admin")
        with _client(repo) as client:
            response = client.post(
                "/api/documents/doc_missing/submit", headers=_auth(token)
            )
        assert response.status_code == 404
        assert response.json()["error"] == "DEL_NOT_FOUND"

    def test_not_visible_document_404_without_existence_leak(self):
        """Aislamiento: draft ajeno para un estudiante → 404 (no 403)."""
        repo = FakeModerationRepository()
        _setup_draft(repo, owner_id="usr_author", role="admin")
        stranger = _user("usr_stranger", "student")
        repo.users[stranger.user_id] = stranger
        stranger_token = _session_token(repo, stranger)
        with _client(repo) as client:
            response = client.post(
                "/api/documents/doc_1/submit", headers=_auth(stranger_token)
            )
        assert response.status_code == 404
        assert response.json()["error"] == "DEL_NOT_FOUND"

    def test_document_not_in_draft_409(self):
        """El endpoint no transiciona desde otro estado (p. ej. published)."""
        repo = FakeModerationRepository()
        token = _setup_draft(repo, owner_id="usr_author", role="admin")
        repo.documents["doc_1"].editorial_status = "published"
        with _client(repo) as client:
            response = client.post("/api/documents/doc_1/submit", headers=_auth(token))
        assert response.status_code == 409
        assert response.json()["error"] == "MOD_INVALID_TRANSITION"
        assert repo.documents["doc_1"].editorial_status == "published"

    def test_withdrawn_document_409(self):
        """withdrawn -> published es válida en ADR-0005, pero NO por aquí."""
        repo = FakeModerationRepository()
        token = _setup_draft(repo, owner_id="usr_author", role="admin")
        repo.documents["doc_1"].editorial_status = "withdrawn"
        with _client(repo) as client:
            response = client.post("/api/documents/doc_1/submit", headers=_auth(token))
        assert response.status_code == 409
        assert repo.documents["doc_1"].editorial_status == "withdrawn"

    def test_version_not_available_409(self):
        repo = FakeModerationRepository()
        token = _setup_draft(
            repo, owner_id="usr_author", role="admin", version_status="indexing"
        )
        with _client(repo) as client:
            response = client.post("/api/documents/doc_1/submit", headers=_auth(token))
        assert response.status_code == 409
        assert response.json()["error"] == "DEL_CONFLICT"
        assert repo.documents["doc_1"].editorial_status == "draft"

    def test_missing_active_version_409(self):
        repo = FakeModerationRepository()
        token = _setup_draft(repo, owner_id="usr_author", role="admin")
        repo.documents["doc_1"].active_version_id = None
        with _client(repo) as client:
            response = client.post("/api/documents/doc_1/submit", headers=_auth(token))
        assert response.status_code == 409

    def test_deleted_document_404(self):
        """Cortina editorial: un borrado no es operable ni para su autor."""
        repo = FakeModerationRepository()
        token = _setup_draft(repo, owner_id="usr_author", role="admin")
        repo.documents["doc_1"].editorial_status = "deleted"
        repo.documents["doc_1"].deleted_at = datetime.now(UTC).isoformat()
        with _client(repo) as client:
            response = client.post("/api/documents/doc_1/submit", headers=_auth(token))
        assert response.status_code == 404
        assert response.json()["error"] == "DEL_NOT_FOUND"

    def test_requires_authentication_401(self):
        repo = FakeModerationRepository()
        _setup_draft(repo, owner_id="usr_author", role="admin")
        with _client(repo) as client:
            response = client.post("/api/documents/doc_1/submit")
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# Auditoría
# ---------------------------------------------------------------------------


class TestSubmitAudit:
    def test_submit_records_exactly_one_audit_event(self):
        repo = FakeModerationRepository()
        token = _setup_draft(repo, owner_id="usr_author", role="admin")
        with _client(repo) as client:
            client.post("/api/documents/doc_1/submit", headers=_auth(token))
        events = [
            e for e in repo.audit_events if e.action == "document.submit_for_review"
        ]
        assert len(events) == 1
        event = events[0]
        assert event.actor_id == "usr_author"
        assert event.entity_type == "document"
        assert event.entity_id == "doc_1"
        assert event.version_id == "ver_1"
        assert event.result == "ok"
        assert event.reason == "editorial: draft -> pending_review"
        assert event.created_at is not None

    def test_failed_submit_records_no_audit_event(self):
        repo = FakeModerationRepository()
        token = _setup_draft(repo, owner_id="usr_author", role="admin")
        repo.documents["doc_1"].editorial_status = "published"
        with _client(repo) as client:
            client.post("/api/documents/doc_1/submit", headers=_auth(token))
        # Un envío rechazado no deja rastro en la auditoría.
        assert repo.audit_events == []


# ---------------------------------------------------------------------------
# Cliente UI (ApiClient puro contra la app ASGI)
# ---------------------------------------------------------------------------


class TestUiClientSubmit:
    def test_submit_for_review_calls_endpoint(self):
        repo = FakeModerationRepository()
        token = _setup_draft(repo, owner_id="usr_author", role="admin")
        with _client(repo) as http:
            client = ApiClient("http://testserver", token, http=http)
            payload = client.submit_for_review("doc_1")
            assert payload["editorial_status"] == "pending_review"
            assert repo.documents["doc_1"].editorial_status == "pending_review"

    def test_submit_for_review_propagates_409(self):
        repo = FakeModerationRepository()
        token = _setup_draft(repo, owner_id="usr_author", role="admin")
        repo.documents["doc_1"].editorial_status = "pending_review"
        with _client(repo) as http:
            client = ApiClient("http://testserver", token, http=http)
            try:
                client.submit_for_review("doc_1")
            except ApiError as exc:
                assert exc.status_code == 409
                assert exc.error_code == "MOD_INVALID_TRANSITION"
            else:
                raise AssertionError("Se esperaba ApiError (409)")

    def test_submit_for_review_propagates_403(self):
        repo = FakeModerationRepository()
        _setup_draft(repo, owner_id="usr_author", role="admin")
        moderator = _user("usr_mod", "moderator")
        repo.users[moderator.user_id] = moderator
        mod_token = _session_token(repo, moderator)
        with _client(repo) as http:
            client = ApiClient("http://testserver", mod_token, http=http)
            try:
                client.submit_for_review("doc_1")
            except ApiError as exc:
                assert exc.status_code == 403
                assert exc.error_code == "DEL_NOT_ALLOWED"
            else:
                raise AssertionError("Se esperaba ApiError (403)")


# ---------------------------------------------------------------------------
# UI pura: visibilidad del botón y mensajes de error
# ---------------------------------------------------------------------------


class TestSubmitButtonVisibility:
    def _record(self, status: str, owner: str | None = "usr_author") -> DocumentRecord:
        now = datetime.now(UTC).isoformat()
        return DocumentRecord(
            document_id="doc_1",
            filename="doc_1.pdf",
            active_version_id="ver_1",
            editorial_status=status,  # type: ignore[arg-type]
            uploaded_by=owner,
            created_at=now,
            updated_at=now,
        )

    def test_draft_own_document_shows_button(self):
        assert can_submit_for_review(self._record("draft"), "usr_author") is True

    def test_draft_third_party_hides_button(self):
        """Moderator/admin viendo un borrador ajeno NO obtiene la acción."""
        assert can_submit_for_review(self._record("draft"), "usr_other") is False

    def test_non_draft_hides_button(self):
        for status in ("pending_review", "published", "rejected", "withdrawn"):
            assert can_submit_for_review(self._record(status), "usr_author") is False

    def test_unknown_viewer_hides_button(self):
        assert can_submit_for_review(self._record("draft"), None) is False


class TestSubmitErrorMessage:
    def _error(self, code: str, status: int) -> ApiError:
        return ApiError(code, "detalle", status)

    def test_403_message(self):
        assert "403" in describe_submit_error(self._error("DEL_NOT_ALLOWED", 403))

    def test_404_message(self):
        assert "404" in describe_submit_error(self._error("DEL_NOT_FOUND", 404))

    def test_409_message(self):
        assert "409" in describe_submit_error(
            self._error("MOD_INVALID_TRANSITION", 409)
        )

    def test_other_error_message(self):
        assert "ING_INTERNAL" in describe_submit_error(
            self._error("ING_INTERNAL", 500)
        )


# ---------------------------------------------------------------------------
# Sin publicación directa + flujo con Moderación existente
# ---------------------------------------------------------------------------


class TestNoDirectPublishAndModerationFlow:
    def test_approve_directly_on_draft_409(self):
        """La moderación NO permite draft -> published (ADR-0005 intacto)."""
        repo = FakeModerationRepository()
        token = _setup_draft(repo, owner_id="usr_author", role="admin")
        with _client(repo) as client:
            response = client.post(
                "/api/moderation/documents/doc_1/decide",
                headers=_auth(token),
                json={"action": "approve"},
            )
        assert response.status_code == 409
        assert response.json()["error"] == "MOD_INVALID_TRANSITION"
        assert repo.documents["doc_1"].editorial_status == "draft"

    def test_submitted_document_appears_in_moderation_queue(self):
        repo = FakeModerationRepository()
        token = _setup_draft(repo, owner_id="usr_author", role="admin")
        with _client(repo) as client:
            client.post("/api/documents/doc_1/submit", headers=_auth(token))
            queue = client.get("/api/moderation/queue", headers=_auth(token))
        assert queue.status_code == 200
        ids = [d["document_id"] for d in queue.json()["queue"]]
        assert "doc_1" in ids

    def test_full_flow_submit_then_existing_approve_publishes(self):
        """submit → decide approve: publicación SOLO vía moderación existente.

        El MISMO admin que envió puede aprobarlo: la política actual no lo
        impide (no se introduce ninguna excepción de permisos).
        """
        repo = FakeModerationRepository()
        token = _setup_draft(repo, owner_id="usr_author", role="admin")
        with _client(repo) as client:
            submit = client.post("/api/documents/doc_1/submit", headers=_auth(token))
            assert submit.status_code == 200
            decide = client.post(
                "/api/moderation/documents/doc_1/decide",
                headers=_auth(token),
                json={"action": "approve"},
            )
        assert decide.status_code == 200
        assert decide.json()["editorial_status"] == "published"
        assert repo.documents["doc_1"].editorial_status == "published"
        actions = [e.action for e in repo.audit_events]
        assert actions == ["document.submit_for_review", "moderation.approve"]
