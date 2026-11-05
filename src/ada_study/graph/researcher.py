"""Nodo researcher del grafo pedagógico (G2, ADR-0007).

Mecánica conservada de M7: el researcher posee solo `research_artifact`; al
regenerar limpia los errores pendientes e invalida resultados posteriores.
Cambios de dominio: contratos de study.py; el texto recuperado se trata como
dato no confiable (Spec 06 §94) y la insuficiencia se reconoce explícitamente
(Spec 00/03/04).

Criterio determinístico de pertinencia y suficiencia (RAG-QUALITY-01, D-1):

- La cantidad de fragmentos por sí sola NUNCA determina suficiencia: cada
  fragmento debe compartir términos significativos con la consulta (ver
  `significant_terms` en bm25_index). "Términos significativos" excluye las
  palabras funcionales del español (stopwords) y conserva números, nombres
  propios y términos académicos.
- Objetivo de la pertinencia: en consultas nuevas son los términos
  significativos de la consulta; en seguimientos contextuales, el tema previo
  (espejo exacto de `build_retrieval_query`, que añade el tema a la búsqueda).
- Umbral de coincidencia por fragmento: al menos `min(2, |términos
  objetivo|)` términos significativos compartidos (tolerancia simple de
  tildes y número, sin stemming). Con consultas de un solo término
  significativo, basta UNA coincidencia: una consulta puntual ("¿qué es la
  mitosis?") identifica su tema con un solo término.
- Un fragmento que solo coincide por palabras genéricas marginales (p. ej.
  "documento" en una consulta de Historia) NO alcanza el umbral: la
  insuficiencia se declara explícitamente (`evidence_gaps` + evidencia
  vacía) y el tutor publica `insufficient_evidence` sin invocar al LLM.
- Limitaciones conocidas: es un criterio LÉXICO, no comprensión semántica
  perfecta; un fragmento pertinente pero cuantitativamente escaso (1 solo
  fragmento) conserva el aviso de insuficiencia de cantidad existente.

Clasificación de términos (RAG-QUALITY-03):

- Marcadores discursivos/deícticos ("dame", "explicame", "usando",
  "utilizando", "exclusivamente", "otro", "el ejemplo anterior"): pueden
  guiar la búsqueda contextual, pero NUNCA acreditan pertinencia temática.
- Términos genéricos por frecuencia en el corpus: encabezados repetidos,
  restricciones de fuente ("Historia 5.º grado") y palabras ubicuas aparecen
  en al menos la mitad de los chunks indexados (y en 3+ chunks). Tampoco
  acreditan pertinencia: un fragmento que solo los comparte con la consulta
  no responde al tema. Subir el umbral de coincidencias NO resuelve el
  problema (informe RAG-QUALITY-02): por eso los genéricos se excluyen del
  cómputo en lugar de exigir más coincidencias.
- Aceptación por fragmento: al menos `min(2, |términos temáticos|)`
  coincidencias con términos TEMÁTICOS (no marcadores, no genéricos). Si la
  consulta no tiene ningún término temático (todos ubicuos), se conserva el
  umbral original sobre los términos objetivo: exigir términos
  discriminativos inexistentes sería un falso negativo.
- Contexto conversacional: los términos temáticos explícitos de la consulta
  actual tienen prioridad; el tema previo solo se hereda cuando la consulta
  es anáfora pura ("dame otro ejemplo"), nunca cuando trae un tema nuevo
  ("ahora explicame la fotosíntesis").

Clasificación de la consulta y restricciones de fuentes (RAG-QUALITY-05):

- La consulta se clasifica en: tema central, términos accesorios/conversa-
  cionales (marcadores discursivos, ya excluidos), RESTRICCIÓN DE FUENTES y
  referencias contextuales heredadas (tema previo, solo en anáfora pura).
- Los nombres de documentos y las restricciones de fuentes ("el material de
  Historia y Matemática que tenés disponible") NUNCA acreditan pertinencia
  temática: son metadatos del pedido, no contenido solicitado. Esto cubre el
  caso que la genericidad por frecuencia no alcanza (RAG-QUALITY-05): en un
  corpus grande y compartimentado los nombres de documento quedan bajo el
  ratio df/corpus (p. ej. 18/54 = 0.33) y "material" (df=5/54) aparece dentro
  de textos de actividades, así que sin esta regla contaban como temáticos.
- La restricción se detecta de forma determinística y acotada: sustantivos de
  fuente ("material", "documentos", "apuntes", ...), claves de exclusividad
  ("exclusivamente", "únicamente", ...) y de disponibilidad ("disponible",
  "tenés", ...) que aparezcan juntos; el tramo restringido llega hasta el fin
  de la cláusula (coma/punto) y los términos que contiene no acreditan
  pertinencia. La RECUPERACIÓN no cambia (los términos siguen alimentando
  BM25): solo cambia qué acredita pertinencia en el filtro.
- Anti falso negativo: si tras excluir marcadores y restricción no queda
  ningún término, se conserva el comportamiento anterior (los términos
  objetivo completos): una consulta cuyo tema ES la fuente ("explicame el
  material de Historia") no puede quedar sin criterio de pertinencia.
- Ordinales de grado/posición (SMOKE-FIX-01: "quinto", "segunda", ...):
  califican metadatos del material ("5.º grado"), no el contenido, y pueden
  NO existir en el corpus (el PDF escribe «5.º»/«5to» y la consulta dice
  «quinto»). Sin excluirlos, el umbral min(2, |temáticos|) exigía una
  coincidencia estructuralmente imposible y BM25 devolvía exactamente el
  documento pedido solo para verlo rechazado entero (falso negativo). Como
  los marcadores: NO acreditan pertinencia; la RECUPERACIÓN no cambia.

"""

