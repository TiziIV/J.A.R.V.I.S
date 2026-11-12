# AUDITORÍA INDEPENDIENTE FINAL DE SEGURIDAD Y ARQUITECTURA
## PASO #3.B — WEB EXTRACTION SKILL
### VERIFICACIÓN DEL HARDENING INTEGRAL (COMMIT `b6835ee`)

- **Proyecto:** J.A.R.V.I.S. (`jarvis-core`)
- **Commit auditado:** `b6835ee` (`b6835ee5a9cbff7d96a75f80ba986c738e4a9386`, `fix(security): complete Paso 3.B end-to-end trust boundary and post-remediation hardening (F-01, F-04, F-05, F-06)`)
- **Commit anterior de referencia:** `b535594` (`fix(security): remediate Paso 3.B audit findings F-01 to F-06 in WebExtractionSkill`)
- **Línea base inicial del Paso #3.B:** `0a468f4`
- **Rama:** `main` (sincronizada con `origin/main`)
- **Fecha de auditoría:** 2026-11-12
- **Modo de ejecución:** Auditoría independiente, adversarial y estrictamente de solo lectura (sin modificar código de producción ni pruebas).
- **Archivo de informe generado:** `docs/audits/INDEPENDENT_SECURITY_AUDIT_PASO_3B_FINAL.md`

---

## A. Resumen Ejecutivo

Esta auditoría técnica e independiente evaluó de forma adversarial el hardening integral definitivo del Paso #3.B (`WebExtractionSkill`) y sus subsistemas integrados (`SkillManager`, `BrainManager`, `ConversationMemory`, `IntentRouter` y los cinco adaptadores de cerebro: `OllamaBrain`, `GeminiBrain`, `ClaudeBrain`, `OpenAIBrain` y `CodexBrain`), publicado en el commit `b6835ee`.

El objetivo central fue determinar si los cuatro hallazgos que motivaron el rechazo en la auditoría post-remediación previa (`F-05` HIGH, `F-01` MEDIUM, `F-04` MEDIUM y `F-06` LOW/regresión N-01) fueron resueltos en su causa raíz arquitectónica o si persistían vectores de explotación, evasión de límites o contaminación de confianza.

### Conclusión Principal
La inspección estática exhaustiva, la reconstrucción del flujo de ejecución extremo a extremo y la ejecución de pruebas adversariales independientes demostraron que:
1. **La frontera estructural de confianza (F-05) está plenamente integrada en el flujo real:** La salida de `web_extraction` se transporta mediante sobres tipados con `trust_level="untrusted_external_content"`, se almacena en un buffer aislado de `ConversationMemory` (`_external_references`), **nunca adquiere rol `"assistant"` ni entra en `_messages`**, y queda **estrictamente excluida de `get_context_prompt()` y de `inject_context(system_prompt)`**.
2. **Los cinco adaptadores de cerebro segregan instrucciones y datos externos:** En `OllamaBrain`, `GeminiBrain`, `ClaudeBrain` y `OpenAIBrain`, la evidencia externa viaja en un canal de datos acotado y separado de las instrucciones maestras del sistema. En `CodexBrain`, `classify_codex_task()` opera exclusivamente sobre el `prompt` legítimo del usuario, impidiendo que contenidos web escalen permisos hacia modos de escritura o modificación de código.
3. **El secuestro de URLs de citas (F-01) quedó neutralizado:** `_sanitize_citation_label()` neutraliza cualquier esquema de URL embebido dentro de `<title>` antes de construir `[^src-N]:`, garantizando que `RE_URL` en `DocumentParser` resuelva únicamente la URL oficial y validada de la fuente.
4. **El presupuesto global de recursos (F-04) controla todas las estructuras:** `max_normalized_chars` acota conjuntamente metadatos (`description`, `title`), cuerpo y colección de enlaces (`links`), conteniendo la amplificación de salida en JSON y Markdown dentro de los límites esperados y activando el estado `PARTIAL` con advertencia `NORMALIZED_TEXT_TRUNCATED`.
5. **La regresión en `IntentRouter` (F-06 / N-01) fue eliminada:** El recorte de tokens preserva paréntesis balanceados en URLs legítimas (como artículos de Wikipedia) y la detección de intenciones no se dispara ante oraciones conversacionales o condicionales con verbos genéricos.
6. **Integridad total de componentes preexistentes:** Paso #3.A (`DocumentExportSkill`), N5 (`FileEditorSkill`, recuperación transaccional y memoria aislada) y `data/memory_store.json` se mantienen íntegros y sin alteraciones.

