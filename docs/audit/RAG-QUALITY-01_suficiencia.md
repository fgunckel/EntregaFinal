# RAG-QUALITY-01 — Relevancia de evidencia y suficiencia de respuestas

Corrección mínima (sin cambios de arquitectura, esquema, visibilidad ni
dependencias) de los defectos D-1 y D-2 confirmados en la auditoría
RAG-DRAFT-01.

## Semántica de los estados (Spec 04 §6, actualizada)

- **`complete`**: la explicación está respaldada por evidencia **pertinente**
  recuperada del corpus: cada fragmento retenido comparte al menos
  `min(2, |términos significativos de la consulta|)` términos significativos
  con la consulta, y hay al menos un fragmento. Con un único fragmento, la
  respuesta se publica igualmente con el aviso explícito de cantidad en
  `evidence_gaps` (no se declara suficiencia plena solo por encontrarlo).
- **`insufficient_evidence`**: la biblioteca no contiene material con relación
  pertinente suficiente para la consulta (sin coincidencias, coincidencia
  solo marginal, o corpus vacío). No se inventan respuestas ni citas;
  `evidence_gaps` explica el motivo. El tutor NO invoca al LLM en este estado.

La decisión es determinística (researcher + tutor); la salida del LLM no puede
cambiar el estado, y el Validator rechaza un `complete` sin evidencia
pertinente aunque las referencias sean formalmente válidas.

## Criterio determinístico implementado

1. **Términos significativos** (`significant_terms` en `bm25_index.py`):
   tokens en minúsculas sin una lista pequeña y explícita de palabras
   funcionales del español (`_STOPWORDS`). Números, nombres propios y
   términos académicos se conservan íntegros.
2. **Filtro de pertinencia** (`researcher.py`): un fragmento se retiene como
   evidencia solo si comparte `min(2, |términos objetivo|)` términos
   significativos con la consulta (con tolerancia de tildes y plural simple;
   sin stemming). En seguimientos contextuales el objetivo son los términos
   del tema previo (espejo de `build_retrieval_query`); en consultas nuevas,
   los de la consulta.
3. **Insuficiencia explícita**: si nada recuperado alcanza el umbral, la
   evidencia se publica vacía con `evidence_gaps` que lo explican → el tutor
   declara `insufficient_evidence` y el Synthesizer lo publica intacto.

## Tratamiento de scores BM25 / RRF

- BM25: Okapi no acotado (depende del IDF del corpus).
- RRF: `1/(60+rank)`, escala ordinal (~0.016 máximo), **no comparable** con
  BM25. Por eso el criterio NO usa umbrales de score (no hay calibración
  estadística disponible): usa coincidencia de términos significativos,
  invariante a la escala del retriever.

## Limitaciones conocidas

- El matching es **léxico**: la relevancia no equivale a comprensión
  semántica perfecta (sin embeddings en Study: Pinecone no se usa aquí).
- Sin stemming completo: solo tolerancia simple de número (casa/casas,
  fracción/fracciones) y tildes; derivaciones morfológicas ricas pueden no
  coincidir.
- Un fragmento pertinente pero cuantitativamente escaso (1 solo fragmento)
  sigue publicando `complete` con el aviso de cantidad en `evidence_gaps`:
  no existe calibración confiable para decidir lo contrario de forma
  determinística.
- La lista de stopwords es intencionalmente pequeña y explícita; palabras
  genéricas de dominio (p. ej. "documento") no se excluyen: las cubre el
  umbral de pertinencia del researcher, no la lista.

## Archivos modificados

- `src/ada_study/index/bm25_index.py` — D-2: stopwords + `significant_terms`.
- `src/ada_study/graph/researcher.py` — D-1: filtro de pertinencia y gaps.
- `src/ada_study/graph/validator.py` — regla: `complete` exige evidencia
  pertinente.
- `src/ada_study/contracts/study.py` — documentación de semántica de estados.
- Tests: `test_bm25_index.py`, `test_study_graph.py`, `test_study_llm.py`,
  `test_study_endpoint.py`.