import re
import unicodedata

from ada_study.contracts.study import SourceReference
from ada_study.graph.history import last_ada_topic
from ada_study.index.bm25_index import significant_terms, tokenize
from ada_study.observability.trace import new_event

# Marcas de consulta contextual (determinístico, sin LLM): referencias como
# "ese tema", "el ejemplo anterior", "explicámelo de otra manera" requieren el
# tema previo para recuperar evidencia nueva. La reformulación SOLO AÑADE el
# tema previo al final: la consulta original queda intacta (conserva su
# sentido) y las referencias del turno anterior NUNCA se reutilizan (la
# recuperación es nueva en cada turno).
_CONTEXT_MARKERS = (
    "ese", "esa", "eso", "otro", "otra", "mismo", "misma", "anterior",
    "explicame", "explicámelo", "más", "practicar", "ejercicio", "ejemplo",
)
# Por debajo de esta longitud, cualquier consulta se trata como contextual
# (seguimiento típico: "dame otro ejemplo", "un ejercicio para practicar").
_SHORT_QUERY_WORDS = 6


def build_retrieval_query(user_request: str, conversation_history: list[dict]) -> str:
    """Consulta de recuperación del turno, con contexto previo si aplica.

    Pura y determinística (sin LLM): si hay historial y la consulta parece
    contextual (corta o con referencias deícticas), se recupera con
    `<consulta> <tema previo de ADA>`; si no, la consulta original intacta.
    Nunca reutiliza referencias del turno anterior: solo el tema como texto.
    """
    topic = last_ada_topic(conversation_history)
    if not topic:
        return user_request
    words = user_request.split()
    low = f" {user_request.lower()} "
    contextual = len(words) <= _SHORT_QUERY_WORDS or any(m in low for m in _CONTEXT_MARKERS)
    if not contextual:
        return user_request
    return f"{user_request} {topic}"


# Gap cuando la consulta no tiene términos significativos tras el filtrado
# léxico (solo alcanza vía retrievers dobles de prueba; BM25 devuelve vacío).
_NO_SIGNIFICANT_TERMS_GAP = (
    "La consulta no contiene términos significativos para recuperar "
    "evidencia pertinente del corpus."
)


