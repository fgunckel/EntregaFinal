"""Pruebas del grafo pedagógico (G2, ADR-0007).

Pruebas de comportamiento con fake model y retriever doble: verifican la
mecánica conservada de M7 (supervisor, validator determinístico, protección
del synthesizer, limited_finish) sobre el dominio pedagógico.
"""



from ada_study.contracts.documents import ChunkRecord, DocumentRecord, DocumentVersion
from ada_study.graph.researcher import make_researcher_node
from ada_study.graph.synthesizer import limited_finish_node
from ada_study.graph.tutor import make_tutor_node
from ada_study.graph.validator import validator_node

# ---------------------------------------------------------------------------
# Dobles de pruebas
# ---------------------------------------------------------------------------


class FakeCatalog:
    """Catálogo doble: documento publicado con versión activa disponible."""

    def __init__(self, editorial_status="published", active_version_id="ver_1"):
        self.editorial_status = editorial_status
        self.active_version_id = active_version_id

    async def get_document(self, document_id):

        return DocumentRecord(
            document_id=document_id,
            filename="f.pdf",
            editorial_status=self.editorial_status,
            active_version_id=self.active_version_id,
            created_at="t",
            updated_at="t",
        )

    async def get_version(self, version_id):

        return DocumentVersion(
            version_id=version_id,
            document_id="doc_1",
            version_number=1,
            sha256="0" * 64,
            storage_key="k",
            content_type="application/pdf",
            size_bytes=10,
            status="available",
            created_at="t",
        )

    async def list_chunks(self, version_id):
        return []


def _chunks():
    return [
        ChunkRecord(
            chunk_id="c1",
            document_id="doc_1",
            version_id="ver_1",
            page=1,
            chunk_index=0,
            content_hash="0" * 64,
            text="La mitocondria genera energia",
            title="Biologia",
            source="pdf",
        ),
        ChunkRecord(
            chunk_id="c2",
            document_id="doc_1",
            version_id="ver_1",
            page=2,
            chunk_index=1,
            content_hash="1" * 64,
            text="La fotosintesis captura luz",
            title="Biologia",
            source="pdf",
        ),
    ]


def _make_retriever(chunks):
    from ada_study.index.bm25_index import BM25Index
    from ada_study.index.retriever import HybridRetriever

    index = BM25Index.rebuild(chunks)
    retriever = HybridRetriever(FakeCatalog(), index)
    retriever.load_chunks(chunks)
    return retriever


# ---------------------------------------------------------------------------
# Researcher
# ---------------------------------------------------------------------------


async def test_researcher_recovers_evidence_with_provenance():
    retriever = _make_retriever(_chunks())
    node = make_researcher_node(retriever)
    update = await node({"user_request": "energia mitocondria", "steps": 0, "retries": 0})
    artifact = update["research_artifact"]
    assert len(artifact["evidence"]) == 1  # solo c1 coincide con la consulta
    ref = artifact["evidence"][0]
    assert ref["document_id"] == "doc_1"
    assert ref["page"] >= 1
    assert ref["content_hash"]
    assert ref["retrieval_method"] == ["bm25"]
    # Con 1 sola evidencia, el researcher reporta el gap de insuficiencia
    assert artifact["evidence_gaps"] == [
        "Evidencia recuperada insuficiente para una respuesta completa."
    ]


async def test_researcher_reports_gap_without_evidence():
    retriever = _make_retriever(_chunks())
    node = make_researcher_node(retriever)
    update = await node(
        {"user_request": "consulta sin coincidencias xyz", "steps": 0, "retries": 0}
    )
    artifact = update["research_artifact"]
    assert artifact["evidence"] == []
    assert artifact["evidence_gaps"]  # insuficiencia reconocida explícitamente
    assert artifact["confidence_score"] == 0.0


async def test_retriever_excludes_non_published_documents():
    """F1: documentos pendientes/rechazados/retirados no son recuperables."""
    from ada_study.index.bm25_index import BM25Index
    from ada_study.index.retriever import HybridRetriever

    for status in ("pending_review", "rejected", "withdrawn", "deleted", "draft"):
        catalog = FakeCatalog(editorial_status=status)
        index = BM25Index.rebuild(_chunks())
        retriever = HybridRetriever(catalog, index)
        retriever.load_chunks(_chunks())
        results = await retriever.search("mitocondria")
        assert results == [], f"status={status} no debe ser recuperable"


