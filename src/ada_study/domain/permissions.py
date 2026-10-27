"""Matriz de permisos por rol (TASK-04, Spec 06 §11-35).

Funciones puras: sin I/O ni base de datos. Los servicios de dominio las usan
para autorizar cada operación en el backend (Spec 06 §9: la UI no es control
de seguridad). Jerarquía: trusted_contributor incluye los permisos de student
(Spec 06 §20); moderator y admin se modelan como roles con permisos propios,
no como superconjuntos implícitos.
"""

from ada_study.contracts.identity import Role

# ---------------------------------------------------------------------------
# Permisos atómicos (Spec 06 §11-35)
# ---------------------------------------------------------------------------

PERMISSIONS: frozenset[str] = frozenset(
    {
        "documents:upload",        # cargar documentos para revisión
        "documents:publish_auto",  # publicar automáticamente tras controles técnicos
        "documents:read_shared",   # consultar la biblioteca compartida
        "documents:read_own",      # ver el estado de los aportes propios
        "moderation:review",       # ver la cola de moderación
        "moderation:decide",       # aprobar/rechazar/retirar/pedir cambios
        "users:invite",            # invitar y gestionar cuentas
        "users:assign_role",       # cambiar roles
        "audit:read",              # leer eventos de auditoría
    }
)

# Matriz rol -> permisos. `trusted_contributor` hereda los de `student`
# (Spec 06 §20); `moderator` NO hereda admin (Spec 06 §28).
ROLE_PERMISSIONS: dict[Role, frozenset[str]] = {
    "student": frozenset(
        {
            "documents:upload",
            "documents:read_shared",
            "documents:read_own",
        }
    ),
    "trusted_contributor": frozenset(
        {
            "documents:upload",
            "documents:read_shared",
            "documents:read_own",
            "documents:publish_auto",
        }
    ),
    "moderator": frozenset(
        {
            "documents:upload",
            "documents:read_shared",
            "documents:read_own",
            "documents:publish_auto",
            "moderation:queue",
            "moderation:decide",
            "audit:read",
        }
    ),
    "admin": frozenset(
        {
            "documents:upload",
            "documents:read_shared",
            "documents:read_own",
            "documents:publish_auto",
            "moderation:read",
            "moderation:decide",
            "users:invite",
            "users:manage",
            "audit:read",
        }
    ),
}


def has_permission(role: Role, permission: str) -> bool:
    """Indica si el rol tiene el permiso dado (autorización en backend)."""
    return permission in ROLE_PERMISSIONS.get(role, frozenset())


def can_publish_directly(role: Role) -> bool:
    """Indica si el rol publica automáticamente tras controles técnicos.

    Spec 06 §21-22: solo trusted_contributor (y admin por jerarquía de
    operación administrativa); student queda pending_review (§51).
    """
    return role in ("trusted_contributor", "admin")


def can_moderate(role: Role) -> bool:
    """Indica si el rol puede aprobar/rechazar/retirar/pedir cambios."""
    return role in ("moderator", "admin")


def can_manage_users(role: Role) -> bool:
    """Indica si el rol puede invitar, suspender y cambiar roles."""
    return role == "admin"


def can_read_audit(role: Role) -> bool:
    """Indica si el rol puede leer la auditoría (Spec 06 §88)."""
    return role in ("moderator", "admin")


# ---------------------------------------------------------------------------
# Transiciones editoriales por rol (Spec 06 §46-55, §99-104)
# ---------------------------------------------------------------------------

# Transiciones que un autor puede hacer sobre sus PROPIOS aportes.
AUTHOR_TRANSITIONS: dict[Role, frozenset[str]] = {
    "student": frozenset(),  # student no publica: queda pending_review
    "trusted_contributor": frozenset({"published_pending_review"}),
    "moderator": frozenset({"published", "published_pending_review"}),
    "admin": frozenset({"published", "published_pending_review"}),
}

# Transiciones de moderación con el motivo exigido en cada una (Spec 06 §102).
MODERATION_ACTIONS: dict[str, str] = {
    "approve": "published",
    "reject": "rejected",
    "withdraw": "withdrawn",
    "request_changes": "needs_changes",
}


def requires_reason(action: str) -> bool:
    """Indica si la acción de moderación exige motivo (Spec 06 §102).

    Rechazos y retiradas SIEMPRE requieren motivo; aprobar lo tiene opcional.
    """
    return action in ("reject", "withdraw")
