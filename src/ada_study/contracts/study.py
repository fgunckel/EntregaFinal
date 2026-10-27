"""Contratos pedagógicos de ADA Study (G1, ADR-0007, Spec 03).

Fuente de requisitos: specs/03, specs/06 y ADR-0007.
Convenciones: Pydantic en los límites, base StrictModel (extra="forbid"),
estados en minúsculas, códigos de error estables sin texto de excepción.

Mapeo ADR-0007 (M7 -> ADA Study):
- `SystemProfile` -> `StudyContext`
- `Criterion` + pesos -> `LearningObjective`
- `AlternativeAnalysis` -> `PracticeSet` (preguntas desde evidencia)
- `FinalReport.recommendation` -> `StudyResponse` (explicación + referencias)
- `SourceEvidence` (M7) -> `SourceReference` (derivado, con página)
"""

from typing import Literal

from pydantic import Field

from ada_study.strict_model import StrictModel

# ---------------------------------------------------------------------------
# Objetivos de estudio (Spec 03 §25-40)
# ---------------------------------------------------------------------------

StudyMode = Literal["study", "practice", "prepare"]

# Estados del resultado de estudio (Spec 03: estados de carga/error/vacío)
StudyResponseStatus = Literal["complete", "insufficient_evidence", "limited"]

# Tipos de pregunta de práctica (Spec 03 §32: dentro de límites configurados)
QuestionType = Literal["multiple_choice", "short_answer", "conceptual"]

# ---------------------------------------------------------------------------
# Códigos de error estables del dominio pedagógico (ADR-0005). Nunca contienen
# texto de excepción ni exponen contenido de documentos de menores.
# ---------------------------------------------------------------------------

StudyErrorCode = Literal[
    "STUDY_NO_EVIDENCE",
    "STUDY_INSUFFICIENT_EVIDENCE",
    "STUDY_INTERNAL",
]

# ---------------------------------------------------------------------------
# Contratos del dominio pedagógico (ADR-0007)
# ---------------------------------------------------------------------------


class StudyContext(StrictModel):
    """Contexto de estudio (reemplaza `SystemProfile`, ADR-0007).

    Derivado de los metadatos reales de la biblioteca (Spec 03 §14: sin
    inventar categorías que no existen).
    """

    subject: str = Field(min_length=1)
    topic: str | None = None
    # Objetivo pedagógico de la sesión (Spec 03 §9-11)
    objective: StudyMode = "study"
    # Nivel del estudiante (12-15 años, Spec 00)
    student_level: Literal["basico", "intermedio", "avanzado"] = "basico"


class LearningObjective(StrictModel):
    """Objetivo pedagógico con criterios de cobertura (reemplaza `Criterion`)."""

    objective_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    # Temas reales de la biblioteca que cubre el objetivo (Spec 03 §14)
    topics: list[str] = Field(default_factory=list)


class SourceReference(StrictModel):
    """Referencia al corpus con documento/página (Spec 03 §27).

    Derivado de los chunks del catálogo (F1): conserva procedencia completa
    (Spec 02 §18). El texto recuperado es dato no confiable (Spec 06 §94):
    se delimita en los prompts, nunca se interpreta como instrucciones.
    """

    document_id: str = Field(min_length=1)
    version_id: str = Field(min_length=1)
    chunk_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    page: int = Field(ge=1)
    excerpt: str = Field(min_length=1)
    content_hash: str = Field(min_length=1)
    retrieval_method: list[Literal["bm25", "vector"]] = Field(min_length=1)


class PracticeQuestion(StrictModel):
    """Pregunta de práctica generada DESDE evidencia recuperada (Spec 03 §33).

    Sin evidencia suficiente no se generan preguntas (Spec 03 §35: no inventar
    ejercicios supuestamente basados en los apuntes).
    """

    question_id: str = Field(min_length=1)
    question_type: QuestionType = "multiple_choice"
    prompt: str = Field(min_length=1)
    # Opciones para multiple_choice (vacía para otros tipos)
    options: list[str] = Field(default_factory=list)
    # Referencias de las evidencias que soportan la pregunta
    evidence_chunk_ids: list[str] = Field(min_length=1)
    # Explicación pedagógica de la corrección (Spec 03 §34)
    explanation: str = Field(min_length=1)


class PracticeSet(StrictModel):
    """Conjunto de preguntas de práctica desde evidencia (reemplaza `AlternativeAnalysis`)."""

    practice_set_id: str = Field(min_length=1)
    topic: str = Field(min_length=1)
    questions: list[PracticeQuestion] = Field(min_length=1)
    # Referencias trazables: cada pregunta cita los chunks que la sustentan
    source_references: list[SourceReference] = Field(default_factory=list)
    # Insuficiencia explícita (Spec 03 §35): si no hay evidencia suficiente,
    # el conjunto NO se genera y se reporta el gap.
    evidence_gaps: list[str] = Field(default_factory=list)


class StudyResponse(StrictModel):
    """Respuesta estructurada de estudio (reemplaza `FinalReport.recommendation`)."""

    status: StudyResponseStatus = "complete"
    topic: str = Field(min_length=1)
    explanation: str = Field(min_length=1)
    # Referencias al corpus con documento/página (Spec 03 §27)
    references: list[SourceReference] = Field(default_factory=list)
    # Aviso explícito cuando la respuesta use fuentes web (Spec 03 §28)
    uses_web_sources: bool = False
    # Insuficiencia reconocida explícitamente (Spec 00/03/04)
    evidence_gaps: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
