"""RAG-QUALITY-05: control de pertinencia temática y suficiencia.

Casos de la tarea, con corpus sintético y fakes (sin LLM real):
- A: la consulta exacta de la prueba funcional ("Explicame la fotosíntesis
  utilizando exclusivamente el material de Historia y Matemática que tenés
  disponible") termina en insuficiencia de evidencia, SIN invocar al LLM,
  aunque el corpus traiga encabezados con "Historia"/"Matemática"/"material".
- B: la evidencia debe demostrar relación con el TEMA CENTRAL: un fragmento
  pertinente al tema se acepta aunque la consulta traiga restricción de
  fuentes (anti falso negativo).
- C: los nombres de documentos y sustantivos de fuente NO acreditan
  pertinencia (unitaria sobre `_source_restriction_terms`).
- D: cláusula de restricción acotada: el tema después de una coma sigue
  siendo tema central ("Utilizando el material de Historia, explicame la
  mitosis").
- E: regresiones — consultas sin restricción de fuentes quedan intactas.
"""

from types import SimpleNamespace

from ada_study.contracts.documents import ChunkRecord
from ada_study.graph.researcher import _source_restriction_terms, make_researcher_node
from ada_study.index.bm25_index import BM25Index
from ada_study.index.retriever import HybridRetriever, ViewerContext
from ada_study.study import run_study
from tests.test_study_endpoint import FakeStudyRepository, _document, _version
from tests.test_study_llm import FakeChatModel

_QUERY_RQ05 = (
    "Explicame la fotosíntesis utilizando exclusivamente el material de "
    "Historia y Matemática que tenés disponible."
)


# ---------------------------------------------------------------------------
# Dobles y corpus sintético (espejo del caso real: guías con encabezados)
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


