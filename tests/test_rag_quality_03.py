"""RAG-QUALITY-03: pertinencia temática, contexto conversacional y UI.

Casos A–G de la tarea, con corpus sintéticos y fakes (sin LLM real, sin
PostgreSQL, sin escrituras):
- A: tema sin evidencia (fotosíntesis) contra corpus con encabezados
  repetidos «Material de Historia 5.º grado» → insuficiencia, sin LLM.
- B: consulta de Revolución de Mayo con evidencia pertinente → complete.
- C: consulta genérica válida ("¿Qué estudia la Historia?") no queda
  bloqueada, ni dependiendo de términos de baja frecuencia exclusivamente.
- D: fotosíntesis con evidencia real → se acepta la pertinente y se rechaza
  la irrelevante (los encabezados no producen suficiencia falsa).
- E: contexto conversacional (seguimiento anafórico, cambio de tema,
  recuperación nueva en cada turno, sin contaminación del tema previo).
- F: UI — sin conversation_id no se acumulan turnos ficticios.
- G: regresión — sin evidencia no hay LLM; validación formal intacta.
"""

from types import SimpleNamespace

from ada_study.contracts.documents import ChunkRecord
from ada_study.graph.researcher import (
    _is_discourse_marker,
    _split_thematic_terms,
    make_researcher_node,
)
from ada_study.index.bm25_index import BM25Index
from ada_study.index.retriever import HybridRetriever, ViewerContext
from ada_study.study import run_study
from ada_study.ui.pages.study import CONVERSATION_KEY, TURNS_KEY, register_turn
from tests.test_study_endpoint import FakeStudyRepository, _document, _version
from tests.test_study_llm import FakeChatModel

_HEADER = "Material de Historia 5.º grado"
_QUERY_FOTOSINTESIS = (
    "Explicame la fotosíntesis usando exclusivamente el material de "
    "Historia de 5.º grado."
)


# ---------------------------------------------------------------------------
# Dobles y corpus sintético
# ---------------------------------------------------------------------------


def _chunk(
    chunk_id: str,
    text: str,
    title: str,
    document_id: str,
    page: int,
    index: int,
) -> ChunkRecord:
    return ChunkRecord(
        chunk_id=chunk_id,
        document_id=document_id,
        version_id=f"ver_{document_id}",
        page=page,
        chunk_index=index,
        content_hash="3" * 64,
        text=text,
        title=title,
        source="pdf",
    )


def _corpus_con_encabezados(extra: list[ChunkRecord] | None = None) -> list[ChunkRecord]:
    """Historia y Matemática SIN contenido sobre fotosíntesis; cada fragmento
    repite el encabezado «Material de Historia 5.º grado» (fuente de los
    falsos positivos diagnosticados en RAG-QUALITY-02)."""
    contenidos = [
        ("doc_hist", "Historia", "La colonización española de América se organizó en virreinatos."),
        ("doc_hist", "Historia", "La Revolución Francesa proclamó los derechos del hombre."),
        ("doc_hist", "Historia", "La conquista del espacio comenzó en la década de 1960."),
        ("doc_mat", "Matemática", "Las fracciones representan las partes iguales de un todo."),
        ("doc_mat", "Matemática", "El perímetro de un cuadrado es cuatro veces su lado."),
        ("doc_mat", "Matemática", "Los números naturales sirven para contar objetos."),
    ]
    chunks = [
        _chunk(
            f"chk_base_{i}",
            f"{_HEADER}. {contenido}",
            titulo,
            document_id=doc,
            page=i + 1,
            index=i,
        )
        for i, (doc, titulo, contenido) in enumerate(contenidos)
    ]
    chunks.extend(extra or [])
    return chunks


