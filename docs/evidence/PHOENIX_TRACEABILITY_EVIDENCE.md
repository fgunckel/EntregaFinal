# Evidencias de trazabilidad – ADA Study
## Arize Phoenix | Proyecto final AI Engineering

### 1. Objetivo

Se utiliza Arize Phoenix para observar y analizar las ejecuciones del sistema multiagente ADA Study.

Las trazas permiten inspeccionar el recorrido de cada consulta, las decisiones del Supervisor, la recuperación de evidencia mediante RAG, la intervención del modelo de lenguaje, la validación de resultados y la generación de la respuesta final.

Las siguientes capturas documentan dos escenarios reales de ejecución.

---

## 2. TC-02 – Consulta respondida con evidencia documental

**Consulta ejecutada:**

«Según el documento de Historia, ¿cuáles fueron las causas de la Revolución de Mayo?»

**Resultado:** respuesta completa, fundamentada en documentos recuperados y validada por el sistema.

| Archivo | Contenido y evidencia que demuestra |
|---|---|
| `TC-02-full-trace.png` | Vista general de la traza. Muestra el recorrido completo por LangGraph: Supervisor, Researcher, Tutor, Validator y Synthesizer. También permite observar la invocación del modelo y los tiempos de ejecución. |
| `TC-02-researcher-output.png` | Resultado de la recuperación RAG. Muestra cinco fragmentos recuperados mediante BM25, pertenecientes a `Historia_5to_Grado_Guia_Completa.pdf`, con identificación de documentos, páginas y contenido. |
| `TC-02-supervisor-routing.png` | Decisión del Supervisor de continuar hacia el Tutor porque se recuperó evidencia suficiente para elaborar una respuesta pedagógica. |
| `TC-02-tutor-output.png` | Respuesta generada por el Tutor mediante Groq (`openai/gpt-oss-120b`). Incluye explicación y referencias a los fragmentos utilizados. La ejecución registró 2.795 tokens. |
| `TC-02-validator-validation.png` | Validación estructurada de la respuesta. Los ocho controles finalizaron en PASS, sin errores, advertencias, reintentos ni necesidad de revisión humana. |
| `TC-02-synthesizer-output.png` | Resultado final consolidado. Muestra la respuesta completa, su estado y las referencias documentales conservadas después de la validación. |

**Controles del Validator (los ocho en PASS):** `research_artifact_present`, `study_response_present`, `evidence_contract`, `response_contract`, `cited_subset_of_evidence`, `complete_has_references`, `complete_has_pertinent_evidence`, `gaps_acknowledged`.

### 3. TC-04 – Consulta sin evidencia suficiente

**Consulta ejecutada:**

«¿Qué dice la mitosis?»

**Resultado:** el sistema identifica que el corpus disponible no contiene evidencia suficiente y evita generar una respuesta sin fundamento.

| Archivo | Contenido y evidencia que demuestra |
|---|---|
| `TC-04-insufficient-evidence.png` | Traza de la ejecución completa. El Researcher no recupera evidencia, el Tutor no invoca el LLM y el sistema devuelve `insufficient_evidence`. La respuesta incluye la explicación de la limitación, sin referencias inventadas. |

Esta ejecución demuestra el manejo de evidencia insuficiente y la prevención de respuestas no fundamentadas. La validación del resultado final también fue satisfactoria.

---

## 4. Arquitectura observable

Las ejecuciones permiten observar el siguiente flujo:

**Supervisor → Researcher → Supervisor → Tutor → Supervisor → Validator → Synthesizer**

El recorrido puede variar según el resultado de la investigación. Cuando no se recupera evidencia suficiente, el sistema evita ejecutar el Tutor y continúa hacia la validación y síntesis del resultado de insuficiencia.

## 5. Alcance de las evidencias

Las capturas documentan ejecuciones reales observadas en Arize Phoenix. Permiten verificar:

- Orquestación multiagente mediante LangGraph.
- Decisiones y enrutamiento del Supervisor.
- Recuperación documental mediante BM25.
- Generación mediante un modelo LLM cuando corresponde.
- Validación de contratos, referencias y evidencia.
- Síntesis de respuestas finales.
- Manejo de consultas sin evidencia suficiente.
- Tiempos de ejecución y métricas de utilización del modelo.

**Nota:** estas evidencias corresponden a los escenarios y consultas identificados en cada sección. No implican que todas las funcionalidades de recuperación o persistencia estén demostradas por estas capturas.
