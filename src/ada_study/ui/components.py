"""Componentes reutilizables de la UI (UI-REDESIGN-02, etapa 1).

Componentes iniciales del mini design-system «Tinta y aula»:

- ``status_pill``: única pastilla de estado (etiqueta en español + color
  semántico + punto indicador). Sin emojis y sin enums crudos: las etiquetas
  salen del diccionario único de ``ui.theme``.
- ``error_box``: presentación centralizada de errores. Muestra texto ya
  humanizado por el caller (los ``describe_*`` existentes conservan su
  tratamiento específico y sus tests) y registra el código estable en el
  log para diagnóstico, SIN exponerlo al usuario.
- ``page_header``: encabezado reutilizable (kicker + título + acciones),
  disponible para las etapas 2-7.
- ``short_ref``: identificador interno (UUID) abreviado para mostrar como
  referencia (decisión aprobada: autor en Moderación).
"""

from __future__ import annotations

import html
import logging
from collections.abc import Callable

import streamlit as st

from ada_study.contracts.documents import DocumentRecord
from ada_study.contracts.study import StudyResponse
from ada_study.ui.api_client import ApiError
from ada_study.ui.theme import pill_markup, status_style
from ada_study.ui.trace_view import render_trace

logger = logging.getLogger("ada_study.ui")


def status_pill(status: str, *, technical: bool = False) -> None:
    """Pastilla de estado: punto de color + etiqueta en español.

    - Estados editoriales y técnicos conservan familias y estilos
      diferenciados (``technical=True`` fuerza la variante técnica).
    - Estado desconocido: pastilla neutra con el valor crudo ESCAPADO. No se
      inventa una etiqueta (honesta con lo que el backend envía) y nunca se
      muestra sin contexto. Se registra en el log para detectar estados no
      contemplados.
    - ``unsafe_allow_html`` es solo presentación: la etiqueta es una
      constante interna y cualquier valor dinámico se escapa.
    """
    style = status_style(status, technical=technical)
    if style is None:
        # Estado desconocido: no se oculta ni se inventa; estilo neutro.
        logger.warning("UI: estado desconocido=%r", status)
        st.markdown(pill_markup(status, "neutral"), unsafe_allow_html=True)
        return
    st.markdown(
        pill_markup(style.label, style.tone, technical=technical),
        unsafe_allow_html=True,
    )


def error_box(message: str, *, exc: ApiError | None = None, context: str = "") -> None:
    """Presentación centralizada de errores (pura en lo visible).

    - ``message``: texto YA humanizado por el caller; nunca incluye detalle
      interno del backend (mensajes, endpoints ni tracebacks).
    - ``exc``: si se pasa, el código estable y el status HTTP se registran
      en el log (``ada_study.ui``) para diagnóstico técnico; el usuario ve
      únicamente el mensaje humanizado.
    - No cambia la semántica de las excepciones ni los contratos de API: es
      la capa de presentación; los ``describe_*`` específicos y probados de
      cada página se conservan sin cambios.
    """
    if exc is not None:
        logger.error(
            "UI: fallo de API contexto=%s código=%s status=%s",
            context or "sin-contexto",
            exc.error_code,
            exc.status_code,
        )
    st.error(message)


def page_header(kicker: str, title: str, actions: Callable[[], None] | None = None) -> None:
    """Encabezado de página reutilizable: kicker + título (+ acciones).

    ``actions`` es un callable opcional (cierre sobre botones ya creados);
    si se indica, se renderiza a la derecha del título. Disponible y probado
    para las etapas 2-7: NINGUNA página migró todavía (alcance UI-REDESIGN-02).
    """
    if actions is None:
        _render_header_block(kicker, title)
        return
    title_col, actions_col = st.columns([4, 1])
    with title_col:
        _render_header_block(kicker, title)
    with actions_col:
        actions()


def _render_header_block(kicker: str, title: str) -> None:
    """Kicker en mayúsculas + título (misma columna)."""
    if kicker:
        st.markdown(
            f'<p class="ada-kicker">{html.escape(kicker)}</p>',
            unsafe_allow_html=True,
        )
    st.markdown(
        f'<h2 class="ada-page-title">{html.escape(title)}</h2>',
        unsafe_allow_html=True,
    )


