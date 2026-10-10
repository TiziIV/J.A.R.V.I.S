# CONTEXT.md — Estado técnico de J.A.R.V.I.S.

## Objetivo

Asistente modular de voz para Windows, con transcripción local mediante faster-whisper, Ollama como cerebro principal, Gemini como respaldo, síntesis neuronal con Edge TTS y motor de habilidades locales con normalización léxica avanzada (`normalize_text` en `main.py`).

## Entorno documentado

- Windows 11, Python 3.12 en el entorno virtual local `venv/`.
- Hardware reportado por el usuario: GTX 1660 con 6 GB de VRAM y 24 GB de RAM.
- Whisper usa `base`, `cuda` y `float16` por configuración predeterminada; el constructor contempla fallback a CPU `int8`.
- Captura mono a 16 kHz. Ollama y Gemini se configuran por `config/settings.py` y `.env`.

## Arquitectura y estado actual

- `main.py`: coordina captura con VAD, transcripción con Whisper GPU, memoria conversacional (`ConversationMemory`), enrutamiento autónomo de intenciones y Function Calling mediante `IntentRouter`, ejecución de herramientas locales mediante `SkillManager` (`system_time`, `app_launcher`, `system_info`, `web_search`), conversación con fallback a Gemini y síntesis vocal asíncrona con Edge TTS.
- `src/audio/recorder.py`: captura con VAD por energía RMS, silencio de cierre predeterminado de 1,3 s y límite máximo de 20 s.
- `src/audio/stt.py`: transcribe en español con faster-whisper, filtro VAD del modelo y sin contexto de texto previo.
- `src/audio/tts.py`: genera y reproduce en un trabajador dedicado (`Future`).
- `src/brain/memory.py`: clase `ConversationMemory` para gestión de ventana deslizante de memoria a corto plazo (`max_turns=5`).
- `src/brain/intent_router.py`: clase `IntentRouter` para clasificar intenciones y emitir Function Calling estructurado en JSON con contexto histórico.
- `src/brain/ollama_client.py` y `src/brain/gemini_client.py`: adaptadores de Ollama local y Gemini en la nube (`GEMINI_MODEL`).
- `config/tools_schema.py`: esquemas estructurados de herramientas y generador de system prompt.
- `skills/base_skill.py`: contrato abstracto `BaseSkill`.
- `skills/system_skills.py`: `TimeSkill`, `AppLauncherSkill` (con lista blanca segura), `SystemInfoSkill` (monitoreo de CPU y RAM con psutil) y `WebSearchSkill` (búsqueda en Google/YouTube con webbrowser y urllib.parse).
- `src/tools/skill_manager.py`: clase `SkillManager` para registro y ejecución centralizada de las 4 habilidades del sistema.
- `AGENTS.md`: reglas del repositorio consolidadas.
- `prompts_history.txt`: historial local acumulativo, ignorado por Git.

## Cambios de esta iteración (2026-10-09 - Fase 6: Calibración de System Prompt y Depuración de Memoria)

- Archivos modificados: `src/brain/intent_router.py`, `main.py`, `CONTEXT.md` y `prompts_history.txt`.
- Calibración del System Prompt en `src/brain/intent_router.py` y `main.py`:
  - Se incorporó la directiva explícita de identidad y memoria: *"Eres J.A.R.V.I.S., un asistente virtual. Recuerda los datos personales y preferencias que el usuario te comparta en la conversación (como su nombre, su color favorito, etc.). Si el usuario te pregunta por sus gustos o datos previos, responde usando la información guardada en el historial reciente."*
  - Esto previene que el LLM confunda los datos suministrados por el usuario con su propia personalidad y garantiza que consulte activamente la memoria a corto plazo ante consultas retrospectivas.
- Flujo y depuración en `main.py`:
  - Se estandarizó el guardado en memoria con argumentos nombrados explícitos: `memory.add_message(role="user", content=user_transcript)` previo a la inferencia y `memory.add_message(role="assistant", content=jarvis_response)` tras obtener la respuesta definitiva.
  - Se agregó la línea de diagnóstico solicitada: `print(f"[DEBUG Memoria] Turnos almacenados: {len(memory.get_messages_list())}")` tras cada respuesta y al invocar el borrado de contexto.
- Registro histórico: prompt exacto agregado en `prompts_history.txt`.

## Cambios de iteraciones anteriores

- **2026-10-09 (Fase 6 - Integración de ConversationMemory en IntentRouter y main.py)**: Conexión de `ConversationMemory` (`max_turns=5`) en el router y bucle de audio, con comandos de olvido de contexto.
- **2026-10-09 (Fase 6 - Creación de ConversationMemory)**: Creación de la clase `ConversationMemory` en `src/brain/memory.py` con ventana deslizante de turnos (`max_turns=5`).
- **2026-10-09 (Fase 5 - Integración de IntentRouter en main.py)**: Sustitución de detección manual de cadenas por Function Calling estructurado en JSON.
- **2026-10-09 (Fase 4 - SystemInfoSkill y WebSearchSkill)**: Implementación de habilidades de hardware y búsqueda web en `system_skills.py`.
- **2026-10-09 (Documentación Profesional README.md)**: Creación de `README.md` exhaustivo en la raíz del proyecto.

## Verificación y limitaciones

- Se ejecutaron pruebas automatizadas con el intérprete `venv/Scripts/python.exe`:
  - Verificación de integridad e importación: `import main` ejecutado sin errores.
  - Prueba de persistencia y calibración con Ollama (`qwen2.5-coder:3b`):
    - Turno 1: Declaración de datos personales ("Carlos", manzana). Registrado con 2 mensajes (rol usuario y asistente).
    - Turno 2: Consulta retrospectiva directa sobre los datos aportados. El modelo respondió con éxito: *"Tu fruta favorita es la manzana y tu nombre es Carlos."*. Registrado con 4 mensajes en memoria.
  - Verificación del comando de olvido de contexto: `memory.clear()` resetea los turnos almacenados a 0.
- Hardware físico: micrófono y reproducción en altavoces requieren prueba manual por parte del usuario.

## Pendientes

- Validación manual del flujo completo por voz en vivo ejecutando `python main.py`.
