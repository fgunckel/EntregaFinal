"""Pruebas de AGENT-TRACE-01: trazabilidad estructurada real + Phoenix.

Cubren, con fakes locales (sin OpenAI real, sin PostgreSQL y sin costos):
- Contrato `Trace`/`TraceEvent` (Pydantic estricto).
- Runtime determinístico: `run_study` adjunta a `StudyResponse.trace` eventos
  REALES de los nodos (inicio/fin/duración, decisiones del supervisor,
  filtro de pertinencia con aceptadas/descartadas y motivo, publicación).
- Caso clave AGENT-TRACE-01 §3.2: BM25 recupera fragmentos pero el filtro de
  pertinencia los descarta (análogo a prueba.pdf) — queda registrado.
- LLM: invocación real registrada (proveedor/modelo/duración/tokens cuando
  el proveedor los reporta), error con fallback, y NINGÚN evento de llamada
  cuando no hay evidencia (el tutor no invoca).
- Entrega al cliente: la traza viaja en el payload de `/api/study` y el
  contrato de la UI la valida sin cambios rompientes (campo aditivo).
- Phoenix (§6): con el exportador en memoria, la ejecución produce spans
  reales que comparten `trace_id` via metadata `ada_trace_id`.
"""

import json

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError

from ada_study.config import Settings
from ada_study.contracts.study import StudyResponse
from ada_study.contracts.trace import Trace, TraceEvent
from ada_study.index.retriever import ViewerContext
from ada_study.observability.trace import new_event
from ada_study.study import run_study
from ada_study.study.runtime import _build_trace

# Reutiliza fakes y helpers de los tests existentes (patrón del proyecto).
from tests.test_api_auth import _session_token, _user
from tests.test_study_endpoint import FakeStudyRepository, _seed_published_document
from tests.test_study_llm import FakeChatModel, _client_with_model, _seeded_repo

# Caso prueba.pdf: la consulta trae 3 términos temáticos (mitosis, proceso,
# celular) pero el corpus solo comparte "mitosis": BM25 recupera el fragmento
# (umbral OR del ranker) y el filtro de pertinencia lo descarta (1 < 2).
_QUERY_RECHAZADA = "Explícame la mitosis y el proceso celular."
_TEXT_MITOSIS = "La mitosis es una de las fases del ciclo de división."
_QUERY_FRACCION = "¿Qué es una fracción?"

_NODOS_FLUJO_COMPLETO = [
    "supervisor", "researcher", "tutor", "validator", "synthesizer",
]


def _viewer() -> ViewerContext:
    return ViewerContext(user_id="usr_1", role="student")


def _events(trace: Trace, node: str, event_type: str) -> list[TraceEvent]:
    return [
        event
        for event in trace.events
        if event.node == node and event.event_type == event_type
    ]


# ---------------------------------------------------------------------------
# Contrato de trazabilidad (§4)
# ---------------------------------------------------------------------------


class TestTraceContracts:
    def test_trace_event_rejects_extra_fields(self):
        with pytest.raises(ValidationError):
            TraceEvent(
                event_id="e1",
                node="tutor",
                event_type="decision",
                summary="ok",
                started_at="2026-01-01T00:00:00+00:00",
                campo_inexistente=1,
            )

    def test_trace_event_type_is_restricted(self):
        with pytest.raises(ValidationError):
            TraceEvent(
                event_id="e1",
                node="tutor",
                event_type="evento_inventado",
                summary="ok",
                started_at="2026-01-01T00:00:00+00:00",
            )

    def test_trace_defaults_and_statuses(self):
        trace = Trace(trace_id="t1", started_at="2026-01-01T00:00:00+00:00")
        assert trace.status == "completed"
        assert trace.events == []
        assert trace.conversation_id is None
        assert trace.error is None


# ---------------------------------------------------------------------------
# Runtime determinístico: eventos reales por nodo (§3.1-§3.7)
# ---------------------------------------------------------------------------