def short_ref(identifier: str | None) -> str:
    """Referencia abreviada de un identificador interno (pura).

    Decisión aprobada (autor en Moderación): si el contrato solo trae un
    UUID (``uploaded_by``), se muestra abreviado — primeros 8 caracteres +
    ellipsis — sin modificar el backend. Identificadores cortos o vacíos se
    muestran tal cual (o «—» si no hay valor).
    """
    if not identifier:
        return "—"
    if len(identifier) <= 10:
        return identifier
    return f"{identifier[:8]}…"


def semantic_pill(label: str, tone: str, *, technical: bool = False) -> None:
    """Pastilla con etiqueta y tono YA decididos por el caller.

    Para estados que no son de estado editorial/técnico de documento
    (p. ej. el resultado de un evento de auditoría) pero deben verse
    consistentes con el resto de pastillas de la UI.
    """
    st.markdown(pill_markup(label, tone, technical=technical), unsafe_allow_html=True)


def empty_state(
    title: str,
    description: str,
    *,
    action: Callable[[], None] | None = None,
) -> None:
    """Estado vacío reutilizable: mensaje claro + acción opcional.

    Evita la experiencia de «pantalla vacía» al no haber datos: siempre
    explica qué se puede hacer a continuación. ``action`` (opcional) dibuja
    el botón de continuación debajo del mensaje.
    """
    st.markdown(
        '<div class="ada-empty">'
        f'<p class="ada-empty__title">{html.escape(title)}</p>'
        f'<p class="ada-empty__desc">{html.escape(description)}</p>'
        "</div>",
        unsafe_allow_html=True,
    )
    if action is not None:
        action()


def doc_card(
    document: DocumentRecord,
    *,
    on_action: Callable[[], None] | None = None,
) -> None:
    """Tarjeta de documento (presupuesto visual: una tarjeta por ítem).

    - La pastilla de estado comparte el estilo global (``pill_markup``).
    - ``on_action`` renderiza la acción debajo de la tarjeta (el caller crea
      el botón con su key, como exige el patrón de keys de la UI).
    - Los datos (nombre, materia, fecha) SIEMPRE se escapan: el HTML es solo
      la plantilla de la tarjeta.
    """
    status = document.editorial_status
    style = status_style(status)
    pill = pill_markup(
        style.label if style else status,
        style.tone if style else "neutral",
    )
    subject = document.subject or "Sin materia"
    created = getattr(document, "created_at", None)
    meta = f"{subject} · {created[:10]}" if created else subject
    st.markdown(
        '<div class="ada-doc-card">'
        f"{pill}"
        f'<p class="ada-doc-card__name">{html.escape(document.filename)}</p>'
        f'<p class="ada-doc-card__meta">{html.escape(meta)}</p>'
        "</div>",
        unsafe_allow_html=True,
    )
    if on_action is not None:
        on_action()


# ---------------------------------------------------------------------------
# UI-REDESIGN-03: bloques de respuesta, confirmaciones y estado de ingesta.
# ---------------------------------------------------------------------------
EVIDENCE_LABELS: dict[str, tuple[str, str]] = {
    # Estado de evidencia del contrato study.py -> (etiqueta, tono).
    "complete": ("Evidencia suficiente", "ok"),
    "insufficient_evidence": ("Evidencia insuficiente", "danger"),
    "limited": ("Respuesta limitada", "warn"),
}