async def test_retriever_excludes_residual_pinecone_results():
    """Resultados residuales de Pinecone: la capa 2 los excluye (F1)."""
    from ada_study.index.bm25_index import BM25Index
    from ada_study.index.retriever import HybridRetriever

    async def residual_vector_search(query, top_k):
        # Simula un vector residual de una versión retirada en Pinecone
        return [("chunk_residual", 0.9)]

    catalog = FakeCatalog(editorial_status="withdrawn")
    index = BM25Index.rebuild(_chunks())
    retriever = HybridRetriever(catalog, index, vector_search=residual_vector_search)
    retriever.load_chunks(_chunks())
    results = await retriever.search("energia")
    # El residual no es visible: el documento está retirado
    assert results == []


# ---------------------------------------------------------------------------
# Tutor: síntesis pedagógica desde evidencia
# ---------------------------------------------------------------------------


async def _research_state(evidence_count=2):
    retriever = _make_retriever(_chunks())
    node = make_researcher_node(retriever)
    return await node({"user_request": "energia mitocondria", "steps": 0, "retries": 0})


async def test_tutor_composes_response_from_evidence():
    state = await _research_state()
    node = make_tutor_node()
    update = await node(state)
    response = update["study_response"]
    assert response["status"] == "complete"
    assert response["references"]  # procedencia conservada
    assert response["references"][0]["page"] >= 1
    assert not response["uses_web_sources"]


async def test_tutor_recognizes_insufficiency_without_evidence():
    research = {
        "query": "x",
        "evidence": [],
        "evidence_gaps": ["No se recuperó evidencia."],
        "confidence_score": 0.0,
    }
    node = make_tutor_node()
    update = await node({"user_request": "x", "research_artifact": research})
    response = update["study_response"]
    assert response["status"] == "insufficient_evidence"
    assert response["references"] == []
    assert response["evidence_gaps"]  # insuficiencia explícita


# ---------------------------------------------------------------------------
# Validator determinístico
# ---------------------------------------------------------------------------


def _response_with_refs(chunk_ids):

    refs = [
        {
            "document_id": "doc_1",
            "version_id": "ver_1",
            "chunk_id": chunk_id,
            "title": "Biologia",
            "page": 1,
            "excerpt": "texto",
            "content_hash": "0" * 64,
            "retrieval_method": ["bm25"],
        }
        for chunk_id in chunk_ids
    ]
    return {
        "status": "complete",
        "topic": "Biologia",
        "explanation": "Explicación con referencias.",
        "references": refs,
        "uses_web_sources": False,
        "evidence_gaps": [],
        "limitations": [],
    }


async def test_validator_accepts_consistent_artifacts():
    retriever = _make_retriever(_chunks())
    research = (
        await make_researcher_node(retriever)(
            {"user_request": "energia mitocondria", "steps": 0, "retries": 0}
        )
    )["research_artifact"]
    from ada_study.graph.tutor import make_tutor_node

    response = (
        await make_tutor_node()(
            {"user_request": "energia mitocondria", "research_artifact": research}
        )
    )["study_response"]
    update = validator_node(
        {"research_artifact": research, "study_response": response, "retries": 0}
    )
    assert update["validation_result"]["is_valid"] is True


async def test_validator_rejects_citation_of_unknown_chunk():
    research = {
        "query": "x",
        "evidence": [
            {
                "document_id": "doc_1",
                "version_id": "ver_1",
                "chunk_id": "c1",
                "title": "Biologia",
                "page": 1,
                "excerpt": "texto",
                "content_hash": "0" * 64,
                "retrieval_method": ["bm25"],
            }
        ],
        "evidence_gaps": [],
        "confidence_score": 0.2,
    }
    response = {
        "status": "complete",
        "topic": "Biologia",
        "explanation": "Explicación.",
        "references": [
            {
                "document_id": "doc_1",
                "version_id": "ver_1",
                "chunk_id": "chunk_inexistente",
                "title": "Biologia",
                "page": 1,
                "excerpt": "x",
                "content_hash": "0" * 64,
                "retrieval_method": ["bm25"],
            }
        ],
        "uses_web_sources": False,
        "evidence_gaps": [],
        "limitations": [],
    }
    update = validator_node(
        {"research_artifact": research, "study_response": response, "retries": 0}
    )
    result = update["validation_result"]
    assert result["is_valid"] is False
    assert result["tutor_errors"]  # error clasificado al tutor
    assert update["validation_errors"]  # reparación dirigida


