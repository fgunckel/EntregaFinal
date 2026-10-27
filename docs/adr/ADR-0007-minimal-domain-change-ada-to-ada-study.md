# ADR-0007 — Mínimo cambio de dominio: de ADA a ADA Study

- Estado: Propuesto (espera aprobación del usuario). Alineado con Specs 03/06.
- Fecha: 2026-10-25 (rev. 2 — integración Spec Kit v6)

## Contexto

El grafo de M7 (`src/ada/graph.py`) coordina decisiones de arquitectura de software: `SystemProfile`, criterios ponderados, alternativas (monolito/microservicios), scoring. ADA Study necesita un grafo pedagógico: Estudiar, Practicar, Preparar evaluación (Spec 03). La auditoría (BASELINE §6, riesgo 2) marcó esto como el riesgo de esfuerzo más subestimable. Esta ADR acota el cambio mínimo.

## Decisión

### Lo que se conserva sin cambios (infraestructura del grafo)

| Componente | Razón |
|---|---|
| Topología supervisor → especialistas → validator → synthesizer | El patrón de enrutado con límites determinísticos en Python es correcto para cualquier dominio |
| `AgentState`, contribuciones, invalidación de artefactos obsoletos | Mecánica de consistencia del estado, independiente del dominio |
| Validator determinístico (no LLM) con clasificación de errores por responsable | Regla de calidad reutilizable tal cual |
| Checkpointer Redis + HITL + observabilidad + costos | Capa `app/` de M7, agnóstica del dominio |
| `HybridRetriever`/RRF como herramienta del researcher | El RAG es la herramienta de evidencia en ambos dominios |

### Lo que cambia (contratos y prompts, no mecánica)

| M7 (decisiones de arquitectura) | ADA Study (pedagógico) |
|---|---|
| `SystemProfile` | `StudyContext`: tema/materia, nivel del estudiante, objetivo (estudiar/practicar/preparar) |
| `Criterion` + pesos | `LearningObjective`: objetivo pedagógico con criterios de cobertura |
| `AlternativeAnalysis` (monolito vs microservicios) | `PracticeSet`: preguntas generadas desde evidencia recuperada |
| `FinalReport.recommendation` | `StudyResponse`: explicación pedagógica + referencias documento/página + aviso de fuentes web |
| Criterios por defecto hardcodeados (`DEFAULT_CRITERIA`) | Objetivos derivados de los temas reales de la biblioteca (Spec 03: "sin inventar categorías") |

### Regla de insuficiencia (Spec 00/03/04)

El mecanismo ya existe en M7 y se reutiliza: `ResearchArtifact.evidence_gaps` + `ValidationResult.unsupported_criteria` son la base para reconocer que "los materiales no alcanzan" y ofrecer búsqueda web (Spec 04) en lugar de inventar ejercicios.

## Consecuencias

- El cambio es de **contratos + prompts + criterios**, no de topología ni de infraestructura: se reescriben `schemas` del dominio y los prompts de supervisor/especialistas; `graph.py` (mecánica), `state.py`, `app/` quedan intactos.
- Los tests de comportamiento de M7 (`test_behavior.py`, `test_regression.py`) se adaptan a los contratos nuevos; los de mecánica (validación, invalidación, límites) se conservan casi sin cambios.
- El researcher pasa de `TechnicalDocument` (corpus JSON) a chunks del catálogo (ADR-0003/0004); su contrato de salida (`SourceEvidence`) ya tiene `document_id`, `excerpt`, `content_hash` — compatible con la spec (`SourceReference` se deriva de él en TASK-02).
- **Recuperación filtrada por editorial (Spec 06):** el retriever del researcher solo expone fragmentos de documentos `published`/`published_pending_review` con versión activa `available`; aportes `pending_review`/`rejected`/`withdrawn` nunca alimentan el RAG compartido.
- **Prompt injection (Spec 06 §94, Spec 04 §21):** el texto recuperado se trata como dato no confiable, nunca como instrucciones del sistema/agente; los prompts del grafo lo delimitan explícitamente.

## Historial de revisión

- **Rev. 2 (2026-10-25):** integración Spec Kit v6. Cambios: recuperación filtrada por estado editorial (Spec 06); protección explícita contra prompt injection en documentos recuperados (Spec 06 §94).
- **Rev. 1 (2026-10-25):** versión inicial de TASK-01.