class TestRuntimeTrace:
    async def test_run_study_attaches_trace_with_real_node_events(self):
        repo = FakeStudyRepository()
        _seed_published_document(
            repo, "doc_a", "autor",
            "Una ecuación lineal es una igualdad con incógnitas elevadas a la potencia uno.",
        )
        _seed_published_document(
            repo, "doc_b", "autor",
            "El teorema de Pitágoras relaciona los catetos con la hipotenusa.",
        )
        response = await run_study(
            repo, "¿Qué es una ecuación lineal?", _viewer()
        )

        trace = response.trace
        assert trace is not None
        assert trace.trace_id
        assert trace.status == "completed"
        assert trace.error is None
        assert trace.completed_at is not None
        assert trace.duration_ms is not None and trace.duration_ms > 0

        # Nodos efectivamente ejecutados, en orden real (no se asume que
        # corren todos: aquí corren los cinco del flujo exitoso).
        assert trace.nodes_executed == _NODOS_FLUJO_COMPLETO

        # §3.1 Supervisor: decisiones con próximo nodo y motivo. La
        # topología real: tras el tutor el supervisor enruta al validator y
        # el validator (conditional edge) pasa directo al synthesizer.
        decisiones = _events(trace, "supervisor", "decision")
        assert [ev.details["next_agent"] for ev in decisiones] == [
            "researcher", "tutor", "validator",
        ]
        assert all(ev.details["reason"] for ev in decisiones)

        # §3.2 Researcher: recuperación + filtro con aceptadas/descartadas.
        recuperacion = _events(trace, "researcher", "decision")
        assert len(recuperacion) == 1
        assert recuperacion[0].details["retrieved_chunks"] >= 1
        assert recuperacion[0].details["retrieval_query"]
        filtro = _events(trace, "researcher", "result")
        assert len(filtro) == 1
        assert filtro[0].details["accepted"]
        assert filtro[0].details["final_evidence_count"] == len(response.references)
        for item in filtro[0].details["accepted"]:
            assert item["reason"]

        # §3.3 Tutor: modo determinístico, sin LLM configurado ni invocado.
        tutor_decision = _events(trace, "tutor", "decision")
        assert len(tutor_decision) == 1
        assert tutor_decision[0].details["mode"] == "deterministic_composition"
        assert tutor_decision[0].details["llm_configured"] is False
        assert tutor_decision[0].details["llm_invoked"] is False
        # §3.4: sin evidencia de llamada al LLM (ningún evento de invocación).
        assert _events(trace, "tutor", "result") == []

        # §3.5 Validator: cada validación con su resultado.
        validacion = _events(trace, "validator", "result")
        assert len(validacion) == 1
        assert validacion[0].details["is_valid"] is True
        assert validacion[0].details["checks"]["cited_subset_of_evidence"] == "pass"

        # §3.6 Synthesizer: publicación con estado y referencias.
        publicacion = _events(trace, "synthesizer", "decision")
        assert len(publicacion) == 1
        assert publicacion[0].details["validation_approved"] is True
        assert publicacion[0].details["published_status"] == response.status
        assert publicacion[0].details["references_count"] == len(response.references)

        # §3.7 Cada nodo ejecutado tiene inicio y fin con duración medida.
        for node in trace.nodes_executed:
            starts = _events(trace, node, "node_start")
            ends = _events(trace, node, "node_end")
            assert len(starts) == len(ends) >= 1
            for end in ends:
                assert end.duration_ms is not None and end.duration_ms >= 0
                assert end.completed_at is not None

        # Orden real de la ejecución (inserción = cronología): el supervisor
        # se ejecuta UNA vez por decisión de enrutado (3 veces en este flujo);
        # cada bloque de nodo va en secuencia (inicio → eventos → fin).
        assert [(ev.node, ev.event_type) for ev in trace.events] == [
            ("supervisor", "node_start"),
            ("supervisor", "decision"),
            ("supervisor", "node_end"),
            ("researcher", "node_start"),
            ("researcher", "decision"),
            ("researcher", "result"),
            ("researcher", "node_end"),
            ("supervisor", "node_start"),
            ("supervisor", "decision"),
            ("supervisor", "node_end"),
            ("tutor", "node_start"),
            ("tutor", "decision"),
            ("tutor", "node_end"),
            ("supervisor", "node_start"),
            ("supervisor", "decision"),
            ("supervisor", "node_end"),
            ("validator", "node_start"),
            ("validator", "result"),
            ("validator", "node_end"),
            ("synthesizer", "node_start"),
            ("synthesizer", "decision"),
            ("synthesizer", "node_end"),
        ]

    async def test_bm25_retrieves_but_pertinence_filter_rejects(self):
        """Caso prueba.pdf: fragmentos recuperados por BM25 y descartados
        por pertinencia quedan registrados con su motivo por chunk."""
        repo = FakeStudyRepository()
        _seed_published_document(repo, "doc_mit", "autor", _TEXT_MITOSIS)
        response = await run_study(repo, _QUERY_RECHAZADA, _viewer())

        assert response.status == "insufficient_evidence"
        assert response.references == []
        trace = response.trace
        assert trace is not None
        recuperacion = _events(trace, "researcher", "decision")[0].details
        filtro = _events(trace, "researcher", "result")[0].details
        # BM25 SÍ recuperó fragmentos...
        assert recuperacion["retrieved_chunks"] >= 1
        # ...pero el filtro los rechazó con motivo explícito por chunk.
        assert filtro["accepted"] == []
        assert len(filtro["rejected"]) == recuperacion["retrieved_chunks"]
        for item in filtro["rejected"]:
            assert "Descartado por pertinencia" in item["reason"]
            assert item["matched_terms"] < recuperacion["required_matches"]
        assert filtro["evidence_gaps"] == response.evidence_gaps
        assert filtro["evidence_gaps"]

        # El tutor NO invoca al LLM sin evidencia (§3.4: distinguir
        # "no invocado por falta de evidencia" de una llamada real).
        tutor_decision = _events(trace, "tutor", "decision")[0].details
        assert tutor_decision["mode"] == "insufficient_evidence"
        assert tutor_decision["llm_invoked"] is False
        assert _events(trace, "tutor", "result") == []
        assert _events(trace, "tutor", "error") == []

    async def test_conversation_id_correlates_with_thread(self):
        repo = _seeded_repo()
        saver = InMemorySaver()
        response = await run_study(
            repo, _QUERY_FRACCION, _viewer(),
            checkpointer=saver, thread_id="conv-traza-1",
        )
        assert response.trace is not None
        assert response.trace.conversation_id == "conv-traza-1"