def _normalize(term: str) -> str:
    """Minúsculas sin tildes (NFKD) para comparación léxica tolerante."""
    decomposed = unicodedata.normalize("NFKD", term)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _term_matches(term: str, chunk_terms: frozenset[str]) -> bool:
    """Coincidencia de un término significativo contra un fragmento.

    Tolerancia deliberadamente acotada (sin stemming completo): acepta el
    término exacto o variantes simples de número (casa/casas,
    fracción/fracciones) tras normalizar tildes. No implementa análisis
    lingüístico avanzado (RAG-QUALITY-01).
    """
    base = _normalize(term)
    variants = {base, f"{base}s", f"{base}es"}
    if base.endswith("es") and len(base) > 4:
        variants.add(base[:-2])
    if base.endswith("s") and len(base) > 3:
        variants.add(base[:-1])
    return bool(variants & chunk_terms)


# Marcadores discursivos y deícticos (RAG-QUALITY-03): bases normalizadas sin
# tildes derivadas de _CONTEXT_MARKERS más las expresiones de pedido y
# restricción retórica (RAG-QUALITY-05: "utilizando" enmarca el pedido, igual
# que "usando"). Un fragmento que solo comparte estos términos con la
# consulta (p. ej. por un encabezado repetido del material) no responde al
# tema que el estudiante quiere aprender.
_DISCOURSE_MARKERS = frozenset(
    _normalize(marker) for marker in _CONTEXT_MARKERS
) | {
    "dame",
    "usando",
    "usar",
    "utilizando",
    "utilizar",
    "exclusivamente",
    "ahora",
}


def _is_discourse_marker(term: str) -> bool:
    """True si el término es un marcador discursivo/deíctico.

    Tolera plural simple (ejemplo/ejemplos, otro/otros), igual que
    `_term_matches`. Los marcadores NO acreditan pertinencia temática.
    """
    base = _normalize(term)
    candidates = {base}
    if base.endswith("es") and len(base) > 4:
        candidates.add(base[:-2])
    if base.endswith("s") and len(base) > 3:
        candidates.add(base[:-1])
    return bool(candidates & _DISCOURSE_MARKERS)


# ---------------------------------------------------------------------------
# Ordinales de grado (SMOKE-FIX-01): califican METADATOS del material.
# ---------------------------------------------------------------------------

# Ordinales que en el corpus escolar califican metadatos del documento
# ("5.º grado", "quinto grado", "segunda parte") y nunca el tema solicitado.
# El defecto observado (SMOKE-FIX-01): con «historia de quinto grado», el
# PDF escribe el grado como «5.º»/«5to», así que "quinto" tiene df=0; como
# NO era genérico (df=0 < 3) contaba como temático e inflaba el umbral
# min(2, |temáticos|) a 2 coincidencias ESTRUCTURALMENTE imposibles: BM25
# recuperaba exactamente los fragmentos del documento pedido y el filtro los
# rechazaba a todos → `insufficient_evidence` sin referencias. Un ordinal no
# identifica contenido: NO acredita pertinencia (como los marcadores), pero
# SÍ alimenta la recuperación BM25 (que no cambia).
# RAG-RUNTIME-01: la detección se extiende a las formas ABREVIADAS que el
# propio corpus usa («5to», «5.º», «2da», «7mo», «1er») — son el mismo tipo
# de calificador de metadatos y sin ellas el falso negativo reaparece cuando
# el estudiante escribe el grado abreviado (grafía con df=0 en el corpus).
_ORDINAL_TERMS = frozenset(
    _normalize(term)
    for term in (
        "primero", "primera", "primer", "segundo", "segunda", "tercero",
        "tercera", "tercer", "cuarto", "cuarta", "quinto", "quinta",
        "sexto", "sexta", "septimo", "septima", "octavo", "octava",
        "noveno", "novena", "decimo", "decima",
    )
)

# Formas abreviadas de ordinal español: dígito + sufijo ordinal, con o sin
# símbolo («5to», «5ta», «5.º», «2do», «2da», «3ro», «3er», «7mo», «10.°»),
# tolerando plural simple («5tos»). Solo 1-3 dígitos: números de contenido
# (fechas como "1810") siguen siendo términos temáticos normales.
_ORDINAL_ABBREVIATION_RE = re.compile(
    r"^\d{1,3}(?:[º°ª]|[nrdtmv][oa]|er)s?$",
    re.IGNORECASE,
)