**Dictamen General:** **APROBADO**

---

## B. Comparación de Implementaciones (`b535594` vs `b6835ee`)

| Componente / Área | Estado en `b535594` | Estado en `b6835ee` | Evaluación de la Causa Raíz |
| :--- | :--- | :--- | :--- |
| **F-05: Flujo de confianza en `BrainManager`** | Salida devuelta como texto plano; persistida inmediatamente como `assistant` en `self.memory.add_message("assistant", reply_text)`. | `execute_skill_with_envelope` emite sobre; `BrainManager` detecta `is_untrusted_skill` y desvía la salida a `memory.add_external_reference(...)`. | **Resuelto en la causa raíz:** no hay promoción a `assistant`. |
| **F-05: Inyección en `system_prompt`** | `memory.inject_context(base_prompt)` introducía el volcado web previo bajo la etiqueta `Asistente:` dentro de `system_prompt`. | `_external_references` está estrictamente excluido de `inject_context()` y `get_context_prompt()`. `system_prompt` solo recibe una directiva estática de política de seguridad. | **Resuelto en la causa raíz:** canal del sistema limpio de contenido externo. |
| **F-05: Adaptadores de Cerebro** | No recibían parámetro de contexto no confiable; el contenido web debía viajar dentro del prompt único o del system prompt. | Los 5 adaptadores (`ollama_client`, `gemini_client`, `claude_client`, `openai_client`, `codex_client`) reciben `untrusted_context` separado. | **Resuelto:** separación de canales mantenida hasta los payloads de red. |
| **F-05: Autorización en `CodexBrain`** | `classify_codex_task` evaluaba el texto completo recibido, susceptible de escalada por inyecciones en la página web. | `classify_codex_task(prompt)` se ejecuta estrictamente sobre el `prompt` original del usuario; el contenido web no puede forzar `task_type="coding"` ni `allow_write=True`. | **Resuelto:** principio de menor privilegio garantizado. |
| **F-05: Evasiones CSS en `web_parser.py`** | Filtro `_HIDDEN_STYLE_RE` omitía `opacity: .0`, `color: transparent`, `rgba(0,0,0,0)`, `scale(0)`, `left:-9999px`, comentarios CSS y clases `.sr-only`. | Se implementó limpieza de comentarios/escapes CSS, soporte para todas las variantes de opacidad/color/transform/posicionamiento, `inert`, y mapeo de reglas de clases en bloques `<style>`. | **Resuelto:** las 14 técnicas adversariales probadas fueron bloqueadas. |
| **F-01: Citation URL Hijack** | `<title>` con URL antecedía a la URL real en `[^src-N]:`, permitiendo que `DocumentParser` extrajera la URL atacante como `Citation.url`. | `_sanitize_citation_label()` neutraliza cualquier esquema URL dentro del título (`[url-neutralizada]`), garantizando que la URL oficial sea la única capturada. | **Resuelto:** invariante de `Citation.url` verificada. |
| **F-01: Sanitización de `search_provider`** | Se interpolaba directamente entre backticks, permitiendo romper el span inyectando blockquotes o tablas. | `_sanitize_inline_code()` sanea backticks, corchetes, saltos de línea y pipes en `search_provider` y `sr.provider`. | **Resuelto:** imposibilidad de inyectar Markdown estructurado. |
| **F-04: Presupuesto Global de Recursos** | `max_normalized_chars` solo limitaba el texto del cuerpo; `description` y `links` crecían sin tope conjunto (PoC previo: 467 KB JSON / 125 KB MD). | Presupuesto compartido: `description` se descuenta de `used_chars`; `links` se acota por remanente presupuestario; `to_markdown()` aplica cotas por fuente y globales. | **Resuelto:** PoC adversarial redujo la salida a 2.0 KB JSON y 1.1 KB MD. |
| **F-06: URLs con Paréntesis en `IntentRouter`** | `u.rstrip(".,;:!?)]\"'")` eliminaba el `)` final en URLs de Wikipedia (regresión N-01). | `_clean_extracted_url_token()` analiza el balance de delimitadores y conserva `)` cuando tiene su correspondiente `(` de apertura. | **Resuelto:** URLs de Wikipedia y anidadas se extraen intactas. |
| **F-06: Falsos Positivos de Enrutamiento** | Regex con verbos genéricos (`lee`, `analiza`) disparaba `web_extraction` ante oraciones condicionales o de diagnóstico. | `_is_explicit_web_extraction_command()` exige orden imperativa y excluye condicionales (`si...`, `cuando...`) y preguntas sobre fallos. | **Resuelto:** 7 casos conversacionales negativos verificados sin falsos disparos. |

