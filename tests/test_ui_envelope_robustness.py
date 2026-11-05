"""Robustez de la UI ante envelopes inesperados (UI-ROBUSTNESS-01).

El incidente `KeyError: 'documents'` en Moderación (proceso Streamlit con
un cliente desactualizado) motivó validar explícitamente los envelopes de
moderación, biblioteca y auditoría: un contrato roto debe producir
`ApiError` con código estable (`INVALID_RESPONSE_ENVELOPE`) y las páginas
deben mostrar mensajes controlados, nunca tracebacks crudos.

Evidencia unitaria con transporte httpx falso: sin red, sin PostgreSQL,
sin Streamlit runtime (mismo patrón de funciones puras que `study.py`).
"""

import httpx
import pytest

from ada_study.contracts.documents import DocumentRecord
from ada_study.ui.api_client import ApiClient, ApiError
from ada_study.ui.pages.audit import describe_audit_error
from ada_study.ui.pages.library import describe_library_error
from ada_study.ui.pages.moderation import describe_queue_error

# ---------------------------------------------------------------------------
# Transporte falso: responde siempre el payload indicado (sin red)
# ---------------------------------------------------------------------------


class _StaticTransport(httpx.BaseTransport):
    """Responde siempre el payload y status configurados."""

    def __init__(self, payload: object, status_code: int = 200) -> None:
        self._payload = payload
        self._status_code = status_code

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if self._payload is None:  # cuerpo no-JSON (caso de error sin payload)
            return httpx.Response(self._status_code, content=b"internal error")
        return httpx.Response(self._status_code, json=self._payload)


def _client(payload: object, status_code: int = 200) -> ApiClient:
    transport = _StaticTransport(payload, status_code)
    return ApiClient("http://testserver", "tok", http=httpx.Client(transport=transport))


def _document_payload(document_id: str = "doc_1") -> dict:
    """DocumentRecord válido en formato JSON (contrato real)."""
    now = "2026-11-02T12:00:00+00:00"
    return DocumentRecord(
        document_id=document_id,
        filename=f"{document_id}.pdf",
        subject=None,
        active_version_id=None,
        editorial_status="pending_review",
        review_status="not_required",
        uploaded_by="usr_1",
        created_at=now,
        updated_at=now,
        deleted_at=None,
    ).model_dump(mode="json")


def _event_payload() -> dict:
    """AuditEvent válido según contracts/identity (Spec 06 §81-86)."""
    return {
        "event_id": "evt_1",
        "actor_id": "usr_1",
        "action": "document.registered",
        "entity_type": "document",
        "entity_id": "doc_1",
        "result": "ok",
        "created_at": "2026-11-02T12:00:00+00:00",
    }


# ---------------------------------------------------------------------------
# Cliente API: validación de envelopes (casos 1-9)
# ---------------------------------------------------------------------------


