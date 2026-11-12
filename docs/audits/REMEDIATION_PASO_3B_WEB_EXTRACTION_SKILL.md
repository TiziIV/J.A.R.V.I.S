# INFORME DE REMEDIACIÓN Y HARDENING DE SEGURIDAD — PASO #3.B (`WebExtractionSkill`)

**Proyecto:** J.A.R.V.I.S. (`jarvis-core`)
**Fase:** Paso #3.B — Remediación de Auditoría Independiente de Seguridad y Arquitectura (`WebExtractionSkill`)
**Commit auditado:** `0a468f4` (`feat(web-extraction): implement Paso 3.B WebExtractionSkill with SSRF-safe pinned transport and Paso 3.A integration`)
**Fecha de remediación:** 2026-11-12
**Estado general de remediación:** **COMPLETADO — 6/6 HALLAZGOS TÉCNICOS RESUELTOS (F-01 a F-06) + 1 LIMITACIÓN DE ENTORNO DOCUMENTADA (F-07)**

---

## 1. ESTADO GENERAL DE REMEDIACIÓN

Se ejecutó la remediación integral de los hallazgos identificados en el informe de auditoría independiente (`docs/audits/AUDIT_PASO_3B_WEB_EXTRACTION_SKILL.md`), manteniendo intactos los límites arquitectónicos de `AGENTS.md`, el diseño cerrado del Paso #3.A (`DocumentExportSkill`), la fase N5 (`Recovery & Transaction Manager`) y todos los cambios locales preexistentes del árbol de trabajo.

Ningún control de seguridad existente (TLS 1.2+, verificación estricta de hostname/certificados, validación SSRF previa a la conexión, DNS pinning en el socket TCP real, revalidación de redirecciones o límites de tamaño en streaming) fue debilitado ni desactivado.

---

## 2. TABLA CONSOLIDADA DE HALLAZGOS (F-01 A F-07)

| ID | Severidad Original | Estado Final | Archivos Modificados | Pruebas de Regresión Agregadas |
|---|---|---|---|---|
| **F-01** | `MEDIUM` | **RESUELTO** | `skills/web_document_converter.py`, `skills/web_parser.py` | `TestSecurityHardeningF01ToF06.test_f01_citation_spoofing_and_markdown_link_injection_remediation` |
| **F-02** | `MEDIUM` | **RESUELTO** | `skills/web_parser.py` | `TestSecurityHardeningF01ToF06.test_f02_metadata_isolation_base_href_and_malformed_html` |
| **F-03** | `MEDIUM` | **RESUELTO** | `skills/web_fetcher.py`, `skills/web_security.py`, `skills/web_extraction_skill.py` | `TestSecurityHardeningF01ToF06.test_f03_truncated_compression_and_redirect_chain_traceability` |
| **F-04** | `LOW` | **RESUELTO** | `skills/web_parser.py`, `skills/web_security.py` | `TestSecurityHardeningF01ToF06.test_f04_shared_char_budget_and_isatap_siit_ipv6_blocking` |
| **F-05** | `LOW` | **RESUELTO** | `skills/web_parser.py`, `skills/web_models.py`, `skills/web_extraction_skill.py`, `src/brain/intent_router.py` | `TestSecurityHardeningF01ToF06.test_f05_prompt_injection_hidden_css_unicode_and_untrusted_envelope` |
| **F-06** | `LOW` | **RESUELTO** | `skills/web_fetcher.py`, `skills/web_extraction_skill.py`, `src/brain/intent_router.py` | `TestSecurityHardeningF01ToF06.test_f06_non_retryable_exceptions_canonical_dedup_and_router_fast_path` |
| **F-07** | `INFO` | **DOCUMENTADO / NO APLICABLE EN PRODUCCIÓN** | `docs/audits/REMEDIATION_PASO_3B_WEB_EXTRACTION_SKILL.md`, `CONTEXT.md` | Verificado mediante suite TLS efímera en loopback (`test_https_pinned_ip_preserves_sni_host_and_strict_tls_verification`) |

---

## 3. EXPLICACIÓN TÉCNICA DE CADA CORRECCIÓN

### F-01 — `MEDIUM`: Remediación de Citation Spoofing y Markdown Link Injection