---

## C. Diagrama de Arquitectura de Confianza Observada

El siguiente diagrama representa el flujo real y verificado en el código del commit `b6835ee`:

```mermaid
flowchart TD
    subgraph EntradaUsuario["Canal de Entrada del Usuario (Confiable)"]
        UserPrompt["Usuario: raw_input"]
    end

    subgraph Enrutamiento["IntentRouter (Fast-Path)"]
        CleanURL["_clean_extracted_url_token()"]
        CheckCmd["_is_explicit_web_extraction_command()"]
        StripUntrusted["_strip_untrusted_web_blocks()"]
        Decision{"¿Comando explícito web_extraction?"}
    end

    subgraph EjecucionSkill["WebExtractionSkill & SkillManager"]
        Fetcher["WebFetcher (SSRF, DNS Rebind, Descompresión segura)"]
        Parser["WebParser (Presupuesto global F-04, Filtro CSS F-05)"]
        EnvelopeBuilder["build_llm_envelope()"]
        ExecWithEnv["execute_skill_with_envelope()"]
    end

    subgraph FronteraMemoria["ConversationMemory (Aislamiento Estructural F-05)"]
        AddMsg["add_message(role='user', ...)"]
        AddExtRef["add_external_reference(trust_level='untrusted_external_content')"]
        MessagesQueue[("self._messages (Deque turnos confiables)")]
        ExtRefsQueue[("self._external_references (Deque datos no confiables)")]
        InjectCtx["inject_context() / get_context_prompt()"]
        FormatUntrusted["format_untrusted_context_for_llm()"]
    end

    subgraph GestorCerebros["BrainManager"]
        PlanReq["plan_request(raw_input) - AutoDeliberator"]
        SysPromptConst["system_prompt + Directiva estática de política"]
        UntrustedPayload["untrusted_context (JSON serializado)"]
    end

    subgraph AdaptadoresLLM["Adaptadores Multi-Brain (Canales Separados)"]
        OllamaAdapter["OllamaBrain: system msg vs user data block"]
        GeminiAdapter["GeminiBrain: system_instruction vs contents data"]
        ClaudeAdapter["ClaudeBrain: system string vs messages text block"]
        OpenAIAdapter["OpenAIBrain: system role vs user data role"]
        CodexAdapter["CodexBrain: classify_codex_task(prompt) EXCLUSIVO"]
    end

    UserPrompt --> CleanURL --> CheckCmd --> Decision
    Decision -- "Sí" --> ExecWithEnv
    Decision -- "No" --> PlanReq

    ExecWithEnv --> Fetcher --> Parser --> EnvelopeBuilder
    EnvelopeBuilder --> AddExtRef --> ExtRefsQueue
    UserPrompt --> AddMsg --> MessagesQueue

    MessagesQueue --> InjectCtx --> SysPromptConst
    ExtRefsQueue --> FormatUntrusted --> UntrustedPayload

    SysPromptConst --> OllamaAdapter
    SysPromptConst --> GeminiAdapter
    SysPromptConst --> ClaudeAdapter
    SysPromptConst --> OpenAIAdapter
    SysPromptConst --> CodexAdapter

    UntrustedPayload -. "Canal de Datos No Confiable" .-> OllamaAdapter
    UntrustedPayload -. "Canal de Datos No Confiable" .-> GeminiAdapter
    UntrustedPayload -. "Canal de Datos No Confiable" .-> ClaudeAdapter
    UntrustedPayload -. "Canal de Datos No Confiable" .-> OpenAIAdapter
    UntrustedPayload -. "Solo lectura (Sin escalada)" .-> CodexAdapter
```

