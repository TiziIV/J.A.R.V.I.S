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

## Cambios de esta iteración (2026-10-17 — Optimización de Contexto para Modelos Externos)

- **Creación de `CODEBASE_INDEX.md`:**
  - Mapa ultracompacto del repositorio con rutas exactas, clases, firmas y líneas de inicio y fin para todos los módulos de `main.py`, `config/`, `src/` y `skills/`.
  - Documentación de puntos críticos específicos:
    1. Wake Word greeting en `main.py` (L157-L165, sustituido por `winsound.Beep`).
    2. Ejecución de `subprocess.run` con `System.Speech` en `src/audio/tts.py` (L104-L111 dentro de `_speak_powershell`).
    3. Parseo de JSON en `src/brain/intent_router.py` (L95 llamada, L134-L155 `_parse_json_response`, L157-L187 `_extract_json_objects`, L199-L240 `_validate_payload`).
- **Regla de ahorro de tokens en `AGENTS.md`:**
  - Incorporación en cabecera de la directiva de concisión de comentarios para fases de trazado y delegación de comentarios junior didácticos a Gemini Flash.
- **Verificación:**
  - Suite de pruebas completa: **36/36 tests aprobados (100% OK)** en 0.835s.


## Cambios de esta iteración (2026-10-17 — Fase 9: Cierre Exhaustivo, Auditoría Didáctica y Empaquetado)

- **Auditoría Integral de Comentarios Nivel Junior y Tipado (Type Hints):**
  - Se recorrieron absolutamente todos los archivos de `config/`, `src/`, `skills/` y `main.py`.
  - Se añadieron explicaciones didácticas en español orientadas a programadores junior detallando qué hace cada bloque y el fundamento técnico de las decisiones (paridad de prompts entre Ollama y Gemini, carga en memoria con `soundfile.read`, `condition_on_previous_text=False` anti-alucinaciones en Whisper, filtros probabilísticos, pre-roll de 250 ms en VAD, etc.).
  - Se verificaron y completaron todos los type hints en funciones y métodos (`-> None`, tipos explícitos en atributos de inicialización).
  - Se blindó la resiliencia de argumentos en `AppLauncherSkill` y `WebSearchSkill` ante entradas no-string (`None`, `int`, `dict`) para prevenir excepciones.
  - Se unificó `_clean_for_voice` en `src/audio/tts.py` con parámetro `max_chars=300` opcional y alias `_sanitize_for_speech`, armonizando la compatibilidad entre pruebas de estrés (50.000 caracteres sin cortes) y llamadas rápidas de voz.

- **Diagnóstico de Pre-Vuelo (`check_health.py`):**
  - Se creó la herramienta de diagnóstico de pre-vuelo en la raíz del proyecto para evaluar el entorno en <2 segundos.
  - Verificaciones implementadas: Python Runtime (3.12.8), GPU CUDA con CTranslate2 y PyTorch (GeForce GTX 1660), DLLs de NVIDIA (`cublas64_*.dll`, `cudnn64_*.dll`), conexión a Ollama local con verificación de modelos, validación segura de `.env` enmascarando claves privadas, micrófono de entrada predeterminado y voces hispanas en `System.Speech` (Microsoft Sabina).
  - Ejecución verificada en terminal con 6 estados `[ OK ]`, 1 `[ WARN ]` controlado y 0 `[ERROR]`.

- **Empaquetado y Lanzadores Autónomos (`build_exe.py`, `run_jarvis.bat`, `run_jarvis_background.vbs`):**
  - `build_exe.py`: Perfeccionado con la lista exhaustiva de `--hidden-import` requeridos (`faster_whisper`, `ctranslate2`, `openwakeword`, `pystray`, `PIL`, `pygame`, `psutil`, `scipy`, `edge_tts`, etc.) y carpetas de datos `config/` y `skills/`.
  - `run_jarvis.bat`: Lanzador rápido interactivo que detecta `%~dp0`, activa automáticamente el entorno virtual `venv` y ejecuta `main.py` con pausa defensiva ante errores.
  - `run_jarvis_background.vbs`: Lanzador en segundo plano sin ventana de consola que invoca `pythonw.exe` para colocar a J.A.R.V.I.S. silenciosamente en el System Tray (apto para `shell:startup`).

- **Documentación Maestra (`ARCHITECTURE.md` y `README.md`):**
  - Se creó `ARCHITECTURE.md` con diagramas ASCII de flujo end-to-end, consumo medido de VRAM en GTX 1660 (52% de carga) y matriz de tolerancia a fallos para defensa académica.
  - Se actualizó `README.md` con la suite completa de habilidades, lanzadores, herramienta de diagnóstico y arquitectura de tests.

- **Verificación Final de la Suite de Pruebas Automatizadas:**
  - Comando ejecutado: `.\venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v`
  - Resultado: **188 pruebas ejecutadas, 0 fallos, 0 errores (100% OK)** en 3.555 segundos.
  - Repositorio certificado y 100% en verde para amanecer en producción.

