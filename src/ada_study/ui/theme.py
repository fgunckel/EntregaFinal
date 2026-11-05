"""Sistema de estilos «Tinta y aula» (UI-REDESIGN-02, etapas 0-1).

Centraliza la identidad visual aprobada (docs/ui/UI_REDESIGN_PROPOSAL.md §5-8):

- Tokens de color, tipografía, espaciado, bordes y radios.
- El ÚNICO diccionario de estados (editoriales + técnicos de ingesta) con
  etiqueta en español y estilo semántico. Reemplaza los tres mecanismos
  que convivían (G3): ``EDITORIAL_LABELS`` (home, sin uso),
  ``VERSION_STATUS_LABELS`` y ``_status_badge`` (library, sin uso).
- CSS global mínimo: SOLO clases propias con prefijo ``ada-``; cero
  selectores de clases internas generadas por Streamlit. El CSS no toca
  widgets existentes y los datos nunca viajan por ``unsafe_allow_html``
  (las etiquetas son constantes internas; los valores desconocidos se
  escapan con ``html.escape``).
- Helpers puros y testeables para obtener etiqueta/estilo de un estado.

El tema base (fondos, color primario, tipografía) lo aplica
``.streamlit/config.toml``: Streamlit resuelve ese archivo desde el CWD del
proceso (raíz del repo en local, ``/app`` en Docker — ver Dockerfile:
``COPY .streamlit ./.streamlit``). Este módulo agrega únicamente las piezas
que ``config.toml`` no cubre (pastillas, encabezados).
"""

from __future__ import annotations

import html
from dataclasses import dataclass

import streamlit as st

# ---------------------------------------------------------------------------
# Tokens «Tinta y aula» (decisión aprobada UI-REDESIGN-02 §1)
# ---------------------------------------------------------------------------

INK = "#1A1D29"  # texto principal
INK_MUTED = "#6B7280"  # texto secundario
PAPER = "#FAF9F7"  # fondo papel cálido
SURFACE = "#FFFFFF"  # superficies (tarjetas, sidebar)
BORDER = "#E8E5E0"
ACCENT = "#4338CA"  # índigo: único acento de marca
ACCENT_SOFT = "#EEF0FA"

# Estados semánticos (siempre acompañados de etiqueta textual).
OK = "#15803D"  # verde: publicado / disponible
OK_BG = "#E9F4EC"
WARN = "#B45309"  # ámbar: borrador / en revisión / en proceso
WARN_BG = "#FCF3E2"
DANGER = "#B42318"  # rojo: rechazado / falló
DANGER_BG = "#FBECE9"
NEUTRAL = "#52525B"  # gris: estados terminales o desconocidos
NEUTRAL_BG = "#F1F0EC"

RADIUS_SM = "0.375rem"
RADIUS_MD = "0.625rem"
RADIUS_PILL = "999px"

FONT_STACK = (
    '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif'
)

SPACE_1 = "0.25rem"
SPACE_2 = "0.5rem"
SPACE_3 = "0.75rem"
SPACE_4 = "1rem"


# ---------------------------------------------------------------------------
# Único diccionario de estados (G3): etiqueta en español + estilo semántico.
# Los estados editoriales (ciclo de publicación) y los técnicos (ingesta)
# NO se mezclan: cada familia tiene su diccionario y su variante visual.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class StatusStyle:
    """Estilo semántico de un estado: etiqueta + colores."""

    label: str
    color: str
    background: str
    tone: str  # clase CSS: ok | warn | danger | neutral | accent


EDITORIAL_STATUSES: dict[str, StatusStyle] = {
    "draft": StatusStyle("Borrador", WARN, WARN_BG, "warn"),
    "pending_review": StatusStyle("En revisión", WARN, WARN_BG, "warn"),
    "published": StatusStyle("Publicado", OK, OK_BG, "ok"),
    "published_pending_review": StatusStyle("Publicado (revisión pendiente)", OK, OK_BG, "ok"),
    "rejected": StatusStyle("Rechazado", DANGER, DANGER_BG, "danger"),
    "withdrawn": StatusStyle("Retirado", NEUTRAL, NEUTRAL_BG, "neutral"),
    "deleted": StatusStyle("Borrado", NEUTRAL, NEUTRAL_BG, "neutral"),
}

TECHNICAL_STATUSES: dict[str, StatusStyle] = {
    "queued": StatusStyle("En cola", NEUTRAL, NEUTRAL_BG, "neutral"),
    "processing": StatusStyle("Procesando", WARN, WARN_BG, "warn"),
    "indexing": StatusStyle("Indexando", WARN, WARN_BG, "warn"),
    "available": StatusStyle("Disponible", OK, OK_BG, "ok"),
    "failed": StatusStyle("Falló", DANGER, DANGER_BG, "danger"),
    # Estado real del contrato de versiones (specs/02): transición a borrado.
    "deleting": StatusStyle("Eliminándose", NEUTRAL, NEUTRAL_BG, "neutral"),
}


