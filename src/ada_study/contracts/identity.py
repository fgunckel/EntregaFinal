"""Contratos de identidad, sesiones y auditoría (TASK-04, Spec 06).

Fuente de requisitos: specs/06 §11-44, §80-88 y ADR-0005.
Convenciones: Pydantic en los límites, base StrictModel (extra="forbid"),
estados en minúsculas, códigos de error estables sin texto de excepción.
"""

from typing import Literal

from ada_study.strict_model import StrictModel

# ---------------------------------------------------------------------------
# Roles y estados de cuenta (Spec 06 §11-35)
# ---------------------------------------------------------------------------

Role = Literal["student", "trusted_contributor", "moderator", "admin"]

AccountStatus = Literal["invited", "active", "suspended", "deactivated"]

# ---------------------------------------------------------------------------
# Códigos de error estables de identidad (Spec 06 §41-43, ADR-0005).
# Nunca contienen texto de excepción ni exponen si un correo existe.
# ---------------------------------------------------------------------------

AuthErrorCode = Literal[
    "AUTH_INVALID_CREDENTIALS",
    "AUTH_ACCOUNT_NOT_ACTIVE",
    "AUTH_SESSION_INVALID",
    "AUTH_SESSION_EXPIRED",
    "AUTH_FORBIDDEN",
    "AUTH_INVITATION_INVALID",
    "AUTH_INTERNAL",
]

# ---------------------------------------------------------------------------
# Contratos de identidad
# ---------------------------------------------------------------------------


class UserRecord(StrictModel):
    """Cuenta de aplicación (tabla `users`, Spec 06 §35)."""

    user_id: str
    email: str
    display_name: str
    role: Role = "student"
    account_status: AccountStatus = "invited"
    # Hash PBKDF2 (formato "pbkdf2_sha256$iterations$salt_hex$hash_hex").
    # Nunca se expone en listados; solo el servicio de identidad lo usa.
    password_hash: str | None = None
    created_at: str
    updated_at: str


class InvitationRecord(StrictModel):
    """Invitación de un solo uso con vencimiento (Spec 06 §38-39)."""

    invitation_id: str
    email: str
    role: Role = "student"
    token_hash: str
    status: Literal["pending", "consumed", "revoked"] = "pending"
    created_by: str
    created_at: str
    expires_at: str
    consumed_at: str | None = None
    consumed_by: str | None = None


class SessionRecord(StrictModel):
    """Sesión con token opaco; en PostgreSQL solo se guarda el hash (Spec 06 §42)."""

    session_id: str
    user_id: str
    token_hash: str
    status: Literal["active", "revoked"] = "active"
    created_at: str
    expires_at: str
    revoked_at: str | None = None


class AuditEvent(StrictModel):
    """Evento de auditoría append-only (Spec 06 §81-86)."""

    event_id: str
    actor_id: str | None = None
    action: str
    entity_type: str
    entity_id: str
    version_id: str | None = None
    result: Literal["ok", "denied", "error"] = "ok"
    reason: str | None = None
    created_at: str