- **Causa raíz verificada:**
  1. `DocumentParser.RE_CITATION_DEF` en el Paso #3.A (`skills/document_parser.py`) reconoce definiciones de citas con acento circunflejo opcional (`^\[\^?([^\]]+)\]:\s*(.+)$`), mientras que `_MD_BLOCK_SPOOF_RE` en `skills/web_document_converter.py` solo escapaba líneas con `\^` obligatorio (`\[\^[^\]]+\]:`), permitiendo que un párrafo extraído que comenzara por `[id]: https://...` inyectara un objeto `Citation` falso sin modificar `skills/document_parser.py`.
  2. La interpolación de URLs extraídas en enlaces Markdown (`[{label}]({url})`) y citas finales no codificaba paréntesis `(` / `)`, corchetes `[` / `]` ni espacios, permitiendo romper el delimitador Markdown `](...)`.
- **Corrección implementada:**
  - En `skills/web_document_converter.py`, se actualizó `_MD_BLOCK_SPOOF_RE` a `r"^(#{1,6}\s|>\s?|```|~~~|\[\^?[^\]]+\]:|[-*+]\s|\d+[.)]\s)"`, neutralizando tanto `[id]: url` como `[^id]: url` al inicio de encabezados, párrafos o ítems de lista mediante prefijo `\`, sin incluir patrones tipo `|.*|` que pudieran confundir tablas Markdown legítimas con citas falsas.
  - En `skills/web_parser.py` (`normalize_extracted_url`) y `skills/web_document_converter.py` (`_sanitize_markdown_url`), se rechazan caracteres de control y se codifican en porcentaje espacios (`%20`), paréntesis (`%28`, `%29`), corchetes (`%5B`, `%5D`), comillas (`%22`, `%27`), ángulos (`%3C`, `%3E`) y acentos graves (`%60`), preservando URLs legítimas con paréntesis (p. ej. Wikipedia) y bloqueando toda inyección de enlaces Markdown.

### F-02 — `MEDIUM`: Aislamiento de Metadatos HTML y Política Same-Origin para `<base href>`

- **Causa raíz verificada:**
  - En `_StructuredHTMLParser.handle_starttag()` (`skills/web_parser.py`), la inspección de `<html lang>`, `<base href>`, `<meta>`, `<link rel="canonical">`, `<script type="application/ld+json">` y `<title>` ocurría antes de verificar `self._ignored_depth > 0`, y `<head>` compartía contador con `_ignored_depth`. Esto permitía que etiquetas dentro de `<template>`, `<noscript>`, nodos `hidden`/`aria-hidden="true"`, `<footer>`, `<nav>`, `<aside>` o `<body>` inyectaran metadatos globales o que un `<base href>` cross-origin secuestrara la resolución de enlaces relativos.
- **Corrección implementada:**
  - Se separó el seguimiento de `self._head_depth` y `self._body_started` respecto de `self._ignored_depth` en `_StructuredHTMLParser`.
  - Si `self._ignored_depth > 0`, `handle_starttag()` apila la etiqueta únicamente para balancear el cierre y retorna de inmediato antes de inspeccionar cualquier etiqueta de metadatos.
  - Cuando comienza cualquier etiqueta estructural de cuerpo (`_BODY_INITIATOR_TAGS`: `body`, `main`, `article`, `section`, `div`, `p`, `h1`–`h6`, `ul`, `ol`, `table`, `header`, `footer`, `nav`, `aside`), se cierra cualquier `<title>` o `<head>` mal cerrado (`_close_open_title()`) y se marca `_body_started = True`, impidiendo que etiquetas tardías en `<body>` contaminen metadatos globales sin perder tolerancia ante HTML imperfecto.
  - `<base href>` solo se acepta dentro de `<head>` explícito antes de `<body>` y fuera de nodos ocultos/ignorados; además, en `WebContentExtractor.extract_from_fetch()`, se valida con `_has_same_origin(candidate_base, source_url)`. Si el `<base href>` apunta a otro origen, se ignora, se conserva `source_url` como base y se registra la advertencia trazable `CROSS_ORIGIN_BASE_IGNORED`.

### F-03 — `MEDIUM`: Rechazo de Streams Comprimidos Truncados y Trazabilidad de Redirecciones en Fallos

- **Causa raíz verificada:**
  1. `_IncrementalBoundedDecompressor.finish()` ejecutaba `self._decompressor.flush()` sin comprobar `self._decompressor.eof`, aceptando silenciosamente respuestas `gzip` o `deflate` truncadas a mitad de transmisión.
  2. Cuando ocurría un `WebSecurityError` (p. ej. redirección hacia IP privada/loopback) o `WebFetchError` (p. ej. HTTP 502 o timeout) en un salto posterior a una redirección 301/302, la excepción no transportaba `redirect_chain` ni `final_url`, por lo que `WebExtractionSkill.extract_url()` devolvía `redirect_chain=()` y `final_url=original_url`.
