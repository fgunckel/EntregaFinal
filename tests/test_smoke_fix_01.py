"""SMOKE-FIX-01: regresiones del smoke test de ADA Study.

§3 Recuperación de Historia: la consulta «historia de quinto grado» sobre la
guía publicada terminaba en `insufficient_evidence` aunque BM25 recuperaba
exactamente sus fragmentos. Causa: "quinto" (el PDF escribe «5.º»/«5to») tenía
df=0 pero contaba como temático e inflaba el umbral min(2, |temáticos|) a una
cantidad de coincidencias ESTRUCTURALMENTE imposible. La corrección excluye
los ordinales de grado del cómputo de pertinencia: califican metadatos del
material, nunca el contenido. La RECUPERACIÓN BM25 no cambia y cada fragmento
sigue exigiendo compartir un término temático real (RAG-QUALITY-03/05).

Corpus sintético espejo del real: guías con encabezado «… 5.º grado»
repetido en cada fragmento (por eso "grado" cae bajo la regla de genericidad
de RAG-QUALITY-03 y "historia" queda temático).
"""

from ada_study.contracts.documents import ChunkRecord
from ada_study.graph.researcher import _is_ordinal_term, make_researcher_node
from ada_study.index.bm25_index import BM25Index
from ada_study.index.retriever import HybridRetriever
from tests.test_study_endpoint import _document, _version

_CONSULTA = "historia de quinto grado"


def _chunk(
    chunk_id: str, text: str, title: str, document_id: str, page: int, index: int
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
def _corpus_guias() -> list[ChunkRecord]:
    """Espejo del corpus real: encabezado de guía repetido por fragmento.

    La guía escribe el grado como «5.º grado» (igual que el PDF real); la
    consulta «historia de quinto grado» usa la palabra «quinto», que NO
    existe en el corpus (df=0) — el patrón exacto del caso. Con 5 fragmentos
    el encabezado "grado" (5/5) cae bajo la genericidad de RAG-QUALITY-03.
    """
    contenidos = [
        ("doc_hist", "Historia", "La Revolución de Mayo cambió el gobierno."),
        ("doc_hist", "Historia", "Las invasiones inglesas mostraron organización local."),
        ("doc_hist", "Historia", "Los cabildos abiertos debatieron el destino del virreinato."),
        ("doc_mat", "Matemática", "Las fracciones representan partes iguales de un todo."),
        ("doc_mat", "Matemática", "El perímetro de un cuadrado es cuatro veces su lado."),
        ("doc_mat", "Matemática", "Los números naturales sirven para contar objetos."),
        ("doc_mat", "Matemática", "La suma de enteros respeta el orden de los números."),
        ("doc_mat", "Matemática", "Las tablas de multiplicar organizan productos sencillos."),
    ]
    return [
        _chunk(
            f"chk_sf_{i}",
            f"{titulo} 5.º grado GUÍA COMPLETA. {contenido}",
            titulo,
            document_id=doc,
            page=i + 1,
            index=i,
        )
        for i, (doc, titulo, contenido) in enumerate(contenidos)
    ]


class _CatalogoVisible:
    """Catálogo mínimo: todo documento publicado con versión disponible."""

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


def test_ordinales_de_grado_clasificados():
    """Los ordinales califican metadatos; los temas reales, no.

    Incluye las formas abreviadas (RAG-RUNTIME-01): «5to» es la grafía que
    usa el propio PDF («Historia 5to grado»), tokenizada como un solo token.
    """
    for term in (
        "quinto", "Quinta", "segundos", "primer", "décimo",
        "5to", "5ta", "2do", "2da", "7mo", "1er", "5tos", "6to", "10mo",
    ):
        assert _is_ordinal_term(term), term
    for term in ("historia", "mitosis", "fotosíntesis", "quintaesencia", "1810", "5"):
        assert not _is_ordinal_term(term), term


async def test_consulta_de_documento_con_ordinal_recupera_su_guia():
    """Caso SMOKE-FIX-01: «historia de quinto grado» recupera la guía.

    BM25 ya devolvía los fragmentos correctos; con los ordinales excluidos
    del cómputo, el umbral deja de ser inalcanzable y las referencias son
    del documento exactamente nombrado.
    """
    update = await make_researcher_node(_make_retriever(_corpus_guias()))(
        {"user_request": _CONSULTA, "steps": 0, "retries": 0}
    )
    artifact = update["research_artifact"]
    assert artifact["evidence"], "la guía pedida debe producir evidencia"
    assert all(ref["document_id"] == "doc_hist" for ref in artifact["evidence"])
    assert artifact["evidence_gaps"] == []


async def test_consulta_con_ordinal_abreviado_recupera_su_guia():
    """Regresión RAG-RUNTIME-01: «historia de 5to grado» también recupera.

    «5to» es la grafía que usa el propio PDF; sin la detección de formas
    abreviadas el ordinal volvía a inflar el umbral (df=0 → temático →
    required=2 → 0 aceptadas). Con la detección ampliada queda documentado
    en la traza como ordinal y el umbral vuelve a 1.
    """
    update = await make_researcher_node(_make_retriever(_corpus_guias()))(
        {"user_request": "historia de 5to grado", "steps": 0, "retries": 0}
    )
    artifact = update["research_artifact"]
    assert artifact["evidence"], "la guía pedida (5to) debe producir evidencia"
    assert all(ref["document_id"] == "doc_hist" for ref in artifact["evidence"])
    assert artifact["evidence_gaps"] == []
    decision = next(
        e for e in update["trace_events"] if e["event_type"] == "decision"
    )
    details = decision["details"]
    assert details["ordinal_terms"] == ["5to"]
    assert details["required_matches"] == 1


async def test_la_traza_registra_el_ordinal_y_el_umbral():
    """El ordinal queda documentado en la traza y NO infla el umbral."""
    update = await make_researcher_node(_make_retriever(_corpus_guias()))(
        {"user_request": _CONSULTA, "steps": 0, "retries": 0}
    )
    decision = next(
        e for e in update["trace_events"] if e["event_type"] == "decision"
    )
    details = decision["details"]
    assert "quinto" in details["ordinal_terms"]
    assert "quinto" not in details["target_terms"]
    assert details["thematic_terms"] == ["historia"]  # "grado" es genérico
    assert details["required_matches"] == 1
    assert details["source_restriction_terms"] == []


async def test_los_ordinales_no_relajan_temas_reales():
    """Una consulta con tema contenido (ausente del corpus) sigue rechazada.

    «historia de la mitosis»: "mitosis" NO es ordinal (es contenido ausente),
    así que sigue contando como temático y el umbral exige 2 coincidencias:
    los fragmentos que solo comparten el nombre de la fuente NO se aceptan
    (sin evidencia irrelevante, igual que RAG-QUALITY-03/05).
    """
    update = await make_researcher_node(_make_retriever(_corpus_guias()))(
        {"user_request": "historia de la mitosis", "steps": 0, "retries": 0}
    )
    artifact = update["research_artifact"]
    assert artifact["evidence"] == []
    # El gap de insuficiencia se reporta explícitamente (Spec 03/04): la
    # consulta exige 2 coincidencias y los chunks solo comparten 1.
    assert artifact["evidence_gaps"], "debe reportarse el gap de insuficiencia"
    assert artifact["confidence_score"] == 0.0