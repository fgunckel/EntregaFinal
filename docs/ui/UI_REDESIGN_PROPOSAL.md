# UI-REDESIGN-01 — Auditoría visual y propuesta de rediseño de ADA Study

| Campo | Valor |
|---|---|
| Tarea | UI-REDESIGN-01 (auditoría + propuesta, **sin implementación**) |
| Fecha | 2026-11-02 |
| Alcance | Solo capa de presentación (`src/ada_study/ui/`); sin cambios en API, agentes, RAG, checkpointer, datos ni lógica de negocio |
| Estado | **PROPUESTA — pendiente de aprobación para UI-REDESIGN-02** |

---

## 1. Resumen ejecutivo

ADA Study funciona correctamente de punta a punta (login real, biblioteca con
ingesta asincrónica, estudio con RAG y trazabilidad, moderación y auditoría),
pero su capa visual es **Streamlit puro sin una sola línea de personalización**:
no existe `.streamlit/config.toml`, no hay CSS propio, no hay tema, no hay
sistema de componentes. El resultado es la apariencia por defecto de la
herramienta, con tres consecuencias visibles:

1. **Lee como herramienta interna, no como producto**: la UI expone jerga
   técnica al usuario final (códigos HTTP "(202)", identificadores internos
   "Job: &lt;uuid&gt;", referencias a ADRs, códigos de error crudos como
   `DEL_NOT_FOUND`, estados editoriales en inglés sin traducir).
2. **No hay identidad visual**: cada página improvisa su propia jerarquía; hay
   tres mecanismos distintos de mostrar estados (emoji crudo, texto crudo en
   inglés, función de badge que existe pero **nunca se invoca**), y emojis
   intercalados arbitrariamente en botones y títulos.
3. **El chat no es protagonista**: la pantalla de estudio —el corazón del
   producto— es un formulario con la respuesta renderizada debajo y el
   historial escondido en un expander colapsado; la conversación se lee de
   forma no natural y la trazabilidad convive con la respuesta sin separación
   clara.

La propuesta define una identidad visual propia ("**tinta y aula**": fondo
cálido off-white, tinta oscura, un único acento índigo, estados semánticos
verde/ámbar/rojo), un mini design-system de **9 componentes reutilizables**, y un
plan de implementación en **8 etapas numeradas de 0 a 7** que no toca ni una línea
de lógica de negocio, no agrega dependencias y preserva la arquitectura actual
(Single-page Streamlit + `ApiClient` puro + página por módulo).

**Restricción de entorno declarada:** la aplicación sí fue ejecutada y
verificada viva (Compose con `api`, `ui`, `postgres`, `phoenix`, `worker`
healthy; la UI responde HTTP 200 en `:8501`), pero este entorno no permite
capturar pantallas del navegador. El diagnóstico combina esa verificación de
ejecución con la lectura exhaustiva del código de render (que determina
unívocamente lo que Streamlit pinta). Los marcadores **[SIN CAPTURA]**
identifican los puntos donde una captura del navegador complementaría la
evidencia.

---

## 2. Diagnóstico de la interfaz actual

### 2.1 Metodología y fuentes

- Lectura completa del código de render: `src/ada_study/ui/app.py`,
  `auth.py`, `api_client.py`, `trace_view.py` y las 6 páginas de
  `ui/pages/` (login, home, study, library, moderation, audit).
- Verificación de ejecución real: `docker compose ps` (5 servicios up 3 h,
  healthy) e `Invoke-WebRequest http://localhost:8501` → **HTTP 200**
  (shell HTML de Streamlit, sin tema personalizado: `<title>Streamlit`).
- Versión del framework: **Streamlit 1.66.1** (soporta `st.Page`/`st.navigation`,
  `st.fragment`, `st.badge`, `st.chat_message`, theming por `config.toml`).
- No es posible capturar el navegador en este entorno: el diagnóstico visual se
  apoya en que el código de render es determinista (no hay CSS custom ni temas),
  por lo que el aspecto puede describirse con exactitud desde el código.
  ### 2.2 Hallazgos globales (afectan a todas las pantallas)