## Cambios de esta iteración (2026-10-21 — Pre-procesamiento de Contexto y Optimización para Claude Opus)

- **Diagnóstico del Bucle de Sesión Continua (`main.py`):**
  - Se analizó el flujo de ejecución de `main.py` (L112-L292) identificando que el sistema opera en un bucle plano que vuelve inmediatamente a `[Modo Reposo]` (L112/L142-L148) tras cada turno individual.
  - Se mapearon las 3 fugas tempranas (`continue` en L187, L198, L224) y la caída automática al final del bloque `try` (L281), documentando la necesidad de una máquina de estados de 2 niveles (Standby Pasivo en CPU vs Sesión Conversacional Continua con contador de silencio consecutivo).
- **Mapa de Componentes y Conexiones:**
  - Se documentó la interconexión exacta en runtime de `WakeWordDetector`, `AudioRecorder`, `SpeechToText`, `ConversationMemory`, `IntentRouter`, `SkillManager`, `TextToSpeech` y `JarvisTray`.
- **Creación de `OPUS_BRIEFING.md`:**
  - Archivo maestro en la raíz diseñado para alimentar directamente a Claude Opus sin que gaste tokens explorando el disco.
  - Contiene: diagnóstico línea por línea, arquitectura de sesión continua (bucle activo con salida tras 2 silencios o triggers vocales), especificaciones acústicas HUD Stark Industries (`winsound.Beep`), diseño de personalidad cinematográfica Paul Bettany (mayordomo británico de alta tecnología, conciso, leal, ingenioso) y catálogo de 4 protocolos compuestos del MCU (`protocolo_trabajo`, `protocolo_diagnostico`, `protocolo_silencio`, `protocolo_bloqueo`).
- **Activación de Regla de Ahorro de Tokens en `AGENTS.md`:**
  - Se actualizó `AGENTS.md` con la nota formal: `> **MODO AHORRO DE TOKENS OPUS:** Escribe código directo y compacto sin comentarios largos línea por línea; Gemini Flash agregará los comentarios didácticos nivel junior al finalizar.`
- **Registro en Bitácora:**
  - Prompt del usuario añadido a `prompts_history.txt`.

## Cambios de esta iteración (2026-10-21 — Sesión Continua Ilimitada, Personalidad MCU Stark, SFX HUD y Protocolos)

- **Eliminación del Límite de 3 Mensajes y Arquitectura "Continuous Active Session" (`src/session.py` y `main.py`):**
  - Se eliminó cualquier reinicio forzado a reposo por turno individual. Ahora el usuario dice "Hey Jarvis" **una sola vez**.
  - Al despertar, J.A.R.V.I.S. entra en un bucle de sesión activa ilimitada (`ActiveSession`). Puede sostener 10, 30 o más de 50 mensajes consecutivos de forma fluida.
  - Tras cada respuesta y una pausa anti-eco de 0.35 s (`SESSION_POST_SPEECH_PAUSE`), el micrófono reabre inmediatamente sin repetir "Hey Jarvis" ni "¿Sí, señor?".
  - El retorno a `[Modo Reposo]` ocurre **únicamente** ante:
    1. Inactividad por silencio prolongado de 7 segundos (`SESSION_IDLE_TIMEOUT`), tras lo cual emite el tono descendente HUD y pasa a standby.
    2. Comandos explícitos de descanso ("descansa", "modo reposo", "gracias jarvis es todo", "espera en segundo plano", "eso es todo"), ante los cuales responde cortésmente ("En espera, señor.") y pasa a standby.
- **Efectos Sonoros HUD Stark Industries en Memoria (`src/audio/sfx.py`):**
  - Módulo sintetizador de ondas senoidales en memoria mediante NumPy sin requerir archivos externos de audio en disco:
    - `wake()`: Doble chirp ascendente cristalino para activación por Wake Word.
    - `confirm()`: Blip corto y nítido para ejecución de skills o protocolos.
    - `standby()`: Glissando descendente suave al retornar a modo reposo.
  - Totalmente blindado con ejecución asíncrona/segura y fallback silencioso si el hardware de sonido no está disponible.
- **Personalidad Cinematográfica Paul Bettany MCU (`config/prompts.py` y `src/brain/intent_router.py`):**
  - Rediseño completo de `JARVIS_SYSTEM_PROMPT`: mayordomo británico de alta tecnología, culto, sereno, sutil ironía y máxima elegancia.
  - Respuestas ultra concisas de 1 a 2 oraciones (menos de 180 caracteres), optimizadas para voz hablada.
  - Eliminación absoluta de fórmulas robóticas de atención al cliente ("¡Hola! ¿En qué puedo ayudarte hoy?").