- **Corrección implementada:**
  - En `skills/web_fetcher.py`, `_IncrementalBoundedDecompressor.finish()` verifica tras `flush()` que `getattr(self._decompressor, "eof", False)` sea `True` y que no queden bytes sin consumir (`unused_data` / `unconsumed_tail`), lanzando `UnsupportedEncodingError` ante cualquier stream `gzip` o `deflate` truncado o corrupto, mientras preserva respuestas vacías válidas (`identity` de 0 bytes y `gzip.compress(b"")`).
  - Se añadieron los atributos `final_url` y `redirect_chain` a `WebSecurityError` (`skills/web_security.py`) y `WebFetchError` (`skills/web_fetcher.py`), poblados en `WebFetcher.fetch()` en cada salto y preservados en `WebExtractionSkill.extract_url()` dentro del `WebSourceDocument` y sus `WebIssue`.

### F-04 — `LOW`: Presupuesto Compartido de Caracteres (`max_normalized_chars`) y Bloqueo de ISATAP / SIIT IPv6

- **Causa raíz verificada:**
  1. `max_normalized_chars` solo truncaba el string plano `full_normalized` al final de `extract_from_fetch()`, dejando las tuplas `headings`, `paragraphs`, `lists` y `sections` sin acotar.
  2. `_check_ip_recursive()` en `skills/web_security.py` desempaquetaba `ipv4_mapped`, `sixtofour`, `teredo` y NAT64 (`64:ff9b::/96` y `64:ff9b:1::/48`), pero una dirección IPv6 con prefijo global e identificador de interfaz ISATAP (`::5efe:w.x.y.z` o `::0200:5efe:w.x.y.z`) o SIIT (`::ffff:0:w.x.y.z`) podía encapsular una IPv4 privada o loopback sin que se evaluara recursivamente.
- **Corrección implementada:**
  - En `skills/web_parser.py` (`WebContentExtractor.extract_from_fetch`), `max_normalized_chars` se aplica como presupuesto compartido durante la construcción de `headings`, `paragraphs`, `lists`, `sections` y `normalized_text`. De este modo, `doc.paragraphs`, `doc.lists`, `doc.sections`, `doc.normalized_text`, `doc.to_dict()` y `WebDocumentConverter.to_markdown(doc)` permanecen mutua y estrictamente acotados, marcando `truncated = True`, `status = ExtractionStatus.PARTIAL` y warning `NORMALIZED_TEXT_TRUNCATED`.
  - En `skills/web_security.py` (`_check_ip_recursive`), se añadió detección y validación recursiva de la IPv4 embebida en los últimos 32 bits para direcciones ISATAP (`((raw_int >> 32) & 0xFCFFFFFF) == 0x00005EFE`) y SIIT IPv4-translated (`(raw_int >> 32) == 0xFFFF0000`).

### F-05 — `LOW`: Defensa en Profundidad frente a Prompt Injection Indirecto, CSS Oculto y Controles Unicode

- **Causa raíz verificada:**
  - El parser HTML filtraba atributos `hidden` y `aria-hidden="true"`, pero no estilos inline de ocultamiento evidente (`display:none`, `visibility:hidden`, `opacity:0`, `font-size:0`, `clip:rect(0,0,0,0)`, `clip-path:inset(100%)`, `width:0;overflow:hidden`), ni limpiaba controles bidireccionales Unicode (`U+202A`–`U+202E`, `U+2066`–`U+2069`, `U+200E`, `U+200F`, `U+061C`) o caracteres del bloque Unicode Tags (`U+E0000`–`U+E007F`).
- **Corrección implementada:**
  - En `skills/web_parser.py`:
    - Se amplió `_INVISIBLE_CHARS_RE` y `_CONTROL_CHARS_RE` para eliminar controles bidireccionales, operadores invisibles, bloque C1 (`\x7f-\x9f`) y Unicode Tags (`\U000e0000-\U000e007f`), preservando caracteres multilingües legítimos.
    - Se incorporó `_is_hidden_style()` dentro de `_has_hidden_attrs()` / `_is_noise_element()` para descartar elementos ocultos mediante CSS inline.
  - En `skills/web_models.py` y `skills/web_extraction_skill.py`:
    - `WebSourceDocument.to_dict()` y `WebExtractionReport.to_dict()` incluyen `"trust_level": "untrusted_external_content"`.
    - Se añadió `WebExtractionSkill.build_llm_envelope(source)` (y soporte `output_format="envelope"`), que separa estructuralmente la política del sistema (`allow_tool_execution_from_content: False`) de los datos externos (`untrusted_data`).
    - `_format_human_summary()` neutraliza prefijos de suplantación de rol (`Sistema:`, `System:`, `Usuario:`, `Assistant:`, `JARVIS:`) y patrones `{"action":"call_tool"` en los extractos de texto plano.
  - En `src/brain/intent_router.py`:
    - `_build_system_prompt()` instruye explícitamente al modelo a tratar cualquier contenido extraído o contexto previo como dato pasivo no confiable y a no ejecutar nunca `call_tool` por órdenes embebidas en datos externos.