| # | Hallazgo | Evidencia | Severidad |
|---|---|---|---|
| G1 | **Cero personalización visual**: sin `.streamlit/config.toml`, sin `st.markdown(..., unsafe_allow_html=True)`, sin `<style>`, sin theme. Todo el look es el default de Streamlit. | No existe `.streamlit/`; búsqueda de `.css(` y `<style` = 0 resultados en todo el repo | Alta |
| G2 | **`layout="wide"` sin contenedor de lectura**: el contenido ocupa todo el ancho; en desktop una respuesta del tutor se convierte en líneas de 150+ caracteres, agotando la lectura. | `app.py:21` (`layout="wide"`); ningún `max-width` en ningún lado | Alta |
| G3 | **Tres lenguajes de estado conviviendo**: (a) función `_status_badge` con emojis (🟡🟢🔴⚪) definida y **nunca invocada**; (b) `EDITORIAL_LABELS` en español también sin usar; (c) el render real muestra el enum crudo en inglés (`Estado: pending_review`, `published_pending_review`). | `library.py:64-73` (definición sin uso), `library.py:132` (caption con enum crudo), `home.py:16-24` (labels sin usar), `home.py:53-56` (mapa parcial con fallback al enum crudo) | Alta |
| G4 | **Jerga de desarrollador filtrada al producto**: mensajes muestran códigos HTTP ("Documento registrado (202)", "Borrado aceptado (202)"), IDs internos ("Job: &lt;uuid&gt;"), referencias a ADRs ("la purga física de los índices es eventual (ADR-0004)", "cortina inmediata") y códigos de error estables ("No se pudo cargar la biblioteca (DEL_NOT_FOUND)") que solo significan algo para el equipo. | `library.py:113-117, 205-209, 216`; `home.py:38`; `study.py` (parcial: algunos errores sí están humanizados, otros no) | Alta |
| G5 | **Emojis como sistema de iconografía**: ✅⚠️🟡📄🗑️🔁📤⏳🔍❌❔⏭️🔄 mezclados de forma inconsistente — algunos botones tienen emoji ("🗑️ Borrar documento") y su vecino no ("Ver detalle"). | `library.py:203,213,222` vs `library.py:149`; `trace_view.py:63-70` | Media |
| G6 | **Estados vacíos y errores inconsistentes**: cada página redacta su propio mensaje; unos son amables ("Todavía no hay documentos visibles…"), otros exponen códigos; no hay componente ni tono comunes. | `home.py:38,48`; `library.py:29-31,180-184`; `moderation.py:15-23`; `audit.py:14-18` | Media |
| G7 | **Polling bloqueante**: la ingesta hace `time.sleep(2)` + `st.rerun()` en el hilo principal: la app completa se congela/redibuja cada 2 s durante la ingesta, y el expander de upload (colapsado por defecto) se reinicia en cada rerun. | `library.py:171-173` | Media |
| G8 | **Sin identidad de marca**: el único gesto de identidad es el emoji 📚 en el favicon (`app.py:20`); el sidebar dice "ADA Study" en texto plano. | `app.py:18-23, 29` | Media |
| G9 | **Navegación que no refleja el rol**: el `st.radio` muestra SIEMPRE las 5 opciones, incluidas "Moderación" y "Auditoría" (solo moderator/admin). Un estudiante las ve y al entrar recibe un error de permisos en vez de no verlas. | `app.py:41-51` (lista fija) vs `moderation.py:21-23` / `audit.py:14-18` (manejo del 403 que sí existe) | Media |
| G10 | **Densidad y alineación irregulares**: filas de 3 columnas con anchos ad hoc (`[3,2,1]`, `[3,1]`, tres iguales) en cada página sin patrón común; contenido clave y acciones comparten la misma fila sin jerarquía. | `library.py:135`; `study.py:163`; `moderation.py:51`; `audit.py:29` | Media |

### 2.3 Diagnóstico por pantalla

#### Login (`pages/login.py`)
- **Aspecto actual:** `st.title("ADA Study")` + "Iniciar sesión" + tres inputs
  apilados (URL de la API, Correo, Contraseña) + botón "Entrar". Formulario
  Streamlit default, centrado, sin marca, sin ilustración, sin pie.
- **Problemas:** (1) el campo **"URL de la API" es lo primero que ve un
  estudiante** — un detalle de configuración de despliegue expuesto como campo
  principal del login comunica "herramienta interna" (los valores técnicos
  deberían ser un ajuste avanzado colapsado); (2) sin jerarquía: título y
  subheader compiten; (3) errores correctos pero sin ubicación fija (aparecen
  bajo el botón tras el submit).
- **Lo que está bien:** mensajes de error ya humanizados y diferenciados por
  código (`AUTH_INVALID_CREDENTIALS`, `AUTH_ACCOUNT_NOT_ACTIVE`); botón ancho
  full-width correcto.

#### Mi centro de estudio (`pages/home.py`)
- **Aspecto actual:** título + "Mi biblioteca" + fila de métricas + listado de
  documentos como bullets markdown (`- filename — draft`) dentro de contenedores
  con borde.
- **Problemas:** (1) **bug de layout**: `cols[0].metric(...)` dentro del loop
  apila TODAS las métricas en la primera columna (`home.py:56`: `zip(cols,
  counts.items())` pero siempre renderiza en `cols[0]`); (2) etiquetas de
  métrica solo mapean `published`/`pending_review` — el resto muestra el enum
  crudo en inglés; (3) la lista es texto plano sin acciones ni estados
  visuales; (4) la página duplica el concepto de "biblioteca" con una lista
  pobre de un solo nivel, sin enlace al detalle (la biblioteca real sí lo
  tiene): dos lugares que muestran lo mismo con distinta calidad.
- **Lo que está bien:** honestidad de datos (sin métricas inventadas), el
  principio de "mostrar solo lo real" es correcto y se conserva.

#### Estudiar (`pages/study.py` + `trace_view.py`) — pantalla crítica
- **Aspecto actual:** título + caption aclaratorio; si hay conversación activa,
  un botón "Nueva conversación" en una columna a la derecha y un caption
  "Conversación activa"; debajo, un expander colapsado "Turnos anteriores (n)"
  con la historia como texto (`**Vos:** … / **ADA:** …`); debajo, el
  `text_area` de pregunta con contador de caracteres; botón "Consultar"; tras
  enviar, la respuesta aparece ABAJO del input.