class TestApiClientEnvelopes:
    # -- moderación ----------------------------------------------------------

    def test_moderation_queue_valid_envelope(self):
        """Caso 1: `{"queue": [...]}` se parsea al contrato real."""
        client = _client({"queue": [_document_payload()]})
        queue = client.moderation_queue()
        assert [d.document_id for d in queue] == ["doc_1"]
        assert all(isinstance(d, DocumentRecord) for d in queue)

    def test_moderation_queue_rejects_alternative_envelope(self):
        """Caso 2: envelope alternativo (`documents`) → ApiError, no KeyError."""
        client = _client({"documents": [_document_payload()]})
        with pytest.raises(ApiError) as excinfo:
            client.moderation_queue()
        assert excinfo.value.error_code == "INVALID_RESPONSE_ENVELOPE"
        # El mensaje identifica el endpoint, sin secretos ni detalle interno.
        assert "/api/moderation/queue" in str(excinfo.value)

    def test_moderation_queue_rejects_missing_key(self):
        """Caso 3: clave ausente → ApiError (sin fallback silencioso)."""
        client = _client({"data": []})
        with pytest.raises(ApiError) as excinfo:
            client.moderation_queue()
        assert excinfo.value.error_code == "INVALID_RESPONSE_ENVELOPE"

    def test_moderation_queue_rejects_non_dict_payload(self):
        """Caso 4: payload que no es diccionario → ApiError."""
        client = _client([_document_payload()])
        with pytest.raises(ApiError) as excinfo:
            client.moderation_queue()
        assert excinfo.value.error_code == "INVALID_RESPONSE_ENVELOPE"

    def test_moderation_queue_rejects_non_list_value(self):
        """Extra: clave presente pero valor no lista → ApiError."""
        client = _client({"queue": {"0": _document_payload()}})
        with pytest.raises(ApiError) as excinfo:
            client.moderation_queue()
        assert excinfo.value.error_code == "INVALID_RESPONSE_ENVELOPE"

    # -- biblioteca -----------------------------------------------------------

    def test_list_documents_valid_envelope(self):
        """Caso 5: `{"documents": [...]}` se parsea al contrato real."""
        client = _client({"documents": [_document_payload("doc_pub")]})
        documents = client.list_documents()
        assert [d.document_id for d in documents] == ["doc_pub"]

    def test_list_documents_rejects_alternative_envelope(self):
        """Caso 6: envelope incorrecto → ApiError (no lista vacía silenciosa)."""
        client = _client({"queue": [_document_payload()]})
        with pytest.raises(ApiError) as excinfo:
            client.list_documents()
        assert excinfo.value.error_code == "INVALID_RESPONSE_ENVELOPE"
        assert "/api/documents" in str(excinfo.value)

    # -- auditoría --------------------------------------------------------------

    def test_audit_events_valid_envelope(self):
        """Caso 7: `{"events": [...]}` se parsea al contrato real."""
        client = _client({"events": [_event_payload()]})
        events = client.audit_events()
        assert [e.event_id for e in events] == ["evt_1"]

    def test_audit_events_rejects_alternative_envelope(self):
        """Caso 8: envelope incorrecto → ApiError."""
        client = _client({"documents": []})
        with pytest.raises(ApiError) as excinfo:
            client.audit_events()
        assert excinfo.value.error_code == "INVALID_RESPONSE_ENVELOPE"
        assert "/api/audit/events" in str(excinfo.value)

    # -- errores HTTP intactos ---------------------------------------------------

    def test_http_errors_keep_stable_codes(self):
        """Caso 9: errores HTTP siguen produciendo los ApiError actuales."""
        client = _client({"error": "AUTH_FORBIDDEN", "detail": ""}, status_code=403)
        with pytest.raises(ApiError) as excinfo:
            client.moderation_queue()
        assert excinfo.value.error_code == "AUTH_FORBIDDEN"
        assert excinfo.value.status_code == 403

    def test_http_error_without_valid_json_keeps_http_error_code(self):
        """Error HTTP sin JSON válido → código HTTP_ERROR (comportamiento actual)."""
        client = _client(None, status_code=500)
        with pytest.raises(ApiError) as excinfo:
            client.list_documents()
        assert excinfo.value.error_code == "HTTP_ERROR"
        assert excinfo.value.status_code == 500


# ---------------------------------------------------------------------------
# Páginas UI: mensajes controlados (casos 10-12)
# ---------------------------------------------------------------------------


class TestUiPagesControlledErrors:
    """Los mensajes de las páginas son controlados y no exponen detalle."""

    def test_moderation_forbidden_message(self):
        message = describe_queue_error(ApiError("AUTH_FORBIDDEN", "", 403))
        assert "rol" in message.lower()

    def test_moderation_envelope_error_is_controlled(self):
        exc = ApiError("INVALID_RESPONSE_ENVELOPE", "/api/moderation/queue", 200)
        message = describe_queue_error(exc)
        assert "INVALID_RESPONSE_ENVELOPE" in message
        assert "cola" in message.lower()

    def test_moderation_message_does_not_expose_backend_detail(self):
        message = describe_queue_error(ApiError("HTTP_ERROR", "detalle-interno", 500))
        assert "detalle-interno" not in message
        assert "HTTP_ERROR" in message

    def test_library_error_message(self):
        exc = ApiError("INVALID_RESPONSE_ENVELOPE", "/api/documents", 200)
        message = describe_library_error(exc)
        assert "biblioteca" in message.lower()
        assert "INVALID_RESPONSE_ENVELOPE" in message

    def test_audit_forbidden_message(self):
        message = describe_audit_error(ApiError("AUTH_FORBIDDEN", "", 403))
        assert "rol" in message.lower()

    def test_audit_generic_message(self):
        message = describe_audit_error(ApiError("HTTP_ERROR", "detalle-interno", 500))
        assert "detalle-interno" not in message
        assert "eventos" in message.lower()