### F-06 — `LOW`: Excepciones No Reintentables, Deduplicación Canónica de URLs y Fast-Path en `IntentRouter`

- **Causa raíz verificada:**
  1. `_fetch_single_hop_with_retries()` en `skills/web_fetcher.py` capturaba `Exception` genéricas y las reintentaba como si fueran fallos transitorios de red.
  2. `WebExtractionSkill.extract_urls()` deduplicaba únicamente por igualdad literal (`raw_item.strip()`), permitiendo que variantes equivalentes (`https://EXAMPLE.com:443/doc#a` y `https://example.com/doc#b`) consumieran el cupo `max_pages`.
  3. `IntentRouter.detect_local_skill()` carecía de una regla determinista para órdenes explícitas de extracción web con URLs HTTP/HTTPS.
- **Corrección implementada:**
  - En `skills/web_fetcher.py`, solo se reintentan instancias de `WebFetchError` marcadas con `retryable=True` y únicamente si resta presupuesto en `deadline`; cualquier `Exception` no tipada se envuelve de inmediato con `retryable=False` sin reintento.
  - En `skills/web_extraction_skill.py`, `_canonical_url_dedup_key()` normaliza esquema y host (IDNA ASCII en minúsculas), elimina puertos por defecto (`:80` en HTTP, `:443` en HTTPS), elimina fragmentos `#...` y normaliza path vacío a `/`, preservando diferencias reales de mayúsculas/minúsculas en `path` y `query` antes de aplicar el recorte `[:effective_limit]`.
  - En `src/brain/intent_router.py`, se añadió la regla `# 9. WebExtractionSkill ('web_extraction')` en `detect_local_skill()` para solicitudes explícitas con verbos de extracción/lectura/análisis y una o más URLs `https?://`, resolviendo en `< 1 ms` sin invocar al LLM.

### F-07 — `INFO`: Limitación del Entorno Windows / Avast Web Shield en Pruebas HTTPS Externas

- **Documentación técnica:**
  - El hallazgo F-07 describe la interceptación TLS local de conexiones salientes hacia el puerto `443` en el host de pruebas por parte de Avast Web Shield (`Issuer: CN=Web Shield Root`), que reemplaza el certificado remoto por uno emitido por una CA local no incluida en el bundle de `certifi`.
  - Este comportamiento confirma que la verificación estricta de certificados (`ssl.CERT_REQUIRED`, `check_hostname = True`) en `WebFetcher` funciona exactamente como debe al rechazar interceptores MITM no confiables. Por ello, **no se realizó ningún cambio de producción que debilite la validación TLS**.

---

## 4. RESULTADOS DE LAS PRUEBAS EJECUTADAS

- Compilación estática (`py_compile`) sobre todos los módulos modificados y pruebas: **OK (código 0)**.
- `tests.test_web_extraction_skill`: **22 tests ejecutados — 22 OK (`0` fallos, `0` errores)**, incluyendo las 6 pruebas integrales de regresión `test_f01` a `test_f06`.
- `tests.test_document_export_skill` (Paso #3.A): **41 tests ejecutados — 38 OK, 3 skipped por privilegios de symlink de Windows (`0` fallos)**.
- Suite N5 y resiliencia (`tests.test_memory_isolation`, `tests.test_transaction_recovery`, `tests.test_file_editor_and_chain`, `tests.test_intent_router_chaos`): **100% OK**.
- Suite global completa (`.\venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"`): **529 tests ejecutados en 96.895s — OK (`0` fallos, `0` errores, `3` skipped por privilegio de symlink de Windows)**.
- Arranque de CLI (`.\venv\Scripts\python.exe main.py --help`): **OK (código 0)**.

---

## 5. RIESGOS RESIDUALES

- No quedan hallazgos `CRITICAL`, `HIGH`, `MEDIUM` ni `LOW` pendientes en el Paso #3.B.
- Como en cualquier sistema que procesa texto externo con modelos de lenguaje, la defensa frente a prompt injection indirecto (F-05) es multicapa (filtrado de nodos ocultos y CSS invisible, limpieza de controles Unicode/Tags, neutralización de prefijos de rol en resúmenes, etiquetado `trust_level: "untrusted_external_content"`, envoltura `build_llm_envelope` y directivas de aislamiento en `IntentRouter`).