# ---------------------------------------------------------------------------
# LLM real/fake: §3.4 distinción entre invocado, error y no invocado
# ---------------------------------------------------------------------------


class TestLLMTrace:
    async def test_llm_invocation_recorded_with_provider_model_duration(self):
        fake = FakeChatModel(response="Explicación del modelo falso.")
        response = await run_study(
            _seeded_repo(), _QUERY_FRACCION, _viewer(), chat_model=fake
        )
        assert response.status == "complete"
        trace = response.trace
        assert trace is not None
        decision = _events(trace, "tutor", "decision")[0].details
        assert decision["mode"] == "llm"
        assert decision["llm_configured"] is True
        assert decision["llm_invoked"] is True
        invocations = _events(trace, "tutor", "result")
        assert len(invocations) == 1  # exactamente 1 llamada LLM
        call = invocations[0].details
        # Fake sin base_url de Groq y sin model_name: descriptor honesto.
        assert call["provider"] == "openai"
        assert call["model"] == "desconocido"
        assert call["duration_ms"] is not None and call["duration_ms"] >= 0
        # Tokens: solo cuando el proveedor los reporta (aquí no).
        assert call["input_tokens"] is None
        assert call["output_chars"] == len(response.explanation)

    async def test_llm_error_records_error_event_and_fallback(self):
        fake = FakeChatModel(error=RuntimeError("provider down"))
        response = await run_study(
            _seeded_repo(), _QUERY_FRACCION, _viewer(), chat_model=fake
        )
        trace = response.trace
        assert trace is not None
        errors = _events(trace, "tutor", "error")
        assert len(errors) == 1
        assert errors[0].status == "error"
        assert errors[0].error == "RuntimeError"
        assert errors[0].details["duration_ms"] is not None
        # La decisión registra el fallback determinístico.
        decision = _events(trace, "tutor", "decision")[0].details
        assert decision["mode"] == "deterministic_fallback"
        assert decision["llm_invoked"] is True
        # Y la limitación contractual declara la degradación.
        assert response.limitations

    async def test_endpoint_delivers_trace_in_payload(self):
        repo = _seeded_repo()
        user = _user("usr_stu", "student")
        repo.users[user.user_id] = user
        repo.users_by_email[user.email] = user
        token = _session_token(repo, user)
        with _client_with_model(repo, FakeChatModel()) as client:
            response = client.post(
                "/api/study",
                json={"user_request": _QUERY_FRACCION},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert response.status_code == 200
        payload = response.json()
        trace = payload["trace"]
        assert trace["status"] == "completed"
        assert trace["trace_id"]
        assert trace["nodes_executed"] == _NODOS_FLUJO_COMPLETO
        # El contrato de la UI valida el payload con la traza incluida
        # (campo aditivo: retrocompatible con clientes viejos).
        parsed = StudyResponse.model_validate(payload)
        assert parsed.trace is not None
        assert parsed.trace.trace_id == trace["trace_id"]
        # La llamada al LLM del tutor quedó registrada exactamente 1 vez.
        llamadas = _events(parsed.trace, "tutor", "result")
        assert len(llamadas) == 1
        assert llamadas[0].details["model"] == "desconocido"

    def test_study_response_without_trace_stays_compatible(self):
        payload = {
            "status": "complete",
            "topic": "general",
            "explanation": "Texto",
            "references": [],
        }
        parsed = StudyResponse.model_validate(payload)
        assert parsed.trace is None


# ---------------------------------------------------------------------------
# Construcción de la traza (§3.7): fallos y derivación de nodos ejecutados
# ---------------------------------------------------------------------------


class TestTraceAssembly:
    def test_nodes_executed_derived_from_real_node_start_events(self):
        events = [
            new_event("supervisor", "node_start", "Inicio de supervisor."),
            new_event("supervisor", "decision", "Enrutado a researcher."),
            new_event("supervisor", "node_end", "supervisor completado."),
            new_event("researcher", "node_start", "Inicio de researcher."),
        ]
        trace = _build_trace(
            "t1", None, events[0]["started_at"], 0.0,
            events, status="failed", error="RuntimeError",
        )
        assert trace.nodes_executed == ["supervisor", "researcher"]
        assert trace.status == "failed"
        assert trace.error == "RuntimeError"
        assert [event.event_type for event in trace.events] == [
            "node_start", "decision", "node_end", "node_start",
        ]

    def test_trace_survives_no_events(self):
        trace = _build_trace(
            "t1", "conv-1", "2026-01-01T00:00:00+00:00", 0.0,
            [], status="completed", error=None,
        )
        assert trace.events == []
        assert trace.nodes_executed == []
        assert trace.conversation_id == "conv-1"


# ---------------------------------------------------------------------------
# Phoenix (§6): spans reales compartiendo el trace_id
# ---------------------------------------------------------------------------


class TestPhoenix:
    def test_phoenix_disabled_by_default(self):
        settings = Settings(database_url="postgresql+asyncpg://fake", _env_file=None)
        assert settings.ada_phoenix_enabled is False
        assert settings.ada_phoenix_endpoint == "http://localhost:6006/v1/traces"

    async def test_spans_share_ada_trace_id(self):
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )

        from ada_study.observability.phoenix import init_phoenix

        settings = Settings(
            database_url="postgresql+asyncpg://fake",
            ada_phoenix_enabled=True,
            _env_file=None,
        )
        exporter = InMemorySpanExporter()
        init_phoenix(settings, span_exporter=exporter)

        response = await run_study(_seeded_repo(), _QUERY_FRACCION, _viewer())
        assert response.trace is not None

        spans = exporter.get_finished_spans()
        # La ejecución real produjo spans (nodos LangGraph y/o LLM).
        assert spans
        # Correlación: el trace_id estructurado viaja en los atributos con la
        # clave de metadata `ada_trace_id` (identificadores compartidos).
        attributes_blob = json.dumps(
            [dict(span.attributes or {}) for span in spans], default=str
        )
        assert "ada_trace_id" in attributes_blob
        assert response.trace.trace_id in attributes_blob



