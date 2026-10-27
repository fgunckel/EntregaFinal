# ADR-0004 — Borrado, restauración y consistencia eventual entre destinos de indexación

- Estado: Propuesto (espera aprobación del usuario). Alineado con `specs/ADR-001` (aprobada) y Specs 02/06.
- Fecha: 2026-10-25 (rev. 2 — integración Spec Kit v6)

## Contexto

Specs 02/06 exigen: eliminar un documento de **todos** los índices afectados, retirar fragmentos parciales si la ingesta falla, garantizar que el contenido viejo no se mezcle con el nuevo, y que la retirada invalida la recuperación **sin depender de la latencia de borrado de Pinecone** (Spec 06 §104). PostgreSQL (ADR-0001), el almacén de archivos (ADR-0002) y Pinecone (ADR-0003) son tres destinos que **no** comparten transacción. La auditoría confirmó que M7 no tiene ningún mecanismo de borrado.

## Decisión

### 1. Cortina de visibilidad a nivel editorial (Spec 06 §104)

La retirada/borrado lógico actúa primero sobre el ciclo **editorial** (`withdrawn`/`deleted` en `documents`, ADR-0001):

1. `editorial_status -> withdrawn` (o `deleted` para borrado definitivo) en una transacción PostgreSQL. El retriever filtra por estados editoriales publicables + versión activa `available`, así los fragmentos dejan de recuperarse **inmediatamente**, aunque los índices aún contengan vectores.
2. El worker ejecuta la purga física: vectores en Pinecone (por namespace de versión), chunks en PostgreSQL, archivo huérfano en el almacén (ADR-0002).
3. La purga se registra como **operación pendiente por destino** en `pending_index_ops` (outbox, ADR-0001) y se reintenta hasta completarse (Spec 02 §50).

**Propiedad clave:** la cortina convierte un problema de transacción distribuida en una secuencia con un único punto de verdad. Nunca hay un estado donde el documento figure como visible con sus índices ya purgados.

### 2. Versiones y namespaces en Pinecone

- Cada versión indexa en su **propio namespace** `v{version_id}`.
- Reemplazar contenido: indexar la versión nueva en su namespace → verificar → apuntar `active_version_id` a la nueva → marcar la vieja `superseded_at` → encolar borrado del namespace viejo vía `pending_index_ops` (asíncrono, tolerante a fallos).
- Borrar el namespace completo es una operación atómica de Pinecone: no hay borrado fragmento a fragmento que pueda dejar residuos.
- **Una falla de la versión nueva no desactiva la activa anterior** (`specs/ADR-001` §20): la activación ocurre solo tras verificación de ambos destinos (ADR-0003).

### 3. Restauración (Spec 02 §49, Spec 06 §74)

- Las versiones son inmutables: restaurar **no reescribe el pasado**.
- Restaurar = apuntar `active_version_id` a una versión histórica + reconciliar sus fragmentos en Pinecone/BM25 si ya no están indexados (vía `pending_index_ops`).
- Si la política exige no reactivar contenido histórico directamente, se registra como nueva versión que lo referencia; la elección se resuelve en TASK-02.

### 4. Reintento controlado de ingesta fallida

- Si la ingesta falla: la versión queda `failed` con `error_code` estable; los fragmentos parciales se aíslan porque el retriever solo expone versiones activas `available` de documentos publicables, y el namespace de la versión fallida se limpia vía `pending_index_ops`.
- Reintentar reutiliza el mismo `document_id`/`version_id` (IDs deterministas), sobrescribiendo vectores parciales — no duplica (patrón M4).
- La versión activa anterior queda intacta (`specs/ADR-001` §20).

### 5. Consistencia eventual explícita (Spec 02 §52)

- PostgreSQL es la fuente de verdad; Pinecone y BM25 son derivables (ADR-0003).
- Patrón **outbox** (`pending_index_ops`): toda indexación/purga se registra primero como operación pendiente por destino; el worker la ejecuta con reintentos idempotentes; un **reconciliador** (al arrancar y periódico) detecta divergencias (versiones publicables sin vectores, namespaces huérfanos, chunks sin indexar) y las repara reconstruyendo desde PostgreSQL.
- Toda operación es **idempotente** por diseño (IDs deterministas + namespaces por versión).

## Consecuencias

- El retiro percibido por el usuario es inmediato (cortina editorial), la purga física es eventual y observable hasta completarse (Spec 02 §68).
- `document_chunks` en PostgreSQL es obligatorio como fuente de verdad de fragmentos (ADR-0001).
- El reconciliador es obligatorio, no opcional: sin él, un fallo de Pinecone dejaría divergencias permanentes.
- Los criterios de Spec 02 (§64, §66, §68, §69, §104 de Spec 06) se satisfacen por cortina editorial + filtrado del retriever + outbox + idempotencia.

## Historial de revisión

- **Rev. 2 (2026-10-25):** integración Spec Kit v6. Cambios: la cortina pasa de estado técnico `deleting` a **cortina editorial** (`withdrawn`/`deleted`), alineada con Spec 06 §104; se agrega restauración de versiones históricas (Spec 02 §49); se agrega patrón outbox `pending_index_ops` (Spec 02 §52); falla de versión nueva no desactiva la activa (`specs/ADR-001` §20); reintento de ingesta fallida reutiliza versión fallida.
- **Rev. 1 (2026-10-25):** versión inicial de TASK-01 (cortina técnica `deleting`).