---

## D. Análisis Detallado de Hallazgos

### F-05: Frontera Estructural de Confianza y Evasiones CSS/HTML (P0 — HIGH)
- **Estado:** **CERRADO Y VERIFICADO**
- **Archivos auditados:** `skills/web_extraction_skill.py`, `src/tools/skill_manager.py`, `src/brain/memory.py`, `src/brain/brain_manager.py`, `src/brain/intent_router.py`, `skills/web_parser.py`, `config/tools_schema.py`, adaptadores (`ollama_client.py`, `gemini_client.py`, `claude_client.py`, `openai_client.py`, `codex_client.py`).
- **Causa raíz previa:** `WebExtractionSkill.build_llm_envelope()` no tenía llamadores reales. `BrainManager.process_turn()` guardaba la salida como `"assistant"` en memoria conversacional e `inject_context()` la colocaba en `system_prompt` como instrucción privilegiada. El parser CSS no cubría opacidad decimal, colores transparentes, transformaciones ni clases auxiliares.
- **Verificación empírica realizada:**
  1. **Aislamiento en Memoria:** Se verificó que `add_external_reference()` almacena el payload en `self._external_references` con `role="untrusted_reference"` y `trust_level="untrusted_external_content"`. Se comprobó que `add_message()` rechaza cualquier intento de registrar un volcado web bajo `assistant`, desviándolo automáticamente a `_external_references`.
  2. **Inyección en Prompt del Sistema:** Se comprobó que `ConversationMemory.get_context_prompt()` e `inject_context(system_prompt)` solo iteran sobre `self._messages` y jamás leen `self._external_references`. `system_prompt` permanece 100 % libre de contenido web.
  3. **15 Payloads Adversariales:** Se ejecutaron 15 vectores de ataque (incluyendo comandos de borrado, suplantación de usuario/sistema, llamadas a herramientas en JSON, lectura de `.env`, escapes de delimitadores y contaminación en turnos subsiguientes). En los 15 casos, la evidencia externa quedó confinada como dato pasivo en el bloque `[EVIDENCIA_EXTERNA_NO_CONFIABLE_SOLO_LECTURA]`, sin activación de herramientas ni contaminación en turnos 2 o 3.
  4. **Autorización en Codex:** Se verificó en `CodexBrain.generate_response()` que `classify_codex_task(prompt)` se ejecuta estrictamente sobre el `prompt` del usuario. Un contenido web malicioso con directivas de modificación de archivos no logró activar `task_type="coding"` ni `allow_write=True`.
  5. **14 Evasiones CSS Bloqueadas:** Se probaron y validaron los 14 vectores de ocultamiento CSS (`opacity: .0`, `color: transparent`, `rgba(0,0,0,0)`, `hsla(...,0)`, `#00000000`, `transform: scale(0)`, `position:absolute;left:-9999px`, comentarios inline `/*x*/display:none`, clases `.sr-only`, `inert`, `content-visibility: hidden`, `clip-path: polygon(...)`, selectores `.clase` e `#id` en `<style>`). En todos los casos el texto oculto fue descartado de la extracción.

---

