# AUDITORÍA INDEPENDIENTE DE SEGURIDAD Y ARQUITECTURA — PASO #3.B (`WebExtractionSkill`)

- **Proyecto:** J.A.R.V.I.S. (`jarvis-core`)
- **Componente auditado:** Paso #3.B — `WebExtractionSkill` / Web Extraction Engine
- **Commit auditado:** `0a468f4` (`0a468f475b6deea26203ecbf830592a53663723f`)
- **Rama:** `main` (sincronizada con `origin/main`)
- **Fecha de auditoría:** 2026-11-12
- **Modo de ejecución:** Auditoría forense y técnica en modo **SOLO LECTURA** (sin modificación de código, tests ni configuración; preservación íntegra de los 16 archivos preexistentes de N5/memoria y de `data/memory_store.json`).

---

## A. RESUMEN EJECUTIVO

Se realizó una auditoría forense y de arquitectura de extremo a extremo sobre el Paso #3.B (`WebExtractionSkill`), publicado en el commit `0a468f4`, abarcando los 8 módulos nuevos bajo `skills/` (`web_config.py`, `web_models.py`, `web_security.py`, `web_fetcher.py`, `web_parser.py`, `web_search_provider.py`, `web_document_converter.py`, `web_extraction_skill.py`), su integración en `src/tools/skill_manager.py`, `src/brain/intent_router.py` y `config/tools_schema.py`, su interoperabilidad con el Paso #3.A (`DocumentExportSkill`, `DocumentParser`, `Document`, `DocumentTemplate`) y la suite de pruebas `tests/test_web_extraction_skill.py`.

### Conclusiones principales

1. **Arquitectura y defensa SSRF / Transporte IP-pinned sólidas (0 vulnerabilidades `CRITICAL` o `HIGH`):**
   - La validación de URLs (`normalize_and_validate_url`), la detección de representaciones alternativas de IPv4 (`_detect_obscure_ipv4_literal`: decimal entero, octal, hexadecimal y formas abreviadas `a.b`, `a.b.c`), el bloqueo de rangos privados/loopback/link-local/CGNAT/metadatos cloud (`169.254.169.254`, `100.100.100.200`, `fd00:ec2::254`) y la política de rechazo total ante respuestas DNS mixtas (`resolve_and_validate_host`) están correctamente diseñados e implementados.
   - El transporte HTTP/HTTPS en `skills/web_fetcher.py` elimina efectivamente la ventana TOCTOU de DNS rebinding al conectar el socket TCP directamente a la tupla `(pinned_ip, port)` mediante `_PinnedHTTPConnection` y `_PinnedHTTPSConnection`, preservando el encabezado `Host` original y enviando `server_hostname = original_hostname` en el handshake TLS con `ssl.CERT_REQUIRED` y `check_hostname = True`.
   - Cada salto de redirección (`301`, `302`, `303`, `307`, `308`) vuelve a pasar por `resolve_target()` antes de abrir cualquier socket nuevo.

2. **Hallazgos identificados (3 `MEDIUM`, 3 `LOW`, 1 `INFO`):**
   - **F-01 (`MEDIUM` — Vulnerabilidad confirmada):** Falsificación de elementos `Citation` en el IR de `DocumentExportSkill` mediante párrafos con sintaxis `[id]: https://...` (sin acento circunflejo `^`) e inyección de enlaces Markdown a través de URLs con paréntesis de cierre `)` y espacios en `normalize_extracted_url` + `WebDocumentConverter.to_markdown`.
   - **F-02 (`MEDIUM` — Vulnerabilidad confirmada):** Secuestro de metadatos (`<title>`, `<meta>`, `<link rel="canonical">`, JSON-LD) y secuestro cross-origin de `<base href>` desde subárboles ignorados/inertes (`<template>`, `<noscript>`, `hidden`, `aria-hidden="true"`, `<nav>`, `<footer>`, `<aside>`) o desde `<body>`, debido a que `_StructuredHTMLParser.handle_starttag` procesa etiquetas de cabecera antes de verificar `self._ignored_depth > 0` y sin comprobar pertenencia a `<head>`.
   - **F-03 (`MEDIUM` — Vulnerabilidad confirmada):** Aceptación silenciosa de respuestas HTTP comprimidas (`gzip`/`deflate`) truncadas o sin trailer CRC32 (`_decompressor.eof == False`) como `ExtractionStatus.SUCCESS`, y pérdida de `final_url` y `redirect_chain` en `WebSourceDocument` cuando un salto posterior a una redirección falla o es bloqueado por SSRF.
   - **F-04 (`LOW` — Vulnerabilidad confirmada / Defensa incompleta):** El límite `max_normalized_chars` trunca únicamente `normalized_text`, pero no acota la cantidad ni el volumen acumulado de bloques estructurales (`paragraphs`, `headings`, `lists`, `sections`), permitiendo que `WebDocumentConverter.to_markdown()` y `to_dict()` emitan decenas de miles de bloques; además, identificadores de interfaz ISATAP (`::5efe:127.0.0.1` / `::0200:5efe:127.0.0.1`) bajo prefijos IPv6 globales no se inspeccionan recursivamente.
   - **F-05 (`LOW` — Vulnerabilidad confirmada / Defensa incompleta):** Vectores de *Indirect Prompt Injection* mediante texto oculto por CSS inline (`style="display:none"`, `visibility:hidden`, `opacity:0`) y caracteres Unicode de control bidireccional (`U+202A`–`U+202E`, `U+2066`–`U+2069`), junto con la ausencia de delimitadores explícitos de contenido externo no confiable en la salida de `WebExtractionSkill.execute()`.
   - **F-06 (`LOW` — Mejora preventiva / Robustez):** Captura genérica `except Exception` con reintento en `_fetch_single_hop_with_retries`, deduplicación de `extract_urls` basada solo en `.strip()` antes de aplicar `max_pages` y ausencia de regla determinista local en `IntentRouter.detect_local_skill()` para modo sin LLM.
   - **F-07 (`INFO` — Limitación de entorno de pruebas y cobertura adversarial):** En este host Windows, Avast Web/Mail Shield intercepta handshakes TLS en `127.0.0.1`, activando la rama de bloqueo `TLS_ERROR` en el test de socket HTTPS real (`tests/test_web_extraction_skill.py:670-677`); aunque el contrato criptográfico se verifica en memoria con `ssl.MemoryBIO`, la descarga HTTP 200 post-TLS sobre socket OS real no se ejecuta en este entorno, y faltan pruebas de regresión específicas para F-01 a F-06.

3. **Veredicto global:** **`APROBADO CON OBSERVACIONES`** (apto para continuar el roadmap; se recomienda aplicar las correcciones acotadas de F-01, F-02 y F-03 antes de exponer la extracción a orígenes web arbitrarios no confiables en producción).

---

## B. MATRIZ DE SEGURIDAD