- **Problemas:**
  1. **Paradigma de formulario, no de chat** (el caso más grave del producto):
     el usuario escribe ARRIBA y lee la respuesta más abajo; en la siguiente
     pregunta la respuesta anterior desaparece de la vista principal y pasa a
     vivir dentro de un expander colapsado arriba. La lectura conversacional es
     descendente pero el contenido nuevo aparece en el pie.
  2. La respuesta del tutor se renderiza como un único `st.markdown`
     (`study.py:78`) sin estructura de lectura: sin separación visual entre la
     explicación, las referencias (expander `expanded=True` que empuja todo),
     los avisos y las limitaciones.
  3. **Referencias con jerga**: "📄 **título** — página 3 (recuperado por
     bm25)" expone el nombre del mecanismo de recuperación; el excerpt —el
     contenido que valida la respuesta— va en `st.caption` (gris chico), es
     decir, la evidencia es lo menos legible de la pantalla.
  4. Estado de la respuesta como subheader con emoji ("✅ Respuesta con
     evidencia suficiente"); limitaciones concatenadas con `" | "` en un
     `st.info` de una línea (`study.py:112-114`).
  5. Sin `chat_input` ni burbujas: en una conversación multi-turno el usuario
     scrollea entre un expander arriba y el input fijo, con el foco visual
     partido.
  6. El expander de trazabilidad vive inmediatamente después de la respuesta,
     con filas de texto markdown denso (`**Supervisor** · ✅ Completado ·
     120 ms`) y **expanders anidados** ("Ver detalle" dentro del expander de
     traza): patrón de consola de debugging dentro del flujo de lectura.
- **Lo que está bien:** el contrato de honestidad (solo se muestra lo que
  devuelve la API; `trace` por turno; historial solo si hay `conversation_id`)
  es una decisión de producto excelente que el rediseño debe preservar; el
  contador de caracteres y la prevención de doble envío están bien pensados.

#### Panel de trazabilidad (`trace_view.py`)
- **Aspecto actual:** expander "¿Cómo se construyó esta respuesta?" con una
  fila por agente: `**Nombre** · ✅ Completado · 120 ms` + caption con la
  responsabilidad + resumen + expander anidado "Ver detalle" con líneas
  markdown ("- Fragmentos aceptados: 3").
- **Problemas:** (1) parece una **consola de debugging**: listas de bullets
  homogéneas sin diferenciación visual entre decisión, resultado y motivo; (2)
  expanders anidados (traza → detalle) duplican la profundidad de clics; (3)
  los 5 agentes aparecen SIEMPRE como lista plana — no comunica el flujo
  (secuencia, reintentos, ciclo de reparación); (4) emojis de estado (✅🔄⏭️⚠️❌❔)
  con tamaño de texto corrido, sin pastillas ni color semántico.
- **Lo que está bien:** la semántica de estados es impecable (derivada solo de
  eventos reales, con reglas explícitas y testeables); el contenido es oro, el
  **recipiente** es lo que hay que rediseñar: debe sentirse como la
  característica diferencial del producto, no como un log.

#### Mi biblioteca (`pages/library.py`)
- **Aspecto actual:** expander de upload colapsado arriba + divisor + lista de
  contenedores con borde (filename / "Estado: &lt;enum&gt;" / botón "Ver
  detalle"); el detalle del documento seleccionado se renderiza MÁS ABAJO de
  la lista, tras otro divisor.
- **Problemas:** (1) el estado editorial visible es el enum crudo (G3) — la
  función de badge con emojis existe y no se usa; (2) el detalle no es una
  vista: al hacer clic en "Ver detalle" el contenido aparece al final de la
  página (hay que hacer scroll por toda la lista para encontrarlo);
  (3) upload oculto tras dos clics (expander + botón) y sin zona de arrastre
  visual destacada; (4) el polling de ingesta es un caption que cambia de texto
  cada 2 s con la página completa re-dibujándose (G7); (5) mensajes con
  "(202)", "Job: &lt;uuid&gt;", "ADR-0004" (G4); (6) acciones de destrucción
  (borrar) al mismo nivel visual y sin confirmación que acciones seguras.
- **Lo que está bien:** las reglas de acción son correctas y ya probadas
  (`can_submit_for_review`, `describe_submit_error`): solo hay que
  re-empaquetarlas visualmente.

#### Moderación (`pages/moderation.py`)
- **Aspecto actual:** lista de contenedores con borde: filename — estado crudo;
  caption "Autor: &lt;uuid&gt;"; 3 columnas: [✅ Aprobar] | [input Motivo del
  rechazo + ❌ Rechazar] | [input Motivo del retiro + ⚪ Retirar].
- **Problemas:** (1) **dos campos de texto siempre visibles por documento**
  aunque solo se usará uno — ruido y confusión grave de formulario; (2) el
  autor se muestra como UUID crudo sin nombre; (3) sin vista previa del
  contenido del documento antes de decidir (decidir a ciegas); (4) botones
  destructivos y constructivos con el mismo peso visual.

#### Auditoría (`pages/audit.py`)
- **Aspecto actual:** 3 inputs de filtro en columnas + listado de eventos como
  contenedores: `` `action` — entity_type/entity_id `` + caption de actor,
  resultado y fecha.
- **Problemas:** (1) estética de **log de consola**: monospace crudo, UUIDs
  completos, nombres de campo internos ("entity_type/entity_id"); (2) sin
  paginación real (limit hasta 500 en un solo render); (3) sin colores
  semánticos por `result`; (4) filtros que piden escribir IDs a mano en vez de
  ofrecer selección.

#### Sidebar y navegación (`app.py`)
- **Aspecto actual:** header de texto, caption "Nombre · rol", divider, radio
  de 5 opciones, botón "Cerrar sesión".
- **Problemas:** (1) sin logo/marca; (2) G9 (opciones no filtradas por rol);
  (3) la identidad del usuario es un caption gris poco visible; (4) el radio
  de Streamlit tiene línea de selección tenue y no comunica la sección actual
  con fuerza.

---

## 3. Inventario de pantallas y componentes

Puntos de entrada: un único app multi-página manual (`ui/app.py`) con router
por `st.radio`; sesión en memoria (`ui/auth.py`); cliente HTTP puro
(`ui/api_client.py`, ~310 líneas, testeado con `httpx.ASGITransport`).

| Pantalla / componente | Archivo | Líneas | Estado visual actual |
|---|---|---:|---|
| Shell + navegación lateral | `ui/app.py` | 87 | Sidebar default, radio, sin marca |
| Login | `ui/pages/login.py` | 47 | Form default, URL de API visible |
| Mi centro de estudio | `ui/pages/home.py` | 63 | Métricas apiladas (bug) + bullets crudos |
| Estudiar (consulta + respuesta + turnos) | `ui/pages/study.py` | 218 | Formulario, sin chat, historial en expander |
| Panel de trazabilidad | `ui/trace_view.py` | 411 | Expander con expanders anidados, log-like |
| Mi biblioteca (lista + upload + detalle) | `ui/pages/library.py` | 233 | Contenedores, estado crudo, polling bloqueante |
| Moderación | `ui/pages/moderation.py` | 88 | Formulario doble siempre visible, UUIDs |
| Auditoría | `ui/pages/audit.py` | 59 | Log de consola sin paginación |
| Sesión (memoria) | `ui/auth.py` | 70 | — (no visual) |
| Cliente API | `ui/api_client.py` | ~314 | — (no visual; NO tocar) |

Estados gestionados hoy: vacíos (`st.info`), errores (mezcla de texto humano y
códigos crudos), éxito (`st.success` con jerga), advertencias (`st.warning`),
polling de ingesta (caption + rerun). No hay skeletons, no hay spinners
contextuales por operación, no hay confirmaciones para acciones destructivas.

---

## 4. Problemas visuales y de UX detectados (síntesis priorizada)

**P0 — rompen la percepción de producto (atacar primero):**
1. Jerga técnica y estados crudos en inglés visibles al usuario (G3+G4).
2. Paradigma formulario en la pantalla de estudio (§2.3 Estudiar, punto 1).
3. Detalle de documento al pie de la lista (biblioteca) y métricas apiladas
   (home, bug real de `cols[0]`).

**P1 — degradan la experiencia:**
4. Moderación con doble input siempre visible y sin vista previa.
5. Trazabilidad con estética de consola y expanders anidados.
6. Polling bloqueante con rerun de página completa.
7. Navegación sin filtrado por rol + sidebar sin marca.

**P2 — pulido:**
8. Sin tema/espaciado/tipografía propia (G1), ancho de lectura (G2),
   inconsistencia de emojis (G5), estados vacíos/errores sin patrón (G6),
   auditoría sin paginar (§2.3), destructivos sin confirmación visual.

Los problemas **funcionales existentes no se tocan**: reglas de permisos,
estados, contratos y mensajes ya probados se conservan tal cual; el rediseño
reescribe **cómo se pintan**, no **qué** se pinta ni **qué se llama**.

---

## 5. Dirección visual propuesta

**Concepto: "Tinta y aula"** — un cuaderno de estudio digital serio y cálido.
El producto se percibe como un espacio de estudio personal: papel claro, tinta
oscura, un único color de acento que señala lo interactivo, y color semántico
reservado exclusivamente para estados. La trazabilidad de agentes se presenta
como una **línea de tiempo del razonamiento**, con el mismo lenguaje visual del
resto del producto.

Principios:

1. **El contenido manda.** Cero decoración por sí misma: borde 1px, sombras
   casi nulas, superficies planas. El color se usa para estado y acción, nunca
   como decoración.
2. **Un acento, tres semánticos.** Índigo para identidad y acción primaria;
   verde/ámbar/rojo SOLO para estados editoriales y de ejecución. Nada más.
3. **La evidencia es contenido de primera clase**: referencias y trazas no son
   notas al pie grises; son bloques legibles con jerarquía propia.
4. **Una sola familia de componentes**: misma tarjeta, misma pastilla de
   estado, mismo encabezado de sección en las 6 pantallas.
5. **Iconografía mínima y consistente**: sin emojis en botones ni títulos; los
   pocos íconos (documento, estado, usuario) con un solo set y un solo tamaño.
   Los emojis actuales desaparecen de labels y botones.
6. **Ancho de lectura acotado**: contenido principal con `max-width` (via
   CSS del bloque de contenido) para respuestas de ~70–80 caracteres por
   línea; el `layout="wide"` se conserva para listas/tablas que aprovechan
   el ancho.
7. **El estado de una cosa SIEMPRE se pinta igual**: pastilla con punto de
   color + etiqueta en español, un único diccionario de estados en un módulo
   compartido (`ui/theme.py`), eliminando los tres lenguajes actuales.

---

## 6. Paleta de colores y tipografía sugeridas

### 6.1 Paleta (8 tokens, sin exceso)

| Token | Valor | Uso |
|---|---|---|
| `--bg` | `#FAF9F7` (papel cálido) | Fondo de app |
| `--surface` | `#FFFFFF` | Tarjetas, paneles |
| `--ink` | `#1A1D29` | Texto principal |
| `--muted` | `#6B7280` | Texto secundario, captions |
| `--line` | `#E8E5E0` | Bordes 1px, divisores |
| `--accent` | `#4338CA` (índigo) | Botones primarios, links, selección, marca |
| `--accent-soft` | `#EEF0FF` | Fondo de burbuja del usuario, hover, resaltados suaves |
| `--ok` / `--warn` / `--danger` | `#188652` / `#B45309` / `#B42318` | Estados (publicado / revisión-fallo / rechazo-error) — solo con etiqueta de texto, nunca color solo |

Mapa de estados (único en `ui/theme.py`): `published/published_pending_review →
verde` · `draft/pending_review → ámbar` · `rejected → rojo` ·
`withdrawn/deleted → gris`. Estados técnicos de versión (queued/processing/
indexing/available/failed) → gris/índigo/índigo/verde/rojo con etiqueta en
español.

### 6.2 Tipografía

- **Base:** pila del sistema (`system-ui, -apple-system, Segoe UI, Roboto`)
  vía theming de Streamlit — legible, cero dependencias, cero descargas.
- **Escala:** H1 28/36, H2 20/28, body 16/26, caption 13/20. Los títulos de
  página bajan un escalón respecto al default de Streamlit (`h1 { font-size }`
  por CSS) para recuperar sobriedad.
- **Mono** solo para identificadores técnicos (trace_id, conversation_id),
  siempre con copia al portapapeles.
- Opcional (decisión pendiente, §12): Inter auto-hospedada si se quiere un
  carácter tipográfico propio; se evalúa en la etapa 7.

### 6.3 Forma y espacios

- Tarjetas: radio 12px, borde `--line`, sin sombra (o sombra `0 1px 2px` al
  4%); padding interno 20px; gap entre tarjetas 12px.
- Ritmo vertical: 24px entre bloques; secciones separadas por título de
  sección (kicker en mayúsculas pequeñas + título), no por `st.divider`
  consecutivos como hoy.

---

## 7. Propuesta de layout por pantalla

Wireframes en texto (suficientemente precisos para implementar). Convención:
`▓` superficie/tarjeta, `░` zona acentuada, `→` acción, `···` estado pastilla.

### 7.1 Shell global y navegación

- Sidebar con **marca propia**: bloque de logo ("ADA" en índigo + "Study"),
  tarjeta de usuario compacta (nombre visible + rol como pastilla), navegación
  con `st.navigation`/`st.Page` (API moderna de 1.66) filtrada **por rol real
  de `/api/me`**: el estudiante nunca ve Moderación/Auditoría (el backend sigue
  siendo la frontera; la UI solo deja de anticipar opciones imposibles).
- El router por `st.Page` mantiene el mismo mapeo actual de páginas; el cambio
  es del `st.radio` al mecanismo nativo (breadcrumb + URL estable).

```
┌────────────┬──────────────────────────────────────────────────┐
│ ADA Study  │  (contenido de la página, max-width lectura)     │
│ ─────────  │                                                  │
│ ▸ Estudiar │   [título de página + kicker]                    │
│   Biblioteca                                                  │
│   Mi centro│   …contenido…                                    │
│            │                                                  │
│ 👤 Ana ·   │                                                  │
│ estudiante │                                                  │
│ [Salir]    │                                                  │
└────────────┴──────────────────────────────────────────────────┘
```

### 7.2 Login

- Dos zonas: panel izquierdo de marca (logo "ADA Study", una frase de valor,
  nota de versión) y columna derecha con el formulario en tarjeta centrada,
  `max-width ~380px`.
- **"URL de la API" pasa a "Opciones avanzadas"** (expander colapsado bajo el
  formulario, con valor por defecto preservado): el estudiante ve Correo +
  Contraseña + Entrar, nada más.
- Mensajes de error en una franja fija bajo el formulario (componente único).

```
┌──────────────────────────────────────────────┐
│  [logo]  ADA Study                           │
│          Estudiá con evidencia de tu         │
│          biblioteca.                         │
│  ┌──────────────────────────┐                │
│  │ Correo                   │                │
│  │ Contraseña               │                │
│  │ [        Entrar        ] │                │
│  └──────────────────────────┘                │
│  ▸ Opciones avanzadas (URL de la API)        │
└──────────────────────────────────────────────┘
```

### 7.3 Mi centro de estudio

- Hero compacto: "Hola, &lt;nombre&gt;" + resumen en una fila de tarjetas
  métricas (una por estado, arregladas en fila real — corrige el bug de
  `cols[0]`), cada una clicable → lleva a Biblioteca con ese filtro.
- Debajo, "Continuar donde dejaste": última conversación activa si existe
  (solo si hay `conversation_id` real en sesión) y accesos a Estudiar y
  Biblioteca. Nada de métricas inventadas: solo lo que la API devuelve.

```
┌──────────────────────────────────────────────┐
│ HOLA, MARÍA                                  │
│ 3 publicados · 1 en revisión · 1 borrador    │  ← pastillas clicables
├──────────────────────────────────────────────┤
│ Continuar: Conversación del 02/11 → Estudiar │
└──────────────────────────────────────────────┘
```

### 7.4 Biblioteca

- **Barra superior**: título + botón primario "Subir PDF" (abre el panel de
  carga expandido, no un expander escondido) + filtro por estado (pills
  `st.pills` reales, cliente, sin tocar API).
- **Lista**: una tarjeta por documento en grilla responsiva (2–3 columnas en
  desktop, 1 en móvil): nombre, materia, pastilla de estado editorial en
  español, fecha relativa; clic en la tarjeta → detalle.
- **Detalle como vista propia** (misma página, estado de selección, sin
  duplicar rutas): cabecera con nombre + estado + acciones autorizadas;
  acciones seguras visibles, **Borrar con confirmación previa**
  (`st.dialog`) y estilo destructivo.
- **Ingesta visible**: tras subir, la tarjeta muestra un `st.status` con las
  etapas en español; el polling pasa a `st.fragment(run_every=2)` (sin
  congelar la app) con `st.progress`/`st.status` por etapa real del job
  (queued → processing → indexing → available).

```
┌ Mi biblioteca ────────────────── [ Subir PDF ] ┐
│ (Todos)(Publicado)(En revisión)(Borrador)      │
│ ┌────────────┐ ┌────────────┐ ┌────────────┐   │
│ │ teoria.pdf │ │ guia2.pdf  │ │ apunte.pdf │   │
│ │ ● Publicado│ │ ● En revis.│ │ ● Borrador │   │
│ │ Matemática │ │ Historia   │ │ Matemática │   │
│ └────────────┘ └────────────┘ └────────────┘   │
└────────────────────────────────────────────────┘
Detalle (al seleccionar, arriba de la lista):
┌ guia2.pdf ── En revisión ──────────────────────┐
│ [Enviar a revisión]  [Borrar]  [Reintentar]    │
└────────────────────────────────────────────────┘
```

### 7.5 Experiencia de estudio (chat protagonista)

- **Paradigma de chat real** con `st.chat_message` + `st.chat_input`:
  - Turnos previos: burbuja del usuario en `--accent-soft`, respuesta de ADA
    en tarjeta blanca a ancho completo.
  - El input vive **fijo al pie** (`st.chat_input`): escribir y enviar es el
    mismo gesto que en cualquier app de IA.
  - "Nueva conversación" como botón secundario junto al encabezado.
- **Respuesta estructurada en 4 bloques con jerarquía fija** (solo datos del
  `StudyResponse`):
  1. **Pastilla de estado** ("Evidencia suficiente" / "Evidencia
     insuficiente" / "Respuesta limitada") — verde/ámbar/gris, sin emoji.
  2. **Explicación** — el markdown del tutor, tipografía de lectura (16/26,
     ancho acotado); los `evidence_gaps` como lista legible, no captions.
  3. **Referencias** — bloque propio, NO expander colapsable por defecto: cada
     referencia es una mini-tarjeta con título, página y excerpt legible a
     tamaño normal; el mecanismo (`bm25`) pasa a texto de usuario ("coincidencia
     por palabras clave") o se omite si no aporta.
  4. **Avisos y limitaciones** — `st.warning`/`st.info` con texto redactado
     (sin " | " concatenado), solo cuando la respuesta los declara.
- Fórmulas: `st.markdown` ya renderiza LaTeX `$…$`; se normaliza el estilo de
  listas/citas del tutor vía CSS (sin tocar al tutor).
- En pantalla también queda visible el indicador de conversación activa
  ("Conversación activa" + botón "Nueva conversación" en el encabezado, no
  flotando junto al caption).

```
┌ Estudiar ────────────────── [Nueva conversación] ┐
│ ┌─(ADA)───────────────────────────────────────┐  │
│ │ ● Evidencia suficiente                      │  │
│ │ La función cuadrática… (markdown legible,   │  │
│ │ títulos, listas, fórmulas)                  │  │
│ │                                              │  │
│ │ ── Fuentes (2) ─────────────────────────    │  │
│ │ ▸ Apunte de álgebra — pág. 12                │  │
│ │   «…excerpt legible a tamaño normal…»       │  │
│ │ [¿Cómo se construyó esta respuesta? ▸]      │  │
│ └──────────────────────────────────────────────┘ │
│ (turnos anteriores como burbujas encadenadas)    │
├──────────────────────────────────────────────────┤
│ [ Escribe tu pregunta…                    (→) ]  │
└──────────────────────────────────────────────────┘
```

### 7.6 Trazabilidad (dentro de Estudiar)

- Permanece **dentro** de la respuesta (no interfiere en la lectura: colapsada
  por defecto), pero rediseñada como **línea de tiempo del razonamiento**:
  - Fila por agente: punto de color de estado + nombre + etiqueta ("Completado",
    "Omitido"…) + duración; al lado, el resumen en una línea.
  - El detalle por agente deja de ser expander anidado: se abre en un
    `st.expander` único por agente con secciones rotuladas (Decisión /
    Resultado / Motivo) y tipografía diferenciada — sin listas planas.
  - Identificadores (trace_id, hilo) en pie del panel con mono + copiar.
- La semántica de estados de `trace_view.py` se conserva exacta (es lo mejor
  del módulo); solo cambia el recipiente visual.

```
▸ ¿Cómo se construyó esta respuesta?
  ● Supervisor      Completado · 12 ms
    → Continuar con Researcher
  ● Researcher      Completado · 890 ms
    3 fragmentos aceptados · 1 rechazado
  ● Tutor           Completado · 1.2 s
  ● Validator       Completado · 8 ms
  ● Synthesizer     Omitido
  ID de traza: 8f3c…  [copiar]
```

### 7.7 Moderación

- Tarjeta por documento con: nombre, estado actual, autor **por nombre visible**
  (el backend ya provee identidad; si no hay endpoint de nombre, mostrar el
  email del actor si el contrato lo trae — verificar en UI-REDESIGN-02), y
  **decisión en un solo selector**: `st.segmented_control` o radio
  (Aprobar / Rechazar / Retirar) que muestra UN solo campo de motivo cuando
  corresponde. Botón primario solo para Aprobar; Rechazar/Retirar con estilo
  secundario/destructivo.

```
┌ guia2.pdf · En revisión · autor: Ana ───────────┐
│ (Aprobar) (Rechazar) (Retirar)  ← segmented     │
│ Motivo: [____________________]  (solo si aplica)│
│                                     [Confirmar] │
└─────────────────────────────────────────────────┘
```

### 7.8 Auditoría

- Filtros en una barra compacta (tipo de entidad como `st.selectbox` con las
  opciones reales del dominio, límite con selector de página); tabla con
  columnas (acción, entidad, actor, resultado con pastilla, fecha) usando
  `st.dataframe` estilizado o tarjetas compactas; **paginación cliente**
  (25 por página) sobre los datos ya descargados.

---

## 8. Componentes reutilizables a desarrollar

Todos en un módulo `ui/theme.py` (tokens + CSS + helpers) y `ui/components.py`
(render puro, testeable con AppTest como el resto de la UI):

| Componente | Qué es | Reemplaza a |
|---|---|---|
| `status_pill(status)` | Única pastilla de estado editorial/técnico: punto de color + etiqueta en español. Un solo diccionario (`ui/theme.py`). | `_status_badge` (muerta), captions con enum crudo, mapa parcial de home |
| `page_header(kicker, title, actions)` | Encabezado de página: kicker en mayúsculas + título + acciones a la derecha | `st.title` + `st.caption` sueltos |
| `doc_card(document, on_select)` | Tarjeta de documento (nombre, materia, estado, acción) para lista y moderación | Contenedores con bullets crudos |
| `answer_block(response)` | Bloque de respuesta estructurado (estado → explicación → fuentes → avisos) | `_render_response` actual |
| `trace_panel(trace)` | Línea de tiempo de agentes (§7.6) | `render_trace` con expanders anidados |
| `empty_state(titulo, texto, accion)` | Estado vacío ilustrado con llamado a la acción único | `st.info` dispersos |
| `error_box(exc, mapping)` | Mensaje de error humanizado por código estable, con tono común; el código estable se loguea en la consola/trace, no al usuario | `st.error(f"…({exc.error_code})")` |
| `ingest_status(job)` | Seguimiento de ingesta con `st.status`/`st.progress` y `st.fragment(run_every=2)` | Polling bloqueante `time.sleep(2)` |
| `confirm_destructive(...)` | Diálogo de confirmación (`st.dialog`) para Borrar | Botón directo actual |

Carga de CSS: un único `st.markdown("<style>…</style>", unsafe_allow_html=True)`
en `app.py` con el bloque de tokens; **sin `unsafe_allow_html` para contenido de
datos** (solo el CSS shell), sin iframes, sin componentes custom.

**Dependencias nuevas: ninguna.** Todo lo propuesto (theming por config,
`st.Page/st.navigation`, `st.fragment`, `st.pills`, `st.dialog`,
`st.chat_message/chat_input`, `st.badge`) está incluido en Streamlit 1.66.1.

---

## 9. Referencias visuales (2–3)

1. **Notion** — producto de conocimiento con fondo cálido y superficies planas:
   aporta el modelo de "documento como superficie de lectura" (ancho acotado,
   jerarquía tipográfica sobria, referencias como bloques citables). Para ADA:
   la tarjeta de documento, el ancho de lectura y la presentación de
   referencias/excerpts.
2. **Khan Academy / Duolingo (capa de estudio)** — la cara "educativa y
   amigable": resúmenes de progreso con tarjetas grandes y honestas, estados
   con color semántico siempre acompañado de texto, tono cercano en vacíos
   ("Todavía no subiste material"). Para ADA: la fila de métricas del Home, el
   tono de mensajes y el uso de un solo acento con estados claros.
3. **Perplexity / ChatGPT (capa de estudio)** — el patrón de chat con
   **fuentes de primera clase**: respuesta arriba, citas numeradas bajo cada
   párrafo, fuentes como mini-tarjetas con excerpt; input fijo al pie. Para
   ADA: la estructura de la respuesta (explicación → fuentes → "cómo se
   construyó") y el gesto de conversación.

No se copian interfaces ni funcionalidades: se toman decisiones de layout,
jerarquía y presentación de evidencia dentro del alcance actual (biblioteca,
estudio, trazabilidad, moderación, auditoría).

---

## 10. Plan de implementación por etapas (para UI-REDESIGN-02)

Cada etapa entrega valor visible y deja la suite de tests en verde. Sin
dependencias nuevas en ninguna etapa.

| Etapa | Contenido | Riesgo |
|---|---|---|
| **0. Base visual** | `.streamlit/config.toml` (tema: colores, tipografía, radio) + `ui/theme.py` con tokens/CSS y diccionarios únicos de estados; corrección del bug de métricas en home | Baja |
| **1. Limpieza de lenguaje** | Pastillas de estado en español en todas las pantallas; reescritura de mensajes sin jerga ("(202)", "Job:", "ADR-0004"); `error_box` con tono común | Baja |
| **2. Shell y navegación** | `st.navigation` con filtrado por rol real; marca en sidebar; tarjeta de usuario; ancho de lectura | Media |
| **3. Login** | Tarjeta de marca + "Opciones avanzadas" para URL de API | Baja |
| **4. Biblioteca** | `doc_card` en grilla, detalle como vista, upload destacado, `ingest_status` con `st.fragment`, confirmación de borrado | Media |
| **5. Estudio (chat)** | `st.chat_message`/`st.chat_input`, `answer_block` con referencias de primera clase; migración del estado de turnos existente | Alta (pantalla crítica; se testea con AppTest) |
| **6. Trazabilidad** | `trace_panel` línea de tiempo; se conservan funciones puras y tests de `trace_view.py` | Media |
| **7. Moderación + Auditoría + pulido** | Selector único de decisión, paginación cliente, estados vacíos, destructivos, revisión responsiva final | Media |

Criterio de corte entre etapas: nada de la etapa N+1 empieza sin revisión
manual de la N en la app desplegada (Docker Compose local, sin llamadas a LLM
de pago — el modo determinístico del tutor cubre el flujo completo).

---

## 11. Riesgos y consideraciones técnicas

1. **CSS sobre clases internas de Streamlit**: los selectores atacan clases
   generadas (ej. `st-emotion-cache-*`) que cambian entre versiones. Mitigación:
   CSS mínimo y acotado (tokens + algunos overrides de tipografía/ancho),
   Streamlit ya pineado en `pyproject`, verificación visual tras cada bump.
2. **Modelo de rerun completo**: el rediseño del chat (etapa 5) exige cuidado
   con `session_state` de turnos y la bandera `IN_FLIGHT_KEY` (doble envío);
   las funciones puras existentes (`validation_error`, `describe_error`,
   `register_turn`) se reutilizan sin cambios.
3. **Polling con `st.fragment`**: `run_every` corre el fragment en background;
   hay que asegurar que el fragmento solo re-renderice el estado de ingesta y
   no dispare reruns de página (reemplaza el `time.sleep(2)` actual, que
   congela el script).
4. **Filtrado del nav por rol**: la identidad sigue viniendo de `/api/me`
   (backend como frontera); si `me()` falla, se cae al login como hoy. Es un
   cambio de visibilidad, no de autorización.
5. **Tests UI existentes**: `tests/test_ui_study*.py` usan AppTest con claves
   de widgets actuales; cada etapa debe actualizar esos tests sin reducir
   cobertura (los tests de funciones puras no cambian).
6. **Contratos intactos**: `ApiClient`, contratos y endpoints no se tocan; si
   moderación necesita el nombre del autor y el contrato solo trae
   `uploaded_by` (UUID), se muestra el UUID acortado — agregar endpoint sería
   funcional y queda fuera de alcance.

### Cambios visuales vs. funcionales (separación explícita)

| Cambio propuesto | Tipo |
|---|---|
| Tema, paleta, tipografía, CSS shell | Visual |
| Pastillas de estado en español | Visual (usa datos existentes) |
| Chat con `st.chat_input` | Estructural-UI (misma llamada a `/api/study`) |
| Detalle de documento como vista | Estructural-UI (misma página, mismo cliente) |
| Nav filtrado por rol | Visual (el backend revalida igual que hoy) |
| Confirmación previa al borrar | **Funcional-UX** (agrega un paso; la llamada es la misma) |
| Polling con `st.fragment` | Estructural-UI (mismo endpoint, sin congelar) |
| Paginación de auditoría en cliente | Visual (sobre los datos ya descargados) |

---

## 12. Decisiones — APROBADAS (2026-11-02, acto UI-REDESIGN-02 §1)

Las cinco decisiones pendientes de la versión inicial fueron **aprobadas por
el usuario**. Se aplican sin volver a solicitar aprobación:

| # | Tema | Decisión aprobada |
|---|---|---|
| 1 | **Marca** | Concepto «Tinta y aula» con acento índigo `#4338CA`: aprobado. |
| 2 | **Tipografía** | **Pila del sistema**; NO se incorpora Inter ni fuentes externas. |
| 3 | **Chat** | Se migrará a `st.chat_input` (Enter para enviar) en la etapa de estudio. |
| 4 | **URL de la API en login** | Se conserva; en una etapa posterior pasa a «Opciones avanzadas». |
| 5 | **Autor en Moderación** | UUID abreviado si no hay nombre disponible; NO se modifica el backend. |

---

## 13. Cumplimiento de criterios de aceptación

| Criterio | Estado |
|---|---|
| Auditoría contempla todas las pantallas existentes | ✅ 6 pantallas + shell + panel de traza (§2.3, §3) |
| Recomendaciones basadas en código y app real | ✅ Código leído completo + app ejecutándose en Compose y verificada (HTTP 200); sin capturas de navegador posible, declarado en §1 |
| Mantiene todas las funcionalidades | ✅ §4 y tabla visual-vs-funcional: mismas llamadas, mismos datos, mismos permisos |
| Identidad visual coherente | ✅ "Tinta y aula": 8 tokens, un acento, componentes únicos |
| Diferenciación visual vs funcional | ✅ Tabla explícita en §11 |
| Plan incremental | ✅ 8 etapas numeradas de 0 a 7, con revisión entre etapas |
| No se modificó código de la aplicación | ✅ Cero cambios fuera de `docs/ui/` |

---

*Fin de la propuesta (UI-REDESIGN-01). Las decisiones de §12 fueron
APROBADAS por el usuario el 2026-11-02; la implementación quedó autorizada y
la etapa inicial (UI-REDESIGN-02: identidad visual, sistema de estilos y
componentes, estados y corrección del bug de Home) se ejecutó conforme a las
restricciones de esa tarea.*