def _repo_with_chunks(chunks: list[ChunkRecord]) -> FakeStudyRepository:
    """FakeStudyRepository con documento publicado + versión por version_id."""
    repo = FakeStudyRepository()
    versions: dict[str, list[ChunkRecord]] = {}
    for chunk in chunks:
        versions.setdefault(chunk.version_id, []).append(chunk)
    for version_id, version_chunks in versions.items():
        document_id = version_chunks[0].document_id
        repo.documents[document_id] = _document(document_id, "autor")
        repo.versions[document_id] = [_version(document_id)]
        repo.chunks[version_id] = version_chunks
    return repo


def _viewer() -> ViewerContext:
    return ViewerContext(user_id="usr_1", role="student")


class _RetrieverGrabador:
    """Envuelve un HybridRetriever real y registra cada consulta (E)."""

    def __init__(self, retriever: HybridRetriever) -> None:
        self._retriever = retriever
        self.queries: list[str] = []

    async def search(self, query: str, top_k: int = 5):
        self.queries.append(query)
        return await self._retriever.search(query, top_k=top_k)

    def term_document_frequency(self, term: str) -> int:
        return self._retriever.term_document_frequency(term)

    def indexed_chunk_count(self) -> int:
        return self._retriever.indexed_chunk_count()


class _CatalogoVisible:
    """Catálogo doble: todo documento publicado con versión disponible."""

    async def get_document(self, document_id):
        return _document(document_id, "autor")

    async def get_version(self, version_id):
        return _version(version_id.replace("ver_", ""))

    async def list_chunks(self, version_id):
        return []


def _make_grabador(chunks: list[ChunkRecord]) -> _RetrieverGrabador:
    index = BM25Index.rebuild(chunks)
    retriever = HybridRetriever(_CatalogoVisible(), index)
    retriever.load_chunks(chunks)
    return _RetrieverGrabador(retriever)


# ---------------------------------------------------------------------------
# Caso A: fotosíntesis sin evidencia → insuficiencia, sin LLM
# ---------------------------------------------------------------------------


async def test_a_fotosintesis_sin_evidencia_es_insuficiencia_sin_llm():
    """Encabezados repetidos y restricciones de fuente NO acreditan pertinencia
    temática: sin contenido sobre fotosíntesis, la respuesta es insuficiencia
    explícita y el LLM NUNCA se invoca."""
    repo = _repo_with_chunks(_corpus_con_encabezados())
    fake = FakeChatModel()
    response = await run_study(repo, _QUERY_FOTOSINTESIS, _viewer(), chat_model=fake)
    assert response.status == "insufficient_evidence"
    assert response.references == []
    assert response.evidence_gaps
    assert fake.calls == []  # sin evidencia no hay llamada al proveedor


async def test_a_researcher_rechaza_todos_los_fragmentos_por_encabezado():
    """Ningún fragmento irrelevante se acepta: los términos del encabezado
    repetido y de la restricción de fuente son genéricos por frecuencia."""
    retriever = _make_grabador(_corpus_con_encabezados())
    update = await make_researcher_node(retriever)(
        {"user_request": _QUERY_FOTOSINTESIS, "steps": 0, "retries": 0}
    )
    artifact = update["research_artifact"]
    assert artifact["evidence"] == []
    assert artifact["evidence_gaps"]
    assert artifact["confidence_score"] == 0.0


# ---------------------------------------------------------------------------
# Caso B: causas de la Revolución de Mayo sigue funcionando
# ---------------------------------------------------------------------------


_REVOLUCION_1 = (
    "Las causas de la Revolución de Mayo incluyen la invasión napoleónica a "
    "España y la cautividad del rey."
)
_REVOLUCION_2 = (
    "La Revolución de Mayo de 1810 instituyó la Primera Junta de gobierno."
)
_QUERY_REVOLUCION = "¿Cuáles fueron las causas de la Revolución de Mayo?"