def _corpus_guias(extra: list[ChunkRecord] | None = None) -> list[ChunkRecord]:
    """Guías de Historia y Matemática SIN fotosíntesis: cada fragmento repite
    el encabezado de su guía y contiene «material» disperso en actividades,
    como el corpus real donde falló la prueba funcional."""
    contenidos = [
        (
            "doc_hist",
            "Historia",
            "La colonización española organizó virreinatos y llegó material didáctico.",
        ),
        ("doc_hist", "Historia", "La Revolución Francesa proclamó los derechos del hombre."),
        (
            "doc_hist",
            "Historia",
            "Diferenciá una fuente escrita de una material y escribí ejemplos.",
        ),
        ("doc_mat", "Matemática", "Las fracciones representan las partes iguales de un todo."),
        ("doc_mat", "Matemática", "El perímetro de un cuadrado es cuatro veces su lado."),
        ("doc_mat", "Matemática", "Los números naturales sirven para contar objetos del material."),
    ]
    chunks = [
        _chunk(
            f"chk_g_{i}",
            f"Material de {titulo} 5.º grado. {contenido}",
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


class _CatalogoVisible:
    async def get_document(self, document_id):
        return _document(document_id, "autor")

    async def get_version(self, version_id):
        return _version(version_id.replace("ver_", ""))

    async def list_chunks(self, version_id):
        return []


def _make_retriever(chunks: list[ChunkRecord]) -> HybridRetriever:
    retriever = HybridRetriever(_CatalogoVisible(), BM25Index.rebuild(chunks))
    retriever.load_chunks(chunks)
    return retriever


# ---------------------------------------------------------------------------
# Caso A: la consulta exacta termina en insuficiencia, sin LLM
# ---------------------------------------------------------------------------


async def test_a_consulta_con_restriccion_de_fuentes_termina_en_insuficiencia():
    """Los fragmentos que solo comparten 'material' + nombre de documento NO
    acreditan pertinencia: insuficiencia explícita y cero llamadas al LLM."""
    repo = _repo_with_chunks(_corpus_guias())
    fake = FakeChatModel()
    response = await run_study(repo, _QUERY_RQ05, _viewer(), chat_model=fake)
    assert response.status == "insufficient_evidence"
    assert response.references == []
    assert response.evidence_gaps
    assert fake.calls == []  # el Tutor NUNCA invoca a Groq


async def test_a_researcher_rechaza_los_fragmentos_solo_documentales():
    retriever = _make_retriever(_corpus_guias())
    update = await make_researcher_node(retriever)(
        {"user_request": _QUERY_RQ05, "steps": 0, "retries": 0}
    )
    artifact = update["research_artifact"]
    assert artifact["evidence"] == []
    assert artifact["confidence_score"] == 0.0
    # La traza registra la clasificación de la consulta.
    decision = next(e for e in update["trace_events"] if e["event_type"] == "decision")
    assert {"material", "Historia", "Matemática", "tenés", "disponible"} <= set(
        decision["details"]["source_restriction_terms"]
    )
    assert decision["details"]["thematic_terms"] == ["fotosíntesis"]


# ---------------------------------------------------------------------------
# Caso B: el tema central con evidencia real se acepta (anti falso negativo)
# ---------------------------------------------------------------------------

_FOTOSINTESIS = (
    "La fotosíntesis es el proceso por el cual las plantas convierten la luz "
    "solar en energía química."
)


async def test_b_tema_central_con_evidencia_real_se_acepta():
    """La restricción de fuentes no bloquea: si existe contenido pertinente al
    tema central, se acepta aunque la consulta restrinja fuentes."""
    extra = [
        _chunk(
            "chk_foto",
            f"Material de Ciencias naturales. {_FOTOSINTESIS}",
            "Ciencias naturales",
            "doc_cie",
            page=1,
            index=6,
        ),
    ]
    repo = _repo_with_chunks(_corpus_guias(extra))
    response = await run_study(repo, _QUERY_RQ05, _viewer())
    assert {ref.chunk_id for ref in response.references} == {"chk_foto"}
    assert response.status == "complete"


# ---------------------------------------------------------------------------
# Caso C: clasificación determinística de la restricción
# ---------------------------------------------------------------------------


def test_c_restriccion_detecta_la_consulta_del_caso():
    restriction = _source_restriction_terms(_QUERY_RQ05)
    assert {"material", "Historia", "Matemática", "tenés", "disponible"} <= set(restriction)
    assert "fotosíntesis" not in restriction


def test_c_utilizando_es_marcador_conversacional():
    """§4.1 de la tarea: 'utilizando' enmarca el pedido (como 'usando') y
    NUNCA acredita pertinencia temática."""
    from ada_study.graph.researcher import _is_discourse_marker

    assert _is_discourse_marker("utilizando")
    assert _is_discourse_marker("usando")
    assert not _is_discourse_marker("fotosíntesis")


def test_c_nombres_de_documento_no_son_tema():
    restriction = _source_restriction_terms(
        "Trabajá exclusivamente con los apuntes de Biología que tengas."
    )
    assert "Biología" in restriction
    assert "exclusivamente" in restriction


def test_c_consulta_sin_restriccion_devuelve_vacio():
    assert _source_restriction_terms("¿Qué estudia la Historia?") == frozenset()
    assert _source_restriction_terms("Dame otro ejemplo") == frozenset()
    assert (
        _source_restriction_terms("¿Cuáles fueron las causas de la Revolución de Mayo?")
        == frozenset()
    )


def test_c_solo_no_basta_para_restringir():
    """'solo' restringe la SALIDA, no las fuentes: sin sustantivo de fuente ni
    disponibilidad, no hay restricción y 'mitosis' sigue siendo el tema."""
    assert _source_restriction_terms("Dame solo la definición de mitosis") == frozenset()


# ---------------------------------------------------------------------------
# Caso D: la cláusula de restricción está acotada por puntuación
# ---------------------------------------------------------------------------


async def test_d_tema_despues_de_la_coma_sigue_siendo_tema_central():
    """'Utilizando el material de Historia, explicame la mitosis': la
    restricción no atraviesa la coma y 'mitosis' conserva su umbral."""
    restriction = _source_restriction_terms(
        "Utilizando el material de Historia, explicame la mitosis."
    )
    assert {"material", "Historia"} <= set(restriction)
    assert "mitosis" not in restriction

    chunks = [
        _chunk(
            "chk_mito",
            "Material de Ciencias naturales. La mitosis es la división celular "
            "en la que una célula produce dos células idénticas.",
            "Ciencias naturales",
            "doc_cie",
            page=1,
            index=0,
        ),
    ]
    retriever = _make_retriever(chunks)
    update = await make_researcher_node(retriever)(
        {
            "user_request": "Utilizando el material de Historia, explicame la mitosis.",
            "steps": 0,
            "retries": 0,
        }
    )
    artifact = update["research_artifact"]
    assert {ref["chunk_id"] for ref in artifact["evidence"]} == {"chk_mito"}


# ---------------------------------------------------------------------------
# Caso E: regresiones
# ---------------------------------------------------------------------------


async def test_e_consulta_cuyo_tema_es_la_fuente_no_queda_sin_criterio():
    """Anti falso negativo: 'explicame el material de Historia' no puede
    quedar sin términos objetivo (fallback al comportamiento anterior)."""
    retriever = _make_retriever(_corpus_guias())
    update = await make_researcher_node(retriever)(
        {"user_request": "Explicame el material de Historia.", "steps": 0, "retries": 0}
    )
    decision = next(e for e in update["trace_events"] if e["event_type"] == "decision")
    assert decision["details"]["target_terms"]  # nunca vacío
    assert "historia" in [
        t.lower() for t in decision["details"]["target_terms"]
    ]


async def test_e_fotosintesis_con_restriccion_sin_evidencia_sigue_igual():
    """La consulta de RAG-QUALITY-03 (restricción de fuente, sin evidencia)
    sigue terminando en insuficiencia, sin LLM."""
    repo = _repo_with_chunks(_corpus_guias())
    fake = FakeChatModel()
    response = await run_study(
        repo,
        "Explicame la fotosíntesis usando exclusivamente el material de "
        "Historia de 5.º grado.",
        _viewer(),
        chat_model=fake,
    )
    assert response.status == "insufficient_evidence"
    assert fake.calls == []


def test_e_restriccion_no_interfiere_con_turnos_de_la_ui():
    state = {}
    response = SimpleNamespace(conversation_id=None)
    from ada_study.ui.pages.study import register_turn

    register_turn(state, _QUERY_RQ05, response)
    assert state == {}