### F-01: Citation URL Hijack y Sanitización Markdown (P1 — MEDIUM)
- **Estado:** **CERRADO Y VERIFICADO**
- **Archivos auditados:** `skills/web_document_converter.py`, `skills/web_parser.py`, `skills/web_models.py`.
- **Causa raíz previa:** Si un documento web incluía una URL atacante dentro de su etiqueta `<title>`, `WebDocumentConverter.to_markdown()` emitía `[^src-1]: {title} — {original_url}`, permitiendo que `DocumentParser` asignara la URL atacante al objeto `Citation.url`. Además, `search_provider` y `sr.provider` no se saneaban contra saltos de línea ni metacaracteres.
- **Verificación empírica realizada:**
  1. **Neutralización de Esquemas en Citas:** `_sanitize_citation_label()` aplica la expresión regular `_EMBEDDED_URL_SCHEME_RE` y sustituye cualquier esquema (`http://`, `https://`, `ftp://`, `file://`, `javascript:`, `data:`, `vbscript:`) por `[url-neutralizada]`.
  2. **Prueba con DocumentParser:** Se procesó un documento con `title="See https://evil.example/phish now"`. La salida emitida fue `[^src-1]: See [url-neutralizada] now — https://victim.example.com/real-article`. Al procesarlo con `DocumentParser().parse()`, `Citation.url` capturó de forma unívoca y correcta `https://victim.example.com/real-article`.
  3. **Sanitización de Proveedores:** Se probó `search_provider="evil\n> injected quote"` y proveedores con backticks o tablas. `_sanitize_inline_code()` neutralizó los saltos de línea y backticks, impidiendo la inyección de blockquotes o tablas falsas.
  4. **Validación de URLs:** Se confirmaron los bloqueos a URLs con credenciales (`https://user:pass@...` -> `""`), esquemas peligrosos (`javascript:`, `file:`, `data:` -> `""`), longitudes > 1024 caracteres, y se verificó la conversión correcta de dominios IDN a Punycode ASCII (`https://раypаl.com` -> `xn--pypal-4ve5c.com`).

---

### F-04: Presupuesto Global Compartido de Recursos (P1 — MEDIUM)
- **Estado:** **CERRADO Y VERIFICADO**
- **Archivos auditados:** `skills/web_parser.py`, `skills/web_document_converter.py`, `skills/web_config.py`.
- **Causa raíz previa:** `max_normalized_chars` se evaluaba únicamente contra los párrafos y encabezados del cuerpo, dejando sin límite acumulado a `metadata.description` y `links`.
- **Verificación empírica realizada:**
  1. **Presupuesto Unificado:** En `WebContentExtractor.extract_from_fetch()`, `max_budget` descuenta la longitud de `metadata.description` de `used_chars`. El remanente presupuestario gobierna los párrafos y la acumulación de enlaces (`remaining_link_budget = min(4000, max(0, max_budget - used_chars))`).
  2. **Prueba Adversarial Extrema:** Se sometió a prueba un documento HTML con descripción de 50.000 caracteres, título de 10.000 caracteres, párrafos de 20.000 caracteres y 150 enlaces largos de 2.200 caracteres cada uno, configurando `max_normalized_chars=500`.
  3. **Resultados Obtenidos:**
     - Caracteres contabilizados: `desc_len=100`, `norm_len=300`, `links_cost=0` (total 400 <= 500).
     - Estado asignado: `ExtractionStatus.PARTIAL` con advertencia `NORMALIZED_TEXT_TRUNCATED`.
     - Tamaño JSON serializado (`to_dict`): **2.084 bytes** (frente a los 467.444 bytes del reporte anterior).
     - Tamaño Markdown (`to_markdown`): **1.184 bytes** (frente a los 125.856 bytes del reporte anterior).
     - Exportación DOCX: Completada limpiamente sin desbordamiento de recursos.

---

