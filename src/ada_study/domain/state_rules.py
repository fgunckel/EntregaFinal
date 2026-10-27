"""Reglas de transición de estado (ADR-0005).

Funciones puras: sin I/O, sin base de datos. El repositorio y los servicios
de dominio las usan para rechazar transiciones inválidas antes de persistir.
"""

from ada_study.contracts.documents import EditorialStatus, VersionStatus

# ---------------------------------------------------------------------------
# Ciclo editorial (Spec 06 §61-68, ADR-0005)
# ---------------------------------------------------------------------------

EDITORIAL_TRANSITIONS: dict[EditorialStatus, frozenset[EditorialStatus]] = {
    "draft": frozenset({"pending_review", "published_pending_review", "withdrawn", "deleted"}),
    "pending_review": frozenset({"published", "rejected", "withdrawn", "deleted"}),
    "published_pending_review": frozenset({"published", "rejected", "withdrawn", "deleted"}),
    "published": frozenset({"withdrawn", "deleted"}),
    "rejected": frozenset({"pending_review", "deleted"}),
    "withdrawn": frozenset({"published", "deleted"}),
    "deleted": frozenset(),
}

# Estados editoriales cuyos fragmentos son recuperables (Spec 06 §72, §104)
RECOVERABLE_EDITORIAL: frozenset[EditorialStatus] = frozenset(
    {"published", "published_pending_review"}
)


def is_editorial_transition_valid(
    current: EditorialStatus, target: EditorialStatus
) -> bool:
    """Indica si la transición editorial actual -> target es válida."""
    return target in EDITORIAL_TRANSITIONS.get(current, frozenset())


# ---------------------------------------------------------------------------
# Ciclo técnico de la versión (Spec 06 §71, ADR-0005)
# ---------------------------------------------------------------------------

VERSION_TRANSITIONS: dict[VersionStatus, frozenset[VersionStatus]] = {
    "queued": frozenset({"processing", "failed"}),
    "processing": frozenset({"indexing", "failed"}),
    "indexing": frozenset({"available", "failed", "deleting"}),
    "available": frozenset({"deleting"}),
    "failed": frozenset({"queued", "deleting"}),
    "deleting": frozenset(),
}


def is_version_transition_valid(current: VersionStatus, target: VersionStatus) -> bool:
    """Indica si la transición técnica de versión current -> target es válida."""
    return target in VERSION_TRANSITIONS.get(current, frozenset())


def is_version_recoverable(
    version_status: VersionStatus, editorial_status: EditorialStatus
) -> bool:
    """Regla de visibilidad: editorial publicable Y versión activa available.

    Spec 06 §72/§104: la recuperación solo expone fragmentos de documentos
    published/published_pending_review con versión activa available.
    """
    return version_status == "available" and editorial_status in RECOVERABLE_EDITORIAL
