# CONTEXT.md — Estado técnico de J.A.R.V.I.S.

## Objetivo

Asistente modular de voz para Windows, con transcripción local mediante faster-whisper, Ollama como cerebro principal, Gemini como respaldo, síntesis neuronal con Edge TTS y motor de habilidades locales con normalización léxica avanzada (`normalize_text` en `main.py`).

## Entorno documentado

- Windows 11, Python 3.12 en el entorno virtual local `venv/`.
- Hardware reportado por el usuario: GTX 1660 con 6 GB de VRAM y 24 GB de RAM.
- Whisper usa `base`, `cuda` y `float16` por configuración predeterminada; el constructor contempla fallback a CPU `int8`.
- Captura mono a 16 kHz. Ollama y Gemini se configuran por `config/settings.py` y `.env`.

## Arquitectura y estado actual

- `main.py`: coordina captura con VAD, transcripción con Whisper GPU, normalización léxica de intenciones (`normalize_text`), ejecución directa de herramientas (`SkillManager` para `system_time` y `app_launcher`), inferencia LLM (Ollama con fallback a Gemini) y síntesis vocal asíncrona con Edge TTS.
- `src/audio/recorder.py`: captura con VAD por energía RMS, silencio de cierre predeterminado de 1,3 s y límite máximo de 20 s.
- `src/audio/stt.py`: transcribe en español con faster-whisper, filtro VAD del modelo y sin contexto de texto previo.
- `src/audio/tts.py`: genera y reproduce en un trabajador dedicado (`Future`).
- `src/brain/ollama_client.py` y `src/brain/gemini_client.py`: adaptadores de Ollama local y Gemini en la nube (`GEMINI_MODEL`).
- `skills/base_skill.py`: contrato abstracto `BaseSkill`.
- `skills/system_skills.py`: `TimeSkill`, `AppLauncherSkill` (con lista blanca segura), `SystemInfoSkill` (monitoreo de CPU y RAM con psutil) y `WebSearchSkill` (búsqueda en Google/YouTube con webbrowser y urllib.parse).
- `src/tools/skill_manager.py`: clase `SkillManager` para registro y ejecución centralizada de las 4 habilidades del sistema.
- `AGENTS.md`: reglas del repositorio consolidadas.
- `prompts_history.txt`: historial local acumulativo, ignorado por Git.

## Cambios de esta iteración (2026-10-09 - Fase 4: SystemInfoSkill y WebSearchSkill)

- Archivos modificados: `skills/system_skills.py`, `src/tools/skill_manager.py`, `main.py`, `CONTEXT.md` y `prompts_history.txt`.
- Se implementó `SystemInfoSkill` en `skills/system_skills.py`, utilizando `psutil.cpu_percent(interval=0.5)` y `psutil.virtual_memory()` para calcular el uso de CPU (%) y RAM disponible (GB) con respuesta natural para voz.
- Se implementó `WebSearchSkill` en `skills/system_skills.py`, utilizando `webbrowser` y `urllib.parse.quote_plus` para abrir búsquedas en Google o YouTube en el navegador predeterminado.
- Se actualizaron las importaciones y el registro de `_register_default_skills()` en `src/tools/skill_manager.py` para instanciar las dos nuevas habilidades.
- Se integró la detección de intenciones en `main.py` para recursos del sistema (`recursos`, `cpu`, `ram`, `memoria`, `estado del sistema` con coincidencia de palabras exactas para prevenir falsos positivos) y búsquedas web (`busca`, `buscar`, `buscame`, `googlear`, `en youtube`, `en google` con extracción limpia de términos y selección de plataforma).
- Se refinó la normalización léxica en `normalize_text()` para limpiar signos de apertura y puntuación en español (`¿`, `¡`).
- El prompt completo de esta iteración se agregó a `prompts_history.txt`; ese archivo es local y no debe versionarse.

## Cambios de iteraciones anteriores

- **2026-10-09 (Fase 5 - Esquemas de Herramientas y System Prompt)**: Creación de `config/tools_schema.py` con esquemas de herramientas y system prompt para function calling.
- **2026-10-09 (Normalización de Intenciones)**: Implementación inicial de `normalize_text()` en `main.py` y detección flexible de intenciones.

## Verificación y limitaciones

- Se ejecutaron pruebas funcionales unitarias en `venv/Scripts/python.exe`:
  - `SkillManager` inicializa correctamente las 4 habilidades (`system_time`, `app_launcher`, `system_info`, `web_search`) y genera su manifiesto estructurado.
  - `SystemInfoSkill` mide CPU y RAM en tiempo real y devuelve el mensaje formateado para TTS (ej: "El procesador está al X% y dispone de Y gigabytes de RAM libres, señor.").
  - `WebSearchSkill` maneja búsquedas con y sin término, seleccionando la plataforma correspondiente.
  - Validación de enrutamiento con múltiples frases de prueba (ej: "¿Cuánta memoria RAM y CPU tenemos disponibles?", "Busca en YouTube música para programar", "Busca en Google noticias de hoy") confirmando que la extracción léxica y derivación a skills funcionan sin falsos positivos.
- Micrófono físico, capturas reales de voz, GPU/CUDA en vivo y reproducción de audio por parlantes requieren validación manual en el entorno del usuario.

## Pendientes

- Probar la interacción por micrófono físico con las nuevas órdenes de voz en Windows.
- Integrar el enrutador inteligente de intenciones (`IntentRouter`) con los LLMs cuando se complete la Fase 5.