### F-06: IntentRouter, URLs con Paréntesis y Deduplicación RFC 3986 (P1 — LOW / Regresión N-01)
- **Estado:** **CERRADO Y VERIFICADO**
- **Archivos auditados:** `src/brain/intent_router.py`, `skills/web_extraction_skill.py`.
- **Causa raíz previa:** `rstrip(".,;:!?)]\"'")` eliminaba el paréntesis de cierre en URLs válidas (ej. Wikipedia `Python_(lenguaje)`). Regex genérica capturaba oraciones conversacionales con verbos como `"lee"` o `"analiza"`. Deduplicación no normalizaba mayúsculas/minúsculas en codificación porcentual.
- **Verificación empírica realizada:**
  1. **Preservación de Paréntesis Balanceados:** `_clean_extracted_url_token()` verifica el balance de `(`, `[` y `{`. En `lee https://es.wikipedia.org/wiki/Python_(lenguaje)`, la URL extraída mantuvo íntegro `(lenguaje)`. En `extrae (https://es.wikipedia.org/wiki/Python_(lenguaje)).`, eliminó los paréntesis externos de la oración y el punto final, reteniendo la URL completa.
  2. **Falsos Positivos Conversacionales Eliminados:** Se verificó que oraciones condicionales o de diagnóstico (`"si alguien lee esto en https://example.com avisame"`, `"analiza por qué falla https://example.com/api"`, `"no leas https://evil.com"`, `"abre https://example.com en el bloc de notas"`) devuelven `None` y no activan `web_extraction` ni `app_launcher`.
  3. **Comandos Positivos Válidos:** Se verificó que órdenes legítimas con verbos imperativos (`extrae`, `raspa`, `scrapea`, `analiza la página`, `resume el artículo`, `obten la web`) se enrutan inmediatamente hacia `web_extraction`.
  4. **Deduplicación Canónica RFC 3986:** `_canonical_url_dedup_key()` decodifica octetos no reservados (`%7E` -> `~`), normaliza caracteres porcentuales a mayúsculas (`%2f` -> `%2F`), codifica espacios como `%20`, descarta fragmentos `#` y colapsa puertos por defecto `:80`/`:443`.

---

### F-02: Aislamiento de `<head>`, `<meta>` y `<base href>` (Preservado)
- **Estado:** **CERRADO Y VERIFICADO**
- **Archivos auditados:** `skills/web_parser.py`.
- **Verificación:** Se comprobó que `<base href>` cruzado a otro origen genera advertencia `CROSS_ORIGIN_BASE_IGNORED` y no afecta la resolución de URLs relativas. Se verificó que etiquetas `<noscript>` sin cerrar o elementos SVG en `<head>` no filtran metadatos hacia el documento.

---

### F-03: Descompresión HTTP Segura y Trazabilidad (Preservado)
- **Estado:** **CERRADO Y VERIFICADO**
- **Archivos auditados:** `skills/web_fetcher.py`.
- **Verificación:** `_IncrementalBoundedDecompressor.finish()` valida estrictamente el cierre del flujo. Gzip truncado, zlib truncado y gzip vacío lanzan `UnsupportedEncodingError` con mensaje explicativo de `EOF`. Streams Gzip multi-miembro concatenados se descomprimen de forma íntegra.

---

### F-07: Verificación Estricta de TLS y Entorno Local (INFO)
- **Estado:** **DOCUMENTADO (INFO)**
- **Verificación:** La configuración en `skills/web_fetcher.py` mantiene `ssl.CERT_REQUIRED` y `check_hostname=True`. La limitación observada en entornos locales Windows donde antivirus interceptan sockets TLS loopback sin su CA instalada en el trust store de Python es una condición del entorno y no un defecto del producto.

---

## E. Matriz de Cobertura y Trazabilidad

