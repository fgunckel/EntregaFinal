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