def pill_markup(label: str, tone: str, *, technical: bool = False) -> str:
    """Markup de UNA pastilla (solo presentación), reutilizable.

    ``label`` SIEMPRE se escapa con ``html.escape``: las etiquetas son
    constantes internas o valores dinámicos escapados; el HTML es fijo.
    Lo usan ``components.status_pill``, las tarjetas y el panel de traza
    para que TODAS las pastillas compartan el mismo estilo.
    """
    css_class = f"ada-pill ada-pill--{tone}"
    if technical:
        css_class += " ada-pill--tech"
    return (
        f'<span class="{css_class}"><span class="ada-pill__dot"></span>'
        f"{html.escape(label)}</span>"
    )


def status_style(status: str, *, technical: bool = False) -> StatusStyle | None:
    """Estilo del estado conocido; ``None`` si es desconocido (pura).

    Sin ``technical=True`` busca primero en los estados editoriales; con
    ``technical=True`` busca primero en los técnicos. Si el valor no está en
    la familia principal, se consulta la otra (el caller puede no conocer la
    familia a priori y las claves no colisionan entre familias).
    """
    primary = TECHNICAL_STATUSES if technical else EDITORIAL_STATUSES
    secondary = EDITORIAL_STATUSES if technical else TECHNICAL_STATUSES
    return primary.get(status) or secondary.get(status)


def editorial_label(status: str) -> str | None:
    """Etiqueta en español de un estado editorial; ``None`` si es desconocido."""
    style = EDITORIAL_STATUSES.get(status)
    return style.label if style else None


def technical_label(status: str) -> str | None:
    """Etiqueta en español de un estado técnico; ``None`` si es desconocido."""
    style = TECHNICAL_STATUSES.get(status)
    return style.label if style else None