| Hallazgo | Severidad | Implementación Auditada | Pruebas Ejecutadas | Evidencia Observada | Resultado |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **F-05** | **HIGH** | `web_extraction_skill.py`, `skill_manager.py`, `memory.py`, `brain_manager.py`, adaptadores LLM, `web_parser.py` | 15 vectores de inyección adversarial, 14 evasiones CSS, flujo Multi-Brain | `_messages` limpio de datos web; `system_prompt` sin fugas; `Codex` protegido de escalada; CSS oculto filtrado | **RESUELTO Y VERIFICADO** |
| **F-01** | **MEDIUM** | `web_document_converter.py`, `web_parser.py` | URL hijack en `<title>`, inyección en `search_provider`, credenciales, IDN | `_sanitize_citation_label` neutraliza URLs en `<title>`; `DocumentParser` extrae `original_url`; proveedores limpios | **RESUELTO Y VERIFICADO** |
| **F-04** | **MEDIUM** | `skills/web_parser.py`, `skills/web_document_converter.py` | HTML inflado (descripción 50 KB, 150 enlaces 300 KB, párrafos 20 KB) con presupuesto 500 | Salida JSON: 2.084 B; Markdown: 1.184 B; estado `PARTIAL`; advertencia `NORMALIZED_TEXT_TRUNCATED` | **RESUELTO Y VERIFICADO** |
| **F-06** | **LOW** | `src/brain/intent_router.py`, `skills/web_extraction_skill.py` | URLs Wikipedia con `()`, 7 frases negativas, 7 comandos positivos, RFC 3986 | Paréntesis balanceados conservados; cero falsos positivos conversacionales; deduplicación `%2F`/`~` | **RESUELTO Y VERIFICADO** |
| **F-02** | MEDIUM | `skills/web_parser.py` | `<base>` cross-origin y same-origin, `<noscript>` sin cerrar en `<head>` | `<base>` externo bloqueado con advertencia; `<base>` interno aplicado; `<noscript>` contenido | **RESUELTO Y VERIFICADO** |
| **F-03** | MEDIUM | `skills/web_fetcher.py` | Gzip truncado, Gzip vacío, Zlib truncado, Gzip concatenado | `EOF` validado; errores `UNSUPPORTED_ENCODING` ante truncamiento; streams concatenados funcionales | **RESUELTO Y VERIFICADO** |
| **F-07** | INFO | `skills/web_fetcher.py` | Inspección de contexto SSL | `CERT_REQUIRED` y `check_hostname=True` preservados sin debilitamiento | **DOCUMENTADO (INFO)** |

---

## F. Resultados de Pruebas Ejecutadas

Todas las suites fueron ejecutadas directamente en el entorno de trabajo:

1. **Compilación Estática (`py_compile`):**
   - Comando: `.\venv\Scripts\python.exe -m py_compile skills/web_config.py skills/web_models.py skills/web_security.py skills/web_fetcher.py skills/web_parser.py skills/web_search_provider.py skills/web_document_converter.py skills/web_extraction_skill.py src/tools/skill_manager.py src/brain/intent_router.py src/brain/brain_manager.py src/brain/memory.py src/brain/ollama_client.py src/brain/gemini_client.py src/brain/claude_client.py src/brain/openai_client.py src/brain/codex_client.py config/tools_schema.py tests/test_web_extraction_skill.py`
   - **Resultado:** **OK (código de salida 0, 0 errores sintácticos)**.

2. **Suite Específica de WebExtractionSkill (`test_web_extraction_skill.py`):**
   - Comando: `.\venv\Scripts\python.exe -m unittest tests.test_web_extraction_skill -v`
   - **Resultado:** **Ran 26 tests in 6.477s — OK (0 fallos, 0 errores, 0 omitidas)**.