async def test_b_causas_revolucion_mayo_con_evidencia_pertinente():
    extra = [
        _chunk("chk_rev_1", _REVOLUCION_1, "Historia", "doc_hist", page=4, index=6),
        _chunk("chk_rev_2", _REVOLUCION_2, "Historia", "doc_hist", page=5, index=7),
    ]
    repo = _repo_with_chunks(_corpus_con_encabezados(extra))
    response = await run_study(repo, _QUERY_REVOLUCION, _viewer())
    assert response.status == "complete"
    assert {ref.chunk_id for ref in response.references} == {"chk_rev_1", "chk_rev_2"}


# ---------------------------------------------------------------------------
# Caso C: consulta genérica válida no queda bloqueada
# ---------------------------------------------------------------------------


_DEFINICION = (
    "La Historia es la ciencia que estudia las sociedades humanas a través "
    "del tiempo."
)
_QUERY_ESTUDIA_HISTORIA = "¿Qué estudia la Historia?"


async def test_c_consulta_generica_con_definicion_pertinente():
    """La definición que contiene 'estudia' + 'historia' se acepta; los
    fragmentos que solo coinciden por el encabezado repetido, no."""
    extra = [
        _chunk("chk_def", f"{_HEADER}. {_DEFINICION}", "Historia", "doc_hist", page=4, index=6),
    ]
    repo = _repo_with_chunks(_corpus_con_encabezados(extra))
    response = await run_study(repo, _QUERY_ESTUDIA_HISTORIA, _viewer())
    assert response.references
    assert {ref.chunk_id for ref in response.references} == {"chk_def"}


async def test_c_consulta_con_termino_ubicuo_no_queda_bloqueada():
    """Anti falso negativo: si NINGÚN término de la consulta es discriminativo
    (todos ubicuos en el corpus), se conserva el umbral original y la consulta
    legítima NO se bloquea."""
    contenidos = [
        "La Historia estudia las sociedades humanas.",
        "La Historia estudia el pasado de los pueblos.",
        "La Historia estudia los cambios y las épocas.",
        "Las fracciones representan partes iguales del todo.",
        "El perímetro de un cuadrado es cuatro veces su lado.",
        "Los números naturales sirven para contar objetos.",
    ]
    chunks = [
        _chunk(f"chk_u{i}", f"{_HEADER}. {texto}", "Historia", "doc_hist", page=i + 1, index=i)
        for i, texto in enumerate(contenidos)
    ]
    retriever = _make_grabador(chunks)
    update = await make_researcher_node(retriever)(
        {"user_request": _QUERY_ESTUDIA_HISTORIA, "steps": 0, "retries": 0}
    )
    artifact = update["research_artifact"]
    # 'historia' y 'estudia' resultan genéricos (df=6/6 y 3/6, df>=3): sin
    # término temático, el umbral original conserva la evidencia compartida.
    assert artifact["evidence"]
    assert {ref["chunk_id"] for ref in artifact["evidence"]} <= {"chk_u0", "chk_u1", "chk_u2"}


# ---------------------------------------------------------------------------
# Caso D: fotosíntesis con evidencia real
# ---------------------------------------------------------------------------


_FOTOSINTESIS = (
    "La fotosíntesis es el proceso por el cual las plantas convierten la luz "
    "solar en energía química."
)


async def test_d_fotosintesis_con_evidencia_real_acepta_pertinente():
    extra = [
        _chunk(
            "chk_foto",
            f"{_HEADER}. {_FOTOSINTESIS}",
            "Ciencias naturales",
            "doc_cie",
            page=1,
            index=6,
        ),
    ]
    repo = _repo_with_chunks(_corpus_con_encabezados(extra))
    response = await run_study(repo, "Explicame la fotosíntesis.", _viewer())
    assert {ref.chunk_id for ref in response.references} == {"chk_foto"}


# ---------------------------------------------------------------------------
# Caso E: contexto conversacional
# ---------------------------------------------------------------------------


