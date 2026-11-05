"""Cliente HTTP de la UI (TASK-08): puro, testeable sin browser.

- Envía `Authorization: Bearer <token>` en cada llamada; el token opaco
  lo gestiona el llamador (en la UI: `st.session_state`).
- No duplica lógica de dominio: devuelve los payloads reales de la API y
  propaga los códigos de error estables (ADR-0005) como excepciones.
- Sin reintentos silenciosos ni estados inventados: un fallo se propaga.
"""

from typing import Any

import httpx

from ada_study.contracts.documents import DocumentRecord, IngestionJob
from ada_study.contracts.identity import AuditEvent, UserRecord
from ada_study.contracts.study import StudyResponse


class ApiError(Exception):
    """Error de la API con código estable (ADR-0005)."""

    def __init__(self, error_code: str, detail: str, status_code: int) -> None:
        self.error_code = error_code
        self.status_code = status_code
        super().__init__(detail)


class ApiClient:
    """Cliente HTTP de la API con autenticación Bearer por llamada.

    Puro (sin Streamlit): las pruebas lo ejercitan contra la app FastAPI
    con repositorio fake vía `httpx.ASGITransport`.
    """

    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        *,
        http: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        # Cliente http inyectable: en producción None (se crea uno por
        # llamada, red real); en pruebas un TestClient de FastAPI (que ES
        # un httpx.Client síncrono contra la app ASGI, sin red).
        self._http = http

    def with_token(self, token: str) -> "ApiClient":
        """Devuelve un cliente con el token indicado (inmutable)."""
        return ApiClient(self._base_url, token, http=self._http)

    @property
    def base_url(self) -> str:
        return self._base_url

    # -- infraestructura ---------------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        if not self._token:
            raise ApiError("AUTH_SESSION_INVALID", "No hay sesión activa", 401)
        return {"Authorization": f"Bearer {self._token}"}

    def _call(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        params: dict | None = None,
        content: bytes | None = None,
        extra_headers: dict | None = None,
        auth: bool = True,
    ) -> httpx.Response:
        headers = dict(extra_headers) if extra_headers else {}
        if auth:
            if not self._token:
                raise ApiError("AUTH_SESSION_INVALID", "No hay sesión activa", 401)
            headers["Authorization"] = f"Bearer {self._token}"
        # Cliente http: inyectado (pruebas, TestClient de FastAPI) o creado
        # por llamada (producción, red real).
        if self._http is not None:
            return self._http.request(
                method,
                f"{self._base_url}{path}",
                json=json_body,
                content=content,
                params=params,
                headers=headers,
            )
        with httpx.Client() as http:
            return http.request(
                method,
                f"{self._base_url}{path}",
                json=json_body,
                content=content,
                params=params,
                headers=headers,
            )

    def _parse_json(self, response: httpx.Response) -> dict:
        if response.status_code >= 400:
            try:
                payload = response.json()
            except Exception:
                payload = {}
            raise ApiError(
                payload.get("error", "HTTP_ERROR"),
                payload.get("detail", ""),
                response.status_code,
            )
        return response.json()

    def _parse_json_list(
        self, response: httpx.Response, *, key: str, endpoint: str
    ) -> list:
        """Valida el envelope de un listado y devuelve la lista (UI-ROBUSTNESS-01).

        El contrato exige un diccionario con la clave esperada y su valor
        de tipo lista. Un envelope inesperado (payload no dict, clave
        ausente, clave alternativa o valor que no es lista) se convierte
        en `ApiError` con código estable `INVALID_RESPONSE_ENVELOPE` en
        vez de propagar un `KeyError` crudo a Streamlit. Sin fallbacks
        silenciosos: un cambio de contrato se hace visible.
        """
        payload = self._parse_json(response)
        if (
            not isinstance(payload, dict)
            or key not in payload
            or not isinstance(payload[key], list)
        ):
            raise ApiError(
                "INVALID_RESPONSE_ENVELOPE",
                f"Respuesta con formato inesperado de la API ({endpoint}).",
                response.status_code,
            )
        return payload[key]

    # -- identidad (B1/B3) --------------------------------------------------------

    def login(self, email: str, password: str) -> str:
        """POST /api/auth/login → token opaco de sesión.

        Propaga AUTH_INVALID_CREDENTIALS / AUTH_ACCOUNT_NOT_ACTIVE sin
        distinguir si el correo existe (el backend garantiza el genérico).
        """
        response = self._request_auth(
            "POST",
            "/api/auth/login",
            json_body={"email": email, "password": password},
        )
        payload = self._parse_json(response)
        return payload["token"]

    def logout(self) -> None:
        """POST /api/auth/logout: revoca la sesión del token actual."""
        self._request_auth_expect_empty("POST", "/api/auth/logout")

    def me(self) -> UserRecord:
        """GET /api/me: perfil del usuario autenticado (B3)."""
        response = self._call("GET", "/api/me")
        return UserRecord.model_validate(self._parse_json(response))

    # -- documentos ----------------------------------------------------------------

    def list_documents(self) -> list[DocumentRecord]:
        """GET /api/documents: biblioteca visible según rol (Spec 06 §106)."""
        items = self._parse_json_list(
            self._call("GET", "/api/documents"), key="documents", endpoint="/api/documents"
        )
        return [DocumentRecord.model_validate(item) for item in items]

    def get_document(self, document_id: str) -> DocumentRecord:
        """GET /api/documents/{id}: detalle con autorización por ID."""
        payload = self._parse_json(self._call("GET", f"/api/documents/{document_id}"))
        return DocumentRecord.model_validate(payload)

    def get_ingestion_job(self, job_id: str) -> IngestionJob:
        """GET /api/ingestion-jobs/{job_id}: polling del estado de ingesta."""
        payload = self._parse_json(self._call("GET", f"/api/ingestion-jobs/{job_id}"))
        return IngestionJob.model_validate(payload)

    # -- estudio (TASK-12) ---------------------------------------------------------

    def study(self, user_request: str, conversation_id: str | None = None) -> StudyResponse:
        """POST /api/study: respuesta de estudio con evidencia verificada.

        Envía SOLO `user_request` + `conversation_id` (CHECKPOINTER-03): la
        identidad/permisos los resuelve el backend con el token de sesión; la
        UI nunca envía roles, usuarios, thread_id ni claves de proveedores.
        Con `conversation_id` se continúa la conversación (mismo identificador
        en cada petición posterior); None inicia una conversación nueva (el
        UUID lo genera el servidor y llega en la respuesta).
        Valida la salida con el contrato real `StudyResponse`.
        """
        body: dict[str, str] = {"user_request": user_request}
        if conversation_id:
            body["conversation_id"] = conversation_id
        payload = self._request_json("POST", "/api/study", json_body=body)
        return StudyResponse.model_validate(payload)

    def upload_document(
        self,
        content: bytes,
        *,
        filename: str,
        content_type: str,
        subject: str | None = None,
    ) -> dict:
        """POST /api/documents: 202 + job para polling (sin ingesta en la UI)."""
        response = self._call(
            "POST",
            "/api/documents",
            content=content,
            extra_headers={
                "Content-Type": content_type,
                "X-Filename": filename,
                **({"X-Subject": subject} if subject else {}),
            },
        )
        return self._parse_json(response)

    def delete_document(self, document_id: str) -> dict:
        """DELETE /api/documents/{id}: 202 + cortina + purga eventual (ADR-0004)."""
        return self._parse_json(self._call("DELETE", f"/api/documents/{document_id}"))

    def restore_version(self, document_id: str, version_id: str) -> dict:
        """POST .../restore: versión nueva basada en una histórica (202)."""
        return self._parse_json(
            self._call("POST", f"/api/documents/{document_id}/versions/{version_id}/restore")
        )

    def retry_version(self, document_id: str, version_id: str) -> dict:
        """POST .../retry: reintento idempotente (solo versiones failed)."""
        return self._parse_json(
            self._call("POST", f"/api/documents/{document_id}/versions/{version_id}/retry")
        )

    def submit_for_review(self, document_id: str) -> dict:
        """POST /api/documents/{id}/submit: envío del autor a revisión.

        PUBLISH-FLOW-02: draft -> pending_review. Solo el autor puede (403
        si no); 404 si no existe/no es visible; 409 si no está en draft o la
        versión activa no está `available`. No publica directamente (ADR-0005).
        """
        return self._parse_json(
            self._call("POST", f"/api/documents/{document_id}/submit")
        )

    # -- moderación y auditoría (rol) -----------------------------------------------

    def moderation_queue(self) -> list[DocumentRecord]:
        """GET /api/moderation/queue: pendientes de decisión (moderator/admin)."""
        items = self._parse_json_list(
            self._call("GET", "/api/moderation/queue"),
            key="queue",
            endpoint="/api/moderation/queue",
        )
        return [DocumentRecord.model_validate(item) for item in items]

    def decide(self, document_id: str, action: str, reason: str | None = None) -> dict:
        """POST /api/moderation/documents/{id}/decide.

        Devuelve el payload reducido de la API (document_id,
        editorial_status, action); no reconstruye el DocumentRecord.
        """
        body: dict[str, Any] = {"action": action}
        if reason:
            body["reason"] = reason
        return self._parse_json(
            self._call("POST", f"/api/moderation/documents/{document_id}/decide", json_body=body)
        )

    def audit_events(
        self,
        *,
        entity_type: str | None = None,
        entity_id: str | None = None,
        limit: int = 100,
    ) -> list[AuditEvent]:
        """GET /api/audit/events: eventos append-only (moderator/admin)."""
        params: dict[str, str | int] = {"limit": limit}
        if entity_type and entity_id:
            params["entity_type"] = entity_type
            params["entity_id"] = entity_id
        items = self._parse_json_list(
            self._call("GET", "/api/audit/events", params=params),
            key="events",
            endpoint="/api/audit/events",
        )
        return [AuditEvent.model_validate(item) for item in items]

    # -- helpers privados -------------------------------------------------------------

    def _request_auth(
        self, method: str, path: str, *, json_body: dict | None = None
    ) -> httpx.Response:
        """Llamada de autenticación SIN token previo (login)."""
        return self._call(method, path, json_body=json_body, auth=False)

    def _request_auth_expect_empty(self, method: str = "POST", path: str = "") -> None:
        response = self._call(method, path)
        if response.status_code not in (200, 204):
            self._parse_json(response)  # propaga el ApiError con el código estable

    def _get(self, path: str, params: dict | None = None) -> dict:
        return self._parse_json(self._call("GET", path, params=params))

    def _request_json(self, method: str, path: str, json_body: dict | None = None) -> dict:
        return self._parse_json(self._call(method, path, json_body=json_body))
