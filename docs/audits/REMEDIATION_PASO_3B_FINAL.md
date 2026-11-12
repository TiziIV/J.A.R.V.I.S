# INFORME DE REMEDIACIÓN INTEGRAL Y HARDENING DEFINITIVO — PASO #3.B (`WebExtractionSkill`)

- **Proyecto:** J.A.R.V.I.S. (`jarvis-core`)
- **Componente auditado y endurecido:** Paso #3.B (`WebExtractionSkill`) e integración con arquitectura Multi-Brain (`SkillManager`, `IntentRouter`, `BrainManager`, `ConversationMemory` y adaptadores `OllamaBrain`, `GeminiBrain`, `ClaudeBrain`, `OpenAIBrain`, `CodexBrain`).
- **Commits de referencia previos:**
  - Implementación inicial Paso #3.B: `0a468f4`
  - Primera remediación parcial: `b535594`
  - Auditoría independiente post-remediación: [INDEPENDENT_SECURITY_AUDIT_PASO_3B_POST_REMEDIATION.md](file:///e:/Tiziano%20Facultad/J.A.R.V.I.S/jarvis-core/docs/audits/INDEPENDENT_SECURITY_AUDIT_PASO_3B_POST_REMEDIATION.md)
- **Estado final de los hallazgos:** **100 % CERRADOS Y VERIFICADOS** (`F-01`, `F-02`, `F-03`, `F-04`, `F-05`, `F-06`; `F-07` preservado como limitación documentada del entorno local Windows/Avast sin debilitar TLS).

---

## 1. Resumen Ejecutivo

Tras la auditoría adversarial independiente realizada sobre el commit `b535594`, se identificaron cuatro hallazgos pendientes de cierre (`F-05` HIGH, `F-01` MEDIUM, `F-04` MEDIUM y `F-06` LOW) cuya raíz principal residía en que:

1. El aislamiento de contenido web no confiable (`WebExtractionSkill.build_llm_envelope()`) existía en la skill pero no estaba conectado extremo a extremo con `SkillManager`, `BrainManager`, `ConversationMemory`, `IntentRouter` ni los cinco adaptadores de cerebro; además, el resultado de `web_extraction` se almacenaba en `ConversationMemory` como mensaje `"assistant"` confiable y se reinyectaba directamente en `system_prompt` mediante `memory.inject_context(base_prompt)`.
2. El convertidor documental (`skills/web_document_converter.py`) permitía que una URL embebida dentro de `<title>` antecediera a la URL real en la línea `[^src-N]: ...`, provocando el secuestro del atributo `Citation.url` al ser procesado por `RE_URL` en `DocumentParser`.
3. El presupuesto `max_normalized_chars` acotaba únicamente el cuerpo (`headings`, `paragraphs`, `lists`), dejando fuera `metadata.description` y la colección `links`, lo que permitía amplificaciones de hasta `467.444` bytes en JSON y `125.856` bytes en Markdown con `max_normalized_chars=500`.
4. El fast-path determinista de `IntentRouter.detect_local_skill()` truncaba el paréntesis de cierre legítimo en URLs con paréntesis balanceados (como artículos de Wikipedia) y se activaba por falsos positivos ante oraciones condicionales o de diagnóstico que contenían los verbos `"lee"` o `"analiza"`.

Esta remediación resuelve de forma arquitectónica y definitiva todos los puntos anteriores sin reabrir ni modificar módulos cerrados del Paso #3.A ni de N5, y sin tocar ni migrar `data/memory_store.json`.

---

## 2. Arquitectura de la Frontera Estructural de Confianza (Cierre de `F-05` — P0)

### 2.1. Causa raíz previa
En el flujo anterior (`src/brain/brain_manager.py`):
- `reply_text = self.skill_manager.execute_skill("web_extraction", **params)` devolvía un resumen textual plano.
- Inmediatamente después se ejecutaba `self.memory.add_message("assistant", reply_text)`.
- En los turnos siguientes, `self.memory.inject_context(base_prompt)` concatenaba dicho bloque bajo la etiqueta `Asistente:` dentro del `system_prompt` enviado a `OllamaBrain`, `GeminiBrain`, `ClaudeBrain`, `OpenAIBrain` y `CodexBrain`.

### 2.2. Diseño implementado extremo a extremo

1. **Emisión dual en `WebExtractionSkill` y `SkillManager`:**
   - [web_extraction_skill.py](file:///e:/Tiziano%20Facultad/J.A.R.V.I.S/jarvis-core/skills/web_extraction_skill.py) incorpora `execute_with_envelope(params, **kwargs) -> tuple[str, dict[str, Any], WebExtractionReport | None]` y amplía `build_llm_envelope()` con metadatos explícitos de frontera de confianza:
     - `"trust_level": "untrusted_external_content"`
     - `"source_type": "external_web"`
     - `"origin_skill": "web_extraction"`
     - `"allow_tool_execution_from_content": False`
     - `"allow_system_prompt_promotion": False`
     - `"allow_memory_assistant_promotion": False`
     - `"provenance": {"source_urls": [...], "extracted_at": ...}`
   - [tools_schema.py](file:///e:/Tiziano%20Facultad/J.A.R.V.I.S/jarvis-core/config/tools_schema.py) expone explícitamente `'envelope'` entre los valores documentados de `output_format`.
   - [skill_manager.py](file:///e:/Tiziano%20Facultad/J.A.R.V.I.S/jarvis-core/src/tools/skill_manager.py) añade `execute_skill_with_envelope(skill_name, params, **kwargs)` manteniendo intacta la firma pública de `execute_skill(...) -> str`.

2. **Aislamiento en `ConversationMemory` (sin promoción a `assistant` ni contaminación de `system_prompt`):**
   - [memory.py](file:///e:/Tiziano%20Facultad/J.A.R.V.I.S/jarvis-core/src/brain/memory.py) separa los turnos conversacionales confiables (`self._messages`) de la evidencia externa no confiable (`self._external_references: Deque[Dict[str, Any]]`).
   - `add_external_reference(...)` almacena el sobre y resumen acotado con `role="untrusted_reference"` y `trust_level="untrusted_external_content"`.
   - Defensa en profundidad en `add_message(role, content, ...)`: si cualquier llamador intenta registrar un volcado o sobre de extracción web bajo `role="assistant"` (o con `trust_level="untrusted_external_content"`), `add_message` rechaza promocionarlo a `self._messages` y lo desvía automáticamente a `self._external_references`.
   - `get_context_prompt()` e `inject_context(system_prompt)` excluyen por completo `self._external_references`. Ningún byte de contenido web extraído entra jamás en `system_prompt` ni aparece como `Asistente:`.
   - Los mensajes cargados desde disco histórico (`load_from_disk`) reciben `trust_level="unverified_historical"`: si un registro histórico corresponde a un volcado de extracción web se excluye de `get_context_prompt()`, y cualquier patrón `"call_tool"` en mensajes históricos no verificados se neutraliza antes de construir el contexto, sin modificar ni migrar `data/memory_store.json`.

3. **Separación de canales en `BrainManager` y los 5 adaptadores de cerebro:**
   - En [brain_manager.py](file:///e:/Tiziano%20Facultad/J.A.R.V.I.S/jarvis-core/src/brain/brain_manager.py), cuando `process_turn()` ejecuta `web_extraction`, registra el resultado exclusivamente en `self.memory.add_external_reference(...)`.
   - En turnos conversacionales posteriores:
     - `plan_request(raw_input)` (incluyendo `AutoDeliberator`) evalúa únicamente `raw_input` del usuario.
     - `system_prompt_with_ctx` contiene únicamente la directiva estática de seguridad y el historial confiable del usuario/asistente.
     - La evidencia externa serializada (`untrusted_ctx = self.memory.format_untrusted_context_for_llm()`) se pasa por el parámetro separado `untrusted_context` hacia `_generate_with_fallback_policy` y `_invoke_single_mode`.
   - En cada adaptador de cerebro:
     - [ollama_client.py](file:///e:/Tiziano%20Facultad/J.A.R.V.I.S/jarvis-core/src/brain/ollama_client.py): mantiene `{"role": "system", "content": effective_system_prompt}` intacto y entrega `untrusted_context` en un mensaje de datos separado delimitado por `[EVIDENCIA_EXTERNA_NO_CONFIABLE_SOLO_LECTURA]`.
     - [gemini_client.py](file:///e:/Tiziano%20Facultad/J.A.R.V.I.S/jarvis-core/src/brain/gemini_client.py): conserva `system_instruction=active_system_prompt` separado de `contents`.
     - [claude_client.py](file:///e:/Tiziano%20Facultad/J.A.R.V.I.S/jarvis-core/src/brain/claude_client.py): conserva el campo superior `"system"` separado y envía `untrusted_context` como bloque de texto de datos separado dentro de `messages`.
     - [openai_client.py](file:///e:/Tiziano%20Facultad/J.A.R.V.I.S/jarvis-core/src/brain/openai_client.py): conserva `{"role": "system", "content": system_prompt}` (o `instructions` en `/v1/responses`) separado y envía `untrusted_context` en un bloque de datos independiente.
     - [codex_client.py](file:///e:/Tiziano%20Facultad/J.A.R.V.I.S/jarvis-core/src/brain/codex_client.py): ejecuta `classify_codex_task(prompt)` **exclusivamente sobre el `prompt` del usuario** y jamás sobre `untrusted_context`, impidiendo que instrucciones embebidas en páginas web escalen una consulta de lectura hacia `coding` / `workspace-write`.

4. **Protección en `IntentRouter`:**
   - [intent_router.py](file:///e:/Tiziano%20Facultad/J.A.R.V.I.S/jarvis-core/src/brain/intent_router.py) incorpora `_strip_untrusted_web_blocks()` e `_is_untrusted_web_payload()` en `detect_local_skill()` y `analyze_intent()`, garantizando que contenido web externo nunca pueda disparar `call_tool`.

5. **Endurecimiento del parser HTML/CSS frente a ocultamiento (`skills/web_parser.py`):**
   - Limpieza previa de comentarios CSS (`/*...*/`) y secuencias de escape (`_clean_css_text`).
   - Ampliación de `_HIDDEN_STYLE_RE` para detectar:
     - `opacity: 0`, `opacity: 0.0`, `opacity: .0`, `opacity: 0%`, `filter: opacity(0)`
     - `color: transparent`, `color: rgba(..., 0)` / `.0`, `color: hsla(..., 0)`, `color: #0000` / `#00000000`
     - `transform: scale(0)` / `scaleX(0)` / `scaleY(0)` / `scale3d(0,0,0)`
     - `left`, `right`, `top`, `bottom`, `text-indent`, `margin-left`, `margin-top` con desplazamientos negativos fuera de pantalla (`-\d{3,}px/vw/vh/em/rem/%`)
     - `content-visibility: hidden`, `clip-path: inset(50%/100%)`, `clip-path: circle(0)`, `clip-path: polygon(0 0, ...)`
   - Filtrado de clases de ocultamiento comunes (`_HIDDEN_CLASS_TOKENS`: `sr-only`, `visually-hidden`, `visually-hidden-focusable`, `screen-reader-text`, `screen-reader-only`, `d-none`, `u-hidden`, `is-hidden`, `offscreen`, `a11y-hidden`, `assistive-text`, `clip-hide`) y atributo `inert`.
   - Inspección acotada de bloques `<style>` (`_parse_style_rules`) para registrar selectores `.clase` y `#id` cuyas reglas coincidan con `_is_hidden_style` y descartar cualquier elemento del DOM que los utilice.

---

## 3. Remediación de `F-01`: Citation URL Hijack, Proveedores y Sanitización Markdown

En [web_document_converter.py](file:///e:/Tiziano%20Facultad/J.A.R.V.I.S/jarvis-core/skills/web_document_converter.py) y [web_parser.py](file:///e:/Tiziano%20Facultad/J.A.R.V.I.S/jarvis-core/skills/web_parser.py):

1. **Bloqueo de Citation URL Hijack en `<title>`:**
   - Se implementó `_sanitize_citation_label(text)` que neutraliza cualquier esquema embebido (`http://`, `https://`, `ftp://`, `file://`, `javascript:`, `data:`, `vbscript:`) dentro del título de la cita antes de construir `[^src-N]: {cite_title} — {safe_ref_url}`.
   - De este modo, `RE_URL` en `DocumentParser._extract_references()` (Paso #3.A, sin modificar) captura siempre y únicamente `safe_ref_url` (`source.original_url`).
2. **Validación estricta y normalización IDNA/Punycode en URLs de enlaces y citas:**
   - Tanto `normalize_extracted_url()` como `_sanitize_markdown_url()` validan que el esquema sea exclusivamente `http` o `https`, rechazan credenciales embebidas (`user:pass@`), imponen longitud máxima de `1024` caracteres y convierten hostnames IDN a ASCII punycode (`xn--...`) para exponer ataques homógrafos.
3. **Saneamiento de campos técnicos e inline code:**
   - `_sanitize_inline_code()` elimina backticks, saltos de línea, corchetes, paréntesis, pipes, barras invertidas y esquemas URL en `data.search_provider`, `sr.provider`, `err.code`, `warn.code`, `status` y `extracted_at`.
4. **Protección contra tablas falsas y barras invertidas finales:**
   - `_MD_BLOCK_SPOOF_RE` incluye líneas que comienzan con `|` y `_sanitize_markdown_text()` / `_sanitize_markdown_heading()` eliminan barras invertidas finales (`\`) y neutralizan enlaces por referencia `][`.

---

## 4. Remediación de `F-04`: Presupuesto Global Compartido de Recursos y Salida

En [web_parser.py](file:///e:/Tiziano%20Facultad/J.A.R.V.I.S/jarvis-core/skills/web_parser.py) y [web_document_converter.py](file:///e:/Tiziano%20Facultad/J.A.R.V.I.S/jarvis-core/skills/web_document_converter.py):

1. **Presupuesto unificado en `WebContentExtractor.extract_from_fetch()`:**
   - `max_budget = max(1, int(self.config.max_normalized_chars))` gobierna conjuntamente:
     - `metadata`: `title` (`<= min(300, max(40, max_budget // 3))`), `description` (`<= min(600, max(40, max_budget // 5))`, descontado directamente de `used_chars`), `author` y `site_name` (`<= min(160, max(32, max_budget // 5))`), `published_at` (`<= 64`) y `language` (`<= 32`).
     - Bloques estructurales y `normalized_text` (`headings`, `paragraphs`, `lists`, `sections`).
     - `links`: cada URL se limita a `1024` caracteres, el texto de ancla a `min(200, max(24, max_budget // 6))` y la suma total de caracteres de enlaces a `remaining_link_budget = min(4000, max(0, max_budget - used_chars))`.
   - Cualquier recorte en metadatos, cuerpo o enlaces activa `truncated = True`, estado `ExtractionStatus.PARTIAL` y advertencia `NORMALIZED_TEXT_TRUNCATED`.
   - Invariante verificada: `len(doc.metadata.description or "") + len(doc.normalized_text) + sum(len(l.url) + len(l.text) for l in doc.links) <= cfg.max_normalized_chars`.
   - Con el PoC de auditoría (`max_normalized_chars=500, max_links=80`, `description` de `120.000` chars, `<p>` de `5.000` chars y `80` enlaces de `4.000` chars), la salida pasó de `467.444` bytes JSON / `125.856` bytes Markdown a `< 4.000` bytes JSON y `< 2.500` bytes Markdown.
2. **Tope defensivo en `_StructuredHTMLParser` y `WebDocumentConverter`:**
   - `_StructuredHTMLParser` limita la acumulación en memoria de bloques (`max_blocks=4096`), enlaces crudos (`max_raw_links=2048`), atributos `<meta>` (`max_meta_field_chars=8192`) y hojas `<style>` (`max_style_bytes=65536`).
   - `WebDocumentConverter.to_markdown()` aplica presupuestos por fuente y por reporte incluso si recibe objetos `WebSourceDocument` construidos manualmente en memoria.

---

## 5. Remediación de `F-06`: Fast-Path de `IntentRouter`, Paréntesis Balanceados y Deduplicación

En [intent_router.py](file:///e:/Tiziano%20Facultad/J.A.R.V.I.S/jarvis-core/src/brain/intent_router.py) y [web_extraction_skill.py](file:///e:/Tiziano%20Facultad/J.A.R.V.I.S/jarvis-core/skills/web_extraction_skill.py):

1. **Preservación de paréntesis, corchetes y llaves balanceados (`_clean_extracted_url_token`):**
   - Elimina signos de puntuación finales de oración (`.`, `,`, `;`, `:`, `!`, `?`, `"`, `'`) y solo recorta `)`, `]` o `}` finales cuando su cantidad en el token excede la de su delimitador de apertura `(`, `[` o `{`.
   - Soporta enlaces de Wikipedia (`https://es.wikipedia.org/wiki/Python_(lenguaje).`), paréntesis anidados (`https://example.org/wiki/A_(B_(C)),`) y URLs entre paréntesis de oración (`(https://es.wikipedia.org/wiki/Python_(lenguaje)).`).
2. **Eliminación de falsos positivos conversacionales (`_is_explicit_web_extraction_command`):**
   - Rechaza oraciones condicionales, negativas o interrogativas de diagnóstico (`"Si alguien lee https://..."`, `"Analiza por qué falla https://..."`, `"No leas https://..."`, `"Cuando el cliente lee https://..."`).
   - Exige verbos específicos (`extrae`, `raspa`, `scrapea`) en posición imperativa de comando o verbos generales (`analiza`, `lee`, `resume`, `obten`) seguidos inmediatamente por la URL o un sustantivo web explícito (`el contenido`, `la página`, `la web`, `el sitio`, `el artículo`, `la url`, `el enlace`, `la fuente`, `el documento`).
   - En la Regla 10 (`AppLauncherSkill`), se excluyen cadenas que contengan `http://` o `https://`.
3. **Normalización porcentual RFC 3986 en `_canonical_url_dedup_key`:**
   - Decodifica octetos no reservados (`%7E` -> `~`, `%41` -> `A`), normaliza hexadecimales porcentuales a mayúsculas (`%2f` -> `%2F`) y codifica espacios como `%20`, conservando diferencias reales de mayúsculas/minúsculas en `path`, barra final y `query`.

---

## 6. Matriz de Trazabilidad

| Hallazgo | Severidad original | Archivos modificados | Funciones / Clases clave | Pruebas que lo verifican | Estado |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **F-05** | **HIGH** | `skills/web_extraction_skill.py`, `src/tools/skill_manager.py`, `src/brain/memory.py`, `src/brain/brain_manager.py`, `src/brain/intent_router.py`, `src/brain/ollama_client.py`, `src/brain/gemini_client.py`, `src/brain/claude_client.py`, `src/brain/openai_client.py`, `src/brain/codex_client.py`, `skills/web_parser.py`, `config/tools_schema.py` | `build_llm_envelope`, `execute_with_envelope`, `execute_skill_with_envelope`, `ConversationMemory.add_external_reference`, `BrainManager.process_turn`, `_invoke_single_mode`, `_StructuredHTMLParser._is_hidden_style`, `_parse_style_rules` | `test_f05_prompt_injection_hidden_css_unicode_and_untrusted_envelope`, `test_post_remediation_f05_css_evasions_and_end_to_end_trust_boundary` | **CERRADO** |
| **F-01** | **MEDIUM** | `skills/web_document_converter.py`, `skills/web_parser.py` | `_sanitize_citation_label`, `_sanitize_markdown_url`, `_sanitize_inline_code`, `_sanitize_markdown_text`, `normalize_extracted_url` | `test_f01_citation_spoofing_and_markdown_link_injection_remediation`, `test_post_remediation_f01_citation_url_hijack_idn_and_provider_sanitization` | **CERRADO** |
| **F-04** | **MEDIUM** | `skills/web_parser.py`, `skills/web_document_converter.py` | `WebContentExtractor.extract_from_fetch`, `_StructuredHTMLParser`, `WebDocumentConverter.to_markdown` | `test_f04_shared_char_budget_and_isatap_siit_ipv6_blocking`, `test_post_remediation_f04_global_budget_across_metadata_links_json_and_markdown` | **CERRADO** |
| **F-06** | **LOW** | `src/brain/intent_router.py`, `skills/web_extraction_skill.py` | `_clean_extracted_url_token`, `_is_explicit_web_extraction_command`, `_canonical_url_dedup_key`, `_normalize_pct_encoding` | `test_f06_non_retryable_exceptions_canonical_dedup_and_router_fast_path`, `test_post_remediation_f06_wikipedia_balanced_parens_false_positives_and_pct_dedup` | **CERRADO** |
| **F-02** | MEDIUM (cerrado en `b535594`) | `skills/web_parser.py` | `_StructuredHTMLParser.handle_starttag`, `_has_same_origin` | `test_f02_metadata_isolation_base_href_and_malformed_html` | **PRESERVADO** |
| **F-03** | MEDIUM (cerrado en `b535594`) | `skills/web_fetcher.py`, `skills/web_security.py`, `skills/web_extraction_skill.py` | `_IncrementalBoundedDecompressor.finish`, `WebFetchError`, `WebSecurityError` | `test_f03_truncated_compression_and_redirect_chain_traceability` | **PRESERVADO** |
| **F-07** | INFO (entorno local) | `docs/audits/REMEDIATION_PASO_3B_FINAL.md` | Verificación TLS `CERT_REQUIRED` + `check_hostname=True` intacta | `test_https_pinned_ip_preserves_sni_host_and_strict_tls_verification` | **DOCUMENTADO** |

---

## 7. Verificación Empírica Realmente Ejecutada

1. **Compilación estática de todos los módulos tocados (`py_compile`):**
   - Comando: `.\venv\Scripts\python.exe -m py_compile skills/web_config.py skills/web_models.py skills/web_security.py skills/web_fetcher.py skills/web_parser.py skills/web_search_provider.py skills/web_document_converter.py skills/web_extraction_skill.py src/tools/skill_manager.py src/brain/intent_router.py src/brain/brain_manager.py src/brain/memory.py src/brain/ollama_client.py src/brain/gemini_client.py src/brain/claude_client.py src/brain/openai_client.py src/brain/codex_client.py config/tools_schema.py tests/test_web_extraction_skill.py`
   - Resultado: **OK (código 0, 0 errores)**.

2. **Suite específica y adversarial de `WebExtractionSkill`:**
   - Comando: `.\venv\Scripts\python.exe -m unittest tests.test_web_extraction_skill -v`
   - Resultado: **Ran 26 tests in 6.426s — OK (0 fallos, 0 errores, 0 omitidas)**.
   - Incluye las 4 nuevas pruebas integrales post-remediación (`test_post_remediation_f01_citation_url_hijack_idn_and_provider_sanitization`, `test_post_remediation_f04_global_budget_across_metadata_links_json_and_markdown`, `test_post_remediation_f05_css_evasions_and_end_to_end_trust_boundary`, `test_post_remediation_f06_wikipedia_balanced_parens_false_positives_and_pct_dedup`) además de las 22 pruebas previas.

3. **Suites de regresión de `IntentRouter`, Paso #3.A (`DocumentExportSkill`), N5 y `ConversationMemory`:**
   - Comando: `.\venv\Scripts\python.exe -m unittest tests.test_intent_router_chaos tests.test_document_export_skill tests.test_memory_isolation tests.test_transaction_recovery tests.test_file_editor_and_chain tests.test_memory_persistence tests.test_memory_window tests.test_memory_overflow -v`
   - Resultado: **Ran 236 tests in 69.174s — OK (0 fallos, 0 errores)**.

4. **Suite global completa del repositorio (`unittest discover`):**
   - Comando: `$env:JARVIS_ENV='test'; .\venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"`
   - Resultado: **Ran 533 tests in 87.151s — OK (0 fallos, 0 errores)**.

5. **Comprobación de arranque CLI:**
   - Comando: `.\venv\Scripts\python.exe main.py --help`
   - Resultado: **OK (código 0)**.

6. **Integridad de `data/memory_store.json`:**
   - Verificado únicamente por metadatos antes y después de todas las suites: tamaño **1.218 bytes**, SHA-256 `83640EAADFF5E33FEB9126F6EEBB9F7960F924F01BB3A1968468014223341222` (idéntico al estado inicial).

---

## 8. Riesgos Residuales Honestos

1. **Hojas de estilo CSS externas (`<link rel="stylesheet">`):** `WebExtractionSkill` descarga únicamente el documento HTML principal y no realiza peticiones adicionales hacia archivos `.css` externos ni ejecuta un motor de layout o JavaScript. Por tanto, las reglas de ocultamiento definidas en hojas CSS externas o aplicadas dinámicamente por scripts no son visibles para el parser estático; no obstante, incluso si ese texto se extrae como dato visible, la frontera estructural de confianza (`F-05`) impide que se promocione a `assistant`, entre en `system_prompt` o ejecute herramientas.
2. **Modelos LLM ante entradas en el canal de datos:** Aunque el contenido web viaja ahora en un canal de datos no confiable estrictamente separado del `system_prompt` y bloqueado para la invocación de herramientas o escritura en Codex, cualquier resumen lingüístico generado por un LLM sigue siendo una síntesis sobre fuentes externas y debe interpretarse como tal.
3. **Entorno local Windows con Avast Web/Mail Shield (`F-07`):** En máquinas donde un antivirus intercepta sockets TLS locales con una CA raíz no instalada en el almacén de confianza de Python/OpenSSL, las conexiones HTTPS loopback con certificados autofirmados de prueba son rechazadas por `CERT_REQUIRED`; el código de producción mantiene intacta la validación estricta de certificados y hostname.