def _is_ordinal_term(term: str) -> bool:
    """True si el término es un ordinal de grado/posición (SMOKE-FIX-01).

    Cubre las formas plenas («quinto») y las abreviadas («5to», «2da»,
    «7mo», «1er», «5.º», «5tos») que el tokenizer emite como un solo token
    (`\\w+` no separa dígitos de letras). Tolera plural simple
    (segundo/segundos), igual que `_term_matches`. Un ordinal califica
    metadatos del material (grado, unidad, parte): no acredita pertinencia
    temática y no debe inflar el umbral de coincidencias exigidas por
    fragmento (falso negativo estructural). Números desnudos (fechas,
    cantidades: "1810", "5") NO son ordinales: siguen contando como
    contenido para no relajar la pertinencia.
    """
    base = _normalize(term).lower()
    candidates = {base}
    if base.endswith("es") and len(base) > 4:
        candidates.add(base[:-2])
    if base.endswith("s") and len(base) > 3:
        candidates.add(base[:-1])
    if candidates & _ORDINAL_TERMS:
        return True
    return bool(_ORDINAL_ABBREVIATION_RE.fullmatch(base))


# ---------------------------------------------------------------------------
# Restricciones de fuentes (RAG-QUALITY-05): clasificación de la consulta.
# Los nombres de documentos y las restricciones de fuentes NUNCA acreditan
# pertinencia temática: son metadatos del pedido, no contenido solicitado.
# ---------------------------------------------------------------------------

# Sustantivos que introducen una fuente/restricción de material ("el material
# de Historia", "los apuntes de Biología", "las fuentes disponibles").
_SOURCE_NOUNS = frozenset(
    {
        "material", "materiales", "documento", "documentos", "apunte",
        "apuntes", "libro", "libros", "guia", "guias", "fuente", "fuentes",
        "biblioteca", "archivo", "archivos", "contenido", "contenidos",
    }
)
# Claves de exclusividad sobre las fuentes ("exclusivamente el material de...").
_EXCLUSIVITY_CUES = frozenset({"exclusivamente", "unicamente", "solamente", "solo"})
# Claves de disponibilidad ("...que tenés disponible", "con lo que tengas").
_AVAILABILITY_CUES = frozenset(
    {"disponible", "disponibles", "dispones", "tenes", "tienes", "tengas", "tienen"}
)
# Puntuación que cierra una cláusula: el tramo restringido no la atraviesa
# ("Utilizando el material de Historia, explicame la mitosis" → "mitosis" es
# tema central, no restricción).
_CLAUSE_BREAKS = ",.;:!?¡¿—"


def _source_restriction_terms(query: str) -> frozenset[str]:
    """Términos de la cláusula de RESTRICCIÓN DE FUENTES (tokens originales).

    Determinístico y acotado (sin LLM): la restricción existe cuando hay un
    sustantivo de fuente seguido de "de" ("material de Historia"), o un
    sustantivo de fuente junto con una clave de exclusividad/disponibilidad,
    o claves de exclusividad + disponibilidad ("solo lo que tenés"). El tramo
    restringido arranca en la primera clave y termina en el fin de la cláusula
    (coma/punto) o de la consulta. Sin restricción, devuelve vacío: el resto
    del pipeline queda exactamente como antes. Los tokens conservan su forma
    original (legibles en la traza); la comparación contra la consulta se hace
    con la base normalizada (ver `researcher_node`).
    """
    raw_tokens = query.split()
    cleaned = [
        token.strip("«»\"'()[]{}¡¿" + _CLAUSE_BREAKS) for token in raw_tokens
    ]
    tokens = [_normalize(token).lower() for token in cleaned]
    source = [i for i, t in enumerate(tokens) if t in _SOURCE_NOUNS]
    exclusive = [i for i, t in enumerate(tokens) if t in _EXCLUSIVITY_CUES]
    available = [i for i, t in enumerate(tokens) if t in _AVAILABILITY_CUES]
    source_of = any("de" in tokens[i + 1 : i + 3] for i in source)
    if not (
        source_of
        or (source and (exclusive or available))
        or (exclusive and available)
        or available
    ):
        return frozenset()
    start = min([*source, *exclusive, *available])
    restricted: set[str] = set()
    for index in range(start, len(raw_tokens)):
        if cleaned[index]:
            restricted.add(cleaned[index])
        if any(mark in raw_tokens[index] for mark in _CLAUSE_BREAKS):
            break
    return frozenset(restricted)