- **Protocolos Stark Compuestos (`skills/system_skills.py`, `src/tools/skill_manager.py`, `config/tools_schema.py`):**
  - Creación de la habilidad `StarkProtocolSkill` registrada bajo `activate_protocol`:
    - **Protocolo de trabajo / modo programación:** Despliega VS Code y navegador, reportando hora actual, CPU y RAM en un reporte cinematográfico.
    - **Protocolo diagnóstico / reporte de estado:** Evalúa en tiempo real aceleración CUDA en GPU, estado del daemon local de Ollama, CPU y RAM.
    - **Protocolo silencio / modo sigilo:** Enmudece el audio maestro de Windows y pausa reproducción activa.
  - Registro de la skill en `SkillManager` (9 habilidades activas) y esquema en `TOOLS_SCHEMA`.
- **Blindaje de JSON y Robustez de Audio (`src/brain/intent_router.py` y `src/audio/tts.py`):**
  - Auto-reparación de JSON truncado por `num_predict` con extracción limpia de `response` y aceptación directa de respuestas en texto plano sin duplicar llamadas al LLM.
  - Sanitizador reforzado en `TextToSpeech._clean_for_voice` que remueve bloques de código, encabezados, viñetas, símbolos Markdown (*, #, `, ~) y emojis, manteniendo las palabras leídas de forma natural e inteligible.
  - Locución síncrona/barge-in limpia sin fugas de procesos ni desincronización de audio.
- **Suite de Pruebas Automatizadas y Certificación:**
  - Creación de `tests/test_continuous_session_and_protocols.py` con 17 pruebas específicas para sesión continua de 10 y 50 turnos, timeouts de inactividad, comandos de descanso, protocolos Stark, generación de WAVs y enrutamiento.
  - **Ejecución de la suite completa:** `python -m unittest discover -s tests -p "test_*.py"`
  - **Resultado: 292 pruebas ejecutadas, 0 fallos, 0 errores (100% OK)** en 7.43 segundos.

## Cambios de esta iteración (2026-10-21 — Auditoría Didáctica Nivel Junior en Español y Restauración de AGENTS.md)

- **Restauración de `AGENTS.md`:**
  - Se eliminó el banner temporal de ahorro de tokens de Opus, restaurando la versión limpia y definitiva de las reglas del proyecto.
- **Auditoría Didáctica Nivel Junior (Explicaciones en Español):**
  - Se revisaron todos los módulos modificados e introducidos, añadiendo comentarios explicativos sobre el *qué* y el *por qué* de las decisiones de ingeniería:
    - `config/settings.py`: Parámetros de sesión continua (`SESSION_IDLE_TIMEOUT = 7.0s`, `SESSION_SILENCE_DURATION = 1.8s`, `SESSION_POST_SPEECH_PAUSE = 0.35s`), flags de hardware para GTX 1660 y switch `SFX_ENABLED`.
    - `config/prompts.py`: Fundamentos del estilo Paul Bettany MCU y la restricción a 180 caracteres para minimizar la latencia de síntesis vocal.
    - `config/tools_schema.py`: Documentación de la herramienta compuesta `activate_protocol` y sus parámetros permitidos.
    - `src/audio/sfx.py`: Síntesis de ondas procedimentales en memoria con NumPy, cálculo de chirps sinusoidales, empaquetado del encabezado RIFF/WAVE en `io.BytesIO()` y reproducción asíncrona segura con `winsound.SND_MEMORY`.
    - `src/session.py`: Bucle `ActiveSession`, normalización Unicode NFKD para robustez fonética, discriminación de órdenes de salida vs. descanso, filtrado de ruido/alucinaciones de Whisper y pausa anti-eco.
    - `skills/system_skills.py`: Implementación de `StarkProtocolSkill` (protocolos `trabajo`, `diagnostico` y `silencio`), uso de `subprocess.Popen` no bloqueante, muestreo ágil de CPU y llamadas virtuales `user32.keybd_event`.
    - `src/tools/skill_manager.py`: Registro y despacho coordinado de habilidades nativas y compuestas.
    - `src/brain/intent_router.py`: Detección de mensajes degradados de proveedor (`is_provider_error`), atajo de texto plano (`_plain_text_decision`) para no duplicar llamadas al LLM, y reparador algorítmico de balance de llaves/cadenas para JSON truncado (`_repair_truncated_json`).
    - `src/audio/tts.py`: Token de generación (`_generation`) para invalidación inmediata de audio sin condiciones de carrera, pipeline de regex anti-markdown y protocolo de desbloqueo de temporales en Windows NTFS.
    - `main.py`: Arquitectura de bucle en dos niveles (Modo Reposo pasivo vs. Sesión Activa Continua) y locución bloqueante defensiva anti-eco.
- **Verificación Final de la Suite de Pruebas:**
  - Se ejecutó la suite completa: `.\venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"`
  - **Resultado: 292 pruebas ejecutadas, 0 fallos, 0 errores (100% OK)** en 7.82 segundos.
  - Todas las interfaces y lógica funcional se mantuvieron intactas al 100%.


## Cambios de esta iteración (2026-10-27 — Arquitectura Multi-Brain, Consola Primero y Voz Opcional)

### 1. Arquitectura Multi-Brain y Separación de Responsabilidades
- **`config/settings.py`:** Centraliza la configuración de motores locales y cloud (`OLLAMA_MODEL_CENSORED`, `OLLAMA_MODEL_UNCENSORED`, `GEMINI_MODEL`, `GEMINI_FALLBACK_MODEL`, `ANTHROPIC_API_KEY`, `CLAUDE_MODEL`, `OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_FALLBACK_MODEL`, `DEFAULT_BRAIN_MODE`, `DEFAULT_INPUT_MODE`, `VOICE_MAX_TOKENS`, `TEXT_MAX_TOKENS`, `CLOUD_TIMEOUT`) con validación segura (`_env_choice`, `_env_int`, `_env_float`, `_env_bool`) y registro de advertencias en `CONFIG_WARNINGS`.
- **`config/prompts.py`:** Conserva intacto `JARVIS_SYSTEM_PROMPT` (modo voz, conciso y libre de Markdown) y añade `JARVIS_TEXT_SYSTEM_PROMPT` y `get_system_prompt(voice_mode)` para explicaciones detalladas y código formateado en consola.
- **`src/brain/exceptions.py`:** Clasificación estructurada de errores (`BrainError`, `BrainErrorKind`: `UNCONFIGURED`, `AUTH`, `QUOTA`, `MODEL_NOT_FOUND`, `DAEMON_OFFLINE`, `TIMEOUT`, `NETWORK`, `SERVER`, `INVALID_REQUEST`, `PARTIAL_STREAM`, `EMPTY_RESPONSE`) y sanitización de claves API (`redact_secrets`).
- **`src/brain/ollama_client.py` (`OllamaBrain`):** Soporta `model_override`, `context_prompt`, `num_predict`, `last_used_model`, `generate_stream` / `generate_response_stream` y fallback único de `OLLAMA_UNCENSORED` a `OLLAMA_MODEL_CENSORED` ante modelo inexistente (HTTP 404), mostrando el comando manual `ollama pull <modelo>` sin descargar automáticamente ni reintentar streams parciales.
- **`src/brain/gemini_client.py` (`GeminiBrain`):** Soporta `GEMINI_MODEL`, `GEMINI_FALLBACK_MODEL`, `last_used_model`, límite de tokens adaptable entre voz y texto, y reintento único por modelo solo ante HTTP 404.
- **`src/brain/claude_client.py` (`ClaudeBrain`):** Cliente HTTP ligero mediante `urllib.request` contra `https://api.anthropic.com/v1/messages` (`anthropic-version: 2023-06-01`), con timeout configurable de 15 s y clasificación de errores HTTP.
- **`src/brain/openai_client.py` (`OpenAIBrain`):** Cliente HTTP ligero mediante `urllib.request` contra `https://api.openai.com/v1/chat/completions` (compatible también con `/v1/responses`), con verificación contextual de HTTP 404 (`model_not_found` / `param == "model"`) antes de intentar `OPENAI_FALLBACK_MODEL` (`gpt-4o`) una sola vez.
- **`src/brain/deliberation.py` (`AutoDeliberator`):** Deliberación automática determinista para el modo `AUTO` según intención, complejidad estructural y controles de seguridad. Prioriza `OLLAMA` estándar para saludos, consultas breves y preguntas educativas de ciberseguridad («¿Qué es el hacking ético?»), reserva `OLLAMA_UNCENSORED` únicamente para solicitudes explícitas de estilo menos restrictivo en contexto seguro, y selecciona el proveedor cloud configurado para arquitectura de software, depuración multi-módulo o análisis profundo.
- **`src/brain/brain_manager.py` (`BrainManager`):** Gestiona `global_mode`, selección puntual One-Shot (sin alterar `global_mode`), `set_mode_by_choice` con alias tolerantes a tildes/mayúsculas/puntuación, selector interactivo con la pregunta exacta `¿Qué motor de inteligencia desea utilizar, señor?`, prioridad de habilidades locales y cadena acotada de fallbacks sin ciclos.
- **`src/brain/intent_router.py` (`IntentRouter.detect_local_skill`):** Detección determinista prioritaria para las 9 habilidades locales de Windows (`TimeSkill`, `AppLauncherSkill`, `SystemInfoSkill`, `WebSearchSkill`, `StarkProtocolSkill`, `VolumeControlSkill`, `MediaControlSkill`, `ClipboardSkill`, `SystemPowerSkill`) antes de consultar cualquier LLM, permitiendo operación 100 % offline.
- **`main.py`:** Arranque predeterminado en consola de texto (`python main.py`) sin cargar `faster-whisper`, `SpeechToText`, `AudioRecorder`, `WakeWordDetector` ni inicializar recursos CUDA de STT. Soporta `--tts` (consola + TTS) y `--voice` (manos libres completo, con prioridad sobre `--tts`), carga diferida de voz con `/voz`, prompt dinámico `Tu [Modo: AUTO] > ` y diagnósticos únicos `[Cerebro: ...]` / `[Skill Local: ...]`.

### 2. Modos Disponibles y Selección
- Modos admitidos: `AUTO`, `OLLAMA`, `OLLAMA_UNCENSORED`, `GEMINI`, `CLAUDE`, `GPT`, `CODEX`.
- Selección global en consola: `/modo` (abre el selector interactivo con la pregunta exacta `¿Qué motor de inteligencia desea utilizar, señor?`) o `/modo <nombre_o_alias>` (ej. `/modo ollama`, `/modo sin censura`, `/modo gemini`, `/modo claude`, `/modo gpt`, `/modo codex`, `/modo auto`).
- Selección puntual One-Shot (solo afecta a esa consulta y conserva `global_mode`): `Con GPT: <consulta>`, `Usando Claude <consulta>`, `Responde con Gemini <consulta>`, `Usá Codex para revisar este archivo`, `Usá Codex para implementar esta función`.

### 3. Variables de Entorno y Configuración de Proveedores (`.env`)
- `DEFAULT_BRAIN_MODE=AUTO` (`AUTO`, `OLLAMA`, `OLLAMA_UNCENSORED`, `GEMINI`, `CLAUDE`, `GPT`, `CODEX`)
- `DEFAULT_INPUT_MODE=TEXT` (`TEXT`, `TTS`, `VOICE`)
- `OLLAMA_BASE_URL=http://localhost:11434`
- `OLLAMA_MODEL=qwen2.5-coder:3b`
- `OLLAMA_MODEL_UNCENSORED=huihui_ai/qwen2.5-abliterated:3b` (para instalar manualmente un modelo faltante: `ollama pull huihui_ai/qwen2.5-abliterated:3b`)
- `GEMINI_API_KEY=<clave>` | `GEMINI_MODEL=gemini-2.0-flash` | `GEMINI_FALLBACK_MODEL=gemini-2.0-flash`
- `ANTHROPIC_API_KEY=<clave_opcional>` | `CLAUDE_MODEL=claude-sonnet-4-6`
- `OPENAI_API_KEY=<clave_opcional>` | `OPENAI_MODEL=gpt-5.4` | `OPENAI_FALLBACK_MODEL=gpt-4o`
- `CODEX_ENABLED=1` | `CODEX_TIMEOUT=120.0` | `CODEX_WORKSPACE_DIR=<ruta_dentro_del_proyecto>` | `CODEX_ALLOW_AUTO=1` | `CODEX_SANDBOX=workspace-write` (`read-only` o `workspace-write`) | `CODEX_MODEL=` (opcional)

### 4. Modos de Inicio y Comandos de Prueba
- Consola texto (sin micrófono ni Whisper): `.\venv\Scripts\python.exe main.py`
- Consola texto con voz TTS: `.\venv\Scripts\python.exe main.py --tts`
- Manos libres completo: `.\venv\Scripts\python.exe main.py --voice`
- Suite específica Multi-Brain: `.\venv\Scripts\python.exe -m unittest tests.test_brain_deliberation -v` → **29 pruebas ejecutadas, 0 fallos, 0 errores (OK)** en 1.2s.
- Suite específica Codex CLI y Git Guard: `.\venv\Scripts\python.exe -m unittest tests.test_codex_integration -v` → **18 pruebas ejecutadas, 0 fallos, 0 errores (OK)** en 0.03s.
- Suite completa: `.\venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"` → **339 pruebas ejecutadas, 0 fallos, 0 errores (100% OK)** en 8.151s.
- Tiempo de inicio medido en modo texto (`main.py` con carga diferida): **0.1124 s** (< 0.3 s), con 0 módulos de captura/reconocimiento de voz (`faster_whisper`, `openwakeword`, `sounddevice`, `src.audio.stt`, `src.audio.recorder`, `src.audio.wake_word`) cargados en memoria.
- Limitaciones y verificaciones en vivo:
  - Se configuró `GEMINI_API_KEY` en `.env` (excluido de Git y sin exponer secretos) y se verificó conexión real contra Google Gemini API (`GeminiBrain.generate_response`). Para cuentas nuevas donde `gemini-2.0-flash` y `gemini-1.5-flash` devuelven `404 NOT_FOUND`, `GeminiBrain` hace fallback automático a `gemini-3-flash-preview` (y `gemini-3.1-flash-lite-preview` como respaldo adicional), fijando el modelo activo en `self.model_name` y `self.last_used_model` para no reintentar en los turnos siguientes.
  - Se instaló `@openai/codex` (`codex-cli 0.164.0`) y se verificó en vivo `CodexBrain.is_installed() == True`, `CodexBrain.check_auth_status()` (`authenticated=True, method='chatgpt'`) y `CodexBrain.run_readonly_task(...)` con `codex exec --json --sandbox read-only` (incluyendo cierre de `stdin=subprocess.DEVNULL` y normalización de saltos de línea para el wrapper `codex.cmd` de Windows).
  - En esta sesión el daemon local de Ollama no estaba encendido (`URLError`) y no hay claves `ANTHROPIC_API_KEY` ni `OPENAI_API_KEY` en `.env`, por lo que las llamadas a Ollama, Claude y GPT se validaron mediante respuestas y errores HTTP simulados offline (`unittest.mock`).

---

## Integración Oficial de OpenAI Codex CLI (`src/brain/codex_client.py` y `src/utils/git_guard.py`)

### 1. ¿Qué es Codex CLI y qué función cumple en J.A.R.V.I.S.?
- **OpenAI Codex CLI** (`codex`) es el agente oficial de programación en línea de comandos de OpenAI.
- En J.A.R.V.I.S. actúa como el motor especializado `CODEX` dentro de `BrainManager`, independiente del cliente HTTP `OpenAIBrain` (`GPT`).
- Se invoca de forma no interactiva mediante `codex exec --json --sandbox <modo>` usando `subprocess.run(..., shell=False)`.

### 2. Requisitos Previos, Instalación Oficial y Autenticación en Windows
- **Requisito previo:** contar con Node.js / npm instalado en Windows (actualmente Node/npm está presente en `C:\Program Files\nodejs\npm.cmd`) y una cuenta de ChatGPT compatible (Plus, Pro, Team, Enterprise/Edu) o configuración oficial de Codex CLI.
- **Instalación oficial manual en Windows (PowerShell / CMD):**
  ```powershell
  npm install -g @openai/codex
  ```
  *(Documentación oficial: https://developers.openai.com/codex/cli/ y https://developers.openai.com/codex/cli/reference/)*
- **Autenticación mediante cuenta de ChatGPT (en versiones de la CLI que lo admitan):**
  ```powershell
  codex login
  ```
- **Comprobación del estado de autenticación (en versiones que lo admitan):**
  ```powershell
  codex login status
  ```
- **Diferencias entre acceso mediante ChatGPT y API:**
  - El motor `CODEX` (`src/brain/codex_client.py`) utiliza la sesión local de **OpenAI Codex CLI** iniciada por el usuario con `codex login` (cuenta de ChatGPT), por lo que **no exige `OPENAI_API_KEY` en `.env`**.
  - El motor `GPT` (`src/brain/openai_client.py`) consume directamente el endpoint HTTPS `https://api.openai.com/v1/chat/completions` y sí requiere `OPENAI_API_KEY` con facturación de plataforma API.
  - J.A.R.V.I.S. nunca solicita contraseñas, no automatiza inicios de sesión interactivos ni lee archivos privados de credenciales.

### 3. Dos Tipos de Tarea y Permisos del Sandbox
1. **Tipo A — Consulta, explicación, revisión y análisis (`run_readonly_task`):**
   - Se activa ante pedidos de revisión, explicación, diagnóstico o búsqueda de errores (ej. *«Usá Codex para revisar este archivo»*).
   - Ejecuta `codex exec --json --sandbox read-only`.
   - No permite modificar archivos del proyecto, crear commits ni hacer push.
2. **Tipo B — Programación y modificación autorizada del proyecto (`run_coding_task`):**
   - Se activa únicamente cuando la solicitud autoriza de forma explícita crear, implementar, corregir o refactorizar código en el proyecto (ej. *«Usá Codex para implementar esta función»*, *«Usá Codex para crear una prueba»*).
   - Valida que el directorio de trabajo (`CODEX_WORKSPACE_DIR`) esté dentro de la raíz del proyecto (`BASE_DIR`).
   - Ejecuta `codex exec --json --sandbox workspace-write`.
   - **Prohibiciones estrictas:** jamás utiliza `--yolo`, `--dangerously-bypass-approvals-and-sandbox` ni `danger-full-access`.
   -Si una solicitud mezcla revisión con una posible edición ambigua, `CodexBrain` no modifica archivos y solicita confirmación explícita al usuario.
   - **Separación de controles:** autorizar la edición de archivos (Tipo B) **nunca** ejecuta `git commit` ni `git push` automáticamente; tras la ejecución se inspecciona el estado de Git y se preservan los cambios preexistentes mediante `GitSafetyGuard` (`src/utils/git_guard.py`).

### 4. Comportamiento en Selección Manual, One-Shot y Modo `AUTO`
- **Selección manual:** con `/modo codex` o eligiendo `Codex` ante la pregunta `¿Qué motor de inteligencia desea utilizar, señor?`.
- **One-Shot puntual:** frases como `Usá Codex para revisar este archivo` o `Con Codex: corrige este error` ejecutan esa única consulta en `CODEX` sin cambiar `global_mode`.
- **Modo `AUTO`:** solo delega tareas claras de programación/depuración de repositorio a `CODEX` cuando `CODEX_ENABLED=1`, `CODEX_ALLOW_AUTO=1`, el binario está instalado y `codex login status` confirma sesión activa; si falta cualquiera de esos requisitos o si ocurre un fallo recuperable, cae automáticamente a `GEMINI` -> `OLLAMA`.

### 5. Limitaciones de Uso y Errores Frecuentes
- **Codex CLI no instalado:** J.A.R.V.I.S. informa cómo instalarlo desde https://developers.openai.com/codex/cli/ sin instalar paquetes automáticamente.
- **Sesión no iniciada (`Not logged in`):** J.A.R.V.I.S. solicita ejecutar manualmente `codex login` en la terminal del usuario.
- **Timeout (`CODEX_TIMEOUT`):** si una tarea supera el plazo configurado (120 s por defecto), el proceso se cancela de forma controlada sin bloquear ni cerrar J.A.R.V.I.S.
- **Directorio fuera del proyecto:** `validate_workspace()` bloquea cualquier ruta externa a `BASE_DIR`.

---

## Cambios de esta iteración (2026-10-27 — Prompt Maestro V2.0: ChainOrchestrator + FileEditorSkill + Windows Safe Execution)

### 1. Arquitectura de `FileEditorSkill` (`skills/file_editor_skill.py`) y `SkillManager`
- **`FileEditorSkill` (`name="file_editor"`):** Habilidad registrada en `src/tools/skill_manager.py` (10 habilidades activas en total) y mapeada en `src/brain/intent_router.py` (`SKILL_CLASS_BY_TOOL_NAME["file_editor"] = "FileEditorSkill"`), manteniendo intacto el Fast-Path de Windows y el inicio instantáneo en modo texto.
- **Validación defensiva de rutas en Windows (`validate_authorized_path`):**
  - Raíces permitidas configuradas en `config/settings.py` (`AUTHORIZED_FILE_ROOTS`: workspace del proyecto y subcarpetas de configuración de usuario como `Documents\My Games`, `Saved Games`, `%LOCALAPPDATA%` y `%APPDATA%`).
  - Prohibición absoluta de directorios del sistema (`FORBIDDEN_SYSTEM_PREFIXES`: `C:\Windows`, `System32`, `Program Files`, etc.), rutas UNC (`\\server\share`), nombres de dispositivos reservados de Windows (`CON`, `PRN`, `AUX`, `NUL`, `COM1..9`, `LPT1..9`), variables de entorno sin expandir (`%...%`) y segmentos de path traversal (`..`).
  - Detección de enlaces simbólicos, junctions NTFS y reparse points (`os.lstat` + `FILE_ATTRIBUTE_REPARSE_POINT`) tanto en el archivo objetivo como en cada directorio ancestro.
- **Descubrimiento acotado multi-juego (`discover_config_file` / `discover_config_files_detailed`):**
  - Busca archivos de configuración por nombre de juego/aplicación y archivo objetivo con límite de profundidad (`FILE_EDITOR_MAX_DISCOVERY_DEPTH = 4`) y cantidad máxima de archivos (`FILE_EDITOR_MAX_DISCOVERY_FILES = 600`).
  - Si existen múltiples instalaciones candidatas, reporta estado `"ambiguous"` y exige selección explícita sin elegir arbitrariamente.
- **Backups verificables (`BackupManager`, `BackupRecord`, `create_backup`):**
  - Antes de cualquier modificación se crea una copia byte a byte `<archivo>.<YYYYMMDD_HHMMSS_ffffff>.bak` usando apertura exclusiva (`"xb"`, nunca sobrescribe un `.bak` existente) con `flush()` + `os.fsync()`.
  - Se recalcula y verifica el hash SHA-256 del original y del backup antes de continuar. Los archivos `*.bak` y `backups/` están excluidos en `.gitignore` y bloqueados por `GitSafetyGuard`.
- **Edición conservadora por formato (`modify_ini_or_text`):**
  - **INI / CFG / CONF / TXT:** Parser propio línea por línea (sin `configparser`) que preserva mayúsculas/minúsculas exactas de claves y secciones, comentarios de línea completa e inline (`;` y `#`), líneas vacías, indentación, orden de secciones, saltos de línea (`CRLF` o `LF`), BOM (`UTF-8-SIG`, `UTF-16-LE/BE`) y archivos sin encabezado de sección. Detecta y aborta ante claves duplicadas o coincidencias ambiguas sin corromper el archivo.
  - **JSON:** Validación sintáctica antes y después de actualizar rutas anidadas (`graphics.shadows`), conservando tipos de datos y saltos de línea.
  - **XML:** Actualización segura de nodos y atributos bloqueando estrictamente declaraciones `<!DOCTYPE` y `<!ENTITY` para prevenir ataques XXE.
  - **Reemplazo completo explícito:** Solo permitido cuando se pasa `replacement_content` sin `key_values` (parámetros mutuamente excluyentes).
- **Escritura atómica en Windows con reintentos (`_atomic_write_bytes_with_retries`):**
  - Escribe en un archivo temporal único en el mismo directorio del archivo objetivo, ejecuta `flush()` + `os.fsync()`, valida formato y SHA-256, comprueba que el archivo original no haya cambiado concurrentemente (anti-TOCTOU) y ejecuta `os.replace` con hasta 3 reintentos (`FILE_EDITOR_MAX_RETRIES = 3`, backoff base `100 ms`) ante bloqueos transitorios de Windows (`PermissionError`, `WinError 32`).
- **Auditoría y Rollback (`AuditLogger`, `RollbackManager`, `rollback`):**
  - `AuditLogger` registra cada operación en `logs/file_editor_audit.jsonl` redactando cualquier posible secreto mediante `redact_secrets`.
  - `rollback` y `RollbackManager.rollback_execution` verifican el SHA-256 del backup, detectan si el archivo fue modificado externamente con posterioridad a la ejecución original, crean un backup preventivo pre-rollback y restauran atómicamente el estado original exacto.

### 2. Arquitectura de `ChainOrchestrator` (`src/brain/chain_orchestrator.py`)
- **Esquemas `ChangeOperation` y `ChangePlan`:**
  - `ChangeOperation` y `ChangePlan` validan tipos y rechazan cualquier campo desconocido en `from_dict`.
  - `ChangePlan.canonical_json()` y `ChangePlan.approval_hash()` generan un hash SHA-256 determinista sobre el contenido inmutable del plan, las rutas canónicas, los hashes originales de cada archivo y los valores propuestos.
- **Motor de políticas local (`PolicyEngine`):**
  - Valida cada operación e impide que cualquier proveedor externo (Codex, Gemini, OpenAI, Claude u Ollama) eluda las raíces autorizadas, incluya recomendaciones `UNSUPPORTED` o `REJECTED`, omita `verification_method`, desactive `reversible` o `requires_approval`, o proponga operaciones contradictorias.
- **Máquina de estados y flujo en 10 fases:**
  - Estados controlados por `ALLOWED_STATE_TRANSITIONS`: `RECEIVED -> INVESTIGATING -> PLANNING -> VALIDATING -> AWAITING_APPROVAL -> APPLYING -> VERIFYING -> COMPLETED` y estados terminales/alternativos `REJECTED`, `CANCELLED`, `FAILED`, `ROLLED_BACK`.
  - Cubre diagnóstico local de hardware en solo lectura (`detect_hardware_context`) e inspección de configuración (`inspect_target_config`), investigación con caché y control de timeout/errores de proveedor (`investigate_optimizations`), clasificación de evidencia (`VERIFIED`, `PLAUSIBLE`, `UNSUPPORTED`, `REJECTED`), simulación dry-run (`simulate_plan`), ejecución vinculada a `approval_hash` (`execute_approved_plan`), verificación post-escritura y rollback automático o manual (`rollback_execution`).

### 3. Gobernanza Autónoma de Git y Tags (`git-release-manager`)
- Se creó la skill de agente `.agents/skills/git-release-manager/SKILL.md` y se documentó en `AGENTS.md` la política de commits atómicos auditados con `GitSafetyGuard` y tags anotados cada 3 fases:
  - `v3.0.0`: Arquitectura Multi-Brain y modo Consola Primero.
  - `v4.0.0`: Integración oficial de OpenAI Codex CLI y `GitSafetyGuard`.
  - `v4.1.0` (Fases A–C): Gobernanza Git, skill `git-release-manager` y constantes de seguridad en `config/settings.py`.
  - `v4.2.0` (Fases D–F): `FileEditorSkill`, parser INI conservador, escritura atómica e integración en `SkillManager`.
  - `v4.3.0` (Fases G–I): `ChainOrchestrator`, `ChangePlan`, `PolicyEngine` y flujo multi-juego con aprobación criptográfica.
  - `v5.0.0` (Fases J–L): Suite exhaustiva `tests/test_file_editor_and_chain.py` (40 escenarios) y documentación final.

### 4. Verificaciones Ejecutadas
- Compilación estática:
  `.\venv\Scripts\python.exe -m py_compile skills/file_editor_skill.py src/brain/chain_orchestrator.py tests/test_file_editor_and_chain.py` → **Código de salida 0 (OK)**.
- Suite específica de `FileEditorSkill` y `ChainOrchestrator`:
  `.\venv\Scripts\python.exe -m unittest tests.test_file_editor_and_chain -v` → **40 pruebas ejecutadas, 0 fallos, 0 errores (OK)** en 0.964s.
- Suite completa del repositorio:
  `.\venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"` → **379 pruebas ejecutadas, 0 fallos, 0 errores (100% OK)** en 8.948s.



