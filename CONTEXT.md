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
- `skills/system_skills.py`: `TimeSkill` y `AppLauncherSkill` (con lista blanca segura).
- `src/tools/skill_manager.py`: clase `SkillManager` para registro y ejecución segura.
- `AGENTS.md`: reglas del repositorio consolidadas.
- `prompts_history.txt`: historial local acumulativo, ignorado por Git.

## Cambios de esta iteración (2026-10-09 - Normalización de Intenciones)

- Archivos modificados: `main.py`, `CONTEXT.md` y `prompts_history.txt`.
- Se implementó la función auxiliar `normalize_text()` en `main.py` utilizando `unicodedata` para remover tildes, signos de puntuación y unificar en minúsculas.
- Se mejoró el enrutamiento y detección léxica para `TimeSkill` (`hora`, `fecha`, `dia`) y `AppLauncherSkill` (`abrir`, `abri`, `ejecutar`, `lanzar`, `iniciar`, `pone`, `pon` con coincidencia flexible para calculadora, bloc de notas, code y navegador).
- El prompt completo de esta iteración se agregó a `prompts_history.txt`; ese archivo es local y no debe versionarse.

## Verificación y limitaciones

- No se ejecutaron pruebas automáticas ni verificaciones de código durante esta iteración.
- Micrófono, pausas naturales, GPU/CUDA, llamadas a proveedores, ejecución de subprocesos y reproducción/liberación del MP3 requieren validación manual en el Windows y hardware reportados.

## Pendientes

- Inyectar el manifiesto de herramientas (`get_skills_manifest()`) directamente en el prompt del sistema de los LLMs para que el modelo decida de forma autónoma cuándo invocar una skill.
- Probar la ejecución real de comandos de voz normalizados en Windows.