async def test_validator_rejects_complete_without_references():
    research = {"query": "x", "evidence": [], "evidence_gaps": [], "confidence_score": 0.0}
    response = {
        "status": "complete",
        "topic": "x",
        "explanation": "Explicación sin fundamento.",
        "references": [],
        "evidence_gaps": [],
        "limitations": [],
    }
    update = validator_node(
        {"research_artifact": research, "study_response": response, "retries": 0}
    )
    result = update["validation_result"]
    assert result["is_valid"] is False
    assert result["tutor_errors"]


# ---------------------------------------------------------------------------
# Protección determinística del synthesizer (mecánica M7)
# ---------------------------------------------------------------------------


async def test_synthesizer_only_with_valid_validation():
    from ada_study.graph.builder import route_after_supervisor

    # Sin validación aprobada, la ruta al synthesizer se redirige a validator
    state = {
        "next_agent": "synthesizer",
        "validation_result": {"is_valid": False},
        "research_artifact": {"evidence": []},
    }
    assert route_after_supervisor(state) == "validator"

    # Con validación vigente, pasa
    state["validation_result"] = {"is_valid": True}
    assert state["next_agent"] == "synthesizer"


async def test_limited_finish_never_fabricates():
    update = limited_finish_node({})
    response = update["study_response"]
    assert response["status"] == "limited"
    assert response["references"] == []
    assert response["evidence_gaps"]  # reconoce el límite explícitamente


# ---------------------------------------------------------------------------
# RAG-QUALITY-01: pertinencia de evidencia y suficiencia (D-1)
# ---------------------------------------------------------------------------

_QUERY_HISTORIA = (
    "Según el documento de Historia, ¿cuáles fueron las causas de la "
    "Revolución de Mayo?"
)
_HISTORIA_1 = (
    "Las causas de la Revolución de Mayo fueron la invasión napoleónica "
    "de España y la cautividad del rey."
)
_HISTORIA_2 = (
    "La Revolución de Mayo de 1810 instituyó la Primera Junta de gobierno."
)
_MATEMATICA = (
    "Documento de Matemática de quinto grado: las fracciones representan "
    "las partes iguales de un todo dividido."
)


def _corpus_chunk(chunk_id: str, text: str, title: str, page: int) -> ChunkRecord:
    return ChunkRecord(
        chunk_id=chunk_id,
        document_id="doc_1",
        version_id="ver_1",
        page=page,
        chunk_index=page - 1,
        content_hash="2" * 64,
        text=text,
        title=title,
        source="pdf",
    )


def _corpus_historia_con_matematica() -> list[ChunkRecord]:
    """Historia pertinente + Matemática con coincidencia léxica genérica."""
    return [
        _corpus_chunk("c3", _HISTORIA_1, "Historia", 1),
        _corpus_chunk("c4", _HISTORIA_2, "Historia", 2),
        _corpus_chunk("c5", _MATEMATICA, "Matemática", 3),
    ]


async def test_researcher_keeps_pertinent_and_drops_marginal_evidence():
    """D-1: la evidencia de Historia se retiene; el fragmento de Matemática
    que solo coincide por 'documento' NO pasa el umbral de pertinencia."""
    retriever = _make_retriever(_corpus_historia_con_matematica())
    node = make_researcher_node(retriever)
    update = await node({"user_request": _QUERY_HISTORIA, "steps": 0, "retries": 0})
    artifact = update["research_artifact"]
    chunk_ids = [ref["chunk_id"] for ref in artifact["evidence"]]
    assert chunk_ids == ["c3", "c4"]  # solo Historia: c5 filtrado
    assert artifact["evidence_gaps"] == []  # cobertura y cantidad suficientes
    assert artifact["confidence_score"] > 0


