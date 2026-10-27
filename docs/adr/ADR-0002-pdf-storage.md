# ADR-0002 — Almacenamiento de PDF originales separado de PostgreSQL

- Estado: Propuesto (espera aprobación del usuario). Alineado con `specs/ADR-001` (aprobada) y `specs/06`.
- Fecha: 2026-10-25 (rev. 2 — integración Spec Kit v6)
- Decisión aprobada previamente: almacenamiento de PDF originales separado de PostgreSQL.

## Contexto

Specs 01/02 exigen conservar el original para reextracción/reindexación, con una `storage_key` persistida en PostgreSQL que **no dependa del nombre del archivo como identidad**. Spec 06 añade requisitos de seguridad: almacenamiento no ejecutable y no servir originales como contenido activo desde rutas arbitrarias. Guardar blobs grandes en PostgreSQL degrada la base del catálogo y complica backups. La auditoría confirmó que ninguna entrega M1–M7 persiste archivos originales.

## Decisión

Los PDF originales se guardan en un **almacén de archivos local** (content-addressable), fuera de PostgreSQL:

- **Raíz configurable** por variable de entorno `ADA_STORAGE_DIR` (default: `./data/storage`), **fuera de `src/`** (Spec 01 §39).
- **Layout content-addressed:** `data/storage/{sha256[:2]}/{sha256}.pdf` — el nombre es la huella del contenido.
- PostgreSQL guarda solo la **`storage_key`** en `document_versions` (ADR-0001). La clave es **opaca** para la API: nunca se expone como ruta navegable ni se construye desde input del usuario.

## Propiedades

- **Dedup natural:** dos versiones con el mismo SHA-256 comparten el mismo archivo físico; no hay copias duplicadas.
- **Integridad verificable:** el nombre del archivo ES su huella; una verificación periódica puede recalcular el hash y detectar corrupción.
- **Borrado seguro:** un archivo se elimina solo cuando **ninguna** versión activa lo referencia (refcount lógico por consulta SQL); el borrado del catálogo es transaccional, el del archivo es posterior y tolerante a fallos (registrado en `pending_index_ops` u operación equivalente).
- **Sin servicio externo:** MVP local; no se añade S3/MinIO. La interfaz queda acotada detrás de funciones tipadas (`save_pdf(bytes) -> storage_key`, `open_pdf(storage_key)`, `delete_if_orphan(storage_key)`) para poder reemplazarla por un backend remoto sin tocar la API.

## Requisitos de seguridad (Spec 06 §92)

- El directorio de almacenamiento **no se sirve** por la web ni por rutas estáticas de la API; el acceso al contenido pasa siempre por el backend y sus permisos.
- Permisos de archivo restrictivos; sin ejecución.
- La validación del archivo real (MIME, tamaño, contenido extraíble) ocurre **antes** de persistir; un archivo rechazado no deja residuos en el almacén.
- No registrar la ruta absoluta ni el contenido en logs/trazas (AGENTS.md regla 11).

## Consecuencias

- Requiere crear `ADA_STORAGE_DIR` con permisos de escritura en el entorno de ejecución (documentado en el README de entrega).
- La eliminación de un documento es en dos fases: (1) transacción en PostgreSQL, (2) recolección de archivos huérfanos; un fallo en (2) deja un archivo huérfano inofensivo, nunca un catálogo inconsistente.
- Los PDF escaneados sin texto extraíble se marcan `failed` con código legible (Spec 02 §57); el original se conserva para permitir reextracción futura si se añade OCR.

## Historial de revisión

- **Rev. 2 (2026-10-25):** integración Spec Kit v6. Cambios: `storage_key` opaca (no identidad por nombre de archivo, Spec 02 §8); requisitos de seguridad de Spec 06 (no ejecutable, no servido, validación previa); raíz fuera de `src/`; borrado de huérfanos registrado como operación pendiente.
- **Rev. 1 (2026-10-25):** versión inicial de TASK-01.