def _corpus_conversacional() -> list[ChunkRecord]:
    otros = [
        "Las fracciones representan las partes iguales de un todo dividido.",
        "Los números naturales sirven para contar objetos completos.",
        "El perímetro de un cuadrado es cuatro veces la medida de su lado.",
    ]
    return [
        _chunk("chk_rev_1", _REVOLUCION_1, "Historia", "doc_hist", page=1, index=0),
        _chunk("chk_rev_2", _REVOLUCION_2, "Historia", "doc_hist", page=2, index=1),
        _chunk("chk_foto", _FOTOSINTESIS, "Ciencias naturales", "doc_cie", page=1, index=2),
        *[
            _chunk(f"chk_mat_{i}", texto, "Matemática", "doc_mat", page=i + 1, index=i + 3)
            for i, texto in enumerate(otros)
        ],
    ]


def _historial_con_tema(topic: str) -> list[dict]:
    return [
        {"role": "student", "content": "Explicame la Revolución de Mayo"},
        {"role": "ada", "content": "respuesta previa", "topic": topic},
    ]


async def test_e_seguimiento_anaforo_conserva_el_tema_previo():
    """'Dame otro ejemplo' (anáfora pura, sin términos temáticos propios)
    hereda el tema previo: recupera Revolución de Mayo, no otra cosa."""
    retriever = _make_grabador(_corpus_conversacional())
    update = await make_researcher_node(retriever)(
        {
            "user_request": "Dame otro ejemplo",
            "conversation_history": _historial_con_tema("Revolución de Mayo"),
            "steps": 0,
            "retries": 0,
        }
    )
    artifact = update["research_artifact"]
    # Recuperación NUEVA con la consulta reformulada (tema previo añadido).
    assert retriever.queries == ["Dame otro ejemplo Revolución de Mayo"]
    ids = {ref["chunk_id"] for ref in artifact["evidence"]}
    assert ids == {"chk_rev_1", "chk_rev_2"}


async def test_e_cambio_de_tema_prevalece_sobre_el_contexto():
    """'Ahora explicame la fotosíntesis' tras un tema de Revolución: el tema
    explícito del turno actual tiene prioridad y el previo no contamina."""
    retriever = _make_grabador(_corpus_conversacional())
    update = await make_researcher_node(retriever)(
        {
            "user_request": "Ahora explicame la fotosíntesis",
            "conversation_history": _historial_con_tema("Revolución de Mayo"),
            "steps": 0,
            "retries": 0,
        }
    )
    artifact = update["research_artifact"]
    # La búsqueda reformulada conserva el sentido y añade el tema previo...
    assert retriever.queries[-1].endswith("Revolución de Mayo")
    assert retriever.queries[-1].startswith("Ahora explicame la fotosíntesis")
    # ...pero la pertinencia la decide SOLO el tema explícito nuevo.
    ids = {ref["chunk_id"] for ref in artifact["evidence"]}
    assert ids == {"chk_foto"}


async def test_e_cambio_de_tema_sin_evidencia_no_recicla_el_tema_previo():
    """Sin contenido del tema nuevo, el tema anterior NO produce evidencia
    (no hay contaminación del contexto previo)."""
    chunks = [c for c in _corpus_conversacional() if c.chunk_id != "chk_foto"]
    retriever = _make_grabador(chunks)
    update = await make_researcher_node(retriever)(
        {
            "user_request": "Ahora explicame la fotosíntesis",
            "conversation_history": _historial_con_tema("Revolución de Mayo"),
            "steps": 0,
            "retries": 0,
        }
    )
    artifact = update["research_artifact"]
    assert artifact["evidence"] == []
    assert artifact["evidence_gaps"]


async def test_e_recuperacion_nueva_en_cada_turno():
    retriever = _make_grabador(_corpus_conversacional())
    node = make_researcher_node(retriever)
    await node({"user_request": "Explicame la Revolución de Mayo", "steps": 0, "retries": 0})
    await node(
        {
            "user_request": "Dame otro ejemplo",
            "conversation_history": _historial_con_tema("Revolución de Mayo"),
            "steps": 0,
            "retries": 0,
        }
    )
    # Dos búsquedas independientes (nunca reutiliza referencias del turno).
    assert retriever.queries == [
        "Explicame la Revolución de Mayo",
        "Dame otro ejemplo Revolución de Mayo",
    ]