# Un término es GENÉRICO cuando aparece en al menos la mitad de los chunks
# del corpus Y en al menos 3 chunks: encabezados repetidos ("Material de
# Historia 5.º grado"), restricciones de fuente y palabras ubicuas. El piso
# absoluto evita falsos negativos en corpus muy pequeños, donde la proporción
# sola no es significativa (un término presente en 1 de 2 chunks no es
# genérico). Sin umbral rígido de descarte: si NINGÚN término de la consulta
# resulta temático, se conserva el umbral original (anti falso negativo).
_GENERIC_MIN_DF = 3
_GENERIC_DF_RATIO = 0.5


def _split_thematic_terms(
    target_terms: list[str],
    df_lookup,
    corpus_size: int,
) -> tuple[list[str], list[str]]:
    """Particiona los términos objetivo en (temáticos, genéricos).

    Genérico = df >= _GENERIC_MIN_DF y df/corpus >= _GENERIC_DF_RATIO
    (encabezados repetidos, restricciones de fuente, palabras ubicuas).
    Sin estadísticas de corpus disponibles (dobles de prueba sin BM25) no hay
    descuento: todos los términos se tratan como temáticos.
    """
    if df_lookup is None or corpus_size <= 0:
        return list(target_terms), []
    thematic: list[str] = []
    generic: list[str] = []
    for term in target_terms:
        df = df_lookup(term)
        if df >= _GENERIC_MIN_DF and df / corpus_size >= _GENERIC_DF_RATIO:
            generic.append(term)
        else:
            thematic.append(term)
    return thematic, generic


