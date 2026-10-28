"""Pruebas de la experiencia de estudio en la UI (TASK-12).

Cubren, con fakes locales (sin OpenAI, PostgreSQL ni Pinecone reales y sin
costos): el método `ApiClient.study` contra la app FastAPI real (ASGI, sin
red), los límites contractuales de entrada, los estados de `StudyResponse`,
los errores HTTP/sesión y las funciones puras de la página de estudio.

Evidencia unitaria simulada: NO valida integraciones reales con proveedores.
"""

import json
from datetime import UTC, datetime

import httpx
import pytest

from ada_study.llm import LLM_FALLBACK_LIMITATION
from ada_study.study import MAX_REQUEST_CHARS
from ada_study.ui.api_client import ApiClient, ApiError
from ada_study.ui.pages.study import describe_error, validation_error

# Fakes y helpers reutilizados (patrón del proyecto: tests/test_api_auth).
from tests.test_api_auth import _session_token, _user  # noqa: F401
from tests.test_study_endpoint import FakeStudyRepository  # noqa: F401
from tests.test_study_llm import FakeChatModel, _seeded_repo

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _user_seeded(repo):
    """Usuario student registrado en el fake (para token de sesión)."""
    user = _user("usr_stu", "student")
    repo.users[user.user_id] = user
    repo.users_by_email[user.email] = user
    return user


def _api_with_repo(repo, chat_model=None):
    """ApiClient autenticado sobre TestClient ASGI (sin red, con lifespan)."""
    from contextlib import contextmanager

    @contextmanager
    def _client():
        from tests.test_study_llm import _client_with_model

        http = _client_with_model(repo, chat_model)
        with http:
            user = repo.users["usr_stu"]
            token = _session_token(repo, user)
            yield ApiClient("http://testserver", token, http=http)

    return _client()


def _payload_complete() -> dict:
    """Payload válido de StudyResponse para el transporte falso."""
    return {
        "status": "complete",
        "topic": "fracciones",
        "explanation": "Una fracción es una parte de un todo.",
        "references": [
            {
                "document_id": "doc_pub",
                "version_id": "ver_doc_pub",
                "chunk_id": "chk_doc_pub_0",
                "title": "Apuntes de Álgebra",
                "page": 1,
                "excerpt": "Una fracción representa partes iguales de un todo.",
                "content_hash": "abc123",
                "retrieval_method": ["bm25"],
            }
        ],
        "uses_web_sources": False,
        "evidence_gaps": [],
        "limitations": [],
    }


class _RecordingTransport(httpx.BaseTransport):
    """Transporte falso que registra el cuerpo enviado y responde 200."""

    def __init__(self, response_payload: dict) -> None:
        self.captured: dict = {}
        self._payload = response_payload

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.captured["json"] = json.loads(request.content.decode("utf-8"))
        self.captured["headers"] = dict(request.headers)
        return httpx.Response(200, json=self._payload)


# ---------------------------------------------------------------------------
# Cliente HTTP: ApiClient.study contra la app real (sin red)
# ---------------------------------------------------------------------------