# ---------------------------------------------------------------------------
# Unitarias: marcadores discursivos y términos genéricos por frecuencia
# ---------------------------------------------------------------------------


def test_marcadores_discursivos_identificados():
    for term in (
        "explicame", "explicámelo", "dame", "otro", "otros", "otra",
        "ejemplo", "ejemplos", "usando", "exclusivamente", "ahora",
        "anterior", "mismo", "esa",
    ):
        assert _is_discourse_marker(term), term
    for term in ("fotosíntesis", "historia", "revolución", "mitocondria", "causas"):
        assert not _is_discourse_marker(term), term


def test_terminos_genericos_por_frecuencia_en_corpus():
    df = {"historia": 32, "fotosíntesis": 0, "grado": 27}
    thematic, generic = _split_thematic_terms(
        ["historia", "fotosíntesis", "grado"], df.get, 54
    )
    assert thematic == ["fotosíntesis"]
    assert set(generic) == {"historia", "grado"}


def test_termino_en_corpus_chico_no_es_generico():
    """Anti falso negativo: un término presente en 1 de 2 chunks NO es
    genérico (el piso absoluto de df evita descuentos sin significancia)."""
    df = {"energia": 1, "mitocondria": 1}
    thematic, generic = _split_thematic_terms(["energia", "mitocondria"], df.get, 2)
    assert generic == []
    assert set(thematic) == {"energia", "mitocondria"}


def test_sin_estadisticas_todos_los_terminos_son_tematicos():
    thematic, generic = _split_thematic_terms(["a", "b"], None, 0)
    assert generic == []
    assert thematic == ["a", "b"]


# ---------------------------------------------------------------------------
# Caso F: UI sin historial ficticio
# ---------------------------------------------------------------------------


def test_f_sin_conversation_id_no_acumula_turnos_ni_conversacion():
    state = {}
    response = SimpleNamespace(conversation_id=None)
    register_turn(state, "¿Qué es la fotosíntesis?", response)
    assert state == {}  # sin historial ficticio ni conversation_id


def test_f_con_conversation_id_conserva_el_comportamiento():
    state = {}
    response = SimpleNamespace(conversation_id="conv-1")
    register_turn(state, "¿Qué es la fotosíntesis?", response)
    assert state[CONVERSATION_KEY] == "conv-1"
    turns = state[TURNS_KEY]
    assert [turn["pregunta"] for turn in turns] == ["¿Qué es la fotosíntesis?"]
    assert turns[0]["respuesta"] is response


# ---------------------------------------------------------------------------
# Caso G: regresión
# ---------------------------------------------------------------------------


def test_g_validador_sigue_exigiendo_referencias_para_complete():
    """La validación formal de citas no se altera: 'complete' sin referencias
    sigue siendo inválido."""
    from ada_study.graph.validator import validator_node

    research = {"query": "x", "evidence": [], "evidence_gaps": [], "confidence_score": 0.0}
    response = {
        "status": "complete",
        "topic": "x",
        "explanation": "Explicación.",
        "references": [],
        "evidence_gaps": [],
        "limitations": [],
    }
    update = validator_node(
        {"research_artifact": research, "study_response": response, "retries": 0}
    )
    assert update["validation_result"]["is_valid"] is False


def test_g_bm25_index_expone_frecuencia_de_documento():
    index = BM25Index.rebuild(_corpus_conversacional())
    assert index.chunk_count() == 6
    assert index.document_frequency("revolución") == 2
    assert index.document_frequency("fotosíntesis") == 1
    assert index.document_frequency("palabra-ausente") == 0