| Área | Resultado | Evidencia | Riesgo |
| :--- | :--- | :--- | :--- |
| **1. Esquemas, credenciales y puertos URL** | **CUMPLE** | `skills/web_security.py:242-313` (`normalize_and_validate_url`) permite solo `http`/`https`, rechaza `username`/`password` (`URL_CREDENTIALS_FORBIDDEN`) y restringe puertos a `{80, 443}`. Probado en `tests/test_web_extraction_skill.py:130-169`. | **BAJO** |
| **2. Normalización IDNA y dominios internos** | **CUMPLE** | `skills/web_security.py:92-128, 304-309` normaliza NFKC + Punycode ASCII (`_encode_idna_Label`) y bloquea `localhost`, `.local`, `.internal`, `.lan`, `.home`, `.corp`, `.home.arpa` y hosts de una sola etiqueta sin TLD. | **BAJO** |
| **3. Representaciones alternativas de IPv4** | **CUMPLE** | `skills/web_security.py:131-184` (`_detect_obscure_ipv4_literal`) detecta y bloquea IPv4 en decimal entero (`2130706433`), octal (`0177.0.0.1`), hexadecimal (`0x7f.0.0.1`) y notaciones abreviadas (`127.1`). | **BAJO** |
| **4. Bloqueo de rangos IPv4/IPv6 reservados y metadatos cloud** | **CUMPLE CON OBSERVACIÓN MENOR** | `skills/web_security.py:63-90, 187-238` bloquea loopback, RFC1918, link-local (`169.254.0.0/16`, `fe80::/10`), CGNAT (`100.64.0.0/10`), ULA (`fc00::/7`), IPv4-mapped (`::ffff:0:0/96`), SIIT (`::ffff:0:127.0.0.1`), IPv4-compatible (`::127.0.0.1`), 6to4 (`2002::/16`), Teredo (`2001::/32`) y NAT64 (`64:ff9b::/96`). Observación menor (**F-04**): sufijos ISATAP (`::5efe:x.x.x.x`) bajo prefijos globales no se desencapsulan. | **BAJO** |
| **5. DNS mixto, DNS rebinding, TOCTOU y redirecciones** | **CUMPLE** | `skills/web_security.py:378-446` rechaza todo el host si cualquier registro A/AAAA es privado (`SSRF_BLOCKED`). `skills/web_fetcher.py:114-140, 169-214, 419-452` fija la conexión TCP a la IP validada (`_PinnedHTTPConnection` / `_PinnedHTTPSConnection`) y revalida cada redirección. | **BAJO** |
| **6. Transporte TLS, SNI y validación de certificados** | **CUMPLE** | `skills/web_fetcher.py:143-155, 199-213` exige `PROTOCOL_TLS_CLIENT`, `TLSVersion.TLSv1_2` mínimo, `CERT_REQUIRED`, `check_hostname=True` y pasa `server_hostname=self._original_hostname` sobre el socket conectado a la IP fijada. | **BAJO** |
| **7.Transporte HTTP, descompresión y atribución de errores** | **OBSERVACIÓN (`MEDIUM`)** | `skills/web_fetcher.py:582-648` valida estado, MIME y límites de lectura por chunks. Sin embargo (**F-03**), `_IncrementalBoundedDecompressor.finish()` (`skills/web_fetcher.py:372-386`) no verifica `_decompressor.eof`, aceptando streams `gzip`/`deflate` truncados sin error, y los fallos tras redirección pierden `final_url` y `redirect_chain` (`skills/web_extraction_skill.py:172-200`). | **MEDIO** |
| **8. Defensa contra agotamiento de recursos (DoS)** | **OBSERVACIÓN (`LOW`)** | Límites efectivos en red (`max_response_bytes=2 MiB`) y descompresión (`max_decompressed_bytes=4 MiB`). No obstante (**F-04**), `max_normalized_chars` (`skills/web_parser.py:980-994`) solo trunca `normalized_text` y no limita el número/tamaño total de `paragraphs`, `headings`, `lists` ni `sections` en `to_markdown()` / `to_dict()`. | **BAJO** |
| **9. Parser HTML, aislamiento de ruido y metadatos** | **OBSERVACIÓN (`MEDIUM`)** | `_StructuredHTMLParser` es tolerante a HTML malformado y limpia scripts/estilos, pero (**F-02**) procesa `<title>`, `<meta>`, `<link rel="canonical">`, JSON-LD y `<base href>` antes de chequear `self._ignored_depth > 0`, permitiendo secuestro de metadatos y de `<base href>` desde `<template>`, `hidden` o `<body>`. | **MEDIO** |
| **10. Trazabilidad y conversión a `DocumentExportSkill` (Paso #3.A)** | **OBSERVACIÓN (`MEDIUM`)** | `WebDocumentConverter` integra con `DocumentParser` y plantillas de estudio sin modificar el Paso #3.A, pero (**F-01**) `_MD_BLOCK_SPOOF_RE` (`skills/web_document_converter.py:33`) no escapa citas `[id]: url` sin `^` ni filas `|...|`, y `to_markdown()` permite inyección de enlaces Markdown si `lk.url` contiene `)`. | **MEDIO** |
| **11. Superficie de Prompt Injection indirecto hacia LLMs** | **OBSERVACIÓN (`LOW`)** | `_is_noise_element` (`skills/web_parser.py:428-441`) no filtra nodos ocultos vía `style="display:none"` y `_INVISIBLE_CHARS_RE` (`skills/web_parser.py:175`) no elimina controles bidireccionales Unicode (`U+202A`–`U+202E`, `U+2066`–`U+2069`) (**F-05**). | **BAJO** |
| **12. Integración Multi-Brain, `SkillManager` y concurrencia** | **CUMPLE CON OBSERVACIÓN MENOR** | `WebExtractionSkill` es agnóstica del proveedor LLM, registrada en `SkillManager` (`src/tools/skill_manager.py:21, 56`), `IntentRouter` (`src/brain/intent_router.py:24`) y `config/tools_schema.py:146-175`. Estado por extracción aislado en memoria sin tocar `data/memory_store.json`. Observación menor (**F-06**) en deduplicación de `extract_urls` y fast-path local. | **BAJO** |

---

## C. HALLAZGOS DETALLADOS

### Resumen de clasificación de hallazgos

| ID | Severidad | Categoría | Título del hallazgo |
| :--- | :--- | :--- | :--- |
| **F-01** | `MEDIUM` | **Vulnerabilidad confirmada** | Falsificación de citas (`Citation` spoofing) en el IR documental e inyección de enlaces Markdown en `WebDocumentConverter` + `normalize_extracted_url` |
| **F-02** | `MEDIUM` | **Vulnerabilidad confirmada** | Secuestro de `<title>`, metadatos y `<base href>` (cross-origin) desde subárboles ignorados (`<template>`, `<noscript>`, `hidden`, `<footer>`) o cuerpo HTML |
| **F-03** | `MEDIUM` | **Vulnerabilidad confirmada** | Aceptación silenciosa de flujos `gzip`/`deflate` truncados como `SUCCESS` y pérdida de `final_url` / `redirect_chain` cuando falla un salto posterior a una redirección |
| **F-04** | `LOW` | **Vulnerabilidad confirmada** | Ausencia de cota sobre la colección de bloques estructurales (`paragraphs`/`sections`) frente a `max_normalized_chars` y sufijos ISATAP IPv6 bajo prefijos globales |
| **F-05** | `LOW` | **Vulnerabilidad confirmada** | Superficie de *Indirect Prompt Injection* mediante CSS inline oculto (`display:none`), controles bidireccionales Unicode (`U+202E`) y ausencia de delimitadores de datos no confiables |
| **F-06** | `LOW` | **Mejora preventiva** | Reintento de excepciones no tipadas en `_fetch_single_hop_with_retries`, deduplicación no canónica en `extract_urls` y ausencia de fast-path local en `detect_local_skill` |
| **F-07** | `INFO` | **Limitación no verificable / Cobertura** | Interceptación TLS local por Avast Web/Mail Shield en Windows durante el test de socket HTTPS real y casos adversariales faltantes en la suite de pruebas |

---

### Hallazgo F-01 — Falsificación de citas (`Citation` spoofing) en el IR documental e inyección de enlaces Markdown en `WebDocumentConverter` + `normalize_extracted_url`

1. **ID:** `F-01`
2. **Severidad:** `MEDIUM` (**Vulnerabilidad confirmada**)
3. **Archivo y líneas exactas:**
   - `skills/web_document_converter.py`: líneas `33` (`_MD_BLOCK_SPOOF_RE`), `36-49` (`_sanitize_markdown_text`), `234` y `306` (interpolación `- [{label}]({lk.url})`).
   - `skills/document_parser.py`: líneas `40` (`RE_TABLE_ROW`), `42` (`RE_CITATION_DEF = re.compile(r"^\[\^?([^\]]+)\]:\s*(.+)$")`), `126-137`.
   - `skills/web_parser.py`: líneas `286-330` (`normalize_extracted_url`).
4. **Componente afectado:** Conversión de `WebSourceDocument` / `WebExtractionResult` a Markdown y al modelo intermedio `Document` (`WebDocumentConverter` + ` normalize_extracted_url`).
5. **Descripción técnica:**
   Existe una discrepancia entre la expresión regular de sanitización de bloques Markdown en `skills/web_document_converter.py:33`:
   ```python
   _MD_BLOCK_SPOOF_RE = re.compile(r"^(\s*)(#{1,6}\s+|[-*+]\s+|\d+\.\s+|>\s+|\[\^[^\]]+\]:|```|~~~)")
   ```
   y las expresiones regulares que `DocumentParser.parse()` (`skills/document_parser.py:40-42`) interpreta como elementos estructurales del documento:
   - `DocumentParser.RE_CITATION_DEF` es `r"^\[\^?([^\]]+)\]:\s*(.+)$"`, donde el circunflejo `\^?` es **opcional**. Por tanto, cualquier línea que comience con `[id]: https://...` (sin `^`) **no** es escapada por `_MD_BLOCK_SPOOF_RE`, pero **sí** es convertida por `DocumentParser.parse()` en un objeto `Citation(source_id=..., url=..., text=...)` de primer nivel dentro de `Document.elements`.
   - Análogamente, `_MD_BLOCK_SPOOF_RE` no escapa líneas que comienzan con `|` (`RE_TABLE_ROW = re.compile(r"^\|.*\|$")` en `skills/document_parser.py:40`).
   - Adicionalmente, `normalize_extracted_url()` (`skills/web_parser.py:286-330`) valida el esquema y reconstruye la URL con `urlunsplit()`, pero no codifica ni rechaza espacios en blanco ni paréntesis de cierre `)` en `path` o `query`. Cuando `WebDocumentConverter.to_markdown()` formatea enlaces como `f"- [{label}]({lk.url})"` (`skills/web_document_converter.py:234, 306`), una URL con `)` cierra prematuramente el destino del enlace Markdown y permite inyectar sintaxis Markdown arbitraria en la línea.