async def test_researcher_five_irrelevant_chunks_do_not_support_query():
    """Cinco fragmentos irrelevantes (coincidencia léxica marginal) no
    producen evidencia ni 'complete': insuficiencia explícita."""
    chunks = [
        _corpus_chunk(f"m{i}", _MATEMATICA, "Matemática", i + 1) for i in range(5)
    ]
    retriever = _make_retriever(chunks)
    node = make_researcher_node(retriever)
    update = await node({"user_request": _QUERY_HISTORIA, "steps": 0, "retries": 0})
    artifact = update["research_artifact"]
    assert artifact["evidence"] == []
    assert artifact["confidence_score"] == 0.0
    assert artifact["evidence_gaps"] == [
        "La evidencia recuperada no guarda relación pertinente con los "
        "términos significativos de la consulta: los fragmentos solo "
        "coinciden marginalmente y no se usan como respaldo."
    ]


async def test_researcher_historia_with_relevant_evidence_is_complete():
    """Consulta de Historia + evidencia pertinente de Historia → 'complete'."""
    retriever = _make_retriever(_corpus_historia_con_matematica())
    research = (
        await make_researcher_node(retriever)(
            {"user_request": _QUERY_HISTORIA, "steps": 0, "retries": 0}
        )
    )["research_artifact"]
    response = (
        await make_tutor_node()(
            {"user_request": _QUERY_HISTORIA, "research_artifact": research}
        )
    )["study_response"]
    assert response["status"] == "complete"
    assert [ref["chunk_id"] for ref in response["references"]] == ["c3", "c4"]


_GAP_IRRELEVANTE = (
    "La evidencia recuperada no guarda relación pertinente con los "
    "términos significativos de la consulta."
)


async def test_tutor_declares_insufficient_for_irrelevant_evidence():
    """Evidencia no pertinente → el tutor declara insuficiencia y NO genera
    una respuesta factual como si estuviera respaldada."""
    research = {
        "query": _QUERY_HISTORIA,
        "evidence": [],
        "evidence_gaps": [_GAP_IRRELEVANTE],
        "confidence_score": 0.0,
    }
    update = await make_tutor_node()(
        {"user_request": _QUERY_HISTORIA, "research_artifact": research}
    )
    response = update["study_response"]
    assert response["status"] == "insufficient_evidence"
    assert response["references"] == []
    assert response["evidence_gaps"] == [_GAP_IRRELEVANTE]


def test_validator_rejects_formally_valid_references_without_relevance():
    """El Validator no acepta referencias formalmente válidas como
    sustituto de relevancia: sin evidencia del researcher, no hay 'complete'."""
    research = {"query": "x", "evidence": [], "evidence_gaps": [], "confidence_score": 0.0}
    response = _response_with_refs(["c1"])  # referencia bien formada
    update = validator_node(
        {"research_artifact": research, "study_response": response, "retries": 0}
    )
    result = update["validation_result"]
    assert result["is_valid"] is False
    assert any("relevancia" in error for error in result["tutor_errors"])


async def test_synthesizer_preserves_insufficient_state():
    """El Synthesizer conserva el estado de insuficiencia al publicar."""
    research = {
        "query": _QUERY_HISTORIA,
        "evidence": [],
        "evidence_gaps": [_GAP_IRRELEVANTE],
        "confidence_score": 0.0,
    }
    tutor_update = await make_tutor_node()(
        {"user_request": _QUERY_HISTORIA, "research_artifact": research}
    )
    validation = validator_node(
        {
            "research_artifact": research,
            "study_response": tutor_update["study_response"],
            "retries": 0,
        }
    )
    assert validation["validation_result"]["is_valid"] is True
    from ada_study.graph.synthesizer import synthesizer_node

    published = synthesizer_node(
        {
            "study_response": tutor_update["study_response"],
            "validation_result": validation["validation_result"],
            "conversation_history": [],
            "contributions": [],
            "steps": 1,
        }
    )
    assert published["study_response"]["status"] == "insufficient_evidence"
    assert published["study_response"]["references"] == []
    assert published["study_response"]["evidence_gaps"] == [_GAP_IRRELEVANTE]