def answer_block(response: StudyResponse) -> None:
    """Respuesta del tutor en un bloque de lectura consistente.

    Orden de lectura (propuesta §estudio): 1) estado de evidencia, 2)
    explicación, 3) referencias, 4) avisos. La traza del agente vive al final
    (colapsada): es metadato de confianza, no parte de la lectura.

    Conserva la semántica de ``_render_response`` de ``pages/study.py``
    (avisos de estado del backend, lagunas de evidencia, fuentes web y el
    límite de la máquina de estados) pero reorganizada para estudio.
    """
    with st.container(border=True):
        # 1) Estado de la evidencia (etiqueta en español, nunca el enum crudo).
        evidence = EVIDENCE_LABELS.get(response.status)
        if evidence is None:
            logger.warning("UI: estado de evidencia desconocido=%r", response.status)
            st.markdown(pill_markup(response.status, "neutral"), unsafe_allow_html=True)
        else:
            st.markdown(pill_markup(evidence[0], evidence[1]), unsafe_allow_html=True)
        if response.status == "insufficient_evidence":
            st.warning(
                "La evidencia disponible no alcanza para responder con fundamento. "
                "Probá reformular la pregunta o subir más material."
            )
        # 2) Explicación del tutor (markdown tal como la genera el backend).
        st.markdown(response.explanation)
        # 3) Referencias: tarjetas compactas con página + extracto.
        if response.references:
            st.markdown(
                '<p class="ada-kicker">Referencias de tu biblioteca</p>',
                unsafe_allow_html=True,
            )
            for ref in response.references:
                page = f" · página {ref.page}" if getattr(ref, "page", None) else ""
                excerpt = (ref.excerpt or "").strip()
                st.markdown(
                    '<div class="ada-ref">'
                    f'<p class="ada-ref__title">{html.escape(ref.title)}{page}</p>'
                    + (f'<p class="ada-ref__excerpt">{html.escape(excerpt)}</p>' if excerpt else "")
                    + "</div>",
                    unsafe_allow_html=True,
                )
        # 4) Avisos (visibilidad honesta de límites y degradaciones).
        for gap in response.evidence_gaps:
            st.warning(f"Evidencia con vacíos: {gap}")
        if response.uses_web_sources:
            st.warning(
                "Esta respuesta se apoyó en fuentes web porque tu biblioteca "
                "no alcanzó; verificá contra tu material."
            )
        for limitation in response.limitations:
            st.info(f"Limitación: {limitation}")
        # Traza del agente: colapsada, secundaria a la respuesta.
        render_trace(response.trace)


def confirm_destructive(label: str, *, key: str) -> bool:
    """Confirmación explícita previa a una acción destructiva.

    El checkbox es la barrera de seguridad (borrar documento, decisiones de
    moderación): la acción real queda en manos del caller, que valida además
    sus propias precondiciones. ``key`` SIEMPRE sufijada con el id del ítem
    para no colisionar en listas.
    """
    return st.checkbox(label, key=key)


def ingest_status(client) -> None:
    """Estado de la ingesta del último envío (NO bloquea la página).

    Punto de entrada público: sin job activo (``ada_last_job``) NO entra al
    fragmento (nada que consultar y los tests con AppTest —que no soportan
    el auto-rerun de fragmentos— terminan normalmente). Con job activo
    delega en el fragmento que re-ejecuta SOLO este bloque cada 2 s (la
    página completa sigue interactiva).

    - Estado en curso: pastilla técnica + explicación del paso actual.
    - Estado terminal: mensaje final, se limpia ``ada_last_job`` y se fuerza
      un rerun completo para desmontar el fragmento (deja de consultar).
    - Error de consulta: se informa y se reintenta automáticamente en el
      próximo tick; NO se descarta el trabajo por un fallo transitorio.
    """
    if not st.session_state.get("ada_last_job"):
        return
    _ingest_status_fragment(client)


@st.fragment(run_every=2)
def _ingest_status_fragment(client) -> None:
    """Fragmento real (run_every=2): sondeo no bloqueante del job."""
    job_info = st.session_state.get("ada_last_job")
    if not job_info:
        return
    st.markdown('<p class="ada-kicker">Ingesta en curso</p>', unsafe_allow_html=True)
    try:
        job = client.get_ingestion_job(job_info["job_id"])
    except ApiError as exc:
        error_box(
            "No se pudo consultar el estado de la ingesta; se reintentará "
            "automáticamente en unos segundos.",
            exc=exc,
            context="library.poll_job",
        )
        return
    status_pill(job.status, technical=True)
    st.caption(
        "Podés seguir usando la app mientras se procesa el documento; el "
        "estado se actualiza solo."
    )
    if job.status in {"available", "failed", "deleted"}:
        if job.status == "available":
            ref = short_ref(job_info.get("document_id"))
            st.success(
                f"Ingesta completa: el documento {ref} ya está disponible."
            )
        else:
            st.error(
                "La ingesta terminó con error; podés reintentarla desde el "
                "detalle del documento."
            )
        st.session_state["ada_last_job"] = None
        st.rerun()