3. **Suites de Regresión de Componentes Críticos (Paso #3.A, N5, Memoria, Router):**
   - Comando: `$env:JARVIS_ENV='test'; .\venv\Scripts\python.exe -m unittest tests.test_intent_router_chaos tests.test_document_export_skill tests.test_memory_isolation tests.test_transaction_recovery tests.test_file_editor_and_chain tests.test_memory_persistence tests.test_memory_window tests.test_memory_overflow -v`
   - **Resultado:** **Ran 236 tests in 72.194s — OK (0 fallos, 0 errores)**.

4. **Suite Global Completa del Repositorio (`unittest discover`):**
   - Comando: `$env:JARVIS_ENV='test'; .\venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"`
   - **Resultado:** **Ran 533 tests in 93.008s — OK (0 fallos, 0 errores)**.

5. **Verificación de Entrada CLI:**
   - Comando: `.\venv\Scripts\python.exe main.py --help`
   - **Resultado:** **OK (código de salida 0, uso mostrado correctamente)**.

---

## G. Riesgos Residuales Honestos

1. **Hojas de Estilo Externas (`<link rel="stylesheet">`):** `WebExtractionSkill` es un motor de extracción estático de contenido principal; por diseño arquitectónico y de seguridad (prevención de SSRF secundario y agotamiento de recursos), no descarga archivos `.css` externos ni ejecuta JavaScript. Por tanto, clases de ocultamiento configuradas exclusivamente en archivos CSS remotos no son detectadas por el parser estático. No obstante, este riesgo está completamente mitigado a nivel sistémico por la frontera de confianza (F-05): aunque dicho texto sea extraído como visible, viaja en el canal no confiable y jamás adquiere autoridad de ejecución.
2. **Dependencia de Marcadores Textuales en Interfaces Simplificadas:** En adaptadores de lenguaje que solo aceptan un único string de entrada para usuario y datos, se utilizan los marcadores `[EVIDENCIA_EXTERNA_NO_CONFIABLE_SOLO_LECTURA]`. Aunque se comprobó que `IntentRouter._strip_untrusted_web_blocks()` limpia estos bloques y que `json.dumps()` escapa las cadenas, cualquier sistema que ensamble texto en un solo canal debe mantener la directiva estática en el system prompt.
3. **Modelos Locales de Menor Capacidad:** Modelos pequeños (ej. 7B o cuantizaciones agresivas) pueden tener menor adherencia a la directiva de ignorar órdenes contenidas en datos de evidencia que modelos de frontera (GPT-4o, Claude 3.5 Sonnet, Gemini 2.0). La defensa en profundidad implementada en el código asegura que, aun si el modelo emitiera una cadena con apariencia de herramienta, `BrainManager` en su turno conversacional no la ejecuta como llamada a skill.

---

## H. Estado de Integridad del Repositorio

- **Commit auditado:** `b6835ee` en rama `main`.
- **Sincronización remota:** Idéntico a `origin/main` (`## main...origin/main`).
- **Archivo sensible `data/memory_store.json`:**
  - Longitud antes de la auditoría: `1218` bytes.
  - Longitud tras la auditoría: `1218` bytes.
  - SHA-256 verificado antes y después: `83640EAADFF5E33FEB9126F6EEBB9F7960F924F01BB3A1968468014223341222`.
  - **Confirmación:** No fue leído, copiado, alterado ni migrado.
- **Modificación de archivos de producción y pruebas:**
  - Código de producción (`src/`, `skills/`, `config/`): **0 modificaciones realizadas por la auditoría**.
  - Pruebas existentes (`tests/`): **0 modificaciones realizadas por la auditoría**.
  - Árbol de trabajo Git: Conserva intactos y sin tocar los archivos preexistentes no confirmados de la fase N5 en curso.
- **Archivos creados durante esta auditoría:**
  - `docs/audits/INDEPENDENT_SECURITY_AUDIT_PASO_3B_FINAL.md` (este informe técnico).
  - Bitácora local obligatoria: `prompts_history.txt` (excluido de Git por `.gitignore`).

---

## I. Dictamen Final

```
================================================================================
DICTAMEN DE AUDITORÍA: APROBADO
================================================================================
```

**Fundamentación Técnica:**
1. Todos los hallazgos (`F-01` a `F-06`) están resueltos técnica y estructuralmente en el código del commit `b6835ee`.
2. El hallazgo crítico `F-05` cuenta con una frontera de confianza verificada de extremo a extremo: memoria aislada, prompts del sistema limpios, canales de datos segregados en los cinco adaptadores y autorización estricta en Codex.
3. No existen vulnerabilidades abiertas de severidad CRITICAL ni HIGH.
4. Las pruebas adversariales independientes demostraron la resistencia del sistema ante los 15 vectores de ataque ensayados.
5. Se comprobó la total compatibilidad y ausencia de regresiones en las 533 pruebas del repositorio, preservando intactos el Paso #3.A y N5.

El Paso #3.B (`WebExtractionSkill`) queda técnicamente cerrado y seguro para continuar con el roadmap oficial de J.A.R.V.I.S.
