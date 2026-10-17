# CONTEXT.md — Estado técnico de J.A.R.V.I.S.

## Objetivo

Asistente modular de voz para Windows, con transcripción local mediante faster-whisper en GPU, cerebro local Ollama con streaming en tiempo real y respaldo Gemini en la nube, síntesis de voz premium con edge-tts y fallback local offline en español mediante System.Speech, VAD adaptativo prolongado con prevención de cortes prematuros, memoria conversacional a corto plazo (ventana deslizante de 8 turnos), activación por palabra clave manos libres (Wake Word), interfaz en bandeja del sistema (System Tray), control anti-eco estricto por locks y suite de pruebas automatizadas con empaquetado autónomo en `.exe`.

## Hito Actual: Tag 3 — Streaming TTS, VAD Prolongado, Anti-Eco y Memoria Estabilizados

- **Fase 7 (Wake Word Hands-Free):** COMPLETADA. Bucle de dos estados integrado en `main.py` con `WakeWordDetector` en CPU y transición limpia a captura activa.
- **Fase 8 (System Tray y Streaming TTS de Alto Rendimiento):** COMPLETADA. Interfaz System Tray (`pystray` + `Pillow`), streaming de oraciones en tiempo real entre Ollama y TTS, VAD recalibrado a 2.5s de silencio continuo, fallback blindado de voz en español y control anti-eco (`tts.is_speaking` + `time.sleep(0.6)`).
- **Fase 9 (Empaquetado Autónomo .exe):** INICIADA. Script `build_exe.py` configurado con PyInstaller para generar el ejecutable único (`--onefile`, `--noconsole`).

## Entorno documentado

- Windows 11, Python 3.12 en el entorno virtual local `venv/`.
- Hardware reportado por el usuario: GTX 1660 con 6 GB de VRAM y 24 GB de RAM.
- Whisper usa `base`, `cuda` y `float16` por configuración predeterminada; el constructor contempla fallback a CPU `int8`.
- Captura mono a 16 kHz. Ollama y Gemini se configuran por `config/settings.py` y `.env`.
- Dependencias añadidas en `requirements.txt`: `pystray`, `Pillow`, `pyinstaller`, `edge-tts`, `pygame`.

## Arquitectura y estado actual

- `main.py`: coordina la arquitectura de dos estados con control anti-eco estricto: escucha pasiva ligera ("Hey Jarvis") con `WakeWordDetector`, captura activa por VAD (`AudioRecorder`), transcripción con Whisper GPU (`SpeechToText`), memoria conversacional (`ConversationMemory`), enrutamiento autónomo de intenciones y Function Calling (`IntentRouter`), ejecución de herramientas locales (`SkillManager`), síntesis de voz con streaming por oraciones (`TextToSpeech`), y sincronización visual con la bandeja del sistema (`JarvisTray`).
- `src/audio/tts.py`: motor de síntesis de voz con streaming por fragmentos/oraciones. Utiliza `edge-tts` con voces neuronales en español (`es-AR-TomasNeural` / `es-ES-AlvaroNeural`) y reproducción mediante `pygame`. Si `edge-tts` falla (red o HTTP 403), activa inmediatamente el fallback local de PowerShell (`System.Speech`) filtrando OBLIGATORIAMENTE por voces en español (`Culture -eq 'es'`). Gestiona la bandera `is_speaking = True/False` para bloqueo anti-eco.
- `src/audio/recorder.py`: captura mono con VAD adaptativo avanzado: mide el ruido ambiente durante los primeros 400 ms, fija el umbral en `max(ambient_rms * 1.6, 120.0)`, tolera pausas naturales de hasta 2.5 s de silencio antes de cortar la orden y concede 6.0 s de tiempo inicial de gracia.
- `src/brain/ollama_client.py`: cerebro local con Ollama que implementa `stream=True` en `generate_response_stream()`, acumulando tokens y emitiendo oraciones completas delimitadas por '.', '?' y '!' en tiempo real para consumo inmediato por el TTS.
- `src/brain/memory.py`: clase `ConversationMemory` con ventana deslizante de 8 turnos completos (16 mensajes) y método `inject_context()` para incrustar el historial en el prompt del sistema.
- `src/brain/intent_router.py`: clasificador de intenciones con Function Calling y soporte de `fallback_llm` ante caídas de Ollama.
- `src/brain/gemini_client.py`: respaldo en la nube para Ollama ante indisponibilidad local.
- `src/brain/gemini_client.py`: respaldo en la nube para Ollama ante indisponibilidad local, configurado con `gemini-2.0-flash` y fallback a `gemini-1.5-flash`.
- `src/ui/tray.py`: System Tray en segundo plano con ícono dinámico que refleja el estado ("reposo", "hablando", "escuchando", "procesando") y menú contextual.
- `src/utils/logger.py`: sistema centralizado de logging con `propagate = False`, FileHandler UTF-8 en `jarvis_system.log` y StreamHandler en consola.
- `build_exe.py`: script de compilación y empaquetado autónomo con PyInstaller configurado en modo directorio (`--onedir`), sin consola (`--noconsole`), import oculto de `faster_whisper` e inclusión de recursos de `config/` y `skills/`.
- `tests/`: suite de 36 pruebas automatizadas que cubren respuesta hablada garantizada, Circuit Breaker de TTS, VAD prolongado a 2.5s, fallback a PowerShell en español, ventana de memoria, streaming de Ollama, estrés de TTS (50k caracteres), resiliencia de audio, enrutamiento LLM, System Tray, logging sin duplicados y modelos de Gemini.
- `TESTS_REPORT.md`: bitácora detallada de los resultados de pruebas y auto-corrección.
- `AGENTS.md`: reglas del repositorio consolidadas.
- `prompts_history.txt`: historial local acumulativo, ignorado por Git.