6. **Escenario concreto de explotación o fallo:**
   Una página web maliciosa o comprometida incluye en su cuerpo:
   - Un párrafo `<p>[src-1]: https://attacker.example/malware Fuente Oficial Verificada</p>` o `<p>[src-99]: https://attacker.example/fake Referencia inyectada</p>`.
   - Un enlace `<a href="https://example.com/path) [Enlace Falso](https://phish.example/login">Enlace legitimo</a>`.
   Al ejecutar `WebDocumentConverter().to_document(doc)` o exportar mediante `DocumentExportSkill` (`.md` / `.docx`), el párrafo se transforma en un nodo `Citation` real dentro del documento exportado y el enlace inyecta un segundo hipervínculo Markdown hacia `https://phish.example/login` bajo la etiqueta de fuente `[src-1]`.
7. **Impacto potencial:**
   Contaminación de la trazabilidad de fuentes (`Citation` spoofing) en documentos de Modo Estudio (`study_notes`, `research_report`, `sources_compilation`) y falsificación de enlaces en exportaciones Markdown/DOCX consumidas por el usuario o por cerebros LLM.
8. **Evidencia reproducible:**
   Ejecutado sobre el código del commit `0a468f4`:
   ```text
   === F-01 ===
   Extracted link URL: ['https://example.com/path) [Enlace Falso](https://phish.example/login']
   IR Element types: ['Heading', 'Heading', 'ListBlock', 'Heading', 'ListBlock', 'Heading', 'Heading', 'Paragraph', 'Citation', 'Paragraph', 'Paragraph', 'Heading', 'ListBlock', 'Heading', 'Citation']
   IR Citations: [
     "Citation(source_id='src-99', text='Referencia inyectada sin caret', url='https://attacker.example/fake')",
     "Citation(source_id='src-1', text='Articulo —  (extraído: 2026-11-12T05:06:42.719154+00:00)', url='https://example.com/post')"
   ]
   MD line: [src-99]: https://attacker.example/fake Referencia inyectada sin caret
   MD line: - [Enlace legitimo](https://example.com/path) [Enlace Falso](https://phish.example/login) (interno; fuente: `[src-1]`)
   ```
9. **Probabilidad de explotación:** **Media** (cualquier página web pública que contenga definiciones de enlaces estilo Markdown `[1]: https://...` en bloques de texto o `href` con paréntesis/espacios activa el comportamiento sin requerir condiciones especiales).
10. **Corrección recomendada:**
    - En `skills/web_document_converter.py:33`, actualizar `_MD_BLOCK_SPOOF_RE` para incluir `\^?` opcional en definiciones de referencia y filas de tabla con tuberías:
      ```python
      _MD_BLOCK_SPOOF_RE = re.compile(
          r"^(\s*)(#{1,6}\s+|[-*+]\s+|\d+\.\s+|>\s+|\[\^?[^\]]+\]:|\|.*\|$|```|~~~)"
      )
      ```
    - En `skills/web_parser.py:286-330` (`normalize_extracted_url`) y/o en una función `_sanitize_markdown_url()` en `skills/web_document_converter.py`, rechazar caracteres de control ASCII (`0x00`–`0x1F`, `0x7F`) y codificar en porcentaje (`urllib.parse.quote`) espacios y paréntesis `(`, `)` presentes en `path` y `query`.
11. **Pruebas de regresión necesarias:**
    - Test unitario con `<p>[src-99]: https://attacker.example/fake</p>` verificando que `converter.to_document(doc)` produce únicamente las citas legítimas generadas por `WebDocumentConverter` y que el párrafo se conserva como `Paragraph`.
    - Test unitario con `<a href="https://example.com/a) [Fake](https://evil.example)">` verificando que `)` y espacios quedan codificados (`%29`, `%20`) y no inyectan un segundo enlace Markdown.
