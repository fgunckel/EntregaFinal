"""Pruebas del índice BM25 incremental propio (ADR-0003)."""

import json

import pytest

from ada_study.contracts.documents import ChunkRecord
from ada_study.index.bm25_index import BM25Index, tokenize


def _chunk(version_id: str, text: str, chunk_index: int = 0, page: int = 1) -> ChunkRecord:
    from ada_study.contracts.documents import ChunkRecord

    return ChunkRecord(
        chunk_id=f"{version_id}:{chunk_index}",
        document_id="doc_1",
        version_id=version_id,
        page=page,
        chunk_index=chunk_index,
        content_hash="0" * 64,
        text=text,
        title="t",
        source="s",
    )


def test_tokenize_lowercases_and_finds_words():
    assert tokenize("Hola MUNDO") == ["hola", "mundo"]


def test_add_version_indexes_chunks():
    index = BM25Index()
    index.add_version([_chunk("v1", "gato perro")])
    assert index.chunk_count() == 1
    assert index.indexed_versions() == {"v1"}


def test_search_returns_relevant_chunk_first():
    index = BM25Index()
    index.add_version([_chunk("v1", "la fotosintesis ocurre en cloroplastos")])
    index.add_version([_chunk("v2", "la mitocondria produce ATP")])
    results = index.search("fotosintesis cloroplastos", top_k=2)
    # Solo v1 contiene los términos de la consulta
    assert len(results) == 1
    assert results[0][0] == "v1:0"


def test_search_empty_index_returns_empty():
    index = BM25Index()
    assert index.search("cualquier cosa") == []


def test_search_rejects_empty_query():
    index = BM25Index()
    with pytest.raises(ValueError):
        index.search("   ")


def test_add_version_is_idempotent():
    index = BM25Index()
    chunks = [_chunk("v1", "texto de prueba")]
    index.add_version(chunks)
    index.add_version(chunks)
    assert index.chunk_count() == 1


def test_remove_version_removes_only_that_version():
    index = BM25Index()
    index.add_version([_chunk("v1", "gato")])
    index.add_version([_chunk("v2", "perro")])
    removed = index.remove_version("v1")
    assert removed == 1
    assert index.chunk_count() == 1
    assert index.indexed_versions() == {"v2"}
    # El término del chunk retirado ya no aparece en resultados
    assert index.search("gato") == []


def test_remove_version_missing_returns_zero():
    index = BM25Index()
    assert index.remove_version("no_existe") == 0


def test_state_roundtrip_preserves_search():
    index = BM25Index()
    index.add_version([_chunk("v1", "la fotosintesis ocurre en cloroplastos")])
    index.add_version([_chunk("v2", "la mitocondria produce ATP")])
    state = index.to_state()
    restored = BM25Index()
    restored.load_state(state)
    assert restored.search("fotosintesis") == index.search("fotosintesis")
    assert restored.indexed_versions() == {"v1", "v2"}
    # El estado es JSON válido
    json.loads(state)


def test_rebuild_from_persisted_chunks():
    chunks_v1 = [_chunk("v1", "gato que maulla")]
    chunks_v2 = [_chunk("v2", "perro que ladra")]
    index = BM25Index.rebuild(chunks_v1 + chunks_v2)
    assert index.indexed_versions() == {"v1", "v2"}
    assert index.search("maulla")[0][0] == "v1:0"


# ---------------------------------------------------------------------------
# Matching léxico en español (RAG-QUALITY-01, D-2)
# ---------------------------------------------------------------------------


def test_stopwords_do_not_dominate_results():
    """Las palabras funcionales no generan coincidencias por sí solas."""
    index = BM25Index()
    index.add_version([_chunk("v1", "la de el que las en por para un una")])
    index.add_version([_chunk("v2", "fotosintesis ocurre en cloroplastos")])
    # Consulta de solo palabras funcionales: sin coincidencias significativas.
    assert index.search("de la que el en un una") == []
    # Los términos significativos siguen decidiendo; "la"/"en" no puntúan.
    results = index.search("la fotosintesis en el cloroplasto", top_k=2)
    assert [chunk_id for chunk_id, _ in results] == ["v2:0"]


def test_query_without_significant_matches_returns_empty():
    """Sin coincidencias significativas no hay evidencia aparente."""
    index = BM25Index()
    index.add_version([_chunk("v1", "la mitocondria produce energia")])
    # Solo stopwords en la consulta → vacío (no usa términos del corpus).
    assert index.search("de la que") == []
    # Términos significativos ausentes del corpus → vacío.
    assert index.search("fotosintesis cloroplastos") == []


def test_academic_terms_and_numbers_still_retrieved():
    """Números y términos académicos se conservan en la recuperación."""
    index = BM25Index()
    index.add_version(
        [_chunk("v1", "La Revolución de Mayo de 1810 se dio en Buenos Aires")]
    )
    results = index.search("revolución 1810", top_k=1)
    assert results
    assert results[0][0] == "v1:0"
    assert results[0][1] > 0


def test_scores_order_preserved_descending():
    """Los scores decaen con el ranking y el orden se conserva estable."""
    index = BM25Index()
    index.add_version([_chunk("v1", "mitocondria mitocondria energia")])
    index.add_version([_chunk("v2", "mitocondria")])
    results = index.search("mitocondria energia", top_k=2)
    assert len(results) == 2
    scores = [score for _, score in results]
    assert scores == sorted(scores, reverse=True)
    assert scores[0] > 0
