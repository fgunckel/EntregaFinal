# ADRs — ADA Study

Índice de decisiones de arquitectura. Todas en estado **Propuesto**: requieren aprobación explícita del usuario antes de implementarse. Alineadas con el Spec Kit v6 (`specs/00`–`specs/06` y `specs/ADR-001`, esta última aprobada).

| ADR | Decisión | Estado |
|---|---|---|
| [ADR-0001](ADR-0001-postgresql-catalog-versions.md) | Catálogo y versiones en PostgreSQL: dos ciclos de estado (editorial/técnico), outbox `pending_index_ops`, dedup explícita | Propuesto |
| [ADR-0002](ADR-0002-pdf-storage.md) | PDF originales en almacén content-addressed con `storage_key` opaca; seguridad Spec 06 | Propuesto |
| [ADR-0003](ADR-0003-pinecone-bm25-rrf.md) | Pinecone (namespace por versión) + BM25 propio + RRF; verificación de ambos destinos antes de activar; sin ChromaDB | Propuesto |
| [ADR-0004](ADR-0004-deletion-restoration-eventual-consistency.md) | Cortina editorial (`withdrawn`/`deleted`), outbox + reconciliador, restauración inmutable | Propuesto |
| [ADR-0005](ADR-0005-error-codes-and-states.md) | Dos enums de estado (editorial/técnico) + `ReviewStatus`; códigos `ING_*`/`DEL_*`/`MOD_*` | Propuesto |
| [ADR-0006](ADR-0006-endpoints-m7-compatibility.md) | `/api/documents`, `/api/moderation/*`, restauración; `/tasks` de M7 intacta; autorización por rol | Propuesto |
| [ADR-0007](ADR-0007-minimal-domain-change-ada-to-ada-study.md) | Cambio de dominio mínimo: contratos y prompts, no topología; recuperación filtrada por editorial | Propuesto |

## Relación con el Spec Kit

- **Specs (`specs/00`–`specs/06`):** fuente de requisitos funcionales. No se modifican en esta integración.
- **`specs/ADR-001` (aprobada):** decisión de almacenamiento e híbrida; los ADR-0001/0003/0004 de este directorio la detallan técnicamente.
- **ADRs (`docs/adr/`):** registro de decisiones técnicas detalladas; derivan de los specs y de `specs/ADR-001`.
- **Plan (`docs/audit/IMPLEMENTATION_PLAN.md`):** secuencia de implementación; deriva de specs + ADRs.