class TestApiClientStudy:
    def test_study_with_valid_session_returns_contract(self):
        """Pregunta con sesión autenticada → StudyResponse del contrato."""
        repo = _seeded_repo()
        _user_seeded(repo)
        with _api_with_repo(repo) as client:
            response = client.study("¿Qué es una fracción?")
        assert response.status == "complete"
        assert response.explanation.startswith("Consulta:")
        assert [ref.document_id for ref in response.references] == ["doc_pub"]
        assert response.limitations == []

    def test_study_without_session_raises_stable_error(self):
        """Sin token → AUTH_SESSION_INVALID (401) sin llamar a la red."""
        client = ApiClient("http://testserver")
        with pytest.raises(ApiError) as excinfo:
            client.study("¿Qué es una fracción?")
        assert excinfo.value.error_code == "AUTH_SESSION_INVALID"
        assert excinfo.value.status_code == 401

    def test_study_with_invalid_token_raises_auth_session_invalid(self):
        """Token inexistente en el backend → 401 AUTH_SESSION_INVALID."""
        repo = _seeded_repo()
        _user_seeded(repo)
        with _api_with_repo(repo) as client:
            client._token = "token-inexistente"  # noqa: SLF001 - solo prueba
            with pytest.raises(ApiError) as excinfo:
                client.study("¿Qué es una fracción?")
        assert excinfo.value.status_code == 401
        assert excinfo.value.error_code == "AUTH_SESSION_INVALID"

    def test_study_empty_request_rejected_with_stable_code(self):
        """Solo espacios → 422 STUDY_EMPTY_REQUEST (validación del runtime)."""
        repo = _seeded_repo()
        _user_seeded(repo)
        with _api_with_repo(repo) as client:
            with pytest.raises(ApiError) as excinfo:
                client.study("   ")
        assert excinfo.value.status_code == 422
        assert excinfo.value.error_code == "STUDY_EMPTY_REQUEST"

    def test_study_oversized_request_rejected(self):
        """> 2000 caracteres → 422 (Pydantic rechaza en la frontera)."""
        repo = _seeded_repo()
        _user_seeded(repo)
        with _api_with_repo(repo) as client:
            with pytest.raises(ApiError) as excinfo:
                client.study("x" * 2001)
        assert excinfo.value.status_code == 422

    def test_study_sends_only_user_request_and_bearer_token(self):
        """El cliente envía solo `user_request` + Bearer; sin secretos."""
        transport = _RecordingTransport(_payload_complete())
        client = ApiClient(
            "http://testserver", "tok", http=httpx.Client(transport=transport)
        )
        response = client.study("¿Qué es una fracción?")
        assert transport.captured["json"] == {"user_request": "¿Qué es una fracción?"}
        assert response.status == "complete"  # valida con el contrato real
        assert "Bearer " in transport.captured["headers"]["authorization"]
        # Sin credenciales de proveedores ni campos de configuración LLM.
        assert "api_key" not in transport.captured["json"]
        assert "OPENAI_API_KEY" not in json.dumps(transport.captured["headers"])

    def test_insufficient_evidence_without_references(self):
        """Biblioteca sin evidencia relevante → insuficiencia real, sin citas."""
        repo = FakeStudyRepository()  # sin documentos: cero evidencia
        _user_seeded(repo)
        with _api_with_repo(repo) as client:
            response = client.study("¿Qué es la fotosíntesis?")
        assert response.status in {"insufficient_evidence", "limited"}
        assert response.references == []
        assert response.evidence_gaps or response.limitations

    def test_fallback_limitation_comes_from_backend_not_ui(self):
        """Tutor con proveedor caído → limitación real del backend (TASK-11)."""
        repo = _seeded_repo()
        _user_seeded(repo)
        fake = FakeChatModel(error=RuntimeError("provider down"))
        with _api_with_repo(repo, chat_model=fake) as client:
            response = client.study("¿Qué es una fracción?")
        assert response.status == "complete"
        assert LLM_FALLBACK_LIMITATION in response.limitations


# ---------------------------------------------------------------------------
# Funciones puras de la página (sin Streamlit, sin dependencias nuevas)
# ---------------------------------------------------------------------------


class TestStudyPagePureHelpers:
    def test_empty_request_rejected_before_sending(self):
        assert validation_error("") is not None
        assert validation_error("   ") is not None
        assert validation_error(None) is not None

    def test_oversized_request_rejected_before_sending(self):
        assert validation_error("x" * 2001) is not None
        assert validation_error("x" * 2000) is None

    def test_valid_request_passes_client_side_validation(self):
        assert validation_error("¿Qué es una fracción?") is None

    def test_ui_limit_matches_backend_contract(self):
        from ada_study.ui.pages.study import MAX_REQUEST_CHARS as UI_LIMIT

        assert UI_LIMIT == MAX_REQUEST_CHARS == 2000

    def test_session_expired_message(self):
        message = describe_error(ApiError("AUTH_SESSION_EXPIRED", "", 401))
        assert "sesión" in message.lower()

    def test_service_unavailable_message(self):
        message = describe_error(ApiError("STUDY_INTERNAL", "", 503))
        assert "no está disponible" in message

    def test_generic_error_does_not_expose_backend_detail(self):
        message = describe_error(ApiError("STUDY_INTERNAL", "detalle-interno", 503))
        assert "detalle-interno" not in message
        assert "STUDY_INTERNAL" in message

    def test_validation_error_mentions_limit(self):
        message = describe_error(ApiError("STUDY_EMPTY_REQUEST", "", 422))
        assert "2000" in message

    def test_now_is_utc(self):
        # Sanity del entorno de pruebas (imports compartidos de fakes).
        assert datetime.now(UTC).tzinfo is not None