# ---------------------------------------------------------------------------
# CSS global mínimo (solo clases propias `ada-*`). Sin selectores internos de
# Streamlit: el tema base lo aporta .streamlit/config.toml. Acotado a las
# piezas que config.toml no cubre (pastillas de estado, encabezados).
# ---------------------------------------------------------------------------
_GLOBAL_CSS = f"""
<style>
.ada-pill {{
    display: inline-flex;
    align-items: center;
    gap: 0.45em;
    padding: 0.2em 0.75em;
    border-radius: {RADIUS_PILL};
    font-size: 0.82rem;
    font-weight: 600;
    line-height: 1.5;
    border: 1px solid transparent;
    white-space: nowrap;
}}
.ada-pill__dot {{
    width: 0.5em;
    height: 0.5em;
    border-radius: 50%;
    background: currentColor;
    display: inline-block;
    flex: none;
}}
.ada-pill--ok {{ color: {OK}; background: {OK_BG}; border-color: #C4E3CC; }}
.ada-pill--warn {{ color: {WARN}; background: {WARN_BG}; border-color: #F0DBB2; }}
.ada-pill--danger {{ color: {DANGER}; background: {DANGER_BG}; border-color: #F1C6BE; }}
.ada-pill--neutral {{ color: {NEUTRAL}; background: {NEUTRAL_BG}; border-color: {BORDER}; }}
.ada-pill--accent {{ color: {ACCENT}; background: {ACCENT_SOFT}; border-color: #D8DCF2; }}
/* Estados TÉCNICOS de ingesta: contorno (sin relleno) para distinguirlos
   de los editoriales, que usan fondo suave. Significados intactos. */
.ada-pill--tech {{ background: transparent; }}
.ada-kicker {{
    color: {INK_MUTED};
    font-size: 0.75rem;
    font-weight: 700;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    margin-bottom: 0;
}}
.ada-page-title {{ margin-top: 0; }}
.ada-header {{ font-family: {FONT_STACK}; color: {INK}; }}
/* --- UI-REDESIGN-03: composición de pantallas (solo clases ada-*) --- */
.ada-brand__mark {{
    display: inline-flex;
    align-items: center;
    justify-content: center;
    background: {ACCENT};
    color: #FFFFFF;
    font-weight: 800;
    font-size: 0.72rem;
    letter-spacing: 0.14em;
    padding: 0.3em 0.6em;
    border-radius: {RADIUS_SM};
    margin: 0 0 0.5rem 0;
}}
.ada-brand__name {{
    font-size: 1.3rem;
    font-weight: 800;
    letter-spacing: -0.01em;
    color: {INK};
    margin: 0;
    line-height: 1.2;
}}
.ada-brand__sub {{
    color: {INK_MUTED};
    font-size: 0.8rem;
    margin: 0.15rem 0 0 0;
}}
.ada-user-card {{
    display: flex;
    align-items: center;
    gap: 0.6rem;
    background: {PAPER};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_MD};
    padding: 0.6rem 0.75rem;
}}
.ada-user-card__avatar {{
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 2rem;
    height: 2rem;
    border-radius: 50%;
    background: {ACCENT_SOFT};
    color: {ACCENT};
    font-weight: 700;
    flex: none;
}}
.ada-user-card__name {{
    margin: 0;
    font-weight: 600;
    font-size: 0.88rem;
    color: {INK};
    overflow-wrap: anywhere;
}}
.ada-user-card__role {{ margin: 0; font-size: 0.75rem; color: {INK_MUTED}; }}
.ada-page-sub {{ color: {INK_MUTED}; }}
.ada-doc-card {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_MD};
    padding: 1rem;
    height: 100%;
    box-shadow: 0 1px 2px rgba(26, 29, 41, 0.04);
}}
.ada-doc-card__name {{
    font-weight: 600;
    color: {INK};
    margin: 0.5rem 0 0 0;
    overflow-wrap: anywhere;
}}
.ada-doc-card__meta {{
    color: {INK_MUTED};
    font-size: 0.8rem;
    margin: 0.15rem 0 0 0;
}}
.ada-empty {{
    text-align: center;
    padding: 2.25rem 1rem;
    border: 1px dashed {BORDER};
    border-radius: {RADIUS_MD};
    background: {SURFACE};
}}
.ada-empty__title {{
    font-weight: 700;
    color: {INK};
    margin: 0 0 0.3rem 0;
    font-size: 1.05rem;
}}
.ada-empty__desc {{ color: {INK_MUTED}; margin: 0; }}
.ada-ref {{
    border: 1px solid {BORDER};
    border-left: 3px solid {ACCENT};
    border-radius: {RADIUS_SM};
    padding: 0.7rem 0.9rem;
    margin: 0.4rem 0;
    background: {PAPER};
}}
.ada-ref__title {{ font-weight: 600; color: {INK}; margin: 0; }}
.ada-ref__meta {{ color: {INK_MUTED}; font-size: 0.78rem; margin: 0.1rem 0 0 0; }}
.ada-ref__excerpt {{ color: {INK}; font-size: 0.9rem; margin: 0.4rem 0 0 0; }}
.ada-trace-row {{
    border-left: 2px solid {BORDER};
    padding: 0.15rem 0 0.5rem 0.9rem;
    margin-left: 0.25rem;
    position: relative;
}}
.ada-trace-dot {{
    position: absolute;
    left: -0.36rem;
    top: 0.42rem;
    width: 0.62rem;
    height: 0.62rem;
    border-radius: 50%;
    background: {ACCENT};
}}
.ada-trace-head {{
    display: flex;
    align-items: center;
    gap: 0.6em;
    flex-wrap: wrap;
}}
.ada-trace-dur {{ color: {INK_MUTED}; font-size: 0.8rem; }}
.ada-trace-resp {{ color: {INK_MUTED}; font-size: 0.85rem; margin: 0.1rem 0 0 0; }}
.ada-ids {{ font-size: 0.82rem; }}
.ada-login-hero {{ text-align: center; padding: 1.5rem 0 1rem 0; }}
.ada-login-title {{
    font-size: 1.9rem;
    font-weight: 800;
    letter-spacing: -0.02em;
    color: {INK};
    margin: 0.4rem 0 0 0;
    line-height: 1.15;
}}
.ada-login-tag {{ color: {INK_MUTED}; margin: 0.5rem 0 0 0; }}
@media (max-width: 640px) {{
    .ada-doc-card {{ padding: 0.8rem; }}
    .ada-login-title {{ font-size: 1.5rem; }}
}}
</style>
"""


def apply_theme() -> None:
    """Inyecta el CSS global de la sesión (etapa 0).

    Se invoca en cada rerun desde ``app.py`` justo después de
    ``st.set_page_config`` y ANTES de renderizar páginas: Streamlit
    reconstruye el árbol de elementos en cada rerun, por lo que el bloque
    ``<style>`` debe volver a emitirse (la operación es idempotente y barata:
    un único ``st.markdown``). El tema de colores base lo aplica el frontend
    desde ``config.toml`` y no depende de esto.
    """
    st.markdown(_GLOBAL_CSS, unsafe_allow_html=True)
