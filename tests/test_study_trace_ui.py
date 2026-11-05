"""AGENT-TRACE-02: visualización de trazabilidad por consulta en Estudiar.

Dos niveles de evidencia, siguiendo las convenciones del repo:

- Funciones puras (`build_agent_views`, `trace_notes`) sobre `Trace` reales
  construidos con `new_event` (mismas formas de eventos que emiten los nodos).
- Streamlit AppTest sobre `ui_render_study.py` con un cliente falso: la
  respuesta y su traza viajan DENTRO del `StudyResponse` que devuelve el
  cliente, igual que en producción.

Casos cubiertos: respuesta con trace (1-2), sin trace (3), insuficiencia de
evidencia (4), error de ejecución (5), reintentos del validator (6), dos
consultas con trazas independientes (7), historial con asociación por
respuesta (8), detalles ausentes sin romper el render (9) y no confusión
entre trace_id y conversation_id (10).
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from ada_study.contracts.study import StudyResponse
from ada_study.contracts.trace import Trace
from ada_study.observability.trace import new_event
from ada_study.ui import auth
from ada_study.ui.trace_view import (
    AGENT_NAMES,
    AGENT_ORDER,
    AGENT_ROLES,
    STATE_LABELS,
    TRACE_TITLE,
    TRACE_UNAVAILABLE,
    build_agent_views,
    trace_notes,
)

SCRIPT_PATH = Path(__file__).parent / "ui_render_study.py"

# ---------------------------------------------------------------------------
# Constructores de eventos con las MISMAS formas que emiten los nodos reales
# ---------------------------------------------------------------------------


def _trace(
    trace_id: str,
    events: list[dict],
    *,
    status="completed",
    conversation_id=None,
    error=None,
):
    return Trace(
        trace_id=trace_id,
        conversation_id=conversation_id,
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
        duration_ms=25.0,
        status=status,
        events=events,
        error=error,
    )


_UNSET = object()


def _response(trace=_UNSET, *, status="complete", conversation_id=None, trace_id="tr_1"):
    return StudyResponse(
        status=status,
        topic="mitosis",
        explanation="Explicación pedagógica de la respuesta.",
        conversation_id=conversation_id,
        # `trace=None` significa SIN traza (caso 3): no se sustituye por una.
        trace=_trace(trace_id, []) if trace is _UNSET else trace,
    )
def _node(node: str, *, fail: bool = False, duration_ms: float = 1.5) -> list[dict]:
    """node_start + node_end de UNA ejecución real (o cortada si `fail`)."""
    events = [new_event(node, "node_start", f"Inicio de {node}.")]
    if not fail:
        events.append(
            new_event(node, "node_end", f"{node} completado.", duration_ms=duration_ms)
        )
    return events


def _supervisor_events(next_agent="researcher", reason="Sin evidencia recuperada.", steps=1):
    return [
        *_node("supervisor"),
        new_event(
            "supervisor",
            "decision",
            f"Enrutado a {next_agent}: {reason}",
            details={"next_agent": next_agent, "reason": reason, "steps": steps},
        ),
    ]


def _researcher_events(
    *,
    query="¿Qué es la mitosis?",
    retrieved=3,
    rejected=(),
    accepted=2,
    gaps=(),
):
    events = [*_node("researcher")]
    events.append(
        new_event(
            "researcher",
            "decision",
            f"Recuperación BM25: {retrieved} fragmentos.",
            details={
                "query": query,
                "retrieval_query": query,
                "contextual_reformulation": False,
                "retrieved_chunks": retrieved,
                "documents": [
                    {"document_id": f"doc_{i}", "title": f"Apuntes {i}", "page": 1}
                    for i in range(retrieved)
                ],
                "target_terms": ["mitosis"],
                "thematic_terms": ["mitosis"],
                "required_matches": 2,
            },
        )
    )
    events.append(
        new_event(
            "researcher",
            "result",
            f"Filtro de pertinencia: {accepted} aceptadas, {len(rejected)} descartadas.",
            details={
                "accepted": [
                    {"chunk_id": f"ok_{i}", "title": f"Apuntes {i}", "page": 1}
                    for i in range(accepted)
                ],
                "rejected": [
                    {
                        "chunk_id": f"no_{i}",
                        "title": f"Descartado {i}",
                        "page": 2,
                        "matched_terms": 1,
                        "reason": reason,
                    }
                    for i, reason in enumerate(rejected)
                ],
                "final_evidence_count": accepted,
                "evidence_gaps": list(gaps),
            },
        )
    )
    return events


def _tutor_events(*, mode="llm", llm_result=True, llm_invoked=True):
    events = [*_node("tutor")]
    events.append(
        new_event(
            "tutor",
            "decision",
            f"Decisión del tutor: {mode} (status=complete).",
            details={
                "evidence_count": 2,
                "status": "complete",
                "mode": mode,
                "llm_configured": True,
                "llm_invoked": llm_invoked,
            },
        )
    )
    if llm_result:
        events.append(
            new_event(
                "tutor",
                "result",
                "Invocación al LLM completada: redacción del tutor generada.",
                details={
                    "provider": "groq",
                    "model": "openai/gpt-oss-120b",
                    "duration_ms": 950.0,
                    "input_tokens": 2732,
                    "output_tokens": 813,
                    "total_tokens": 3545,
                    "output_chars": 4210,
                },
            )
        )
    return events


def _validator_events(*, approved=True, retries=0, repair_agent=None, checks=None):
    events = [*_node("validator")]
    events.append(
        new_event(
            "validator",
            "result",
            "Validación aprobada." if approved else "Validación rechazada: 1 errores.",
            status="ok" if approved else "error",
            details={
                "is_valid": approved,
                "checks": checks
                or {
                    "evidence_contract": "pass",
                    "response_contract": "pass",
                    "cited_subset_of_evidence": "pass" if approved else "fail",
                    "gaps_acknowledged": "pass",
                },
                "errors": [] if approved else ["La respuesta cita chunks desconocidos."],
                "warnings": [],
                "requires_human_review": False,
                "repair_agent": repair_agent,
                "retries": retries,
                "repair_requested": repair_agent is not None,
            },
        )
    )
    return events


def _synthesizer_events(*, approved=True, references=2, status="complete"):
    events = [*_node("synthesizer")]
    events.append(
        new_event(
            "synthesizer",
            "decision",
            f"Validación aprobada; respuesta publicada (status={status}).",
            details={
                "validation_approved": approved,
                "published_status": status,
                "references_count": references,
                "history_consolidated": True,
            },
        )
    )
    return events


def _happy_events():
    """Flujo normal completo: los cinco agentes ejecutados y aprobados."""
    return [
        *_supervisor_events("researcher"),
        *_researcher_events(),
        *_supervisor_events("tutor", "Con evidencia; generar la respuesta.", steps=2),
        *_tutor_events(),
        *_supervisor_events("validator", "Respuesta pendiente de validación.", steps=3),
        *_validator_events(),
        *_supervisor_events("synthesizer", "Validación vigente; publicar respuesta.", steps=4),
        *_synthesizer_events(),
    ]

# ---------------------------------------------------------------------------
# Casos 1, 2, 4, 5, 6 y 9 (puras): estados y detalles desde eventos reales
# ---------------------------------------------------------------------------


def _view(views, node):
    return next(view for view in views if view.node == node)


class TestAgentViews:
    def test_respuesta_con_trace_muestra_cinco_agentes(self):
        """Casos 1-2: cinco agentes, responsabilidades fijas y estado real."""
        views = build_agent_views(_trace("tr_1", _happy_events()))
        assert [view.node for view in views] == list(AGENT_ORDER)
        for view in views:
            assert view.name == AGENT_NAMES[view.node]
            assert view.responsibility == AGENT_ROLES[view.node]
            assert view.state == "completed"
            assert view.summary
            assert view.duration_ms is not None

    def test_supervisor_aparece_por_cada_ejecucion_real(self):
        """Supervisor es orquestación: sus decisiones múltiples quedan registradas."""
        views = build_agent_views(_trace("tr_1", _happy_events()))
        supervisor = _view(views, "supervisor")
        assert supervisor.executions == 4
        # Última decisión real: publicar.
        assert "synthesizer" in supervisor.summary

    def test_insuficiencia_no_es_error(self):
        """Caso 4: tutor sin LLM y razones de rechazo, finalización controlada."""
        events = [
            *_supervisor_events("researcher"),
            *_researcher_events(
                rejected=("solo 1 de 2 términos temáticos compartidos.",),
                accepted=0,
                gaps=("Evidencia recuperada insuficiente.",),
            ),
            *_tutor_events(mode="insufficient_evidence", llm_result=False, llm_invoked=False),
            *_validator_events(),
            *_synthesizer_events(status="insufficient_evidence"),
        ]
        views = build_agent_views(_trace("tr_1", events))
        tutor = _view(views, "tutor")
        researcher = _view(views, "researcher")
        # Finalización CONTROLADA: nada presentado como error.
        assert tutor.state == "completed"
        assert _view(views, "validator").state == "completed"
        assert any("no se invocó al LLM" in line for line in tutor.details)
        assert any("términos temáticos" in line for line in researcher.details)
        assert any("Razón de insuficiencia" in line for line in researcher.details)

    def test_error_no_muestra_completados_los_que_no_finalizaron(self):
        """Caso 5: nodo cortado en ejecución; los demás según sus eventos."""
        events = [
            *_supervisor_events("researcher"),
            *_researcher_events(),
            # El tutor arranca y el flujo se corta (sin node_end ni decisión).
            *_node("tutor", fail=True),
        ]
        trace = _trace("tr_1", events, status="failed", error="GraphRecursionError")
        views = build_agent_views(trace)
        assert _view(views, "supervisor").state == "completed"
        assert _view(views, "researcher").state == "completed"
        assert _view(views, "tutor").state == "running"
        assert STATE_LABELS[_view(views, "tutor").state] == "En ejecución"
        assert _view(views, "validator").state == "unknown"
        assert _view(views, "synthesizer").state == "unknown"
        notes = trace_notes(trace)
        assert ("error", notes[0][1])[0] == "error"
        assert "GraphRecursionError" in notes[0][1]

    def test_validator_reintento_diferencia_intentos(self):
        """Caso 6: dos ejecuciones del validator con resultados distinguibles."""
        events = [
            *_supervisor_events("researcher"),
            *_researcher_events(),
            *_supervisor_events("tutor", "Con evidencia.", steps=2),
            *_tutor_events(),

            *_supervisor_events("validator", "Respuesta pendiente de validación.", steps=3),
            # Intento 1: rechaza y pide corrección al researcher.
            *_validator_events(
                approved=False,
                repair_agent="researcher",
                checks={"cited_subset_of_evidence": "fail"},
            ),
            *_researcher_events(),
            # Intento 2: aprueba.
            *_validator_events(approved=True, retries=1),
            *_supervisor_events("synthesizer", "Validación vigente.", steps=4),
            *_synthesizer_events(),
        ]
        views = build_agent_views(_trace("tr_1", events))
        validator = _view(views, "validator")
        assert validator.executions == 2
        assert validator.state == "completed"
        assert any(
            "Intento 1" in line and "**rechazada**" in line for line in validator.details
        )
        assert any(
            "Intento 2" in line and "**aprobada**" in line for line in validator.details
        )
        assert any("Corrección solicitada a: **Researcher**" in line for line in validator.details)

    def test_detalles_ausentes_no_rompen_la_vista(self):
        """Caso 9: eventos sin detalles ni resúmenes no producen errores."""
        events = [*_node("supervisor"), *_node("researcher")]
        views = build_agent_views(_trace("tr_1", events))
        assert all(view.details == [] for view in views)
        assert _view(views, "tutor").summary is None
        # Sin eventos y turno completado: omitido, nunca "completado".
        assert _view(views, "tutor").state == "skipped"
        assert _view(views, "validator").state == "skipped"

    def test_agente_sin_eventos_en_turno_completado_es_omitido(self):
        """Los agentes que no intervinieron NO aparecen como completados."""
        events = [*_supervisor_events("researcher"), *_researcher_events()]
        views = build_agent_views(_trace("tr_1", events))
        assert _view(views, "synthesizer").state == "skipped"
        assert STATE_LABELS["skipped"] in ("Omitido",)

# ---------------------------------------------------------------------------
# Casos 1, 3, 7, 8 y 10 (AppTest): página Estudiar con cliente falso
# ---------------------------------------------------------------------------


class FakeStudyClient:
    """Mínimo superficial de ApiClient para render_study."""

    def __init__(self, responses: list[StudyResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    def study(self, question: str, conversation_id: str | None = None) -> StudyResponse:
        self.calls.append({"question": question, "conversation_id": conversation_id})
        return self._responses.pop(0)


def _run_study(monkeypatch: pytest.MonkeyPatch, responses: list[StudyResponse]):
    client = FakeStudyClient(responses)
    monkeypatch.setattr(auth, "get_client", lambda: client)
    at = AppTest.from_file(str(SCRIPT_PATH), default_timeout=15)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at, client


def _ask(at: AppTest, question: str) -> None:
    # UI-REDESIGN-03: el formulario se migró a st.chat_input (misma key
    # contractual `ada_study_request`); el envío es el Enter del propio input.
    at.chat_input(key="ada_study_request").set_value(question).run()
    assert not at.exception, [e.value for e in at.exception]


class TestStudyPageTrace:
    def test_respuesta_con_trace_muestra_su_trazabilidad(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Caso 1: el expander de trazabilidad vive bajo la respuesta."""
        response = _response(
            _trace("tr_aaa", _happy_events(), conversation_id="thread-1"),
            conversation_id="conv-111",
        )
        at, client = _run_study(monkeypatch, [response])
        _ask(at, "¿Qué es la mitosis?")
        labels = [expander.label for expander in at.expander]
        assert TRACE_TITLE in labels
        # La respuesta sigue visible con su traza propia.
        assert client.calls[0]["conversation_id"] is None
        assert any("Explicación pedagógica" in m.value for m in at.markdown)

    def test_respuesta_sin_trace_sigue_funcionando(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Caso 3: sin trace la respuesta se muestra y se informa discretamente."""
        at, _ = _run_study(monkeypatch, [_response(trace=None)])
        _ask(at, "¿Qué es la fotosíntesis?")
        captions = [c.value for c in at.caption]
        assert TRACE_UNAVAILABLE in captions
        assert not any(TRACE_TITLE == label for label in [e.label for e in at.expander])

    def test_dos_consultas_trazas_independientes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Casos 7-8: cada respuesta conserva SU trace; nunca se comparten."""
        first = _response(
            _trace("tr_primera", _happy_events(), conversation_id="thread-1"),
            conversation_id="conv-primera",
        )
        second = _response(
            _trace("tr_segunda", _happy_events(), conversation_id="thread-1"),
            conversation_id="conv-primera",
        )
        at, client = _run_study(monkeypatch, [first, second])
        _ask(at, "Primera consulta")
        _ask(at, "Segunda consulta")
        # La segunda llamada reutiliza el conversation_id de la app (§5).
        assert client.calls[1]["conversation_id"] == "conv-primera"
        turns = at.session_state["ada_study_turns"]
        assert [t["respuesta"].trace.trace_id for t in turns] == ["tr_primera", "tr_segunda"]
        # El historial muestra la traza de cada turno (AppTest renderiza todo).
        ids = [c.value for c in at.caption]
        assert any("tr_primera" in value for value in ids)
        assert any("tr_segunda" in value for value in ids)

    def test_identificadores_no_se_confunden(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Caso 10: trace_id y hilo del checkpointer con etiquetas distintas."""
        response = _response(
            _trace("tr_unica", _happy_events(), conversation_id="thread-checkpoint"),
            conversation_id="conv-app-uuid",
        )
        # No son el mismo identificador y se muestran con etiquetas separadas.
        assert response.trace.trace_id != response.conversation_id
        assert response.trace.conversation_id != response.conversation_id
        at, _ = _run_study(monkeypatch, [response])
        _ask(at, "Consulta con identificadores")
        captions = [c.value for c in at.caption]
        assert any("ID de traza: `tr_unica`" in value for value in captions)
        assert any("Hilo del checkpointer: `thread-checkpoint`" in value for value in captions)
        assert not any("conv-app-uuid" in value for value in captions)

    def test_sin_trace_los_turnos_previos_no_fallan(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Casos 3+8: turnos previos sin trace no rompen el historial."""
        first = _response(_trace("tr_uno", _happy_events()), conversation_id="conv-1")
        second = _response(trace=None, conversation_id="conv-1", trace_id="ignored")
        at, _ = _run_study(monkeypatch, [first, second])
        _ask(at, "Con trace")
        _ask(at, "Sin trace")
        assert not at.exception
        # UI-REDESIGN-03: el historial ya no es el expander «Turnos anteriores»
        # sino el chat visible (pregunta del usuario por turno). El turno sin
        # trace NO rompe la página: la segunda respuesta informa la
        # indisponibilidad en su propio bloque (TRACE_UNAVAILABLE).
        questions = [md.value for md in at.markdown]
        assert any("Con trace" in value for value in questions)
        assert any("Sin trace" in value for value in questions)
        captions = [c.value for c in at.caption]
        assert any("ID de traza: `tr_uno`" in value for value in captions)
        assert any(TRACE_UNAVAILABLE in value for value in captions)
