# RAG-QUALITY-05 — Diagnóstico: pertinencia temática y suficiencia de evidencia

## Caso reproducido (prueba funcional)

Consulta:

> "Explicame la fotosíntesis utilizando exclusivamente el material de Historia y Matemática que tenés disponible."

Reproducido contra el corpus real (54 chunks visibles: 22 de `prueba.pdf`
guía de Matemática + 32 de la guía de Historia), con el pipeline actual
(BM25 → filtro de pertinencia del researcher). Script: `.coda/tmp/diag_rag_quality05.py`.

## Respuestas punto por punto (§3)

**1. Inspección de código.** La pertinencia vive íntegramente en
`src/ada_study/graph/researcher.py` (`_split_thematic_terms` + umbral
`min(2, |términos temáticos|)`); BM25 (`index/bm25_index.py`) solo recupera;
`retriever.py` solo filtra visibilidad; la suficiencia es consecuencia del
artefacto del researcher (evidencia vacía + `evidence_gaps`).

**2. Eventos reales.** BM25 devolvió 5 fragmentos (2 portadas p.1, 2
evaluaciones p.15, 1 actividades p.3). El evento `result` del researcher los
aceptó a los 5 con `matched_terms = 2` cada uno.

**3. Términos considerados relevantes.** Objetivos (sin marcadores):
`fotosíntesis, utilizando, material, historia, matemática, tenés, disponible`.
Frecuencia en corpus: `historia` 18/54 (0.33), `matemática` 15/54 (0.28),
`material` 5/54 (0.09), `fotosíntesis` 0/54.

**4. Por qué se aceptó cada fragmento (no fue la misma razón exacta).**
El umbral exigido fue `min(2, 7) = 2` coincidencias y el tema real
(`fotosíntesis`) tiene df=0: ningún fragmento aceptado compartió el tema.
- Portadas (p.1, ambas guías): coincidieron `material` + `historia`/`matemática`
  por el encabezado/portada («MATEMÁTICA 5.º grado GUÍA COMPLETA…»).
- Página 3 y p.15 de Historia: `material` aparece DENTRO del texto de
  actividades («una fuente escrita de una material») + `historia` del encabezado.
- Página 15 de Matemática: `material` + `matemática` del encabezado de página.
Causa común estructural: **los términos de la cláusula de restricción de
fuentes** («el material de Historia y Matemática que tenés disponible») y los
nombres de documento contaron como evidencia temática.

**5. Cómo se calcula la suficiencia hoy.** El researcher descarta fragmentos
que no alcanzan el umbral; si queda ≥1, `evidence` no está vacía y
`evidence_gaps` queda vacío → `confidence_score = min(1, n/5)`.

**6. Dónde se decide invocar al Tutor.** `graph/tutor.py:61`
(`if evidence:` → `if chat_model is not None: _compose_with_model(...)`):
con evidencia (aunque sea irrelevante) se llama a Groq; solo con evidencia
vacía se publica `insufficient_evidence` sin LLM.

## Causa raíz concreta

La regla de genericidad por frecuencia (RAG-QUALITY-03: df≥3 **y**
df/corpus≥0.5) fue diseñada para encabezados que dominan TODO el corpus. En
un corpus con documentos compartimentados y suficientemente grande, los
nombres de documento quedan bajo el ratio (0.33 y 0.28 < 0.5) y «material»
(0.09) directamente, así que se tratan como **temáticos**. Como además el
umbral por fragmento es bajo (2) y la consulta trae tema específico
(`fotosíntesis`, df=0), bastaba compartir dos términos de la restricción de
fuentes para «acreditar» pertinencia: el filtro nunca exigió relación con el
tema central. La clasificación de la consulta (§4.1) no distinguía tema
central de restricción de fuentes: eso es lo que corrige esta tarea.

## Corrección implementada (RAG-QUALITY-05)

**`src/ada_study/graph/researcher.py`:**
1. Nueva clasificación determinística `_source_restriction_terms(query)`:
   sustantivos de fuente ("material", "documentos", "apuntes", "guías",
   "fuentes", ...), claves de exclusividad ("exclusivamente", "únicamente",
   "solamente", "solo") y de disponibilidad ("disponible", "tenés",
   "tengas", ...). La restricción se activa con sustantivo de fuente + "de",
   o fuente + exclusividad/disponibilidad, o exclusividad + disponibilidad.
   El tramo restringido arranca en la primera clave y termina en el fin de
   la cláusula (coma/punto): el tema después de la coma sigue siendo tema
   central ("Utilizando el material de Historia, explicame la mitosis").
2. Los términos de la restricción y los marcadores discursivos quedan
   excluidos del cómputo de pertinencia (LA RECUPERACIÓN BM25 NO CAMBIA).
   Anti falso negativo: si no queda ningún término, se conserva el criterio
   anterior (términos objetivo completos).
3. "utilizando" agregado a `_DISCOURSE_MARKERS` (igual que "usando"): la
   propia tarea lo clasifica como marcador conversacional; sin esto quedaba
   como "temático" y exigía una segunda coincidencia inexistente.
4. Traza: el evento `decision` del researcher registra
   `source_restriction_terms`; la UI (`trace_view.py`) lo muestra como
   "Restricción de fuentes (no acredita pertinencia)".

**Verificación contra el corpus real (54 chunks, script
`.coda/tmp/diag_rag_quality05.py`):** los mismos 5 fragmentos que antes se
aceptaban ahora se rechazan (coincidencias solo con "historia"/"matemática",
términos restringidos) → **0 aceptados → `insufficient_evidence`, sin
invocación a Groq**.

**Tests nuevos:** `tests/test_rag_quality_05.py` (12): consulta exacta →
insuficiencia sin LLM; tema central con evidencia real se acepta (anti falso
negativo); clasificación unitaria; cláusula acotada por puntuación;
regresiones (RQ-03 intacta, consulta cuyo tema ES la fuente no queda sin
criterio). Suite completa: **521 passed, 1 skipped** (integración PostgreSQL
preexistente), ruff limpio.

**Limitaciones documentadas:** criterio LÉXICO (sin LLM ni embeddings, por
diseño): una restricción redactada con vocabulario fuera de las listas no se
detecta; una consulta cuyo tema comparte la palabra disparadora ("¿qué
material estudia la materia?") puede restringir de más (mitigado por el
fallback anti falso negativo, que conserva el criterio anterior).