## Cambios de esta iteración (2026-10-13 - Corrección Definitiva de Producción)

- **Reparación de Respuesta Hablada en `main.py`:**
  - Se unificó el bucle de procesamiento para que **toda respuesta generada** (`jarvis_response`), ya sea de conversación directa, llamadas a herramientas o consulta directa al LLM, invoque obligatoriamente `tts.speak(jarvis_response)` de forma bloqueante (`speech_future.result()`).
  - Se añadió la pausa obligatoria de `time.sleep(0.5)` tras la locución antes de reanudar el Modo Reposo, evitando cortes de micrófono y previniendo el eco.
- **Circuit Breaker en TTS (`src/audio/tts.py`):**
  - Implementación de la bandera `self.edge_tts_disabled = False`.
  - Ante el primer fallo de red o error 403 / `WSServerHandshakeError` en `edge-tts`, se conmuta `self.edge_tts_disabled = True`, emitiendo un único aviso breve y limpio: `[TTS] Edge-TTS bloqueado (403). Fijando motor local System.Speech como predeterminado.`.
  - En llamadas subsiguientes, se deriva de forma inmediata al motor local de Windows (`System.Speech` con `.txt` UTF-8 síncrono y sin timeout), eliminando demoras de espera de Bing y suprimiendo tracebacks con URLs largas en consola.
- **Supresión de Logs Duplicados (`src/utils/logger.py`):**
  - Se configuró `custom_logger.propagate = False` en `setup_logger()`, evitando la propagación de eventos al root logger y eliminando la duplicación de mensajes en la consola.
- **Corrección de Modelo Gemini (`config/settings.py` y `src/brain/gemini_client.py`):**
  - Se sustituyó la referencia inexistente `gemini-3.8-flash` por `gemini-2.0-flash` como modelo predeterminado, manteniendo `gemini-1.5-flash` como respaldo verificado y normalizando automáticamente cualquier identificador legado.
- **Suite de Pruebas y Certificación (`tests/test_production_fixes.py`):**
  - Suite de 5 nuevas pruebas unitarias cubriendo: activación del Circuit Breaker, derivación directa a PowerShell en la segunda llamada, invocación obligatoria de `tts.speak()` en chat y herramientas, propiedad `propagate = False`, y modelos válidos de Gemini.
  - Ejecución de la suite completa: **36/36 pruebas aprobadas (100% OK)** en 0.908s.

## Verificación y limitaciones

- **Verificación de Sintaxis y Compilación Estática:**
  - `python -m py_compile main.py build_exe.py src/audio/tts.py src/utils/logger.py config/settings.py src/brain/gemini_client.py tests/test_production_fixes.py`: 0 errores.