def make_researcher_node(retriever):
    """Crea el nodo researcher sobre el retriever híbrido (F1).

    El retriever ya aplica los filtros de visibilidad (F1): solo fragmentos
    de documentos publicados con versión activa `available` llegan aquí.
    La recuperación es NUEVA en cada turno (nunca reutiliza referencias
    anteriores); con historial, las consultas contextuales se reformulan
    añadiendo el tema previo (CHECKPOINTER-03, dimensión A).
    """

    async def researcher_node(state: dict) -> dict:
        pending_errors = state.get("validation_errors") or []
        query = state.get("user_request", "")
        history = state.get("conversation_history") or []
        retrieval_query = build_retrieval_query(query, history)
        results = await retriever.search(retrieval_query, top_k=5)

        # Pertinencia determinística (RAG-QUALITY-01, D-1): los scores BM25
        # (no acotados) y RRF (1/(60+rank), solo ordinal) no son comparables
        # entre sí, así que la evaluación usa coincidencia de términos
        # significativos, no umbrales de score inventados.
        topic = last_ada_topic(history)
        contextual = retrieval_query != query
        # RAG-QUALITY-03: los marcadores discursivos no acreditan pertinencia;
        # en seguimientos, el tema EXPLÍCITO del turno actual tiene prioridad y
        # el tema previo solo se hereda en anáfora pura ("dame otro ejemplo").
        # RAG-QUALITY-05: los términos de la restricción de fuentes ("el
        # material de Historia y Matemática que tenés disponible") tampoco
        # acreditan pertinencia: el tema central es lo que quedó fuera de esa
        # cláusula. La RECUPERACIÓN (retrieval_query) no cambia.
        # SMOKE-FIX-01: los ordinales de grado ("quinto" en «historia de
        # quinto grado») califican metadatos del material y pueden NO existir
        # en el corpus (el PDF escribe «5.º»): sin excluirlos inflan el
        # umbral de coincidencias y vuelven la aceptación imposible (falso
        # negativo estructural). La RECUPERACIÓN no cambia.
        ordinal_terms = [
            term for term in significant_terms(query) if _is_ordinal_term(term)
        ]
        restriction_terms = _source_restriction_terms(query)
        restriction_bases = frozenset(
            _normalize(term).lower() for term in restriction_terms
        )
        query_terms = [
            term
            for term in significant_terms(query)
            if not _is_discourse_marker(term)
            and not _is_ordinal_term(term)
            and _normalize(term).lower() not in restriction_bases
        ]
        if contextual and topic:
            topic_terms = [
                term
                for term in significant_terms(topic)
                if not _is_discourse_marker(term)
            ]
            target_terms = query_terms or topic_terms
        else:
            # Anti falso negativo: si la restricción consumió todos los
            # términos (el tema ES la fuente), se conserva el criterio
            # anterior con los términos objetivo completos.
            fallback_terms = [
                term
                for term in significant_terms(query)
                if not _is_discourse_marker(term)
            ]
            target_terms = query_terms or fallback_terms

        # Discriminación por frecuencia (RAG-QUALITY-03): los términos
        # genéricos (encabezados repetidos, restricciones de fuente, palabras
        # ubicuas) no acreditan pertinencia temática por sí solos. Sin
        # estadísticas disponibles (dobles de prueba) no hay descuento.
        thematic_terms, _generic_terms = _split_thematic_terms(
            target_terms,
            getattr(retriever, "term_document_frequency", None),
            getattr(retriever, "indexed_chunk_count", lambda: 0)(),
        )

        relevant: list = []
        matched_counts: list[int] = []
        required: int | None = None
        if target_terms:
            # Coincidencia exigida sobre términos TEMÁTICOS; solo si la
            # consulta no tiene ninguno (todos ubicuos en el corpus) se
            # conserva el umbral original sobre los términos objetivo.
            evidence_terms = thematic_terms or target_terms
            required = min(2, len(evidence_terms))
            # Espejo del indexado BM25 (`title + text`): el tema del turno
            # previo suele vivir en el título del fragmento (multi-turno).
            chunk_term_sets = [
                frozenset(
                    _normalize(token)
                    for token in tokenize(f"{result.title} {result.text}")
                )
                for result in results
            ]
            matched_counts = [
                sum(
                    1 for term in evidence_terms if _term_matches(term, chunk_terms)
                )
                for chunk_terms in chunk_term_sets
            ]
            relevant = [
                result
                for result, matched in zip(results, matched_counts)
                if matched >= required
            ]
        evidence = [_chunk_to_reference(result) for result in relevant]

        gaps = []
        if not results:
            gaps = [
                "No se recuperó evidencia del corpus para esta consulta. "
                "No se inventarán citas ni ejercicios."
            ]
        elif not evidence:
            gaps = [
                _NO_SIGNIFICANT_TERMS_GAP
                if not target_terms
                else "La evidencia recuperada no guarda relación pertinente con "
                "los términos significativos de la consulta: los fragmentos "
                "solo coinciden marginalmente y no se usan como respaldo."
            ]
        elif len(evidence) < 2:
            gaps.append(
                "Evidencia recuperada insuficiente para una respuesta completa."
            )

        summary = f"{len(evidence)} evidencias recuperadas."
        if pending_errors:
            summary = f"Reintento tras validación: {summary}"

        # Trazabilidad (AGENT-TRACE-01 §3.2): la decisión de recuperación
        # (consulta efectiva, términos, fragmentos BM25) y el resultado REAL
        # del filtro de pertinencia con aceptadas/descartadas y su motivo.
        # Caso clave visible aquí: BM25 recupera fragmentos pero el filtro
        # los descarta (p. ej. prueba.pdf) — queda registrado por chunk.
        retrieval_event = new_event(
            "researcher",
            "decision",
            f"Recuperación BM25: {len(results)} fragmentos; "
            f"{len(target_terms)} términos objetivo.",
            details={
                "query": query,
                "retrieval_query": retrieval_query,
                "contextual_reformulation": contextual,
                "retrieved_chunks": len(results),
                "top_k": 5,
                "documents": [
                    {
                        "document_id": result.document_id,
                        "title": result.title,
                        "page": result.page,
                    }
                    for result in results
                ],
                "target_terms": list(target_terms),
                "thematic_terms": list(thematic_terms),
                "source_restriction_terms": sorted(restriction_terms),
                "ordinal_terms": sorted(ordinal_terms),
                "required_matches": required,
            },
        )
        accepted: list[dict] = []
        rejected: list[dict] = []
        if target_terms and required is not None:
            for result, matched in zip(results, matched_counts):
                entry = {
                    "chunk_id": result.chunk_id,
                    "title": result.title,
                    "page": result.page,
                    "matched_terms": matched,
                }
                if matched >= required:
                    entry["reason"] = (
                        f"Cumple el umbral: {matched} términos temáticos "
                        f"compartidos (requiere {required})."
                    )
                    accepted.append(entry)
                else:
                    entry["reason"] = (
                        f"Descartado por pertinencia: solo {matched} de "
                        f"{required} términos temáticos compartidos."
                    )
                    rejected.append(entry)
        else:
            # Sin términos significativos no hay umbral aplicable: todo lo
            # recuperado queda fuera de la evidencia (gap ya reportado).
            rejected = [
                {
                    "chunk_id": result.chunk_id,
                    "title": result.title,
                    "page": result.page,
                    "matched_terms": None,
                    "reason": (
                        "Consulta sin términos significativos: sin umbral de "
                        "pertinencia aplicable."
                    ),
                }
                for result in results
            ]
        filter_event = new_event(
            "researcher",
            "result",
            f"Filtro de pertinencia: {len(evidence)} aceptadas, "
            f"{len(rejected)} descartadas.",
            details={
                "accepted": accepted,
                "rejected": rejected,
                "final_evidence_count": len(evidence),
                "evidence_gaps": gaps,
            },
        )

        return {
            "research_artifact": {
                "query": query,
                # Consulta efectiva (con contexto si la consulta era
                # contextual): trazabilidad de la reformulación (Spec 02 §18).
                "retrieval_query": retrieval_query,
                "evidence": [ref.model_dump() for ref in evidence],
                "evidence_gaps": gaps,
                "confidence_score": round(min(1.0, len(evidence) / 5.0), 6),
            },
            "validation_errors": [],
            # Invalidación de resultados previos (mecánica M7 intacta)
            "validation_result": None,
            "study_response": None,
            "trace_events": [retrieval_event, filter_event],
            "contributions": [
                *state.get("contributions", []),
                {
                    "agent": "researcher",
                    "artifact_type": "research",
                    "summary": summary,
                    "step": state.get("steps", 0),
                    "attempt": state.get("retries", 0),
                    "errors": pending_errors,
                },
            ],
        }

    return researcher_node


def _chunk_to_reference(result) -> SourceReference:
    """Deriva SourceReference desde un RetrievedChunk (procedencia completa)."""
    methods = [
        part for part in result.retriever.split("+") if part in {"bm25", "vector"}
    ]
    return SourceReference(
        document_id=result.document_id,
        version_id=result.version_id,
        chunk_id=result.chunk_id,
        title=result.title,
        page=result.page,
        # El excerpt alimenta el prompt del tutor y las referencias de la UI.
        # 1200 caracteres (LLM-CONFIG-01, antes 280) dan al tutor contexto
        # suficiente para redactar explicaciones desarrolladas sin perder
        # trazabilidad: chunk_id, página, título y content_hash siguen
        # identificando la procedencia completa, y el filtro de pertinencia
        # (RAG-QUALITY-03, intacto) sigue decidiendo qué chunks entran.
        excerpt=result.text[:1200],
        content_hash=result.content_hash,
        retrieval_method=methods or ["bm25"],
    )
