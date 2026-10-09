# CONTEXT.md — Estado técnico de J.A.R.V.I.S.

## Objetivo

Asistente modular de voz para Windows, con transcripción local mediante faster-whisper, Ollama como cerebro principal, Gemini como respaldo, síntesis neuronal con Edge TTS y motor de habilidades locales con normalización léxica avanzada (`normalize_text` en `main.py`).

## Entorno documentado

- Windows 11, Python 3.12 en el entorno virtual local `venv/`.
- Hardware reportado por el usuario: GTX 1660 con 6 GB de VRAM y 24 GB de RAM.
- Whisper usa `base`, `cuda` y `float16` por configuración predeterminada; el constructor contempla fallback a CPU `int8`.
- Captura mono a 16 kHz. Ollama y Gemini se configuran por `config/settings.py` y `.env`.

## Arquitectura y estado actual

- `main.py`: coordina captura con VAD, transcripción con Whisper GPU, enrutamiento autónomo de intenciones y Function Calling mediante `IntentRouter`, ejecución de herramientas locales mediante `SkillManager` (`system_time`, `app_launcher`, `system_info`, `web_search`), conversación con fallback a Gemini y síntesis vocal asíncrona con Edge TTS.
- `src/audio/recorder.py`: captura con VAD por energía RMS, silencio de cierre predeterminado de 1,3 s y límite máximo de 20 s.
- `src/audio/stt.py`: transcribe en español con faster-whisper, filtro VAD del modelo y sin contexto de texto previo.
- `src/audio/tts.py`: genera y reproduce en un trabajador dedicado (`Future`).
- `src/brain/intent_router.py`: clase `IntentRouter` para clasificar intenciones y emitir Function Calling estructurado en JSON.
- `src/brain/ollama_client.py` y `src/brain/gemini_client.py`: adaptadores de Ollama local y Gemini en la nube (`GEMINI_MODEL`).
- `config/tools_schema.py`: esquemas estructurados de herramientas y generador de system prompt.
- `skills/base_skill.py`: contrato abstracto `BaseSkill`.
- `skills/system_skills.py`: `TimeSkill`, `AppLauncherSkill` (con lista blanca segura), `SystemInfoSkill` (monitoreo de CPU y RAM con psutil) y `WebSearchSkill` (búsqueda en Google/YouTube con webbrowser y urllib.parse).
- `src/tools/skill_manager.py`: clase `SkillManager` para registro y ejecución centralizada de las 4 habilidades del sistema.
- `AGENTS.md`: reglas del repositorio consolidadas.
- `prompts_history.txt`: historial local acumulativo, ignorado por Git.

## Cambios de esta iteración (2026-10-09 - Fase 5: Integración de IntentRouter en main.py)

- Archivos modificados: `main.py`, `src/brain/intent_router.py`, `skills/system_skills.py`, `config/tools_schema.py`, `CONTEXT.md` y `prompts_history.txt`.
- Se integró `IntentRouter` en el bucle principal de `main.py`, eliminando la cascada manual de if/else y sustituyéndola por Function Calling autónomo con JSON estructurado.
- Se implementó la lógica de decisión:
  - Si la acción es `call_tool`: se ejecuta dinámicamente `skill_manager.execute_skill(tool_name, **parameters)` y se reproduce la respuesta por voz con TTS.
  - Si la acción es `chat`: se reproduce directamente la respuesta generada por el LLM o se deriva a conversación resiliente con fallback a Gemini en caso de error.
- Se actualizaron los parámetros de las skills en `skills/system_skills.py` y `config/tools_schema.py` para cumplir estrictamente con el estándar JSON Schema (`properties`, `required`), permitiendo validación uniforme en `IntentRouter`.
- Se optimizó el prompt de sistema de `IntentRouter` con ejemplos claros de salida para asegurar respuestas deterministas y precisas desde modelos locales compactos como Ollama `qwen2.5-coder:3b`.
- El prompt completo de esta iteración se agregó a `prompts_history.txt`; ese archivo es local y no debe versionarse.

## Cambios de iteraciones anteriores

- **2026-10-09 (Fase 4 - SystemInfoSkill y WebSearchSkill)**: Implementación de habilidades de hardware y búsqueda web en `system_skills.py`.
- **2026-10-09 (Fase 5 - Esquemas de Herramientas y System Prompt)**: Creación de `config/tools_schema.py` con esquemas de herramientas y system prompt para function calling.
- **2026-10-09 (Documentación Profesional README.md)**: Creación de `README.md` exhaustivo en la raíz del proyecto.

## Verificación y limitaciones

- Se ejecutaron pruebas funcionales completas del pipeline integrado en `venv/Scripts/python.exe`:
  - Entrada de hora: *"¿Qué hora es?"* $\rightarrow$ `IntentRouter` decide `call_tool` para `system_time` $\rightarrow$ ejecuta y responde con la hora actual.
  - Entrada de hardware: *"¿Cuánta memoria RAM tenemos disponible?"* $\rightarrow$ `IntentRouter` decide `call_tool` para `system_info` $\rightarrow$ ejecuta y reporta métricas de CPU/RAM.
  - Entrada de búsqueda web: *"Busca en YouTube música de Daft Punk"* $\rightarrow$ `IntentRouter` decide `call_tool` para `web_search` con parámetros `query` y `platform: youtube` $\rightarrow$ ejecuta y abre navegador.
  - Entrada conversacional: *"Hola, ¿cómo estás hoy?"* $\rightarrow$ `IntentRouter` clasifica como `chat` $\rightarrow$ emite respuesta natural sin activar herramientas.
- Micrófono físico, capturas reales de audio en vivo por hardware y reproducción por parlantes requieren validación manual por parte del usuario.

## Pendientes

- Probar la sesión completa por micrófono físico y altavoces en Windows ejecutando `python main.py`.