- **Verificación de la Suite de Pruebas Automatizadas:**
  - `python -m unittest discover -s tests -p "test_*.py" -v`:
    - 36 pruebas ejecutadas, 0 fallos, 0 errores (OK).
    - Valida: respuesta hablada en toda orden, Circuit Breaker de TTS desactivando edge-tts tras error 403, logs sin duplicación, modelos correctos de Gemini, logging UTF-8 en raíz, VAD a 2.5s, memoria de 8 turnos, y resiliencia de audio/LLM.
- **Verificación de Archivo de Log:**
  - Confirmada la escritura persistente en `jarvis_system.log` en el directorio raíz.
- **Hardware físico:** Las pruebas unitarias y de estrés se ejecutaron con mocks sin depender de GPU ni micrófono real. La prueba de hardware en vivo con micrófono y altavoces físicos para la interacción de voz en tiempo real requiere validación manual por parte del usuario.

## Roadmap — Tag 3 (v3.0.0)

- **Fase 7:** Integración de Wake Word manos libres en bucle principal (**COMPLETADA**).
- **Fase 8:** Interfaz gráfica ligera o HUD en la bandeja del sistema System Tray (**COMPLETADA**).
- **Fase 8.1:** Streaming TTS de alto rendimiento, VAD prolongado a 2.5s, anti-eco y memoria de 8 turnos (**COMPLETADA**).
- **Fase 8.2:** Logging centralizado UTF-8 en raíz con FileHandler y StreamHandler (**COMPLETADA**).
- **Fase 8.3:** Corrección definitiva de TTS hablado, Circuit Breaker 403, supresión de duplicados y modelo Gemini (**COMPLETADA**).
- **Fase 9:** Empaquetado final autónomo (compilación con `build_exe.py` en modo `--onedir`, `--noconsole`, `--hidden-import=faster_whisper`) (**LISTO PARA EJECUCIÓN**).

## Cambios de esta iteración (2026-10-13 — Optimización Paso 1: Wake Word Beep + Anti-Markdown TTS)

- **Eliminación de saludo en Wake Word (`main.py`):**
  - Se eliminó la llamada `tts.speak("¿Sí, señor?")` y sus esperas (`speech_future.result()`, spin-wait de `tts.is_speaking`, `time.sleep(0.5)`) al detectar la palabra de activación.
  - Se reemplazó por un pitido suave de 120 ms a 900 Hz mediante `winsound.Beep(900, 120)` (envuelto en `try/except` para compatibilidad). El sistema inicia la escucha activa de forma inmediata.

- **Filtro Anti-Markdown en TTS (`src/audio/tts.py`):**
  - Se agregó `import re` a la cabecera del módulo.
  - Se implementó el método estático `_clean_for_voice(text: str) -> str` que: (1) elimina `*`, `#`, `` ` `` y `_` con `re.sub(r"[*#\`_]", "", text)`, (2) trunca a 300 caracteres cortando en el punto seguido más cercano. Se aplica en `speak()` antes de encolar en el executor.

- **Límite de tokens ampliado (`src/brain/ollama_client.py`):**
  - `num_predict` subido de `150` a `250` en `generate_response_stream()` para garantizar que el JSON del `IntentRouter` nunca quede truncado a la mitad.

- **Regla de voz en prompt compartido (`config/prompts.py`):**
  - Se añadió al único `JARVIS_SYSTEM_PROMPT` la regla de voz obligatoria: prohibición de Markdown (listas, asteriscos, comillas invertidas, bloques de código). Esta única modificación propaga la restricción a Ollama y Gemini simultáneamente sin duplicar código.

- **Regla de voz en IntentRouter (`src/brain/intent_router.py`):**
  - Se añadió la misma regla de voz como refuerzo adicional en `_build_system_prompt()` para el contexto de routing.

## Verificación (2026-10-13 — Optimización Paso 1)

- **Suite completa:** `python -m unittest discover -s tests -p "test_*.py" -v` → **36/36 pruebas aprobadas (100% OK)** en 0.885s. Sin regresiones introducidas.
- **Hardware físico:** Cambios de lógica; la prueba con micrófono y altavoces reales requiere validación manual del usuario.