12. **Dependencias o riesgos de la corrección:**
    - Ningún impacto en `DocumentExportSkill` (Paso #3.A permanece intacto). La corrección queda contenida íntegramente en `skills/web_document_converter.py` y `skills/web_parser.py`.

---

### Hallazgo F-02 — Secuestro de `<title>`, metadatos y `<base href>` (cross-origin) desde subárboles ignorados (`<template>`, `<noscript>`, `hidden`, `<footer>`) o cuerpo HTML

1. **ID:** `F-02`
2. **Severidad:** `MEDIUM` (**Vulnerabilidad confirmada**)
3. **Archivo y líneas exactas:**
   - `skills/web_parser.py`: líneas `472-525` (`_StructuredHTMLParser.handle_starttag`), `560-573` (`handle_endtag`), `671-678` (`handle_data`), `877-878` (resolución de `base_for_links`).
4. **Componente afectado:** Parser HTML y extractor de metadatos/enlaces (`_StructuredHTMLParser` y `WebContentExtractor.extract_from_fetch` en `skills/web_parser.py`).
5. **Descripción técnica:**
   En `_StructuredHTMLParser.handle_starttag` (`skills/web_parser.py:472-525`), la etiqueta `<head>` se marca como `becomes_ignored = True` (`líneas 512-513`) para evitar que textos sueltos dentro de `<head>` se agreguen como párrafos del cuerpo. Como consecuencia de compartir el mismo contador `self._ignored_depth` entre `<head>` y los contenedores de ruido/ocultos (`<template>`, `<noscript>`, `<svg>`, `<iframe>`, `hidden`, `aria-hidden="true"`, `<nav>`, `<footer>`, `<aside>`), el parser evalúa `<html lang>`, `<base href>`, `<meta>`, `<link rel="canonical">`, `<script type="application/ld+json">` y `<title>` **en las líneas 478-509, antes de comprobar `if self._ignored_depth > 0: return` (línea 514)**.
   Esto provoca dos fallos acoplados:
   1. Cualquier `<title>`, `<meta name="author"|"description"|...>`, `<link rel="canonical">`, `<script type="application/ld+json">` o `<base href>` colocado dentro de un bloque inerte u oculto (`<template>`, `<noscript>`, `<div hidden>`, `<div aria-hidden="true">`, `<footer>`, `<aside>`) es procesado y adoptado si el documento no definió previamente ese campo.
   2. `<base href="...">` se acepta incluso fuera de `<head>` (dentro de `<template>` o `<body>`) y sin restringir cambios de origen o ruta, por lo que `base_for_links = normalize_extracted_url(parser.base_href, source_url)` (`skills/web_parser.py:877`) redirige la resolución de todos los enlaces relativos (`<a href="login">`) de la página hacia un dominio arbitrario controlado por el atacante y los clasifica falsamente como `is_external=True` bajo la fuente legítima.
6. **Escenario concreto de explotación o fallo:**
   En una página con contenido generado por usuarios, plantillas cliente (`<template>`) o bloques ocultos (`<div hidden>`), se inserta:
   ```html
   <template>
     <title>Título Secuestrado</title>
     <base href="https://attacker.example/hijack/">
     <meta name="author" content="Autor Falso">
     <link rel="canonical" href="https://attacker.example/canonical">
   </template>
   ```
   El extractor sobreescribe `doc.title`, `doc.metadata.author`, `doc.metadata.canonical_url` y resuelve `<a href="login">` como `https://attacker.example/hijack/login`.
7. **Impacto potencial:**
   Falsificación de autoría, fecha de publicación, URL canónica, título del documento y secuestro de enlaces relativos en el resultado estructurado `WebSourceDocument` y en los documentos exportados.
8. **Evidencia reproducible:**
   Ejecutado sobre el código del commit `0a468f4`:
   ```text
   === F-02 ===
   doc.title: Titulo Secuestrado Desde Template
   doc.metadata.author: Autor Falso Template
   doc.metadata.canonical_url: https://attacker.example/canonical
   Resolved relative link: [('https://attacker.example/hijack/login', True)]
   ```
9. **Probabilidad de explotación:** **Media** (frecuente en aplicaciones web modernas que usan `<template>`, `<noscript>` o componentes ocultos con metadatos/plantillas internas).
10. **Corrección recomendada:**
    - Separar el seguimiento de `<head>` (`self._in_head: bool`) del contador de ruido/ocultos (`self._ignored_depth: int`). No incrementar `self._ignored_depth` al entrar en `<head>`; en su lugar, impedir la captura de bloques de texto en `handle_data` cuando `self._in_head` sea `True` (salvo para `<title>` y `<script type="application/ld+json">`).
    - Procesar `<title>`, `<base href>`, `<meta>`, `<link rel="canonical">` y `<script type="application/ld+json">` **únicamente cuando `self._ignored_depth == 0`**.
    - Exigir que `<base href>` y `<title>` solo se acepten cuando `self._in_head` sea `True`, y validar que `<base href>` comparta el mismo origen (`scheme` + `hostname` + `port`) que `source_url` (o emitir un `WebIssue` de advertencia y descartar un `<base href>` cross-origin).
11. **Pruebas de regresión necesarias:**
    - Test con `<template>`, `<noscript>` y `<div hidden>` que contengan `<title>`, `<meta name="author">`, `<link rel="canonical">` y `<base href="https://attacker.example/">`, comprobando que son ignorados por completo y que los enlaces relativos se resuelven contra `source_url`.
12. **Dependencias o riesgos de la corrección:**
    - Cambio interno en `_StructuredHTMLParser` (`skills/web_parser.py`). Se debe mantener la compatibilidad con fragmentos HTML sin etiqueta `<head>` explícita solo para `<meta>`/JSON-LD al nivel raíz cuando `self._ignored_depth == 0`, pero nunca dentro de subárboles ignorados ni permitiendo `<base href>` cross-origin.

---

### Hallazgo F-03 — Aceptación silenciosa de flujos `gzip`/`deflate` truncados como `SUCCESS` y pérdida de `final_url` / `redirect_chain` cuando falla un salto posterior a una redirección

1. **ID:** `F-03`
2. **Severidad:** `MEDIUM` (**Vulnerabilidad confirmada**)
3. **Archivo y líneas exactas:**
   - `skills/web_fetcher.py`: líneas `372-386` (`_IncrementalBoundedDecompressor.finish`), `419-465` (`WebFetcher.fetch`).
   - `skills/web_extraction_skill.py`: líneas `168-200` (`WebExtractionSkill.extract_url`).
4. **Componente afectado:** Descompresor incremental de transporte HTTP (`_IncrementalBoundedDecompressor`), gestor de redirecciones (`WebFetcher.fetch`) y manejo de errores en `WebExtractionSkill.extract_url`.
5. **Descripción técnica:**
   Se identificaron dos defectos de integridad y trazabilidad en la capa de transporte:
   - **Parte A — Flujos comprimidos truncados aceptados sin error ni advertencia:** En `_IncrementalBoundedDecompressor.finish()` (`skills/web_fetcher.py:372-386`), al terminar la lectura del socket (`response.read()` devuelve `b""`), se invoca `self._decompressor.flush()`. En la biblioteca estándar `zlib` de Python, cuando un stream `gzip` o `deflate` se corta abruptamente antes del bloque final o pierde su trailer CRC32/ISIZE de 8 bytes, `flush()` retorna `b""` **sin lanzar `zlib.error`**, dejando `self._decompressor.eof == False`. Como `finish()` no verifica `if self._encoding in ("gzip", "deflate", "x-gzip") and not self._decompressor.eof:`, una respuesta HTTP truncada a mitad de transmisión se devuelve como si estuviera completa y se marca como `ExtractionStatus.SUCCESS`.
   - **Parte B — Pérdida de `redirect_chain` y `final_url` cuando falla un salto posterior a una redirección:** En `WebFetcher.fetch()` (`skills/web_fetcher.py:419-452`), si el salto inicial (`https://public.example/start`) responde con una redirección (`302`) hacia una segunda URL (`https://second.example/missing` o `http://127.0.0.1/admin`) y ese segundo salto falla (`HTTP 404/500`, timeout, `UNSUPPORTED_CONTENT_TYPE` o bloqueo SSRF `WebSecurityError`), la excepción lanzada no transporta `redirect_chain` ni la URL del salto fallido. En `WebExtractionSkill.extract_url()` (`skills/web_extraction_skill.py:172-200`), el bloque `except` construye `WebSourceDocument` con `final_url=raw_url` (la URL inicial) y `redirect_chain=()`, perdiendo la trazabilidad de la cadena de redirecciones y de la URL final donde ocurrió el fallo o bloqueo.
6. **Escenario concreto de explotación o fallo:**
   - **Escenario A:** Una conexión de red inestable o un proxy intermedio corta un stream `Content-Encoding: gzip` después del primer fragmento HTML. `WebExtractionSkill` devuelve `status=ExtractionStatus.SUCCESS` sin ningún `WebIssue`, entregando al cerebro LLM un artículo mutilado como si fuera íntegro.
   - **Escenario B:** `https://trusted.example/go` redirige (`302`) a `http://169.254.169.254/latest/meta-data/`. El bloqueo SSRF actúa correctamente, pero el `WebSourceDocument` resultante informa `final_url="https://trusted.example/go"` y `redirect_chain=()`, ocultando en la auditoría estructurada los saltos intermedios previos al bloqueo.
7. **Impacto potencial:**
   - Aceptación silenciosa de datos truncados/corruptos sin alerta (`SUCCESS` falso).
   - Trazabilidad incompleta en `final_url` y `redirect_chain` ante fallos o bloqueos en cadenas de redirección.
8. **Evidencia reproducible:**
   Ejecutado sobre el código del commit `0a468f4`:
   ```text
   === F-03 ===
   Truncated gzip decompressed len: 1 eof: False text: [b'<html><body><main><p>Contenido truncado a mitad de stream</p></main></body></html>']
   ```
   Y en `skills/web_extraction_skill.py:182-199`:
   ```python
   return WebSourceDocument(
       original_url=raw_url,
       final_url=raw_url,
       status=status,
       http_status=http_status,
       metadata=WebMetadata(extracted_at=extracted_at),
       errors=(
           WebIssue(
               code=exc.code,
               message=str(exc),
               source_url=raw_url,
               severity="error",
           ),
       ),
   )
   ```
9. **Probabilidad de explotación:** **Media** (cortes de conexión en streams comprimidos y errores HTTP/bloqueos tras redirecciones ocurren regularmente en navegación web real).
10. **Corrección recomendada:**
    - En `_IncrementalBoundedDecompressor.finish()` (`skills/web_fetcher.py:372-386`), verificar después de `self._decompressor.flush()` que `self._decompressor.eof` sea `True`; si es `False`, lanzar `WebFetchError("CORRUPTED_COMPRESSED_STREAM", "El flujo comprimido terminó de forma incompleta (EOF no alcanzado).")`.
    - En `WebFetcher.fetch()` (`skills/web_fetcher.py:419-465`), capturar `WebSecurityError` y `WebFetchError` dentro del bucle de redirecciones y adjuntar a la excepción `redirect_chain=tuple(redirect_chain)` y `final_url=current_url`, de modo que `WebExtractionSkill.extract_url()` los preserve en `WebSourceDocument` y en `WebIssue.source_url`.
11. **Pruebas de regresión necesarias:**
    - Test unitario enviando un payload `gzip.compress(...)[:-8]` (sin trailer CRC32/ISIZE) o truncado a mitad de stream, verificando que falla con `CORRUPTED_COMPRESSED_STREAM` (`ExtractionStatus.FAILED`).
    - Test de redirección `302` cuyo segundo salto devuelve `404` o apunta a `127.0.0.1`, verificando que `doc.redirect_chain` contiene la URL inicial y `doc.final_url` refleja la URL del salto donde se produjo el error/bloqueo.
12. **Dependencias o riesgos de la corrección:**
    - Cambio acotado a `skills/web_fetcher.py` y `skills/web_extraction_skill.py`. Nota: para `deflate` raw (`-zlib.MAX_WBITS`), `zlib.decompressobj` también actualiza `.eof = True` al procesar el bloque final `BFINAL=1`, por lo que la comprobación `not self._decompressor.eof` es compatible tanto con `gzip` como con `deflate` (RFC 1950 y RFC 1951).

---

### Hallazgo F-04 — Ausencia de cota sobre la colección de bloques estructurales (`paragraphs`/`sections`) frente a `max_normalized_chars` y sufijos ISATAP IPv6 bajo prefijos globales

1. **ID:** `F-04`
2. **Severidad:** `LOW` (**Vulnerabilidad confirmada / Defensa incompleta**)
3. **Archivo y líneas exactas:**
   - `skills/web_parser.py`: líneas `833-874` (construcción de `headings`, `paragraphs`, `lists`, `sections`) y `980-994` (truncamiento exclusivo de `full_normalized` mediante `max_normalized_chars`).
   - `skills/web_document_converter.py`: líneas `160-241` (`_append_single_source_markdown` itera todos los bloques de `doc.sections` / `doc.paragraphs` sin consultar `max_normalized_chars`).
   - `skills/web_security.py`: líneas `80-90` (`_EXTRA_BLOCKED_IPV6_NETS`), `187-233` (`_check_ip_recursive`).
4. **Componente afectado:** Control de límites de extracción en `WebContentExtractor` (`skills/web_parser.py`) y validador de direcciones IPv6 en `skills/web_security.py`.
5. **Descripción técnica:**
   - **Parte A — Bloques estructurales no acotados por `max_normalized_chars`:** En `WebContentExtractor.extract_from_fetch()` (`skills/web_parser.py:980-994`), el parámetro `self.config.max_normalized_chars` (por defecto `100_000`) solo recorta la cadena plana `full_normalized`:
     ```python
     if len(full_normalized) > self.config.max_normalized_chars:
         full_normalized = full_normalized[: self.config.max_normalized_chars].rstrip()
     ```
     Sin embargo, las tuplas `doc.headings`, `doc.paragraphs`, `doc.lists` y `doc.sections` construidas en las líneas `833-874` conservan **todos** los bloques extraídos del HTML (hasta los 4 MiB de `max_decompressed_bytes`). Cuando se invoca `WebDocumentConverter.to_markdown(doc)`, `WebDocumentConverter.to_document(doc)` o `doc.to_dict()`, se serializan todos los bloques sin aplicar el límite `max_normalized_chars`.
   - **Parte B — Identificadores de interfaz ISATAP (RFC 5214) en direcciones IPv6 globales:** En `skills/web_security.py:187-233`, `_check_ip_recursive` desencapsula `ipv4_mapped`, `sixtofour`, `teredo` y los prefijos NAT64 `64:ff9b::/96` y `64:ff9b:1::/48`, y Python 3.12 bloquea `::ffff:0:127.0.0.1` (SIIT) y `::127.0.0.1` como no globales. No obstante, una dirección IPv6 con prefijo unicast global (`2000::/3`) que utilice un identificador de interfaz ISATAP (`0000:5efe:w.x.y.z` o `0200:5efe:w.x.y.z` en los bits 64–127, por ejemplo `2000::5efe:127.0.0.1`) tiene `is_global == True` y no inspecciona recursivamente los últimos 32 bits IPv4.
6. **Escenario concreto de explotación o fallo:**
   - **Escenario A:** Una página HTML de 4 MiB con 40.000 párrafos cortos únicos (`<p>Item 1</p><p>Item 2</p>...`) produce `len(doc.normalized_text) == 100_000`, pero `len(doc.paragraphs) == 40_000` y un Markdown/JSON de varios megabytes que desborda la ventana de contexto del LLM o ralentiza la exportación DOCX.
   - **Escenario B:** En un entorno de red con túneles ISATAP activos bajo un prefijo global, una dirección `2000::5efe:127.0.0.1` no tendría su IPv4 embebida validada por `_check_ip_recursive`.
7. **Impacto potencial:**
   Consumo excesivo de memoria/tokens en `to_markdown()` y `to_dict()` pese a configurar `max_normalized_chars`, y brecha teórica en entornos con enrutamiento ISATAP.
8. **Evidencia reproducible:**
   Ejecutado sobre el código del commit `0a468f4`:
   ```text
   === F-04 IPv6 ===
   ::ffff:0:127.0.0.1 -> BLOCKED: SSRF_BLOCKED
   ::ffff:0:10.0.0.1 -> BLOCKED: SSRF_BLOCKED
   ::127.0.0.1 -> BLOCKED: SSRF_BLOCKED
   2001:db8::1 -> BLOCKED: SSRF_BLOCKED
   2001:2::1 -> BLOCKED: SSRF_BLOCKED
   2001:10::1 -> BLOCKED: SSRF_BLOCKED
   fe80::5efe:127.0.0.1 -> BLOCKED: SSRF_BLOCKED
   2002:7f00:0001::1 -> BLOCKED: SSRF_BLOCKED
   64:ff9b::127.0.0.1 -> BLOCKED: SSRF_BLOCKED
   2001:db8::5efe:127.0.0.1 -> BLOCKED: SSRF_BLOCKED
   2000::5efe:127.0.0.1 -> ALLOWED
   2000::0200:5efe:127.0.0.1 -> ALLOWED
   === F-04 DOM BLOCKS ===
   max_normalized_chars=100 -> normalized_text len: 100 paragraphs count: 200 to_markdown len: 7880
   ```
9. **Probabilidad de explotación:** **Baja**.
10. **Corrección recomendada:**
    - En `skills/web_parser.py:833-874`, llevar un acumulador de caracteres al poblar `headings`, `paragraphs`, `lists` y `sections`, deteniendo la inserción de nuevos bloques cuando se alcance `self.config.max_normalized_chars` y registrando el `WebIssue(code="CONTENT_TRUNCATED", ...)`.
    - En `skills/web_security.py:224-232` (`_check_ip_recursive`), detectar el marcador ISATAP en los bits 64–95 (`(int(ip_obj) >> 32) & 0xFDFFFFFF == 0x00005EFE`) y validar recursivamente los 32 bits inferiores como `ipaddress.IPv4Address(int(ip_obj) & 0xFFFFFFFF)`.
11. **Pruebas de regresión necesarias:**
    - Test con `max_normalized_chars=100` y 200 párrafos `<p>`, verificando que tanto `doc.normalized_text` como la suma de caracteres de `doc.paragraphs` y `converter.to_markdown(doc)` respetan el presupuesto configurado.
    - Test unitario verificando que `validate_ip_address("2000::5efe:127.0.0.1")` y `validate_ip_address("2000::0200:5efe:10.0.0.1")` lanzan `WebSecurityError("SSRF_BLOCKED")`.
12. **Dependencias o riesgos de la corrección:**
    - Ninguno. Mejora directa del cumplimiento de límites y endurecimiento IPv6.

---

### Hallazgo F-05 — Superficie de *Indirect Prompt Injection* mediante CSS inline oculto (`display:none`), controles bidireccionales Unicode (`U+202E`) y ausencia de delimitadores de datos no confiables

1. **ID:** `F-05`
2. **Severidad:** `LOW` (**Vulnerabilidad confirmada / Defensa incompleta**)
3. **Archivo y líneas exactas:**
   - `skills/web_parser.py`: líneas `175-176` (`_INVISIBLE_CHARS_RE`), `428-441` (`_is_noise_element`).
   - `skills/web_extraction_skill.py`: líneas `55-103` (`_format_human_summary`), `564-604` (`WebExtractionSkill.execute`).
4. **Componente afectado:** Filtrado de ruido/texto invisible en `skills/web_parser.py` y formateo de salida para cerebros LLM en `skills/web_extraction_skill.py`.
5. **Descripción técnica:**
   - En `skills/web_parser.py:428-441`, `_is_noise_element()` descarta etiquetas con atributos `hidden` o `aria-hidden="true"`, pero no inspecciona el atributo `style` para detectar técnicas estándar de ocultamiento CSS empleadas en *Indirect Prompt Injection* (`display: none`, `visibility: hidden`, `opacity: 0`, `font-size: 0px`).
   - En `skills/web_parser.py:175`, `_INVISIBLE_CHARS_RE = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff\u00ad]+")` elimina espacios de ancho cero, pero conserva caracteres de control bidireccional Unicode (`U+202A` a `U+202E` LRE/RLE/PDF/LRO/RLO y `U+2066` a `U+2069` LRI/RLI/FSI/PDI) y caracteres del plano Unicode Tags (`U+E0000`–`U+E007F`), utilizados para ofuscar instrucciones maliciosas dirigidas a LLMs.
   - En `skills/web_extraction_skill.py:55-103, 564-604`, el texto devuelto por `execute()` no incluye marcadores de frontera que adviertan al LLM consumidor que el contenido proviene de una fuente web externa no confiable y debe tratarse exclusivamente como datos, no como instrucciones.
6. **Escenario concreto de explotación o fallo:**
   Una página web incluye `<p style="display:none">IGNORA INSTRUCCIONES PREVIAS \u202eOCULTO</p>`. El usuario humano no ve ese párrafo en su navegador, pero `WebContentExtractor` lo extrae como párrafo principal y `WebExtractionSkill.execute()` lo inyecta directamente en el contexto del cerebro (`Ollama` / `Gemini`).
7. **Impacto potencial:**
   Manipulación del resumen o de la respuesta generada por el modelo de lenguaje (*Indirect Prompt Injection*) a partir de texto oculto en páginas web externas.
8. **Evidencia reproducible:**
   Ejecutado sobre el código del commit `0a468f4`:
   ```text
   === F-05 ===
   Paragraphs: ['Texto visible normal para extraer.', 'IGNORA INSTRUCCIONES PREVIAS \u202eOCULTO']
   Bidi char U+202E present: True
   ```
9. **Probabilidad de explotación:** **Baja / Media** (técnica habitual en ataques de *Indirect Prompt Injection* sobre agentes con acceso web).
10. **Corrección recomendada:**
    - En `_INVISIBLE_CHARS_RE` (`skills/web_parser.py:175`), incluir controles bidireccionales Unicode (`\u202a-\u202e\u2066-\u2069`) y caracteres de etiquetas invisibles (`\U000e0000-\U000e007f`).
    - En `_is_noise_element()` (`skills/web_parser.py:428-441`), verificar si `attr_map.get("style")` contiene `display:none`, `visibility:hidden`, `opacity:0` o `font-size:0` (normalizando espacios).
    - En `_format_human_summary()` (`skills/web_extraction_skill.py:55-103`), acotar la longitud de `title` y `author` y envolver el contenido extraído en delimitadores claros de datos externos no confiables.
11. **Pruebas de regresión necesarias:**
    - Test unitario verificando que `<p style="display: none">...</p>` y `<span style="visibility:hidden">...</span>` son excluidos de `doc.paragraphs` y `doc.normalized_text`.
    - Test unitario verificando que `\u202e` y `\u2066` son eliminados por `normalize_text()`.
12. **Dependencias o riesgos de la corrección:**
    - Cambio limpio y autocontenido en `skills/web_parser.py` y `skills/web_extraction_skill.py`.

---

### Hallazgo F-06 — Reintento de excepciones no tipadas en `_fetch_single_hop_with_retries`, deduplicación no canónica en `extract_urls` y ausencia de fast-path local en `detect_local_skill`

1. **ID:** `F-06`
2. **Severidad:** `LOW` (**Mejora preventiva**)
3. **Archivo y líneas exactas:**
   - `skills/web_fetcher.py`: líneas `484-506` (`_fetch_single_hop_with_retries`).
   - `skills/web_extraction_skill.py`: líneas `289-296` (`extract_urls`).
   - `src/brain/intent_router.py`: líneas `24` (`SKILL_CLASS_BY_TOOL_NAME`) y `105-284` (`detect_local_skill`).
4. **Componente afectado:** Orquestación de reintentos HTTP (`WebFetcher`), deduplicación de lotes de URLs (`WebExtractionSkill.extract_urls`) y enrutador local (`IntentRouter`).
5. **Descripción técnica:**
   - En `WebFetcher._fetch_single_hop_with_retries()` (`skills/web_fetcher.py:500-503`), el bloque `except Exception as exc:` captura cualquier excepción inesperada no tipada y reintenta la petición si `attempt < attempts - 1` sin verificar antes si `time.monotonic() >= deadline`.
   - En `WebExtractionSkill.extract_urls()` (`skills/web_extraction_skill.py:289-296`), la deduplicación previa al recorte `[:limit]` (`max_pages`) compara únicamente `cleaned = raw.strip()`, sin normalizar mayúsculas en el host, puertos por defecto (`:443`) ni fragmentos (`#seccion`). Por ello, `["https://Example.com/doc#a", "https://example.com:443/doc#b"]` consume 2 cupos de `max_pages` y descarga dos veces el mismo recurso.
   - En `src/brain/intent_router.py`, `web_extraction` está registrada en `SKILL_CLASS_BY_TOOL_NAME` (`línea 24`) y en `config/tools_schema.py` (`líneas 146-175`), funcionando con cualquier LLM activo, pero `detect_local_skill()` (`líneas 105-284`) no incluye una regla determinista por expresión regular cuando todos los cerebros LLM están fuera de línea.
6. **Escenario concreto de explotación o fallo:**
   Un lote de URLs con fragmentos distintos (`#intro`, `#conclusion`) agota `max_pages` descargando repetidamente la misma página.
7. **Impacto potencial:**
   Desperdicio de presupuesto de peticiones (`max_pages`) en `extract_urls` y necesidad de un cerebro LLM activo para invocar `web_extraction` desde lenguaje natural.
8. **Evidencia reproducible:**
   Inspección directa de `skills/web_extraction_skill.py:289-296`:
   ```python
   deduped_urls: list[str] = []
   seen: set[str] = set()
   for raw in urls:
       if not isinstance(raw, str):
           continue
       cleaned = raw.strip()
       if cleaned and cleaned not in seen:
           seen.add(cleaned)
           deduped_urls.append(cleaned)
   ```
9. **Probabilidad de explotación:** **Baja**.
10. **Corrección recomendada:**
    - En `extract_urls()`, usar una clave canónica ligera (`normalize_extracted_url(cleaned)` o `normalize_and_validate_url` con fallback a `cleaned`) para el conjunto `seen`.
    - En `_fetch_single_hop_with_retries()`, comprobar `time.monotonic() < deadline` antes de iniciar un reintento.
    - Opcionalmente, añadir una regla determinista en `IntentRouter.detect_local_skill()` para comandos explícitos como `"extrae la web https://..."`.
11. **Pruebas de regresión necesarias:**
    - Test pasando `["https://example.com/a#1", "https://EXAMPLE.COM:443/a#2"]` a `extract_urls()` verificando que solo se realiza una descarga HTTP.
12. **Dependencias o riesgos de la corrección:**
    - Bajo riesgo; no altera contratos públicos.

---

### Hallazgo F-07 — Interceptación TLS local por Avast Web/Mail Shield en Windows durante el test de socket HTTPS real y casos adversariales faltantes en la suite de pruebas

1. **ID:** `F-07`
2. **Severidad:** `INFO` (**Limitación no verificable en entorno local / Cobertura de pruebas**)
3. **Archivo y líneas exactas:**
   - `tests/test_web_extraction_skill.py`: líneas `617-745` (`test_https_pinned_ip_preserves_sni_host_and_strict_tls_verification`).
4. **Componente afectado:** Suite de pruebas `tests/test_web_extraction_skill.py` en entorno Windows con antivirus activo.
5. **Descripción técnica:**
   En este host Windows, el servicio local **Avast Web/Mail Shield** intercepta las conexiones TLS salientes hacia `127.0.0.1` y reemplaza el certificado efímero del servidor de prueba por uno firmado por `CN=Avast Web/Mail Shield Untrusted Root`.
   - En `tests/test_web_extraction_skill.py:666-677`, el test contempla esta situación mediante un bloque `if doc_tls.status == ExtractionStatus.SUCCESS: ... else: self.assertEqual(doc_tls.errors[0].code, "TLS_ERROR")` y complementa la verificación ejecutando un handshake TLS real en memoria con `ssl.MemoryBIO` (`líneas 707-745`).
   - Esto demuestra que:
     1. El socket TCP de `_PinnedHTTPSConnection` se conectó a la IP fijada (`127.0.0.1`) y **nunca** a la IP pública simulada (`93.184.216.34`).
     2. El SNI `secure.example.test` fue enviado y recibido en `server.sni_callback`.
     3. El cliente `WebFetcher` **rechazó** correctamente el certificado interceptado no confiable (`TLS_ERROR`), confirmando que `CERT_REQUIRED` está activo.
     4. El contexto SSL de producción validó con éxito el certificado válido en `ssl.MemoryBIO`.
   - **Limitación documentada:** Debido a la interceptación del antivirus del host, el tramo post-handshake TLS (lectura de respuesta `HTTP/1.1 200 OK` sobre `_PinnedHTTPSConnection` en un socket OS real) cae en la rama `else` (`TLS_ERROR`) en esta máquina y solo se ejercita hasta el final en entornos sin interceptación TLS local.
6. **Escenario concreto de explotación o fallo:**
   No constituye una vulnerabilidad del código de J.A.R.V.I.S., sino una limitación ambiental del host de pruebas que debe quedar explícitamente registrada conforme a las reglas de `AGENTS.md` y de la auditoría.
7. **Impacto potencial:**
   Cobertura empírica parcial del camino feliz post-TLS sobre socket OS en hosts con antivirus que interceptan TLS local.
8. **Evidencia reproducible:**
   Ejecución de `.\venv\Scripts\python.exe -m unittest -v tests.test_web_extraction_skill`:
   - 16 tests ejecutados en `0.785s` -> `OK` (en el sub-caso de socket local, `server.sni_observed` recibe `["secure.example.test"]` y el cliente devuelve `TLS_ERROR` ante el certificado re-firmado por Avast; acto seguido, el handshake `ssl.MemoryBIO` con el mismo `ssl.SSLContext` completa con éxito).
9. **Probabilidad de explotación:** **N/A** (observación de entorno y QA).
10. **Corrección recomendada:**
    - Mantener la doble verificación actual (socket OS real + `ssl.MemoryBIO` in-memory) y agregar un test unitario que conecte `_PinnedHTTPSConnection` sobre un par de sockets locales envueltos con `ssl.MemoryBIO` o un transporte no interceptado en puertos altos si se desea ejercitar la lectura HTTP post-TLS de `http.client` sin depender del driver de red de Avast.
    - Incorporar a `tests/test_web_extraction_skill.py` los tests adversariales de regresión descritos en **F-01 a F-06**.
11. **Pruebas de regresión necesarias:**
    - Ver sección D y pruebas detalladas en F-01 a F-06.
12. **Dependencias o riesgos de la corrección:**
    - Ninguno.

---

## D. COBERTURA DE PRUEBAS

### 1. Pruebas ejecutadas y resultados verificados

| Suite / Comando ejecutado | Resultado | Tiempo | Observaciones |
| :--- | :--- | :--- | :--- |
| `.\venv\Scripts\python.exe -m unittest -v tests.test_web_extraction_skill` | **16 / 16 OK** (0 fallos, 0 errores) | `0.785s` | Cubre SSRF IPv4/IPv6/IDNA/puertos/credenciales, DNS mixto, DNS rebinding/IP pinning, redirecciones hacia loopback, TLS/SNI + certificados expirados/autofirmados/hostname mismatch, límites streaming/gzip bomb, parser HTML ruidoso/malformado, `SearchProvider`, `WebDocumentConverter` + `DocumentExportSkill` (`.md` y `.docx`) y orquestación Multi-Brain. |
| `.\venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"` | **523 tests ejecutados: OK** (0 fallos, 0 errores, 3 `skipped`) | `103.435s` | Los 3 `skipped` corresponden a pruebas de symlinks en `tests/test_document_export_skill.py` cuando el proceso Windows no posee `SeCreateSymbolicLinkPrivilege` (`WinError 1314`). |
| Verificación de integridad de `data/memory_store.json` | **INTACTO** (`1218` bytes, SHA-256 `83640EAADFF5E33FEB9126F6EEBB9F7960F924F01BB3A1968468014223341222`) | `< 0.1s` | No fue leído ni modificado durante la implementación ni durante la auditoría. |

### 2. Casos adversariales aún NO cubiertos en `tests/test_web_extraction_skill.py`

1. **Inyección de citas sin circunflejo (`[id]: https://...`) y enlaces con `)` (F-01):** Falta test que verifique que `[src-99]: https://...` dentro de `<p>` no crea un `Citation` en `Document.elements` y que URLs con `)` no inyectan enlaces Markdown.
2. **Secuestro desde `<template>`, `<noscript>` y `hidden` + `<base href>` cross-origin (F-02):** El test actual verifica que el texto de `<template>` no entra en `paragraphs`, pero no prueba etiquetas `<title>`, `<meta>`, `<link rel="canonical">` ni `<base href>` anidadas dentro de `<template>` o `<div hidden>`.
3. **Streams `gzip`/`deflate` truncados sin trailer CRC32 (F-03 Parte A):** El test actual verifica bombas `gzip` que superan `max_decompressed_bytes`, pero no flujos `gzip` truncados antes de `eof`.
4. **Preservación de `redirect_chain` y `final_url` cuando falla un salto posterior (F-03 Parte B):** El test actual de redirección a `127.0.0.1` verifica `status == FAILED` y `errors[0].code == "SSRF_BLOCKED"`, pero no comprueba que `doc.redirect_chain` conserve los saltos previos.
5. **Inundación de bloques `<p>` frente a `max_normalized_chars` e ISATAP global (F-04):** El test actual de `max_normalized_chars` usa pocos párrafos largos y verifica `len(doc.normalized_text)`, sin verificar `len(converter.to_markdown(doc))` con cientos de párrafos cortos ni direcciones `2000::5efe:127.0.0.1`.
6. **Ocultamiento CSS inline (`style="display:none"`) y controles bidireccionales Unicode (`\u202e`) (F-05):** No existen aún tests específicos para estos vectores de *Indirect Prompt Injection*.

---

## E. PLAN DE REMEDIACIÓN

Orden de prioridad recomendado y dependencias entre correcciones:

| Prioridad | Hallazgo | Acción correctiva concreta | Esfuerzo estimado | Dependencias |
| :--- | :--- | :--- | :--- | :--- |
| **1 (Alta prioridad técnica)** | **F-02** (`MEDIUM`) | En `skills/web_parser.py` (`_StructuredHTMLParser`), rastrear `self._in_head` por separado de `self._ignored_depth`; procesar `<title>`, `<meta>`, `<link rel="canonical">`, JSON-LD y `<base href>` solo si `self._ignored_depth == 0`, y restringir `<base href>` a `<head>` y al mismo origen que `source_url`. | Bajo (~25 líneas en `skills/web_parser.py`) | Ninguna. Independiente de otros módulos. |
| **2 (Alta prioridad técnica)** | **F-01** (`MEDIUM`) | En `skills/web_document_converter.py:33`, ampliar `_MD_BLOCK_SPOOF_RE` para cubrir `\[\^?[^\]]+\]:` y `\|.*\|$`. En `skills/web_parser.py` (`normalize_extracted_url`) y `skills/web_document_converter.py`, codificar `(`, `)` y espacios en URLs extraídas. | Bajo (~20 líneas entre `web_document_converter.py` y `web_parser.py`) | Ninguna. No requiere tocar el Paso #3.A (`document_parser.py`). |
| **3 (Media prioridad técnica)** | **F-03** (`MEDIUM`) | En `skills/web_fetcher.py` (`_IncrementalBoundedDecompressor.finish`), verificar `if not self._decompressor.eof:` y lanzar `WebFetchError("CORRUPTED_COMPRESSED_STREAM", ...)`. En `WebFetcher.fetch` y `WebExtractionSkill.extract_url`, preservar `redirect_chain` y `final_url` en las excepciones ocurridas tras redirecciones. | Bajo (~30 líneas entre `web_fetcher.py` y `web_extraction_skill.py`) | Ninguna. |
| **4 (Baja prioridad / Hardening)** | **F-04** (`LOW`) | En `skills/web_parser.py` (`extract_from_fetch`), aplicar el presupuesto `max_normalized_chars` también a la acumulación de `paragraphs`, `headings`, `lists` y `sections`. En `skills/web_security.py` (`_check_ip_recursive`), desencapsular sufijos ISATAP (`::5efe:w.x.y.z` / `::0200:5efe:w.x.y.z`). | Bajo (~25 líneas entre `web_parser.py` y `web_security.py`) | Ninguna. |
| **5 (Baja prioridad / Hardening)** | **F-05** (`LOW`) | En `skills/web_parser.py`, añadir controles bidireccionales Unicode (`\u202a-\u202e\u2066-\u2069`) a `_INVISIBLE_CHARS_RE` y filtrar `style="display:none"|"visibility:hidden"|"opacity:0"` en `_is_noise_element`. En `skills/web_extraction_skill.py`, delimitar el resumen como contenido externo no confiable. | Bajo (~20 líneas) | Ninguna. |
| **6 (Preventiva)** | **F-06** y **F-07** (`LOW` / `INFO`) | Deduplicación canónica en `extract_urls()`, chequeo de `deadline` antes de reintentar en `_fetch_single_hop_with_retries()` y adición de tests de regresión para F-01..F-06 en `tests/test_web_extraction_skill.py`. | Bajo (~80 líneas de tests) | Ejecutar tras aplicar F-01 a F-05. |

---

## F. ASPECTOS CORRECTAMENTE IMPLEMENTADOS

Se verificó empíricamente y mediante inspección de código que los siguientes controles de seguridad y arquitectura están correctamente implementados:

1. **Prevención de DNS Rebinding y TOCTOU mediante IP Pinning real (`skills/web_fetcher.py:114-214`):**
   - `_PinnedHTTPConnection` y `_PinnedHTTPSConnection` abren el socket TCP directamente contra `(self._pinned_ip, self.port)` sin volver a invocar `getaddrinfo` sobre el nombre de dominio.
   - En HTTPS, `self._context.wrap_socket(raw_sock, server_hostname=self._original_hostname)` preserva el SNI y la verificación del certificado X.509 contra el hostname original aunque el socket esté conectado a la IP literal fijada.
2. **Defensa SSRF multicapa en `skills/web_security.py`:**
   - Bloqueo de esquemas distintos de `http`/`https`, rechazo de credenciales embebidas `user:pass@host` (`URL_CREDENTIALS_FORBIDDEN`) y restricción de puertos (`{80, 443}`).
   - Detección de notaciones IPv4 ofuscadas (entero de 32 bits, octal, hexadecimal y formas abreviadas de 2 y 3 partes) antes de permitir cualquier resolución DNS.
   - Bloqueo estricto de respuestas DNS mixtas (`resolve_and_validate_host` en `skills/web_security.py:378-446`): basta con que una sola IP devuelta por `socket.getaddrinfo` sea privada, loopback, link-local o reservada para abortar toda la conexión.
   - Cobertura completa de IPv4-mapped IPv6 (`::ffff:127.0.0.1`), SIIT (`::ffff:0:127.0.0.1`), IPv4-compatible (`::127.0.0.1`), 6to4 (`2002::/16`), Teredo (`2001::/32`) y NAT64 (`64:ff9b::/96`, `64:ff9b:1::/48`).
3. **Control de redirecciones y presupuesto temporal (`skills/web_fetcher.py:419-465`):**
   - `http.client` no sigue redirecciones automáticamente; `WebFetcher.fetch()` controla manualmente hasta `max_redirects=3`, resuelve URLs relativas con `urljoin` y ejecuta `resolve_target()` completo en cada salto.
   - Presupuesto global monótono (`deadline = time.monotonic() + total_timeout_seconds`) respetado en cada chunk de lectura (`skills/web_fetcher.py:604-608`).
4. **Protección contra bombas de compresión y descargas infinitas (`skills/web_fetcher.py:322-386, 569-630`):**
   - Rechazo temprano por `Content-Length` excesivo o inválido/negativo/múltiple contradictorio.
   - Lectura en streaming de bloques de `16 KiB` con corte inmediato al superar `max_response_bytes` (`2 MiB`) o `max_decompressed_bytes` (`4 MiB`).
5. **Inmutabilidad del modelo de dominio y trazabilidad (`skills/web_models.py`):**
   - Todos los modelos (`WebSourceDocument`, `WebExtractionResult`, `WebSection`, `WebHeading`, `WebParagraph`, `WebList`, `WebLink`, `WebMetadata`, `WebIssue`, `SearchResultItem`) son `@dataclass(frozen=True)` con colecciones normalizadas a `tuple` inmutables en `__post_init__`.
   - Separación estricta entre `published_at` (nunca inventado si falta en la página) y `extracted_at` (timestamp UTC ISO-8601 real de la extracción).
6. **Respeto absoluto de los límites arquitectónicos de J.A.R.V.I.S. (`AGENTS.md`):**
   - El Paso #3.A (`DocumentExportSkill`) y N5 (`TransactionManager` / `memory_store.json`) no fueron modificados.
   - Cero dependencias externas nuevas agregadas a `requirements.txt`.
   - Desacoplamiento total respecto de proveedores LLM específicos (`Ollama`, `Gemini`, `Claude`, `GPT`).

---

## G. RECOMENDACIÓN DE CONTINUIDAD Y VEREDICTO FINAL

### Veredicto explícito

> **`APROBADO CON OBSERVACIONES`**

### Justificación técnica del veredicto

1. **Ausencia de vulnerabilidades críticas o altas (`0 CRITICAL`, `0 HIGH`):**
   No existe ninguna ruta de ejecución remota de código (RCE), lectura de archivos locales (`file://`), bypass del pinneo de IP en sockets TCP/TLS, desactivación de validación de certificados TLS ni acceso SSRF directo o por redirección hacia `127.0.0.1`, `localhost`, redes privadas RFC 1918 (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`), link-local (`169.254.0.0/16`) o endpoints de metadatos cloud.
2. **Naturaleza acotada y fácilmente remediable de los hallazgos `MEDIUM` (`F-01`, `F-02`, `F-03`):**
   Los tres hallazgos de severidad media afectan a la fidelidad de sanitización/atribución del contenido extraído (inyección de nodos `Citation` sin `^` y enlaces con `)` en `WebDocumentConverter`, lectura de metadatos/`<base href>` dentro de etiquetas `<template>`/`hidden`, y detección de EOF en flujos `gzip` truncados o preservación de `redirect_chain` tras errores en redirecciones). Su corrección requiere cambios quirúrgicos (~75 líneas en total) dentro de `skills/web_parser.py`, `skills/web_document_converter.py`, `skills/web_fetcher.py` y `skills/web_extraction_skill.py`, sin tocar el Paso #3.A ni la arquitectura general.
3. **Recomendación:**
   El Paso #3.B (`WebExtractionSkill`) posee una base arquitectónica y de seguridad de red sólida. Se recomienda aplicar el paquete de remediación puntual para **F-01, F-02 y F-03** (junto con el endurecimiento ligero de **F-04 y F-05** y sus pruebas de regresión) en una iteración de cierre antes de habilitar la extracción automática de URLs externas no confiables en flujos de producción.
