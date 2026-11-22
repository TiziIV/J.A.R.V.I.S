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
  - `v5.1.0` (Fases M–O): Integración interactiva en consola (`/optimizar <juego_o_ruta>` y `/rollback <execution_id>`) en `main.py` y `ChainOrchestrator` con vista previa de simulación, aprobación total o selectiva por ID (`op-001`) y 42 pruebas automatizadas.

### 4. Comandos Interactivos en Consola (`main.py`)
- `/optimizar <juego_o_ruta>`: Localiza de forma acotada el archivo de configuración del juego/aplicación dentro de las raíces autorizadas, inspecciona sus valores, consulta las recomendaciones para el hardware local, valida con `PolicyEngine` y muestra la tabla de simulación (`SIMULACIÓN EN SOLO LECTURA`) junto con el `approval_hash` SHA-256. A continuación solicita confirmación explícita (`s` para aplicar todo, `op-001,op-002` para aplicar operaciones específicas, o `n` para cancelar sin tocar el disco).
- `/rollback <execution_id>`: Restaura en orden inverso todos los archivos modificados en esa ejecución verificando el hash SHA-256 original y detectando ediciones manuales posteriores.

### 5. Verificaciones Ejecutadas
- Compilación estática:
  `.\venv\Scripts\python.exe -m py_compile main.py skills/file_editor_skill.py src/brain/chain_orchestrator.py tests/test_file_editor_and_chain.py` → **Código de salida 0 (OK)**.
- Suite específica de `FileEditorSkill`, `ChainOrchestrator` y consola interactiva:
  `.\venv\Scripts\python.exe -m unittest tests.test_file_editor_and_chain -v` → **42 pruebas ejecutadas, 0 fallos, 0 errores (OK)** en 1.134s.
- Suite completa del repositorio:
  `.\venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"` → **381 pruebas ejecutadas, 0 fallos, 0 errores (100% OK)**.

---

## Cambios de esta iteración (2026-11-03 — Fase de Endurecimiento, Integridad y Validación de ChainOrchestrator)

### 1. Validación Pre-Flight y Grafo de Dependencias (`src/brain/chain_orchestrator.py`)
- **`PolicyEngine.validate_dependency_graph` y `ChainOrchestrator.preflight_validate`:**
  - Mecanismo centralizado de validación previa (pre-flight) ejecutado íntegramente en memoria antes de abrir cualquier archivo en modo escritura o generar backups.
  - Verifica IDs únicos y no vacíos, ausencia de dependencias autorreferenciales (`A -> A`), existencia de todas las dependencias declaradas y compatibilidad con el estado actual de cada operación.
  - Detecta ciclos dirigidos mediante **DFS de 3 colores** (`0=no visitado`, `1=en pila de recursión`, `2=completado`) reportando la ruta exacta del ciclo (`A -> B -> A`).
  - Genera un orden de ejecución topológico determinista mediante **algoritmo de Kahn con cola de prioridad (`heapq`)** ponderada por el índice original de aparición, garantizando que ante una selección invertida (`[C, B, A]` donde `C -> B -> A`) la ejecución respete estrictamente `A -> B -> C` y que las operaciones independientes conserven su orden original.

### 2. Modelo de Estados de Operación e Idempotencia (`OperationStatus`)
- **`OperationStatus(str, Enum)`:**
  - Estados formales por operación: `PENDING`, `APPLIED` (con alias `EXECUTED`), `FAILED` y `BLOCKED`.
  - Transiciones controladas mediante `ALLOWED_OPERATION_STATUS_TRANSITIONS` (`PENDING -> {APPLIED, FAILED, BLOCKED}`) registradas en `ChangeOperation.status_history`.
  - **Inmutabilidad de estados terminales:** `ChangeOperation.__setattr__` y `transition_status` impiden modificar silenciosamente el estado una vez alcanzado un estado terminal (`APPLIED`, `FAILED`, `BLOCKED`).
  - **Prevención de reejecución accidental y verificación real en disco:** Se rechaza con `DuplicateExecutionError` reejecutar operaciones ya `APPLIED`, y `_verify_single_operation_on_disk` comprueba el contenido real del archivo en disco antes de declarar una operación como aplicada.
  - **Integridad de estado del plan (`ChangePlan.is_fully_completed`):** Un plan que conserve operaciones en estado `PENDING`, `FAILED` o `BLOCKED` nunca se declara `fully_completed=True` (reporta `plan_status="PARTIALLY_APPLIED"` en ejecuciones selectivas parciales).

### 3. Aprobación Selectiva e Integridad Criptográfica SHA-256
- **Validación de selección (`selected_operation_ids`):**
  - Rechaza expresamente IDs desconocidos, selecciones mixtas (ID válido + ID inválido), IDs duplicados en la selección y selecciones vacías (`[]`), distinguiendo errores de validación respecto de la cancelación explícita del usuario (`cancel_plan()` -> `ChainState.CANCELLED`).
  - Verifica dependencias directas y transitivas de las operaciones seleccionadas: una dependencia no incluida en la selección solo se admite si ya figura en `OperationStatus.APPLIED` y supera `_verify_completed_dependency_evidence` (existencia de `executed_file_hash`, registro verificable en `execution_records` o backup en disco y coincidencia de estado real en el archivo).
- **Vinculación criptográfica (`approval_hash` y `bind_selective_approval`):**
  - `ChangePlan.canonical_json()` serializa de forma determinista todos los campos relevantes de cada operación (`operation_id`, `target_path`, `format`, `section`, `key`, `old_value`, `new_value`, `value_type`, `evidence_level`, `risk_level`, `requires_approval`, `reversible`, `verification_method`, `depends_on`, `original_file_hash`).
  - El hash SHA-256 se recalcula inmediatamente antes de ejecutar (`execute_approved_plan`); cualquier alteración posterior a la aprobación en las operaciones del plan o en la lista vinculada por `bind_selective_approval` invalida la autorización (`ApprovalBindingError`).

### 4. Propagación de Errores, Backups y Operaciones Parciales
- **Propagación transitiva (`_propagate_blocked_status`):**
  - Si una operación `A` falla durante la escritura, pasa a `OperationStatus.FAILED` y todas sus dependientes directas y transitivas (`B`, `C`, ...) transicionan inmediatamente a `OperationStatus.BLOCKED` sin invocar `FileEditorSkill` ni crear backups para ellas.
  - Cuando `continue_independent=True` y `auto_rollback=False`, las operaciones independientes continúan su ejecución y el resultado parcial conserva la evidencia completa de las operaciones aplicadas (`applied_operations`, `failed_operations`, `blocked_operations`).
- **Protección de ediciones externas en rollback (`_rollback_applied_records`):**
  - El rollback automático pasa `expected_current_hash=latest_rec.post_edit_sha256` a `FileEditorSkill.rollback`, garantizando que jamás se sobrescriban cambios manuales legítimos realizados por el usuario después de una operación.

### 5. Aislamiento Determinista de Tests en Windows (`tests/test_file_editor_and_chain.py`)
- Se reemplazó `tempfile.TemporaryDirectory()` del directorio `%TEMP%` global por una raíz temporal dedicada y configurable mediante `JARVIS_TEST_TEMP_ROOT` (por defecto `BASE_DIR / "temp" / "jarvis_test_isolated"`), resuelta canónicamente con `Path.resolve()`.
- Cada test crea un subdirectorio único aislado e inyecta sus raíces autorizadas en la instancia de prueba de `FileEditorSkill` sin debilitar las validaciones de producción.
- La rutina de limpieza `_rmtree_strict_windows` utiliza `onexc` (Python 3.12+) / `onerror` para restaurar `stat.S_IWRITE` únicamente al eliminar archivos de prueba marcados como solo lectura, verificando que no queden directorios huérfanos y propagando cualquier error inesperado.

### 6. Resultados Reales de Verificación
- Compilación estática:
  `.\venv\Scripts\python.exe -m py_compile skills/file_editor_skill.py src/brain/chain_orchestrator.py tests/test_file_editor_and_chain.py` → **Código de salida 0 (OK)**.
- Suite específica de `FileEditorSkill` y `ChainOrchestrator` (42 escenarios originales + 20 nuevos grupos de endurecimiento):
  `.\venv\Scripts\python.exe -m unittest tests.test_file_editor_and_chain -v` → **62 pruebas ejecutadas, 0 fallos, 0 errores (100% OK)** en 6.805s.
- Suite global del repositorio:
  `.\venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"` → **401 pruebas ejecutadas, 0 fallos, 0 errores (100% OK)** en 30.444s.





## Fase de corrección de integridad, autorización y recuperación del ChainOrchestrator (2026-11-03)

### Estado implementado

- `src/brain/chain_orchestrator.py`: se agregaron `ROLLED_BACK` y `ROLLBACK_FAILED` para operaciones. Solo se llega a `ROLLED_BACK` después de restaurar y comprobar en disco el SHA-256 original; un error deja estado explícito `ROLLBACK_FAILED`. El historial conserva la etapa anterior `APPLIED`, motivo, hora y referencia al backup. El rollback manual rechaza repetición completa y reanuda únicamente backups cuyas operaciones siguen aplicadas o tienen reversión fallida; `force=True` queda rechazado para no omitir la defensa frente a cambios externos. Rollback parcial conserva resumen y operaciones ya revertidas.
- La autorización se separa del hash de integridad: un nonce de alta entropía se emite solo después de una respuesta afirmativa en el flujo de consola, con vencimiento, `plan_id`, `execution_id`, hash canónico y selección en orden determinista. El token se busca por clave exacta y su consumo se serializa con un lock de un solo uso. `user_approved=True` ya no autoriza ejecución por sí solo. El límite declarado es el proceso local: código arbitrario dentro del proceso podría eludir el flujo de consola.
- Después de la confirmación, la definición de cada operación se congela; la simulación mantiene una instantánea serializada, y la ejecución reconstruye las operaciones desde ella, restaurando su estado de ejecución desde un registro interno. Así, la modificación del objeto externo no modifica silenciosamente la acción aplicada.
- El grafo usa Kahn iterativo para orden y detección de ciclos, sin DFS recursivo. `CHAIN_MAX_OPERATIONS` limita planes (predeterminado 5000); `CHAIN_APPROVAL_TTL_SECONDS` define la duración predeterminada del token (300 s).
- La evidencia de dependencias comprueba el valor de la operación y que el hash en disco coincida con el último paso registrado para ese archivo, permitiendo operaciones compatibles sucesivas sobre el mismo archivo.
- Las operaciones independientes omitidas por política se registran con ID, ruta y motivo en `skipped_operations` y en el audit logger. Las dependencias que no pueden continuar conservan `BLOCKED`.
- `main.py`: el flujo `/optimizar` obtiene el token interactivo después de que el usuario seleccione y confirme las operaciones. `config/settings.py`: límites locales configurables. `tests/test_file_editor_and_chain.py`: se mantienen las 62 pruebas originales y se agregan once pruebas de estados de rollback, autorización, congelamiento/instantánea, grafo de 2000 nodos y límite máximo, selección ordenada, expiración/reutilización, omisiones, rollback parcial reanudable y evidencia transitiva en sucesivas ediciones del mismo archivo.

### Verificaciones realmente ejecutadas

- `python -m py_compile skills/file_editor_skill.py src/brain/chain_orchestrator.py tests/test_file_editor_and_chain.py main.py config/settings.py`: correcto, sin errores de sintaxis.
- La orden solicitada `python -m unittest tests.test_file_editor_and_chain -v` no puede importar el módulo en este intérprete porque falta `psutil`. Como verificación aislada, se ejecutó la misma suite con `psutil` sustituido en memoria (las pruebas del orquestador no requieren su cálculo de CPU/RAM): **73 pruebas, 0 fallos, 0 errores**.
- La suite global se intentó antes del ajuste final de estados de rollback y terminó con **277 pruebas, 9 fallos y 26 errores**. El intérprete no tiene, entre otras dependencias, `edge_tts`; varias pruebas de memoria también fallaron por estado compartido en `data/memory_store.json`. No se repitió después del ajuste final para evitar más escrituras al almacenamiento compartido; esa ejecución no certifica la versión final ni el repositorio completo.
- Esa ejecución global accedió e intentó persistir datos de prueba en `data/memory_store.json`. No se inspeccionó su contenido ni se restauró porque el archivo está ignorado y no existe una copia base segura conocida; preservarlo evita sobrescribir datos locales del usuario.
- No se probó hardware físico, GPU, micrófono ni reproducción; no son parte de esta fase de orquestación.

### Pendiente y riesgos

- Repetir la suite específica y global con el entorno virtual/dependencias del proyecto correctamente instaladas y con los tests de memoria aislados de los datos locales.
- No se ejecutó commit ni push: la condición solicitada para publicarlos exige que toda la suite pase, y la suite global no pasó. No se creó tag. El árbol preparado se mantuvo vacío.
- El token es una garantía del flujo de autorización controlado por la aplicación dentro de este proceso, no una frontera contra código local comprometido. La evidencia de cambios compatibles depende del registro en memoria de la ejecución; no implementa persistencia/recovery manager.
- No se inició la fase `ChangePlan Recovery & Transaction Manager`.

## Fase de aislamiento de memoria y seguridad de tests (2026-11-03)

### Protección inicial del archivo real

- `data/memory_store.json` existía al inicio: **1.218 bytes**, SHA-256 `83640EAADFF5E33FEB9126F6EEBB9F7960F924F01BB3A1968468014223341222`.
- Se creó antes de ejecutar pruebas una copia externa y única en `C:\Users\MV\AppData\Local\Temp\JARVIS_MEMORY_SAFETY_BACKUPS\memory_store_20261103_115628_426.json`. Copia y original coinciden en tamaño y SHA-256. No se mostró ni se usó el contenido del archivo real.
- Tras los conjuntos específicos, integraciones, ChainOrchestrator y suite global (incluidos ambos intentos globales), el original permaneció en **1.218 bytes** y con el mismo SHA-256. No se restauró ni modificó.

### Auditoría y causa raíz

- El almacenamiento se implementa en `src/brain/memory.py`. `ConversationMemory.__init__` carga el archivo configurado; `add_message`, `set_preference` y `clear` disparan `save_to_disk`; `load_from_disk` y `save_to_disk` también aceptan una ruta sobrescrita.
- Mapa: `main.py` crea `ConversationMemory` → lo inyecta en `BrainManager` → `IntentRouter` usa la memoria de la solicitud; en modo voz, `run_voice_session` reutiliza esa instancia o crea una. El constructor de `BrainManager` solo conserva la instancia inyectada y no crea persistencia global. Importar `src.brain.memory` no crea una instancia ni carga el JSON.
- La causa del acceso previo es que varias pruebas crean `ConversationMemory()` con la ruta real predeterminada. La protección anterior dependía de detectar `unittest` junto con argumentos de consola, permitía omisión si `JARVIS_FORCE_PERSISTENCE` estaba definida y no validaba `save_to_disk(file_path=...)` ni `load_from_disk(file_path=...)`. La suite anterior reportada en este contexto encontró fallos de memoria con estado compartido; no hay traza histórica suficiente para atribuir cada escritura previa a una prueba individual.

### Aislamiento implementado

- En `src/brain/memory.py`, los ejecutores detectados (`unittest` o `pytest`) redirigen el valor predeterminado a un archivo JSON con nombre aleatorio bajo `JARVIS_TEST_TEMP_ROOT`; el valor por defecto es `temp/jarvis_test_isolated/`, ignorado por Git. Cada instancia obtiene un archivo distinto para que no recupere datos de ejecuciones anteriores.
- Las rutas se canonicalizan y, en modo de pruebas, constructor, carga y guardado solo aceptan rutas bajo la raíz autorizada. `JARVIS_FORCE_PERSISTENCE` ya no elude el control. `storage_file=None` continúa desactivando persistencia explícitamente. En ejecución de producción sin runner de pruebas, la ruta y el formato existentes se conservan.
- Cada guardado usa un temporal aleatorio y `os.replace`; un lock por instancia serializa los guardados simultáneos y `finally` limpia temporales remanentes. Esto elimina colisiones de nombres observadas durante las pruebas concurrentes en Windows.
- Las pruebas de persistencia existentes usan ahora archivos dentro del directorio temporal dedicado. Se añadió `tests/test_memory_isolation.py` con cobertura de ruta Windows, aislamiento por instancia, crear/actualizar/eliminar, reinicialización, rechazo de rutas externas incluso con `JARVIS_FORCE_PERSISTENCE`, intentos de acceso y ausencia de instancia global retenida.

### Verificaciones realmente ejecutadas

- Memoria específica, orden normal: `tests.test_memory_isolation`, `tests.test_memory_persistence`, `tests.test_memory_window`, `tests.test_memory_overflow` → **42 pruebas, OK**.
- Los mismos módulos en orden inverso → **42 pruebas, OK**.
- Integración de persistencia de `tests.test_advanced_features_mission` → **1 prueba, OK**.
- Integraciones relacionadas y ChainOrchestrator/FileEditor: conjunto combinado con los módulos de memoria, `test_advanced_features_mission`, `test_brain_deliberation`, `test_intent_router_chaos`, `test_concurrency_and_locks` y `test_file_editor_and_chain` → **208 pruebas, OK**. `test_file_editor_and_chain` ejecutó sus 73 pruebas.
- Concurrencia tras corregir temporales únicos y serializar guardados → **4 pruebas de concurrencia de memoria, OK**, sin errores de reemplazo.
- `py_compile` sobre `src/brain/memory.py` y las pruebas afectadas → **OK**.
- Suite global `python -m unittest discover -s tests -p 'test_*.py'` → **419 pruebas; 418 pasaron y 1 tuvo error**. El único error fue `test_codex_integration.TestCodexClientUnit.test_validate_workspace_rejects_outside_project_root`: durante la limpieza de su `TemporaryDirectory`, Windows devolvió `PermissionError [WinError 5]` en `shutil.rmtree`/`chmod`. Se repitió con `TEMP/TMP` en la raíz de visualizaciones autorizada y falló en la misma limpieza. No se alteró ese test ajeno a esta fase.
- Dependencias del entorno virtual: `edge_tts`, `psutil` disponibles. No se instalaron dependencias ni se usaron sustitutos artificiales.
- Hash posterior a cada bloque y al cierre: **1.218 bytes**, SHA-256 `83640EAADFF5E33FEB9126F6EEBB9F7960F924F01BB3A1968468014223341222`.

### Estado de Git, limitaciones y siguiente paso

- El historial completo del prompt se añadió a `prompts_history.txt`; `.gitignore` continúa excluyéndolo. `data/` también sigue ignorado.
- Antes de esta fase ya estaban modificados `CONTEXT.md`, `config/settings.py`, `main.py`, `src/brain/chain_orchestrator.py` y `tests/test_file_editor_and_chain.py`; esos cambios preexistentes se conservaron. Esta fase agrega cambios en memoria y pruebas. No se prepararon archivos ni se hizo commit/tag/push porque la suite global no está completamente validada.
- Limitación: los procesos de prueba personalizados que no carguen `unittest`/`pytest` deben configurar `JARVIS_ENV=test` o inyectar `storage_file` temporal. `JARVIS_TEST_TEMP_ROOT` solo puede seleccionar un subdirectorio de la raíz autorizada canónica.
- Siguiente paso recomendado: resolver el error de permisos de limpieza del `TemporaryDirectory` en el entorno de ejecución y repetir la suite global completa. Hasta entonces, esta fase **no se declara completada** conforme al criterio de aceptación solicitado.

- Durante los dos intentos globales quedó un directorio temporal de TemporaryDirectory bajo la raíz autorizada de visualizaciones; la limpieza también fue rechazada por PermissionError al enumerar/chmod. No se pudo retirarlo desde esta sesión y no contiene datos del proyecto conocidos.

- El conteo global de 419 corresponde al intento anterior a la última prueba que bloquea la redefinición de la raíz. La versión final sí pasó los conjuntos dirigidos actualizados (42 pruebas de memoria; 208 pruebas combinadas), pero no se repitió la suite global completa después de ese endurecimiento.

## Fase de corrección de WinError 5 en test_codex_integration (2026-11-03)

### Diagnóstico reproducido

- Comando original reproducido: `python -m unittest tests.test_codex_integration.TestCodexClientUnit.test_validate_workspace_rejects_outside_project_root -v`. Falló al salir del contexto de `tempfile.TemporaryDirectory`, con `PermissionError [WinError 5]` en `os.walk`/`shutil.rmtree`; la recuperación intentó `chmod` (`tempfile._resetperms`) y también recibió `Access denied`.
- La ruta concreta de reproducción fue `C:\Users\MV\AppData\Local\Temp\tmpg1ctsu7s`. La causa no es el comportamiento funcional de `validate_workspace`: el método solo resuelve la ruta, consulta `exists/is_dir` y rechaza el directorio externo. El bloque problemático no escribe archivos ni ejecuta procesos.
- La suite usa `subprocess.run` simulado en los demás tests. En el test afectado, una regresión ahora exige que esa llamada no se invoque. El rastreo de procesos con `psutil` no encontró procesos inspeccionables con cwd ni archivos abiertos bajo la carpeta. 294 procesos no fueron inspeccionables por permisos del entorno; por ello no se afirma que se inspeccionaron todos los procesos del sistema.
- Diagnóstico: la política de acceso del sandbox de esta sesión impide enumerar y cambiar permisos de los directorios aleatorios creados por Python bajo `%TEMP%` (y también ocurrió al redirigir `TEMP/TMP` a otra raíz permitida). No hay evidencia de un proceso hijo o handle abandonado. La raíz existente `%TEMP%` sí es legible y sirve para probar que `validate_workspace` rechaza una ruta fuera del proyecto.

### Directorio pendiente de la ejecución anterior

- Sigue existiendo `C:\Users\MV\.codex\visualizations\2026\11\03\01a1a1e9-158a-71d2-b044-0b0599c7096a\jarvis-suite-6b589977ceb24ada916d27d471404659`, creado durante el intento de suite con `TEMP/TMP` alternativos. Metadatos: atributo `Directory` (16), creación UTC `2026-11-03 15:03:58.679`, última modificación UTC `15:04:25.439`.
- Se pudo leer su ACL; las entradas reportadas eran heredadas y no apareció una entrada Deny. No se pudo enumerar el contenido y una eliminación explícita del directorio falló por `Access denied`. `psutil` no encontró cwd/handles de procesos inspeccionables bajo esa ruta. Se deja intacto porque el entorno impide comprobar su contenido y demostrar que la eliminación sea segura.

### Corrección y verificaciones de esta fase

- `tests/test_codex_integration.py`: `test_validate_workspace_rejects_outside_project_root` usa el directorio `%TEMP%` existente en vez de crear un `TemporaryDirectory` que no aporta a la validación de límites. Conserva la comprobación de error `INVALID_REQUEST` y confirma que la validación no invoque `subprocess.run`. No se usa `ignore_cleanup_errors`, no se omite la prueba y no se modificó producción.
- Reproducción previa al cambio: **1 test, 1 error**, duración 0,005 s.
- Tras el cambio, el test aislado pasó. Después, `python -m unittest tests.test_codex_integration -v` se ejecutó dos veces: **18/18 aprobadas en cada ejecución**, 0 fallidas, 0 errores, 0 omitidas; cada corrida duró 0,024 s.
- No se han ejecutado todavía la suite de memoria, la integración combinada ni la suite global de esta fase; quedan pendientes en el orden solicitado.
- Integridad antes/después de la reproducción: `data/memory_store.json` sigue en 1.218 bytes y SHA-256 `83640EAADFF5E33FEB9126F6EEBB9F7960F924F01BB3A1968468014223341222`.
- El prompt íntegro de esta fase se agregó a `prompts_history.txt`, que permanece ignorado por Git.

## Cierre de la estabilización temporal en Windows (2026-11-03)

### Resultado final de la corrección

- La causa quedó localizada en el sandbox de esta sesión: el test original solo necesitaba una ruta existente fuera del proyecto, pero creaba una carpeta aleatoria con `TemporaryDirectory`. El runner podía crearla, pero al limpiar no podía enumerarla ni aplicar el `chmod` de recuperación; no era una fuga de procesos, ni un handle del test, ni un defecto funcional de `validate_workspace`.
- Corrección acotada en `tests/test_codex_integration.py`: la prueba valida directamente la raíz `%TEMP%` existente, confirma el rechazo `INVALID_REQUEST` y comprueba que `subprocess.run` no se invoque. No se crean recursos que requieran limpieza, no se oculta ningún error y no cambió código de producción.
- Suite completa de Codex: `python -m unittest tests.test_codex_integration -v` → **18/18 aprobadas en dos ejecuciones consecutivas**, 0 fallidas, 0 errores, 0 omitidas (0,024 s cada ejecución).
- Memoria: `tests.test_memory_isolation`, `tests.test_memory_persistence`, `tests.test_memory_window`, `tests.test_memory_overflow` → **42/42 aprobadas** (0,771 s).
- Integración combinada actualizada (incluye las suites de memoria, BrainManager, IntentRouter, concurrencia, FileEditor/ChainOrchestrator y Codex) → **226/226 aprobadas**, 0 fallidas, 0 errores, 0 omitidas (14,292 s).
- Suite global final, posterior a la corrección: `python -m unittest discover -s tests -p 'test_*.py'` → **420/420 aprobadas**, código de salida 0, 0 fallidas, 0 errores, 0 omitidas. Resumen de unittest: 23,681 s; tiempo medido de proceso: 26,994 s.

### Integridad, temporales y Git

- Antes y después de las ejecuciones, `data/memory_store.json` conservó **1.218 bytes** y SHA-256 `83640EAADFF5E33FEB9126F6EEBB9F7960F924F01BB3A1968468014223341222`. No se leyó, modificó, eliminó ni restauró su contenido.
- El directorio previo `...\visualizations\...\jarvis-suite-6b589977ceb24ada916d27d471404659` sigue presente. Tiene atributo Directory normal; la ACL pudo consultarse y las entradas visibles eran heredadas sin Deny explícito. La enumeración y la eliminación fallaron con `Access denied`. `psutil` no observó procesos inspeccionables con cwd o archivos abiertos debajo de esa ruta; 294 procesos del sistema no fueron inspeccionables por permisos. Se deja intacto, sin forzar limpieza.
- La reproducción original de esta fase creó `C:\Users\MV\AppData\Local\Temp\tmpg1ctsu7s`; su ACL tampoco pudo consultarse y la enumeración fue denegada. No se encontró proceso inspeccionable asociado. Se deja intacto bajo la misma regla de seguridad.
- `git diff --check` pasó. `prompts_history.txt` y `data/` continúan ignorados. No hay archivos preparados; no se hizo commit, tag ni push por instrucción explícita.
- Cambios de esta fase: `tests/test_codex_integration.py`, `CONTEXT.md` y `prompts_history.txt` (ignorado). Los cinco archivos que ya tenían cambios antes de esta fase, más el trabajo de aislamiento local, se conservaron.
- Estado: criterio de validación de esta fase satisfecho; la eliminación de las dos carpetas residuales no se intentó forzar porque Windows/sandbox denegó acceso y no se pudo verificar su contenido.

## Mini-fase N1–N10 — endurecimiento de memoria y autorización (2026-11-03)

### Cambios en curso para revisión

- `src/brain/chain_orchestrator.py`: bloquea asignaciones públicas de estado distintas al valor actual; `APPLIED` exige SHA-256 posterior, estado e historial de transición; `is_fully_completed()` verifica la evidencia de todas las operaciones. Rechaza sustituir un plan asociado a una ejecución, re-simular un plan con operaciones aplicadas o rollback fallido, y reutilizar IDs de ejecución. Modificaciones sobre archivos existentes requieren SHA-256 inicial con formato válido. El token se valida y consume bajo lock antes de transiciones de ejecución; tokens consumidos/vencidos se purgan. Se corrigió la documentación del grafo (Kahn).
- `src/brain/memory.py`: modo test se activa solo con `JARVIS_ENV=test|testing`; ruta productiva absoluta anclada al proyecto; escritura atómica con temporal exclusivo, flush y fsync del archivo. POSIX sincroniza el directorio; Windows no permite usar el mismo mecanismo estándar de fsync para directorios. Se conserva el original corrupto y se bloquean guardados automáticos; existe recuperación explícita desde JSON válido con copia `.corrupt.<uuid>` verificada antes de reemplazar.
- `skills/file_editor_skill.py`: el despacho público de `execute` rechaza backup/modificación/rollback; las APIs internas llamadas tras autorización por ChainOrchestrator permanecen separadas.
- `tests/test_file_editor_and_chain.py`: la suite usa raíz exclusiva `temp/chain_test_runs/run_<uuid>` y solo elimina la raíz de su corrida; casos de estado falso y re-simulación se actualizan/añaden.
- Pruebas de memoria agregan modo explícito, JSON corrupto, recuperación preservando copia y fallo de reemplazo. Las suites que construyen memoria por defecto declaran `JARVIS_ENV=test`.
- N5 sigue reservado: no se agregó WAL, persistencia de ejecuciones, recuperación al arranque, HMAC persistente, locks interproceso ni Transaction Manager. La interfaz futura debe persistir snapshots aprobados, estados y hashes/evidencias; validar integridad y versión del esquema; recuperar bajo diseño explícito y coordinar transiciones entre procesos antes de permitir reanudación.

### Verificación

- Estado de pruebas: modificaciones recientes aún no verificadas al momento de esta anotación. Se registrarán abajo los resultados realmente obtenidos en orden N1–N10.
- El archivo `data/memory_store.json` no se debe usar como fixture; antes de cada suite se comprobarán tamaño y SHA-256 frente a la referencia documentada.
- Sin commit, tag, push, reset ni limpieza de estado preexistente.

## Cierre de implementación N1–N10 (2026-11-03)

### Estado por hallazgo

- N1: implementado; re-simulación rechazada para planes aplicados o con rollback fallido; sustitución del plan y reutilización del ID de ejecución rechazadas. La selección parcial continúa por aprobación nueva sin re-simular.
- N2: implementado; asignación pública no cambia estados, `APPLIED` requiere autoridad interna posterior a verificación de disco, SHA-256 e historial. `is_fully_completed()` exige evidencia interna válida para cada operación. La transición central sincroniza el estado del plan enviado y el del registro.
- N3: implementado con `JARVIS_ENV=test|testing` explícito. La ruta predeterminada de producción se ancla a la raíz del proyecto; el runner o `openwakeword` por sí solos no activan aislamiento. En modo test la ruta productiva y overrides fuera de la raíz se rechazan.
- N4: implementado; estados de almacenamiento para ausente/cargado/corrupto/ilegible, bloqueo de guardado al detectar corrupción, escritura temporal exclusiva con flush/fsync y replace atómico, y recuperación explícita desde archivo válido tras copia única `.corrupt.<uuid>` con verificación SHA-256. POSIX sincroniza el directorio; Windows no ofrece el mismo fsync de directorio estándar.
- N5: reservado y no implementado. No hay WAL, persistencia de ejecuciones, recuperación al arranque, HMAC persistente, locks interproceso ni Transaction Manager.
- N6: implementado; token, plan, hash y selección se comprueban antes de mutar estado; la invalidación no cambia planes pendientes o completados.
- N7: implementado; tokens vencidos/consumidos se purgan y el consumo es atómico bajo lock.
- N8: implementado; el despacho general `FileEditorSkill.execute` rechaza backup/modificación/rollback. La vía de orquestación interna sigue usando APIs del editor después de autorizar.
- N9: implementado para Chain/FileEditor: raíz única por corrida y cleanup limitado a esa raíz; memoria mantiene archivos de prueba únicos y no elimina la raíz común.
- N10: implementado; documentación del algoritmo Kahn corregida, ruta de memoria anclada y requisitos futuros de N5 documentados.

### Pruebas finales ejecutadas

- N1/N2 dirigidas (dependencia válida, estado falso) y prueba ampliada de identidad/re-simulación: incluidas en la suite Chain final; además casos críticos de token y re-simulación en orden inverso: **5/5 OK**.
- N3/N4 y fallos controlados: `tests.test_memory_isolation` (incluye JSON corrupto/vacío, recuperación, permisos simulados, write/replace fallidos y preservación): pasó como parte de la suite de memoria.
- N6/N7 dirigidas: vencimiento, selección, reutilización, expiración purgada y concurrencia: **5/5 OK** en orden inverso.
- N8: integración de SkillManager y bloqueo de `modify`/`rollback`: **1/1 OK**; lectura sigue disponible.
- N9: dos raíces simultáneas y limpieza aislada: **1/1 OK**.
- ChainOrchestrator/FileEditorSkill completa: `python` + `unittest` con stub de `psutil` para métricas no usadas → **78 pruebas, 0 fallos, 0 errores**.
- Memoria, orden normal: aislamiento, persistencia, ventana y overflow → **51 pruebas, OK**. Orden inverso antes del último caso N4 → **50 pruebas, OK**; los nuevos casos también pasaron en la corrida de 51.
- Integración final: **199 pruebas ejecutadas, 0 fallos, 15 errores**. Suite bloqueada por dependencias ausentes: `edge_tts`, `ollama`, `pygame`, `pystray` (y dependencias de otras suites como `numpy` y `PIL`). `psutil` se sustituyó en memoria solo para ejecutar tests que usan sus imports; eso no valida esas dependencias.
- Global final (`unittest discover`): **306 pruebas ejecutadas, 0 fallos, 25 errores**. No certifica el repositorio completo porque faltan dependencias multimedia/audio/LLM/UI; no se omitieron esos errores.
- `py_compile` de módulos/casos afectados pasó. `git diff --check` pasó; Git solo mostró avisos de conversión LF→CRLF.

### Datos, Git y pendientes

- Antes y después de cada conjunto final, `data/memory_store.json`: **1.218 bytes**, SHA-256 `83640EAADFF5E33FEB9126F6EEBB9F7960F924F01BB3A1968468014223341222`.
- `prompts_history.txt` contiene el prompt íntegro de esta mini-fase y sigue ignorado; `data/memory_store.json` también está ignorado. No se prepararon cambios.
- Se preservaron cambios preexistentes detectados al inicio en `CONTEXT.md`, `config/settings.py`, `main.py`, `src/brain/chain_orchestrator.py`, `src/brain/memory.py`, y pruebas existentes; no hubo reset/clean ni eliminación de residuos previos.
- Git permanece en `main...origin/main`, con cambios locales no preparados; no se creó commit, tag ni push.
- La mini-fase **no queda certificada**: las suites dirigidas pasan y los datos reales siguen intactos, pero el criterio requiere global completo sin errores. Repetir integración/global en el entorno del proyecto con dependencias instaladas antes de consolidar Git.

## Cierre de microfase forense A1–A6 (2026-11-03)

### Resolución

- A1: ChainOrchestrator rechaza volver a simular ejecuciones incompatibles, terminales, con plan ya asociado, operaciones ejecutadas/rollback, o snapshots/runtime ya consumidos. Las validaciones se hacen antes de publicar cambios; se cubrieron re-simulación tras ejecución y fallo, reuso de ID y ausencia de mutaciones parciales.
- A2: la memoria identifica explícitamente las cargas de la ruta primaria. Cargar un archivo alternativo (válido, ausente o corrupto) no borra ni altera el error/bloqueo del almacenamiento primario. Las pruebas fijan `JARVIS_ENV=test`, importan dependencias reales desde el `venv` y evitan leer la memoria productiva.
- A3: recuperación respeta `max_turns`; errores de lectura conservan el contenido actual en memoria y no reemplazan el archivo original. Se verificó el camino de `PermissionError` simulado y la copia recuperada conserva verificación de integridad.
- A4: los tests de memoria usan directorios únicos por corrida/caso y limpian únicamente su propio directorio. La prueba de aislamiento verifica que la ruta temporal de test no sea la ruta real; no se accedió al contenido del archivo productivo.
- A5: autorización y consumo de token se validan antes de las transiciones. Rechazos, hash/selección incorrectos y estados incompatibles mantienen el estado anterior; un token ante estado cancelado no se consume. Se probaron además el camino autorizado y concurrencia.
- A6: `CONTEXT.md` ya no describe `hmac.compare_digest` como mecanismo vigente: documenta la búsqueda exacta de token bajo lock. Se añaden aquí los resultados globales actualizados y comandos reproducibles.

### Verificación realmente ejecutada

- Intérprete usado en todas las verificaciones: `.\venv\Scripts\python.exe` (Python 3.12.8), con `JARVIS_ENV=test`; no se usaron stubs globales ni se instalaron dependencias. El entorno tenía los módulos de runtime requeridos y `pip check` informó `No broken requirements found`.
- Aislamiento de memoria: `python -m unittest tests.test_memory_isolation -v` → **21/21 OK**.
- Chain/FileEditor: `python -m unittest tests.test_file_editor_and_chain -v` → **82/82 OK**.
- Grupo memoria: `tests.test_memory_isolation`, `tests.test_memory_persistence`, `tests.test_memory_window` y `tests.test_memory_overflow` → **51 pruebas OK**; también se ejecutaron en orden inverso.
- Integración combinada de Chain/FileEditor, memoria, misión, concurrencia, IntentRouter y Codex → **219/219 OK**, 0 fallos, 0 errores.
- Suite global final, `unittest discover -s tests -p "test_*.py"` con salida capturada para evitar ruido de consola → **442/442 OK**, 0 fallos, 0 errores, 0 omitidas (50,196 s en el resumen del runner). Una ejecución global adicional también se lanzó; la corrida global resumida confirmó el resultado completo.
- `git diff --check` pasó. Git reportó solo avisos de normalización LF→CRLF.
- Para repetir memoria desde PowerShell: `$env:JARVIS_ENV="test"; .\venv\Scripts\python.exe -m unittest tests.test_memory_isolation tests.test_memory_persistence tests.test_memory_window tests.test_memory_overflow`.
- Para repetir toda la suite: `$env:JARVIS_ENV="test"; .\venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"`.

### Integridad del archivo real y estado de Git

- Antes/después de las suites, `data/memory_store.json` existía y conservó **1.218 bytes** y SHA-256 `83640EAADFF5E33FEB9126F6EEBB9F7960F924F01BB3A1968468014223341222`. Solo se consultaron existencia, tamaño y hash; nunca se leyó ni modificó su contenido.
- El prompt completo se agregó a `prompts_history.txt`; el archivo sigue ignorado por Git. `data/` también continúa ignorado.
- Se conservaron los cambios locales preexistentes. No se hizo reset, clean, stage, commit, tag ni push.
- Estado A1–A6: **corregido y verificado por pruebas unitarias y suite global completa**. Esta conclusión cubre A1–A6 y no cambia el estado histórico de hallazgos reservados en fases anteriores.

## Implementación de N5: Recovery & Transaction Manager (2026-11-03)

### Arquitectura y componentes implementados

- **`src/brain/transaction_manager.py` (nuevo módulo):**
  - `TransactionState` y `ALLOWED_TRANSACTION_STATE_TRANSITIONS`: máquina de estados estricta (`PLANNED`, `PREPARING`, `APPLYING`, `COMMITTED`, `ROLLING_BACK`, `ROLLED_BACK`, `RECOVERY_PENDING`, `RECOVERED`, `ABORTED`, `CORRUPTED`) con bloqueo de retrocesos ilegales mediante `InvalidTransactionStateError`.
  - `OperationJournalStage`: etapas por operación (`PENDING`, `INTENT_LOGGED`, `BACKUP_VERIFIED`, `APPLIED`, `FAILED`, `ROLLED_BACK`, `ROLLBACK_FAILED`).
  - `GlobalTransactionLock`: lock exclusivo global que combina cerrojo de hilos por ruta canónica (`threading.Lock`), reentrancia controlada para la misma instancia/hilo y bloqueo interproceso en Windows mediante `msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)` sobre `data/transactions/global_transaction.lock` con timeout configurable (`TRANSACTION_LOCK_TIMEOUT_SEC = 5.0`) y `TransactionLockTimeoutError`.
  - `TransactionJournal`: persistencia **WAL-first** (`tx_<transaction_id>.wal.jsonl`) con `flush()` + `os.fsync()` en cada `WalEvent` encadenado por SHA-256 (`prev_event_hash` → `event_hash`), seguida de actualización atómica de snapshot (`tx_<transaction_id>.snapshot.tmp.<uuid>` → `flush()` + `os.fsync()` → `os.replace()`).
  - Validación criptográfica y estructural de la cadena WAL: verificación de `record_hash` del snapshot, reconstrucción desde génesis o replay de cola WAL posterior a `last_wal_seq`, detección estricta de `DUPLICATE_SEQ`, `MISSING_SEQ`, `OUT_OF_ORDER_SEQ`, `FOREIGN_TRANSACTION_ID`, `SNAPSHOT_INCOMPATIBLE`, y distinción entre `TRUNCATED_FINAL_WAL_LINE` (recuperable ignorando la línea final incompleta tras crash de escritura) e `INTERIOR_WAL_CORRUPTION` (transacción marcada como `CORRUPTED`).
  - Cadena de custodia SHA-256 multi-operación sobre el mismo archivo (`H0 -> H1 -> H2 -> H3`): `TransactionRecord.rebuild_file_hash_chains()` y validación previa al commit garantizando que `expected_pre_sha256[op_i] == actual_post_sha256[op_{i-1}]`.
  - `TransactionManager`: coordinación transaccional (`begin_transaction`, `log_operation_intent`, `log_backup_verified`, `log_operation_applied`, `log_operation_failed`, `log_operation_rolled_back`, `log_operation_rollback_failed`, `commit_transaction`) y motor de recuperación idempotente (`recover_transaction`, `recover_pending_transactions`) con `RecoveryReport`.

- **Integración por composición en `skills/file_editor_skill.py`, `src/brain/chain_orchestrator.py`, `config/settings.py` y `main.py`:**
  - `config/settings.py`: añadidas `TRANSACTION_JOURNAL_DIR = BASE_DIR / "data" / "transactions"` y `TRANSACTION_LOCK_TIMEOUT_SEC = 5.0`.
  - `skills/file_editor_skill.py`: `modify_ini_or_text()` incorpora hooks opcionales (`on_intent_prepared`, `on_backup_verified`, `on_after_atomic_replace`) retrocompatibles que permiten registrar la intención y el backup verificado en el WAL antes del `os.replace()` atómico.
  - `src/brain/chain_orchestrator.py`:
    - Integra `TransactionManager` por composición sin romper constructores ni firmas públicas existentes.
    - `execute_approved_plan()` ejecuta el protocolo transaccional de 14 pasos bajo `GlobalTransactionLock`, registrando cada etapa en el WAL, validando la cadena `H0 -> H1 -> H2` y revirtiendo en orden **LIFO** (`reversed(...)`) ante fallos intermedios.
    - `rollback_execution()` soporta **rehidratación desde disco** (`rehydrate_execution()`) cuando una ejecución `COMMITTED` ya no está en `self._executions` tras reiniciar el proceso, reconstruyendo los `BackupRecord` verificados y ejecutando el rollback transaccional LIFO completo.
    - `recover_pending_transactions()` escanea journals incompletos (`PREPARING`, `APPLYING`, `ROLLING_BACK`, `RECOVERY_PENDING`), restaura en orden LIFO todas las operaciones cuyo `current_disk_sha256 != expected_pre_sha256` verificando el SHA-256 del `.bak` antes y después de restaurar, elimina temporales `.jarvis_tmp` huérfanos, y rehidrata el estado en memoria del orquestador.
  - `main.py`: `_ensure_orchestrator()` invoca `self.chain_orchestrator.recover_pending_transactions()` de forma perezosa al inicializar el orquestador para `/optimizar` o `/rollback`, sin penalizar el arranque de voz.

### Verificación realmente ejecutada (N5)

- Intérprete usado: `.\venv\Scripts\python.exe` (Python 3.12.8) con `$env:JARVIS_ENV="test"`.
- Suite nueva N5 (`tests/test_transaction_recovery.py`, casos **T01–T24**):
  - `$env:JARVIS_ENV="test"; .\venv\Scripts\python.exe -m unittest tests.test_transaction_recovery -v` → **24/24 OK** (24,415 s).
- Suite `ChainOrchestrator` / `FileEditorSkill`:
  - `$env:JARVIS_ENV="test"; .\venv\Scripts\python.exe -m unittest tests.test_file_editor_and_chain -v` → **82/82 OK** (30,402 s).
- Suite de memoria (`test_memory_isolation`, `test_memory_persistence`, `test_memory_window`, `test_memory_overflow`):
  - `$env:JARVIS_ENV="test"; .\venv\Scripts\python.exe -m unittest tests.test_memory_isolation tests.test_memory_persistence tests.test_memory_window tests.test_memory_overflow -v` → **55/55 OK** (17,249 s).
- Suite global completa:
  - `$env:JARVIS_ENV="test"; .\venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"` → **466/466 OK**, 0 fallos, 0 errores (87,821 s).

### Integridad del almacenamiento real y limitaciones conocidas

- `data/memory_store.json` verificado antes y después de la implementación y las pruebas: **1.218 bytes**, SHA-256 `83640EAADFF5E33FEB9126F6EEBB9F7960F924F01BB3A1968468014223341222` (intacto, sin lecturas de contenido ni modificaciones).
- Limitación conocida de plataforma: Windows no expone `os.fsync()` sobre descriptores de directorio como POSIX; la durabilidad se garantiza mediante `flush()` + `os.fsync()` sobre cada archivo `.wal.jsonl`, `.snapshot.tmp` y `.jarvis_tmp` antes de `os.replace()`, combinado con recuperación WAL-first al arranque.



## Implementación de Paso #3.A: DocumentExportSkill y Modo Estudio (2026-11-03)

### 1. Arquitectura y Estructura de Módulos

Se implementó el subsistema modular de exportación documental desacoplado de proveedores de IA y renderizadores externos:

- `skills/document_models.py`: Modelo Documental Intermedio (IR) con inmutabilidad profunda.
  - Dataclasses congeladas (`frozen=True`): `DocElement`, `Heading` (H1-H6), `Paragraph` (bold/italic), `ListItem` (level >= 0), `ListBlock` (ordenado/viñetas), `Table` (encabezados, filas coherentes, caption), `CodeBlock` (código con lenguaje), `Callout` (NOTE, WARNING, TIP, IMPORTANT), `Citation` (source_id, texto, url), y `Document` (título, autor, metadatos inmutables, elementos).
  - Normalización en `__post_init__`: secuencias a tuplas inmutables y metadatos a tuplas ordenadas de pares clave-valor (`metadata_dict` para acceso cómodo), con hashabilidad completa.
- `skills/document_parser.py`: Parser estructurado agnóstico a bibliotecas externas.
  - Transforma Markdown estándar, bloques de código cercados (` ``` ` o ` ~~~ `), tablas con separadores, callouts de estilo GitHub/Obsidian (`> [!NOTE]`), citas y referencias bibliográficas (`[^1]: ...`), listas anidadas por indentación y unicode íntegro a instancias de `Document`.
  - Valida contenido no vacío y límite configurable de longitud (predeterminado 5 MB).
- `skills/document_templates.py`: Plantillas del Modo Estudio.
  - Base abstracta `BaseDocumentTemplate` y 8 plantillas pedagógicas especializadas:
    1. `StructuredNotesTemplate` (`structured_notes` / `apuntes`): marco teórico, desarrollo, puntos críticos y fuentes.
    2. `ExecutiveSummaryTemplate` (`executive_summary` / `resumen`): contexto, síntesis y recomendaciones.
    3. `StudyGuideTemplate` (`study_guide` / `guia`): hoja de ruta, contenido y checklist de autoevaluación.
    4. `QASheetTemplate` (`qa_sheet` / `preguntas`): pares pregunta-respuesta para repaso activo.
    5. `ComparativeTableTemplate` (`comparative_table` / `comparativa`): matrices de contraste y criterios.
    6. `GlossaryTemplate` (`glossary` / `glosario`): compilación terminológica y definiciones.
    7. `QuizTemplate` (`quiz` / `cuestionario`): evaluaciones formativas y clave de autoevaluación.
    8. `TechnicalReportTemplate` (`technical_report` / `informe`): alcance, ingeniería, resultados y conclusiones.
  - Registro dinámico `get_template()` con soporte de alias en español y preservación estricta de fuentes originales sin invención de citas.
- `skills/markdown_renderer.py`: Renderizador Markdown.
  - Genera Markdown estándar y limpio compatible con Obsidian y GitHub, codificado estrictamente en UTF-8.
- `skills/word_renderer.py`: Renderizador Microsoft Word (.docx).
  - Utiliza `python-docx` con **carga diferida estricta** (lazy import).
  - Estilos tipográficos: portada con título y metadatos, encabezados jerárquicos, tablas con bordes ('Table Grid') y encabezados destacados con sombreado, bloques de código monoespaciados (Consolas) con fondo gris, callouts temáticos (azul, verde, ámbar, rojo) y listas con sangría.
  - Si `python-docx` no está instalado, no interrumpe el arranque ni la exportación Markdown.
- `skills/document_export_skill.py`: Coordinador `DocumentExportSkill` (`BaseSkill`).
  - Nombre oficial: `'document_export'`.
  - Parámetros: `content`, `format` (`'md'`, `'docx'`, `'both'`), `title`, `output_path`, `mode` (`'general'`, `'study'`), `template`, `overwrite`.
  - Seguridad Windows: anti-traversal, anti-UNC, anti-symlink/junction fuera de raíces autorizadas, bloqueo de nombres reservados (`CON`, `PRN`, `AUX`, `NUL`, etc.) y prefijos del sistema.
  - Concurrencia y colisiones: reserva atómica con `os.O_CREAT | os.O_EXCL` para generación determinista de sufijos (`doc_(1).docx`, `doc_(2).docx`) libre de condiciones TOCTOU, y reemplazo atómico mediante archivos temporales en el mismo directorio.
- `src/tools/skill_manager.py`: Integración oficial de la skill en el catálogo nativo con soporte unificado para ejecución vía diccionario o kwargs: `SkillManager.execute_skill("document_export", params)`.

### 2. Dependencias y Configuración

- Dependencia añadida a `requirements.txt`: `python-docx` (instalada versión 1.2.0 con `lxml` 6.1.3).
- Raíces autorizadas predeterminadas: `<BASE_DIR>/exports/`. Soporta ampliación mediante `JARVIS_EXPORT_ROOTS`.

### 3. Verificación y Pruebas Realmente Ejecutadas

- Compilación estática:
  `python -m py_compile skills/document_models.py skills/document_parser.py skills/document_templates.py skills/markdown_renderer.py skills/word_renderer.py skills/document_export_skill.py` -> 0 errores.
- Suite exhaustiva específica (`tests/test_document_export_skill.py`):
  `python -m unittest tests.test_document_export_skill -v` -> **25/25 pruebas aprobadas (100% OK)** en 0.427s.
  Cubre: renderizado MD, DOCX, tablas multicelda, código, listas anidadas, callouts, Unicode, vacíos, malformados, path traversal, rutas no autorizadas, UNC, symlinks, prevención de sobrescritura, sufijos secuenciales, concurrencia de hilos, limpieza de temporales, fallos de escritura, disponibilidad opcional de python-docx, integración SkillManager, arranque y carga diferida, independencia de IA, preservación de citas, formato both, e integridad de memoria.
- Suite global completa:
  `python -m unittest discover -s tests -p "test_*.py"` -> **491/491 pruebas aprobadas (100% OK)** en ~85s. Sin regresiones en los 466 tests preexistentes.
- Formato y estilo: `git diff --check` -> 0 errores.
- Arranque instantáneo: `python main.py --help` -> OK.

### 4. Integridad de Memoria y N5

- `data/memory_store.json`: verificado mediante metadatos antes y después del paso. Tamaño: **1.218 bytes**, SHA-256: `83640eaadff5e33feb9126f6eebb9f7960f924f01bb3a1968468014223341222` (intacto, sin lecturas de contenido ni escrituras).
- N5 (`src/brain/transaction_manager.py`): intacto y preservado sin modificaciones.

### 5. Limitaciones Conocidas

- La creación de enlaces simbólicos en Windows requiere privilegios elevados o modo desarrollador habilitado; la resolución física segura de rutas canónicas (`resolve()` y `realpath()`) previene el escape de raíces autorizadas independientemente del tipo de enlace.

## Corrección forense del Paso #3.A (H-01 a H-08) — verificación final (2026-11-03)

- Prompt íntegro registrado en `prompts_history.txt` bajo el paso “Corrección forense Paso 3.A”.
- Línea base Git al iniciar: `main` en `bd5edeb`, alineada con `origin/main`; había cambios locales ajenos en configuración, ChainOrchestrator, memoria y pruebas, incluido el módulo no versionado `src/brain/transaction_manager.py`. Esos archivos se preservan y `transaction_manager.py` no se modificó.
- Cambios en curso limitados a `skills/document_export_skill.py`, `skills/document_templates.py`, `skills/document_parser.py`, `skills/markdown_renderer.py`, `skills/word_renderer.py`, `tests/test_document_export_skill.py` y este documento.
- H-01: se conserva la ruta léxica autorizada junto con su destino físico inicial; cada uso vuelve a comprobar con `lstat` atributos reparse/symlink a través de ancestros y destino, pertenencia léxica y física, raíz y ruta canónica antes de reservar y antes del reemplazo. Rutas relativas preservan subdirectorios; se rechazan drive-relative, root-relative sin unidad, traversal, UNC, componentes reservados y caracteres de ruta inválidos.
- H-02/H-03/H-04: `overwrite` requiere `bool` real; los componentes reservados consideran espacios antes de la extensión; Markdown renderiza antes de reservar; los temporales y reservas fallidos solo se limpian si siguen siendo los archivos creados por la operación.
- H-05: `format="both"` normaliza `.md`/`.docx` a un nombre base común y reserva ambos con `O_EXCL`; si hay colisión en cualquiera, avanza ambos sufijos. Las respuestas parciales se mantienen explícitas.
- H-06/H-07: el parser guarda texto semántico para negrita/cursiva y URLs de citas en un solo campo; Markdown y Word las muestran una vez. Word omite un primer H1 que coincide con el título de portada. `mode` y `template` inválidos se rechazan.
- H-08: escrituras temporales hacen `flush()` y `os.fsync()` antes del reemplazo; solo se reintentan cuatro veces los errores Windows 5/32, con esperas acumuladas breves (0,30 s). El reemplazo sigue atómico en el mismo directorio.
- Riesgo residual H-01: las APIs Python usadas resuelven y luego abren/reemplazan por nombre. Una carrera local que sustituya un directorio después de la última validación y antes de la operación del sistema todavía no se elimina por completo; lograrlo requiere apertura relativa a handles Windows validados y cambios de arquitectura. WordRenderer también acepta una ruta por nombre. No se declara protección TOCTOU absoluta.
- Reproducción pedida para `O_CREAT|O_EXCL` sobre symlink: no pudo completarse porque Windows negó crear el enlace con WinError 1314 (privilegio no concedido). No se cambió la semántica de reserva basándose en esa reproducción; las pruebas reales de symlink se marcan omitidas con esa razón. Las junctions NTFS sí pudieron crearse y probarse.
- Verificación solicitada con pytest: `.\venv\Scripts\python.exe -m pytest tests/test_document_export_skill.py -v` no pudo iniciar porque `pytest` no está instalado (`No module named pytest`). No se instaló ninguna dependencia. Sustitución con el runner estándar del archivo: `.\venv\Scripts\python.exe -m unittest tests.test_document_export_skill -v` → **41 pruebas, 0 fallos, 0 errores, 3 omitidas** (las tres pruebas de symlink por WinError 1314; las cuatro pruebas de junction sí pasaron).
- Suite global final posterior al último cambio: `.\venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"` → **507 pruebas, 0 fallos, 0 errores, 3 omitidas**. Las tres omitidas son solo la creación de symlinks que el sistema rechazó por privilegio. Al finalizar apareció un `RuntimeWarning` de `TextToSpeech._synthesize_edge_tts` (“coroutine ... was never awaited”); no causó fallo y está fuera de los archivos de esta fase.
- `py_compile` de los cinco módulos modificados y la suite pasó; `git diff --check` pasó con avisos normales de conversión LF→CRLF. La comprobación de arranque `main.py --help` pasó. Un subprocess con `JARVIS_ENV=test` inicializó `SkillManager` y comprobó que `docx` y `lxml` no estaban en `sys.modules`.
- Antes y después de la suite global, `data/memory_store.json` mantuvo existencia, tamaño **1.218 bytes** y SHA-256 `83640EAADFF5E33FEB9126F6EEBB9F7960F924F01BB3A1968468014223341222`; solo se calcularon metadatos. `src/brain/transaction_manager.py` no recibió cambios de esta tarea.
- El primer sondeo de symlink usó `TemporaryDirectory` en `%TEMP%`; la escritura recibió `PermissionError` y la limpieza automática encontró `WinError 5`. Residuo propio: `C:\Users\MV\AppData\Local\Temp\jarvis_link_probe_6ot4n5an`; se deja intacto porque el sandbox denegó acceso para enumerarlo/limpiarlo. Los sondeos posteriores usaron directorios únicos bajo `temp/` del proyecto y los limpiaron.
- Estado Git antes de commit: cambios de esta tarea en los cinco módulos `skills/`, `tests/test_document_export_skill.py` y `CONTEXT.md`; cambios preexistentes en otros archivos siguen separados. El prompt está íntegro en `prompts_history.txt` ignorado. GitSafetyGuard y la auditoría de secretos se ejecutarán con candidatos explícitos antes del commit.
- Conclusión H-01: la implementación rechazó redirecciones por junction en directorios, raíz reemplazada, ancestro reemplazado y raíz adicional; los tres escenarios de symlink quedaron omitidos porque el Windows de esta sesión denegó su creación. El chequeo previo de `O_EXCL` sobre symlink tuvo la misma limitación. Se documenta como límite de verificación de plataforma, no como prueba aprobada. Las llamadas por nombre mantienen un riesgo TOCTOU residual descrito arriba; no existe garantía absoluta.

---

## Estado Actual del Paso #3.B: `WebExtractionSkill` (2026-11-12)

### 1. Arquitectura y Módulos Implementados

- Prompt exacto registrado en `prompts_history.txt` con fecha y separador oficial (`PASO #3.B`).
- Línea base previa preservada sin modificaciones en archivos preexistentes sucios de N5/memoria ni en los módulos cerrados del Paso #3.A.
- Nuevos módulos desacoplados creados en `skills/`:
  - `skills/web_config.py`: `WebExtractionConfig` inmutable con valores conservadores por defecto (timeouts, `max_response_bytes=2 MiB`, `max_decompressed_bytes=4 MiB`, `max_pages=5`, `max_redirects=5`, `max_links=100`, `max_normalized_chars=100_000`, `max_concurrent_connections=3`, `allowed_content_types={'text/html', 'application/xhtml+xml'}`), carga vía `config.settings` (`WEB_EXTRACT_*`) y validación estricta de `additional_ports` / `parse_additional_ports_env()` para impedir la desactivación accidental de restricciones de puertos.
  - `skills/web_models.py`: dataclasses congelados e independientes de todo motor IA (`ExtractionStatus`, `WebIssue`, `WebHeading`, `WebParagraph`, `WebList`, `WebSection`, `WebLink`, `WebMetadata`, `WebSourceDocument`, `SearchResult`, `WebExtractionReport`), con referencia `source_url` por fragmento, separación estricta entre `published_at` (solo si la fuente lo declara) y `extracted_at` (UTC ISO-8601), y serialización `.to_dict()`.
  - `skills/web_security.py`: `WebNetworkPolicy` y `validate_ip_address` basados en `ipaddress` y `socket`:
    - Esquemas exclusivamente `http` / `https`; rechazo de credenciales embebidas, barras invertidas en autoridad, caracteres de control, `%` en host y normalización IDNA anti-homóglifos.
    - Política estricta de puertos (`80` HTTP / `443` HTTPS por defecto; puertos adicionales únicamente mediante validación explícita de enteros `1..65535`).
    - Bloqueo de `localhost`, `*.local`, `*.internal`, `*.home.arpa`, etiquetas simples (`wpad`, `isatap`, etc.), formatos IP alternativos (decimal entero, hexadecimal, octal, abreviado) y toda dirección IPv4/IPv6 no global o reservada (`is_private`, `is_loopback`, `is_link_local`, `is_multicast`, `is_reserved`, `is_unspecified`, `not is_global`), incluyendo desempaquetado recursivo de IPv4-mapped IPv6 (`::ffff:127.0.0.1`), 6to4, Teredo, CGNAT y NAT64.
    - Resolución única por conexión (`socket.getaddrinfo`) que rechaza todo el destino si **cualquier** IP resuelta incumple la política.
  - `skills/web_fetcher.py`: `WebFetcher`, `_PinnedHTTPConnection` y `_PinnedHTTPSConnection`:
    - Vinculación DNS-conexión real: `connect()` resuelve y valida todas las IPs una sola vez y abre el socket TCP directamente sobre `target_ep.sockaddr` (IP validada), preservando el hostname original para `Host`, SNI TLS (`server_hostname`) y verificación estricta (`ssl.CERT_REQUIRED` + `check_hostname=True`), rechazando contextos TLS permisivos con `ValueError`.
    - Control manual de redirecciones (`max_redirects`), revalidando esquema, puerto, host y DNS en cada salto, con `Connection: close` para impedir la reutilización ciega entre destinos o reintentos ante un posible DNS rebinding.
    - Lectura incremental acotada por chunks (`max_response_bytes`) y descompresión incremental acotada (`_IncrementalBoundedDecompressor` sobre `zlib.decompressobj` con `max_length`) para abortar bombas `gzip`/`deflate` antes de expandirlas en memoria.
  - `skills/web_parser.py`: `WebContentExtractor` sobre `html.parser.HTMLParser` (biblioteca estándar): decodificación segura (BOM -> HTTP header -> `<meta charset>` -> UTF-8 -> fallback con reemplazo y `WebIssue`), descarte de scripts/estilos/comentarios/SVG/iframes/nav/aside/footer/banners de cookies, extracción de contenido principal (`main`/`article`) cuando aporta bloques reales, metadatos `<meta>` / `<title>` / `<time>` / JSON-LD sin inventar ausentes, normalización Unicode NFC/espacios/caracteres invisibles, deduplicación y acotado de enlaces y `normalized_text`.
  - `skills/web_search_provider.py`: contrato `SearchProvider`, `SearchResponse`, `normalize_search_results()` e implementación `UnconfiguredSearchProvider`. Se comprobó que `skills.system_skills.WebSearchSkill` únicamente invoca `webbrowser.open(...)` sin devolver URLs estructuradas; por ello, las consultas (`query`) sin proveedor externo informan honestamente `SEARCH_PROVIDER_NOT_CONFIGURED` sin scraping frágil ni simulación de resultados, mientras la extracción directa (`url` / `urls`) permanece operativa.
  - `skills/web_document_converter.py`: adaptador desacoplado `WebDocumentConverter` (`to_markdown`, `to_document`, `to_export_params`, `export`) compatible con las interfaces públicas de `DocumentExportSkill` (Paso #3.A), conservando procedencia multi-fuente, URL original y final, citas `[^src-N]`, errores parciales, neutralización anti-spoofing de bloques Markdown y enlace de referencias separado de enlaces relevantes.
  - `skills/web_extraction_skill.py`: `WebExtractionSkill(BaseSkill)` (`name='web_extraction'`), con ejecución acotada de lotes (`ThreadPoolExecutor` limitado por `max_pages` y `max_concurrent_connections`), carga perezosa real de `WebDocumentConverter` (sin importar `python-docx`, `lxml`, audio ni clientes LLM durante su inicialización o extracción) y contrato `execute(params/kwargs)` con formatos `'summary'`, `'json'` y `'markdown'`.
- Integración en archivos limpios del sistema:
  - `src/tools/skill_manager.py`: registra `WebExtractionSkill()`.
  - `src/brain/intent_router.py`: agrega `'web_extraction': 'WebExtractionSkill'` a `SKILL_CLASS_BY_TOOL_NAME`.
  - `config/tools_schema.py`: incorpora el esquema de parámetros para `'web_extraction'`.

### 2. Verificación y Pruebas Realmente Ejecutadas (Paso #3.B)

- Compilación estática (`.\venv\Scripts\python.exe -m py_compile` sobre los 8 módulos `skills/web_*.py`, `src/tools/skill_manager.py`, `src/brain/intent_router.py`, `config/tools_schema.py` y `tests/test_web_extraction_skill.py`): **0 errores**.
- Suite específica (`$env:JARVIS_ENV='test'; .\venv\Scripts\python.exe -m unittest tests.test_web_extraction_skill -v`): **16 pruebas ejecutadas, 16 aprobadas (0 fallos, 0 errores, 0 omitidas)**. Incluye transporte TCP/HTTP/TLS local con `ThreadingHTTPServer`, comprobación empírica de IP pinneada (`93.184.216.34`), SNI real (`docs.example.test`), redirecciones SSRF, bucle de redirecciones, DNS rebinding entre intentos, streams chunked sin `Content-Length`, `Content-Length` engañosos, bombas `gzip`, timeouts, `SkillManager`, `WebDocumentConverter` + `DocumentExportSkill` (MD y DOCX en modo estudio) y subproceso de aislamiento perezoso sin `docx`, `lxml`, `faster_whisper`, `sounddevice` ni `skills.web_document_converter` en `sys.modules`.
- Regresión Paso #3.A (`tests.test_document_export_skill`): **41 pruebas ejecutadas, 0 fallos, 0 errores, 3 omitidas** (las 3 preexistentes de symlink por `WinError 1314`).
- Suite global completa tras el último cambio (`$env:JARVIS_ENV='test'; .\venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"`): **523 pruebas ejecutadas en 103.435s, OK (0 fallos, 0 errores, 3 omitidas)**.
- Comprobación CLI rápida (`$env:JARVIS_ENV='test'; .\venv\Scripts\python.exe main.py --help`): código 0.
- Integridad de memoria y módulos cerrados:
  - `data/memory_store.json` intacto (solo metadatos): **1.218 bytes**, SHA-256 `83640EAADFF5E33FEB9126F6EEBB9F7960F924F01BB3A1968468014223341222`.
  - Módulos cerrados del Paso #3.A y `src/brain/transaction_manager.py` (N5) sin modificaciones.

### 3. Limitaciones Conocidas y Hallazgos de Entorno

- **Proveedor de búsqueda programático:** no existe uno configurado por defecto en el repositorio (`UnconfiguredSearchProvider`), por lo que el modo `query` requiere conectar una implementación concreta de `SearchProvider`.
- **Interceptación TLS local del antivirus del host (observada empíricamente):** en este entorno Windows, Avast Web/Mail Shield intercepta conexiones TLS loopback en sockets OS sustituyendo CA efímeras desconocidas por `CN=Avast Web/Mail Shield Untrusted Root`. Por ello, las pruebas de socket TLS real verificaron la conexión a IP pinneada, el envío de SNI real y el rechazo estricto por verificación TLS (`TLS_ERROR`), mientras que la aceptación positiva de certificado bajo `CERT_REQUIRED` + `check_hostname=True` con la CA efímera de prueba se verificó en memoria mediante `ssl.SSLContext.wrap_bio` (`OpenSSL MemoryBIO`).
- **Alcance de red externa:** todas las pruebas de transporte ejecutadas fueron reproducibles en red local controlada (servidores TCP/TLS locales efímeros), sin consultas hacia Internet público en vivo.

---

## Hardening de Seguridad del Paso #3.B (`WebExtractionSkill`) — Remediación F-01 a F-07 (2026-11-12)

### 1. Hallazgos Resueltos y Archivos Modificados

- Prompt completo registrado en `prompts_history.txt` (`HARDENING DE SEGURIDAD DEL PASO #3.B (WEB EXTRACTION SKILL)`).
- Archivos modificados en este paso:
  - `skills/web_document_converter.py`: remediación **F-01** (`_MD_BLOCK_SPOOF_RE` actualizado a `\[\^?[^\]]+\]:` para neutralizar definiciones de citas con y sin `^` sin afectar tablas Markdown; `_sanitize_markdown_url()` para percent-encoding de paréntesis, corchetes, espacios y delimitadores en URLs antes de interpolar en enlaces Markdown).
  - `skills/web_parser.py`:
    - Remediación **F-01**: percent-encoding de caracteres peligrosos para enlaces Markdown (`_URL_UNSAFE_CHAR_MAP`) y rechazo de caracteres de control en `normalize_extracted_url()`.
    - Remediación **F-02**: separación de `_head_depth` y `_body_started` respecto de `_ignored_depth` en `_StructuredHTMLParser`; bloqueo de inyección/sobrescritura de `<title>`, `<meta>`, `<link rel="canonical">`, `<base href>` y JSON-LD desde `<template>`, `<noscript>`, nodos `hidden`/`aria-hidden`, zonas de ruido (`footer`, `nav`, `aside`) y `<body>`; restricción de `<base href>` a `<head>` con validación same-origin (`_has_same_origin` y advertencia `CROSS_ORIGIN_BASE_IGNORED`).
    - Remediación **F-04 (Parte A)**: aplicación de `max_normalized_chars` como presupuesto compartido al construir `headings`, `paragraphs`, `lists`, `sections` y `normalized_text`.
    - Remediación **F-05**: filtrado de elementos ocultos por CSS inline (`_HIDDEN_STYLE_RE`, `_is_hidden_style`) y limpieza de controles bidireccionales Unicode y bloque Unicode Tags (`\U000e0000-\U000e007f`).
  - `skills/web_security.py`:
    - Remediación **F-03 (Parte B)**: atributos `final_url` y `redirect_chain` en `WebSecurityError`.
    - Remediación **F-04 (Parte B)**: desempaquetado y validación recursiva de direcciones IPv4 embebidas en IPv6 ISATAP (`::5efe:w.x.y.z` / `::0200:5efe:w.x.y.z`) y SIIT (`::ffff:0:w.x.y.z`).
  - `skills/web_fetcher.py`:
    - Remediación **F-03**: verificación de `_decompressor.eof` y ausencia de colas residuales tras `flush()` en `_IncrementalBoundedDecompressor.finish()`, rechazando streams `gzip`/`deflate` truncados con `UnsupportedEncodingError` sin rechazar respuestas vacías válidas; preservación de `final_url` y `redirect_chain` en `WebFetchError` y `WebSecurityError` tras redirecciones.
    - Remediación **F-06 (Parte A)**: no reintentar excepciones genéricas no tipadas en `_fetch_single_hop_with_retries()` y verificar presupuesto de `deadline` antes de reintentar `WebFetchError` transitorios.
  - `skills/web_models.py`: remediación **F-05** (`"trust_level": "untrusted_external_content"` en `WebSourceDocument.to_dict()` y `WebExtractionReport.to_dict()`).
  - `skills/web_extraction_skill.py`:
    - Remediación **F-03**: preservación de `final_url` y `redirect_chain` en `WebSourceDocument` y `WebIssue` cuando falla un salto posterior a redirección.
    - Remediación **F-05**: neutralización de prefijos de suplantación de rol/acción JSON en `_format_human_summary()` y método `build_llm_envelope()` que separa directivas del sistema respecto de `untrusted_data`.
    - Remediación **F-06 (Parte B)**: deduplicación canónica conservadora (`_canonical_url_dedup_key`) antes de aplicar `max_pages`.
  - `src/brain/intent_router.py`:
    - Remediación **F-05**: directiva explícita de aislamiento de datos externos en `_build_system_prompt()`.
    - Remediación **F-06 (Parte C)**: fast-path determinista en `detect_local_skill()` para órdenes explícitas de extracción web con URLs `https?://`.
  - `tests/test_web_extraction_skill.py`: clase `TestSecurityHardeningF01ToF06` con 6 pruebas integrales de regresión (`test_f01` a `test_f06`).
  - `docs/audits/AUDIT_PASO_3B_WEB_EXTRACTION_SKILL.md` y `docs/audits/REMEDIATION_PASO_3B_WEB_EXTRACTION_SKILL.md`: informe de auditoría independiente e informe de remediación técnica (incluyendo **F-07** documentado como limitación del entorno Windows/Avast Web Shield sin debilitar TLS).

### 2. Verificación Realmente Ejecutada

- `.\venv\Scripts\python.exe -m py_compile` sobre todos los módulos modificados y pruebas: **0 errores**.
- `.\venv\Scripts\python.exe -m unittest -v tests.test_web_extraction_skill`: **22 pruebas ejecutadas, 22 aprobadas (0 fallos, 0 errores)**.
- `.\venv\Scripts\python.exe -m unittest -v tests.test_document_export_skill`: **41 pruebas ejecutadas, 38 aprobadas, 3 omitidas por privilegios de symlink en Windows (0 fallos, 0 errores)**.
- Suites N5 (`tests.test_memory_isolation`, `tests.test_transaction_recovery`, `tests.test_file_editor_and_chain`, `tests.test_intent_router_chaos`): **100% OK**.
- Suite global (`.\venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"`): **529 pruebas ejecutadas en 96.895s — OK (0 fallos, 0 errores, 3 omitidas)**.
- `.\venv\Scripts\python.exe main.py --help`: **OK (código 0)**.
- `data/memory_store.json`: verificado solo por metadatos (**1.218 bytes**, SHA-256 `83640EAADFF5E33FEB9126F6EEBB9F7960F924F01BB3A1968468014223341222`, intacto).
- Los 16 archivos preexistentes sucios/no rastreados de N5/memoria y los módulos cerrados del Paso #3.A permanecen intactos.

---

## Hardening Integral Definitivo del Paso #3.B (`WebExtractionSkill`) — Cierre Post-Auditoría Independiente (2026-11-12)

### 1. Hallazgos Cerrados y Arquitectura de Confianza End-to-End

- Prompt registrado íntegramente en `prompts_history.txt` (`HARDENING INTEGRAL DEFINITIVO DEL PASO #3.B`).
- Informe técnico completo generado en `docs/audits/REMEDIATION_PASO_3B_FINAL.md` (conservando `docs/audits/AUDIT_PASO_3B_WEB_EXTRACTION_SKILL.md`, `docs/audits/REMEDIATION_PASO_3B_WEB_EXTRACTION_SKILL.md` y `docs/audits/INDEPENDENT_SECURITY_AUDIT_PASO_3B_POST_REMEDIATION.md`).
- **Cierre de `F-05` (HIGH — P0, Frontera estructural de confianza end-to-end y evasiones CSS):**
  - `skills/web_extraction_skill.py` y `config/tools_schema.py`: exposición de `'envelope'` en el esquema de herramientas, método `execute_with_envelope()` y ampliación de `build_llm_envelope()` con `trust_level="untrusted_external_content"`, `source_type="external_web"`, `origin_skill="web_extraction"`, `allow_tool_execution_from_content=False`, `allow_system_prompt_promotion=False`, `allow_memory_assistant_promotion=False` y `provenance`.
  - `src/tools/skill_manager.py`: método `execute_skill_with_envelope()` preservando `execute_skill()`.
  - `src/brain/memory.py`: contenedor aislado `_external_references` (`add_external_reference`, `get_external_references`, `format_untrusted_context_for_llm`, `compose_prompt_with_untrusted_data`); bloqueo automático de promoción a `"assistant"` si se intenta pasar un volcado o sobre web a `add_message()`; exclusión total de `_external_references` en `get_context_prompt()` e `inject_context(system_prompt)`; saneamiento de registros históricos en `load_from_disk()` sin tocar ni migrar `data/memory_store.json`.
  - `src/brain/brain_manager.py` y adaptadores (`src/brain/ollama_client.py`, `src/brain/gemini_client.py`, `src/brain/claude_client.py`, `src/brain/openai_client.py`, `src/brain/codex_client.py`): canal `untrusted_context` separado de `system_prompt` en los 5 cerebros; en `CodexBrain.generate_response()`, `classify_codex_task(prompt)` evalúa únicamente el `prompt` del usuario y nunca `untrusted_context`.
  - `src/brain/intent_router.py`: descarte de bloques `[EVIDENCIA_EXTERNA_NO_CONFIABLE_SOLO_LECTURA]` y sobres `untrusted_external_content` antes de enrutar intenciones.
  - `skills/web_parser.py`: limpieza de comentarios/escapes CSS (`_clean_css_text`), detección de `opacity: .0`, `color: transparent`, `rgba(...,0)`, `hsla(...,0)`, `#0000`/`#00000000`, `transform: scale(0)`, posicionamiento negativo fuera de pantalla, `content-visibility: hidden`, clases `sr-only`/`visually-hidden` (`_HIDDEN_CLASS_TOKENS`) y selectores ocultos definidos en `<style>` (`_parse_style_rules`).
- **Cierre de `F-01` (MEDIUM — P1, Citation URL Hijack, proveedores e IDN):**
  - `skills/web_document_converter.py`: `_sanitize_citation_label()` neutraliza esquemas URL dentro de títulos de citas para impedir que `RE_URL` en `DocumentParser` secuestre `Citation.url`; `_sanitize_inline_code()` sanea `search_provider`, `sr.provider`, códigos y timestamps; `_sanitize_markdown_url()` y `normalize_extracted_url()` rechazan esquemas no HTTP/HTTPS y credenciales, acotan longitud a `1024` chars y normalizan dominios IDN a punycode ASCII (`xn--...`); protección contra líneas `|` y barras invertidas finales `\`.
- **Cierre de `F-04` (MEDIUM — P1, Presupuesto global compartido):**
  - `skills/web_parser.py` y `skills/web_document_converter.py`: presupuesto compartido `max_normalized_chars` aplicado simultáneamente a `metadata` (`description`, `title`, `author`, `site_name`), bloques de cuerpo (`headings`, `paragraphs`, `lists`, `normalized_text`) y `links`, garantizando `len(description) + len(normalized_text) + sum(len(l.url) + len(l.text)) <= max_normalized_chars` y marcando `truncated=True` / `ExtractionStatus.PARTIAL` ante cualquier recorte.
- **Cierre de `F-06` (LOW — P1, Fast-path de `IntentRouter` y deduplicación):**
  - `src/brain/intent_router.py`: `_clean_extracted_url_token()` preserva paréntesis/corchetes/llaves balanceados en URLs (ej. Wikipedia `Python_(lenguaje)`) y `_is_explicit_web_extraction_command()` evita falsos positivos en oraciones condicionales o de diagnóstico con `"lee"` o `"analiza"`; exclusión de URLs en `AppLauncherSkill`.
  - `skills/web_extraction_skill.py`: normalización porcentual RFC 3986 en `_canonical_url_dedup_key()`.

### 2. Verificación Realmente Ejecutada

- `.\venv\Scripts\python.exe -m py_compile` sobre los 19 módulos y tests: **0 errores**.
- `.\venv\Scripts\python.exe -m unittest tests.test_web_extraction_skill -v`: **26 pruebas ejecutadas en 6.426s — OK (0 fallos, 0 errores)**.
- Suites de regresión (`test_intent_router_chaos`, `test_document_export_skill`, `test_memory_isolation`, `test_transaction_recovery`, `test_file_editor_and_chain`, `test_memory_persistence`, `test_memory_window`, `test_memory_overflow`): **236 pruebas ejecutadas en 69.174s — OK (0 fallos, 0 errores)**.
- Suite global (`$env:JARVIS_ENV='test'; .\venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"`): **533 pruebas ejecutadas en 87.151s — OK (0 fallos, 0 errores)**.
- `.\venv\Scripts\python.exe main.py --help`: **OK (código 0)**.
- `data/memory_store.json`: intacto (**1.218 bytes**, SHA-256 `83640EAADFF5E33FEB9126F6EEBB9F7960F924F01BB3A1968468014223341222`).

---

## Cierre Final de N5 — Hardening Transaccional, Autorización Interactiva, Aislamiento de Memoria y Auditoría (2026-11-12)

### 1. Arquitectura Consolidada y Decisiones de Seguridad

- **Motor transaccional WAL-First (`src/brain/transaction_manager.py`):**
  - `GlobalTransactionLock` con cerrojo entre hilos y bloqueo exclusivo interproceso en Windows (`msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)`) y timeout configurable (`TRANSACTION_LOCK_TIMEOUT_SEC`).
  - Bitácora secuencial encadenada por SHA-256 (`.wal.jsonl` con `flush()` + `os.fsync()`) previa a instantáneas atómicas (`.tx.json` vía temporal + `os.replace()`).
  - Cadena criptográfica por archivo (`H0 -> H1 -> H2 -> H3`), reversión idempotente estrictamente LIFO, detección de línea final truncada (`TRUNCATED_FINAL_WAL_LINE`) vs corrupción interna (`INTERIOR_WAL_CORRUPTION`), bloqueo automático si existen transacciones `CORRUPTED` o `RECOVERY_FAILED`, y rechazo explícito contra accesos a `data/memory_store.json`.
- **Orquestación y Autorización Interactiva Criptográfica (`src/brain/chain_orchestrator.py`, `skills/file_editor_skill.py`):**
  - Eliminada la aprobación meramente booleana: `authorize_plan_interactively()` genera tokens efímeros de un solo uso ligados a `(plan_id, execution_id, approval_hash, normalized_selection)` con TTL configurable (`CHAIN_APPROVAL_TTL_SECONDS = 300`, `CHAIN_MAX_OPERATIONS = 5000`), consumo atómico bajo lock y no-mutación ante rechazo.
  - `simulate_plan()` y `preflight_validate()` impiden re-simulaciones y duplicados sobre operaciones aplicadas/fallidas (`ApprovalBindingError`, `DuplicateExecutionError`); `_verify_completed_dependency_evidence()` exige evidencia física real en disco y hash SHA-256 verificable antes de dar por completada una dependencia.
  - `PolicyEngine` valida rutas autorizadas, nombres reservados, symlinks/junctions, backups y límites antes de mutar disco; operaciones rechazadas no escriben en disco.
  - `main.py`: `_ensure_orchestrator()` ejecuta `recover_pending_transactions()` de manera perezosa en consola al usar `/optimizar` o `/rollback` e integra `authorize_plan_interactively()`.
- **Aislamiento Determinista de Memoria en Pruebas (`src/brain/memory.py`):**
  - Sustituida la heurística basada en `sys.modules["unittest"]` por `_is_test_environment()` explícito (`JARVIS_ENV` en `{"test", "testing"}`), confinando toda escritura en tests dentro de `temp/jarvis_test_isolated/`.
  - Bloqueo incondicional `_storage_blocked` ante almacenamiento corrupto o ilegible para evitar autoguardados destructivos y recuperación atómica verificada mediante `recover_from_file()` con backup `.corrupt.<uuid>` sincronizado y verificado por SHA-256.
- **Auditoría Independiente N5:**
  - Informe `docs/audits/INDEPENDENT_SECURITY_AUDIT_N5_FINAL.md` verificando cierre de hallazgos **N1–N5** y los 20 escenarios adversariales **S01–S20** con dictamen **APROBADO / N6 AUTORIZADO**.

### 2. Archivos Incorporados y Modificados en N5

- Nuevos: `src/brain/transaction_manager.py`, `tests/test_transaction_recovery.py`, `tests/test_memory_isolation.py`, `docs/audits/INDEPENDENT_SECURITY_AUDIT_N5_FINAL.md`.
- Modificados: `config/settings.py` (parámetros `CHAIN_*` y `TRANSACTION_*`), `main.py` (`recover_pending_transactions` y `authorize_plan_interactively`), `skills/file_editor_skill.py`, `src/brain/chain_orchestrator.py`, `src/brain/memory.py`, `tests/test_file_editor_and_chain.py`, `tests/test_advanced_features_mission.py`, `tests/test_codex_integration.py`, `tests/test_concurrency_and_locks.py`, `tests/test_intent_router_chaos.py`, `tests/test_memory_overflow.py`, `tests/test_memory_persistence.py`, `tests/test_memory_window.py`, `CONTEXT.md`.

### 3. Verificaciones Realmente Ejecutadas (N5)

- `.\venv\Scripts\python.exe -m py_compile` sobre todos los módulos N5 modificados/nuevos: **0 errores**.
- Suites core N5 (`$env:JARVIS_ENV="test"; .\venv\Scripts\python.exe -m unittest tests.test_transaction_recovery tests.test_file_editor_and_chain tests.test_memory_isolation tests.test_memory_persistence tests.test_memory_window tests.test_memory_overflow -v`): **161 pruebas en 65.178s — OK (0 fallos, 0 errores, 0 omitidas)**.
- Verificación de aislamiento en memoria/WAL bajo `JARVIS_ENV=test`: rutas resueltas dentro de `temp/jarvis_test_isolated/` y `temp/tx_test_runs/`.
- `data/memory_store.json`: intacto byte a byte (**1.218 bytes**, SHA-256 `83640EAADFF5E33FEB9126F6EEBB9F7960F924F01BB3A1968468014223341222`).
- `GitSafetyGuard`: `find_tracked_sensitive_files() == []`, `scan_text_for_secrets == []`, candidatos N5 validados (`True`).
- Limitación conocida de plataforma: Windows no expone `os.fsync()` sobre descriptores de directorio; la durabilidad se garantiza con `flush()` + `os.fsync()` en cada `.wal.jsonl`, `.snapshot.tmp` y `.jarvis_tmp` antes de `os.replace()`.
- Estado Git y versionado: cierra el bloque de 3 fases completadas desde `v5.1.0` (Paso #3.A `DocumentExportSkill`, Paso #3.B `WebExtractionSkill` y Fase N5), precedido por el registro documental de la auditoría de Paso #3.B (`2d94d79`).

---

## Fase N6 — Autonomous Goal & Planning Engine (2026-11-12)

### 1. Capacidades Implementadas y Responsabilidades de Módulos

- Prompt completo registrado en `prompts_history.txt` (`PROYECTO J.A.R.V.I.S. — N6: AUTONOMOUS GOAL & PLANNING ENGINE`).
- Documentación técnica creada en `docs/N6_AUTONOMOUS_GOAL_ENGINE.md` con diagramas de arquitectura y ciclo de vida.
- Archivos nuevos creados:
  - `src/brain/goal_models.py`: modelos de dominio (`Goal`, `GoalTask`, `GoalPlan`, `GoalEngineLimits`, `SuccessCriteria`, `Constraints`, `RequiredCapabilities`, `GoalContext`, `ExecutionHistoryEntry`, `ReplanDecision`), enumeraciones y máquinas de estado estrictas (`ALLOWED_GOAL_TRANSITIONS`, `ALLOWED_TASK_TRANSITIONS`), hashing estructural SHA-256 (`plan_hash`) e invariantes de evidencia en `COMPLETED`.
  - `src/brain/goal_skill_selector.py`: validación contra `SkillManager`, chequeo de compatibilidad de parámetros y clasificación en 3 categorías (`SAFE_PLANNING`, `LOW_RISK_EXECUTION`, `SENSITIVE_OPERATION`). Bloquea herramientas inexistentes y respeta `read_only` / `allow_network` / `forbidden_actions`.
  - `src/brain/goal_understanding.py`: comprensión de lenguaje natural, detección de ambigüedad (`AWAITING_CLARIFICATION` + `clarification_questions`), extracción de restricciones explícitas e información faltante, con enriquecimiento opcional vía Multi-Brain (`query_raw`) y fallback determinista completo.
  - `src/brain/goal_planner.py`: `TaskDecomposer` + `GoalPlanningEngine` con validación DAG por algoritmo de Kahn (detección de ciclos, dependencias faltantes, `max_tasks`, `max_depth`). Planificar nunca otorga permiso automático de ejecución.
  - `src/brain/goal_persistence.py`: `GoalStore` aislado con bitácora WAL (`.wal.jsonl`) + snapshots atómicos (`.goal.json` con `flush` + `os.fsync` + `os.replace`), redacción de secretos y confinamiento obligatorio bajo `temp/jarvis_test_isolated/` cuando `JARVIS_ENV` es `test`/`testing`. Nunca accede ni modifica `data/memory_store.json`.
  - `src/brain/goal_supervisor.py`: `PlanEvaluator` + `ExecutionSupervisor` con orden por `depends_on`, prevención de duplicados en tareas completadas, aprobación explícita vinculada a `plan_hash`, delegación obligatoria de edición de archivos a `ChainOrchestrator` + `PolicyEngine`, timeouts por tarea y globales, reintentos acotados, cancelación y replanificación controlada preservando historial y resultados previos.
  - `src/brain/goal_engine.py`: fachada unificada `GoalEngine` libre de dependencias de voz/audio.
  - `src/brain/goal_console.py`: adaptador de comandos `/goal`, `/plan`, `/plan ejecutar`, `/plan estado`, `/plan cancelar`, `/plan aclarar` con confirmación `default-deny`.
  - `tests/test_goal_engine.py`: suite integral de 27 pruebas unitarias, funcionales y adversariales para N6.
- Archivos modificados:
  - `config/settings.py`: límites configurables `GOAL_MAX_TASKS`, `GOAL_MAX_DEPTH`, `GOAL_MAX_TASK_RETRIES`, `GOAL_TASK_TIMEOUT_SEC`, `GOAL_TOTAL_TIMEOUT_SEC`, `GOAL_MAX_REPLANS` y ruta `GOAL_STORE_DIR`.
  - `src/brain/brain_manager.py`: nuevo método no intrusivo `query_raw(...)` reutilizando `plan_request` y `_generate_with_fallback_policy` sin mutar `ConversationMemory`.
  - `main.py`: hook perezoso hacia `GoalConsoleHandler` para `/goal` y `/plan*` sin inicializar STT/TTS en consola de texto y sin alterar `/modo`, `/voz`, `/optimizar`, `/rollback`, `/limpiar` o `salir`.

### 2. Verificaciones Realmente Ejecutadas y Estado Git (N6)

- `.\venv\Scripts\python.exe -m py_compile` sobre todos los módulos y pruebas de N5 y N6: **0 errores**.
- `$env:JARVIS_ENV="test"; .\venv\Scripts\python.exe -m unittest tests.test_goal_engine -v`: **27 pruebas ejecutadas en 14.157s — OK (0 fallos, 0 errores, 0 omitidas)**.
- Suite global (`$env:JARVIS_ENV="test"; .\venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -q`): **560 pruebas ejecutadas en 99.901s — OK (0 fallos, 0 errores, 0 omitidas)**.
- `.\venv\Scripts\python.exe main.py --help`: **OK (código 0)**.
- Verificación estática de importaciones perezosas y dependencias circulares: `main.py`, `GoalEngine` y `GoalConsoleHandler` no importan `src.audio.*`, `faster_whisper`, `sounddevice`, `edge_tts` ni `pygame`; cero ciclos de importación y 100% type hints públicos / cero `TODO` o `except:` desnudos.
- Persistencia aislada bajo `JARVIS_ENV=test`: `GoalStore` confinado a `temp/jarvis_test_isolated/goals`.
- Integridad de `data/memory_store.json`: **1.218 bytes**, SHA-256 `83640EAADFF5E33FEB9126F6EEBB9F7960F924F01BB3A1968468014223341222` (intacto).
- `GitSafetyGuard`: cero secretos y cero rutas sensibles candidatas.
- Estado Git base de N6: separado en commit independiente inmediatamente posterior al cierre N5 (`2738393`, etiqueta anotada `v5.2.0`) y al registro de auditoría Paso #3.B (`2d94d79`); N6 inicia el bloque hacia el siguiente hito etiquetado cada 3 fases (`N6 -> N7 -> N8`).
- Limitaciones conocidas y riesgos pendientes: hardware físico (micrófono/TTS real) no probado en esta sesión; `GoalConsoleHandler` instancia `GoalEngine(..., use_ai=False)` por defecto para mantener determinismo y cero costos imprevistos en consola; la ejecución de operaciones sensibles exige confirmación explícita humano-en-el-bucle y `ExecutionSupervisor` sigue acotado por los timeouts de sesión actuales (`GOAL_TASK_TIMEOUT_SEC` / `GOAL_TOTAL_TIMEOUT_SEC`).
- Próxima fase recomendada: **N7** (extensión avanzada de autonomía/ejecución tras mantener N6 como primera de las 3 fases rumbo al siguiente hito etiquetado).

---

## Fase N7 — Context Awareness Engine (2026-11-17)

### 1. Arquitectura Modular y Archivos Incorporados / Modificados

- Prompt completo registrado en `prompts_history.txt` (`PROYECTO J.A.R.V.I.S. — N7: CONTEXT AWARENESS ENGINE`).
- Documentación técnica, de seguridad, configuración y consola creada en `docs/N7_CONTEXT_AWARENESS_ENGINE.md`.
- Archivos nuevos creados:
  - `src/context/__init__.py`: exportaciones públicas del paquete N7.
  - `src/context/models.py`: modelos de datos inmutables (`frozen=True`) y contratos tipados (`ContextCategory`, `AvailabilityStatus`, `MetricReading`, `SystemContextInfo`, `GpuDeviceInfo`, `StorageVolumeInfo`, `HardwareContextInfo`, `ResourceUsageInfo`, `ProcessContextItem`, `ProcessAwarenessInfo`, `ActiveWindowInfo`, `ApplicationItem`, `ApplicationAwarenessInfo`, `FileMetadataItem`, `DirectorySnapshot`, `FileSystemContextInfo`, `ProviderIssue`, `ContextUntrustedEnvelope`, `ContextSnapshot`) y jerarquía de excepciones (`ContextError`, `ContextPolicyError`, `ContextAccessDeniedError`, `ContextProviderError`, `ContextTimeoutError`). Prohibición explícita de asignar `value=0.0` cuando `status` es `UNAVAILABLE` o `ERROR`.
  - `src/context/policy.py`: `ContextAccessPolicy` en modo `default-deny`, reutilización de `AUTHORIZED_FILE_ROOTS` y `FORBIDDEN_SYSTEM_PREFIXES`, validación de rutas absolutas contra traversal `..`, rutas UNC, variables `%...%` sin expandir, dispositivos reservados de Windows (`CON`, `NUL`, `COM1..9`, `LPT1..9`), enlaces simbólicos y junctions NTFS (`FILE_ATTRIBUTE_REPARSE_POINT`), bloqueo de archivos sensibles (`.env`, `prompts_history.txt`, `memory_store.json`, claves privadas), redacción de secretos (`redact_secrets`) y sanitización de etiquetas no confiables (`sanitize_untrusted_label`).
  - `src/context/system_hardware_provider.py`: `SystemInfoProvider`, `StorageInfoProvider`, `HardwareInfoProvider` y `ResourceMonitoringProvider`. Detección dinámica de CPU (descriptor `HKLM\HARDWARE\DESCRIPTION\System\CentralProcessor\0` en `KEY_READ` / `platform.processor()`), núcleos físicos/lógicos, frecuencia, RAM, unidades y GPU/VRAM con cascada tolerante a fallos (`nvidia-smi` con `shell=False` y timeout -> `torch.cuda` si ya está en `sys.modules` -> descriptor de adaptador `winreg.KEY_READ` -> `AvailabilityStatus.UNAVAILABLE`).
  - `src/context/process_app_provider.py`: `ProcessAwarenessProvider` y `ApplicationAwarenessProvider`. Enumeración acotada de procesos sin consultar jamás `cmdline()` ni `environ()`, manejo resiliente de `psutil.AccessDenied` y `psutil.NoSuchProcess`, detección de ventanas visibles/en primer plano mediante `user32.dll` en solo lectura y separación entre aplicaciones instaladas, aplicaciones en ejecución y servicios internos de Windows.
  - `src/context/filesystem_provider.py`: `FileSystemContextProvider` en estricto modo lectura dentro de raíces autorizadas (`inspect_path`, `list_directory`, `find_relevant_files`, `collect`) con límites de profundidad y cantidad de entradas.
  - `src/context/engine.py`: `ContextAwarenessEngine` con selección bajo demanda por categorías, caché TTL configurable, timeout por proveedor, aislamiento de errores parciales, inferencia selectiva de categorías para N6 (`infer_relevant_categories_for_goal`) y empaquetado de datos no confiables (`build_untrusted_envelope`, `build_n6_context_payload`).
  - `src/context/console.py`: `ContextConsoleHandler` para `/contexto` (`sistema`, `hardware`, `recursos`, `almacenamiento`, `procesos`, `aplicaciones`, `archivos`, `ayuda`) sin importar módulos de audio.
  - `tests/test_context_awareness_n7.py`: suite de 20 pruebas integrales cubriendo funcionalidad, seguridad adversarial, auditoría AST de solo lectura, consola e integración N6+N7.
- Archivos modificados:
  - `config/settings.py`: constantes configurables y seguras por defecto (`CONTEXT_ENGINE_ENABLED`, `CONTEXT_ALLOWED_CATEGORIES`, `CONTEXT_ENABLED_CATEGORIES`, `CONTEXT_MAX_PROCESSES`, `CONTEXT_MAX_APPLICATIONS`, `CONTEXT_MAX_WINDOWS`, `CONTEXT_MAX_DIR_DEPTH`, `CONTEXT_MAX_DIR_ENTRIES`, `CONTEXT_CACHE_TTL_SEC`, `CONTEXT_PROVIDER_TIMEOUT_SEC`, `CONTEXT_MINIMIZE_FOR_LLM`, `CONTEXT_INCLUDE_HOSTNAME`, `CONTEXT_INCLUDE_PROCESS_USER`, `CONTEXT_INCLUDE_PROCESS_EXE`).
  - `src/brain/goal_engine.py`: integración no intrusiva con `ContextAwarenessEngine` (`context_engine`, `set_context_engine`, `request_context`, `_enrich_goal_with_n7_context`) manteniendo 100% de retrocompatibilidad cuando N7 no está conectado o se deshabilita.
  - `src/brain/goal_persistence.py`: redacción recursiva de secretos (`_redact_nested_data`) sobre `goal.context` y `goal.missing_info` antes de persistir instantáneas `.goal.json`.
  - `src/brain/goal_console.py`: inyección opcional de `context_engine` hacia `GoalEngine`.
  - `main.py`: inicialización perezosa de `ContextAwarenessEngine` y `ContextConsoleHandler` para `/contexto` (y `/context`) en consola sin penalizar el tiempo de arranque ni cargar `faster_whisper` / STT / TTS.

### 2. Verificaciones Realmente Ejecutadas y Estado del Roadmap (N7)

- Compilación estática (`.\venv\Scripts\python.exe -m py_compile` sobre todos los módulos nuevos/modificados y suites de pruebas): **0 errores (código 0)**.
- Suite específica N7 (`$env:JARVIS_ENV="test"; .\venv\Scripts\python.exe -m unittest tests.test_context_awareness_n7 -v`): **20 pruebas ejecutadas en 10.606s — OK (0 fallos, 0 errores, 0 omitidas)**.
- Suite específica N6 (`$env:JARVIS_ENV="test"; .\venv\Scripts\python.exe -m unittest tests.test_goal_engine -v`): **27 pruebas ejecutadas en 17.930s — OK (0 fallos, 0 errores, 0 omitidas)**.
- Suite global completa (`$env:JARVIS_ENV="test"; .\venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -q`): **580 pruebas ejecutadas en 137.875s — OK (0 fallos, 0 errores)**.
- Smoke test `.\venv\Scripts\python.exe main.py --help` y verificación en subproceso limpio de ausencia de módulos de audio (`src.audio.*`, `faster_whisper`, `sounddevice`) al usar N7 y `/contexto`: **OK (código 0)**.
- Integridad de `data/memory_store.json`: **1.218 bytes**, SHA-256 `83640EAADFF5E33FEB9126F6EEBB9F7960F924F01BB3A1968468014223341222` (intacto antes y después de todas las suites).
- Estado del bloque de tres fases:
  - **N5:** cerrada (`v5.2.0`), sin modificaciones de alcance.
  - **N6:** cerrada (1.ª fase del nuevo bloque), sin modificaciones de alcance.
  - **N7:** completada y verificada (2.ª fase del bloque actual; no se crea tag individual conforme a la regla de etiquetado cada 3 fases).
  - **N8:** no iniciada.

---

## Remediación de Auditoría de Seguridad N7 (Hallazgos F1–F6) y Actualización Documental (2026-11-17)

### 1. Hallazgos Corregidos y Cambios Aplicados

- Prompt completo registrado en `prompts_history.txt` (`PROYECTO J.A.R.V.I.S. — REMEDIACIÓN DE AUDITORÍA N7 (F1–F6) Y ACTUALIZACIÓN DOCUMENTAL`).
- **F1 — Protección contra NTFS Alternate Data Streams (`src/context/policy.py`, `src/context/filesystem_provider.py`):**
  - `is_sensitive_context_filename()` rechaza conservadoramente cualquier nombre que contenga `:` (como `.env:secretstream`, `credentials.json:backup`, `config.ini:stream`), normaliza puntos y espacios finales de Windows y detecta copias `.bak`/`.old`/`.tmp` de archivos sensibles.
  - `ContextAccessPolicy.validate_read_path()` bloquea cualquier presencia de `:` fuera del especificador de unidad de Windows (`C:\`), impidiendo ADS tanto en el archivo hoja como en directorios intermedios.
- **F2 — Junctions NTFS rotos y manejo resiliente de `PermissionError` / `OSError` (`src/context/policy.py`, `src/context/filesystem_provider.py`, `src/context/console.py`, `main.py`):**
  - `_assert_path_node_safe()` reemplaza la comprobación previa basada en `exists()` por `is_symlink()`, `os.path.isjunction()`, `os.lstat()` y `os.path.lexists()`, detectando junctions NTFS rotos antes de `Path.resolve()` y convirtiendo `PermissionError` / `OSError` en `ContextAccessDeniedError`.
  - `ContextConsoleHandler.handle_command()` y `main.py` capturan de forma controlada `(ContextError, PermissionError, OSError)` para que una ruta inaccesible jamás interrumpa el ciclo principal de consola, manteniendo el aislamiento de errores parciales entre proveedores.
- **F3 — Exclusiones de subárboles y archivos sensibles específicas de N7 (`src/context/policy.py`, `src/context/filesystem_provider.py`):**
  - Sin modificar las raíces compartidas de `FileEditorSkill` ni las políticas de N5, `ContextAccessPolicy` incorpora `FORBIDDEN_CONTEXT_DIR_NAMES`, `FORBIDDEN_CONTEXT_SUBTREES` y `is_forbidden_context_subpath()`, bloqueando en `inspect_path`, `list_directory` y `find_relevant_files` el acceso a `.git`, `.ssh`, `.aws`, `.gnupg`, `.kube`, `data/goals`, `data/transactions`, `data/backups`, `file_editor_audit.jsonl`, `*.log`, `.npmrc`, `.netrc`, `secrets.json`, `secrets.yaml`, directorios `Credentials` y almacenes de navegadores (`User Data`, `Profiles`) así como cualquier ruta hija.
- **F4 — Minimización en origen, no-persistencia de ventanas arbitrarias, redacción de secretos y anti-Prompt Injection (`src/context/engine.py`, `src/context/process_app_provider.py`, `src/context/policy.py`, `src/brain/exceptions.py`, `src/brain/goal_engine.py`, `src/brain/goal_persistence.py`):**
  - `ApplicationAwarenessProvider.collect()` incorpora `include_windows` y `target_app`; cuando N6 solicita contexto para un objetivo (`_enrich_goal_with_n7_context`), pasa `include_windows=False` y `target_app` para no recolectar títulos de ventanas privadas desde el origen.
  - `build_untrusted_envelope()` y `build_n6_context_payload()` eliminan la persistencia de `visible_windows`, `foreground_window` y `top_processes` en `.goal.json`, y `GoalStore` (`_VOLATILE_CONTEXT_KEYS`) los filtra defensivamente antes de guardar.
  - `redact_secrets()` amplía `_SECRET_PATTERNS` para cubrir tokens `ghp_*` / `github_pat_*`, claves AWS `AKIA*` / `ASIA*` (insensible a mayúsculas/minúsculas), encabezados `Authorization: Bearer ...`, JWT (`eyJ...`), parámetros URL (`?token=`, `&access_token=`, `?api_key=`) y asignaciones `token=`.
  - `sanitize_untrusted_label()` amplía `_INJECTION_PATTERNS` para neutralizar `</EVIDENCIA...>`, `<|system|>`, `<<SYS>>`, indicadores de rol en español e inglés, JSON con `"tool_name"` / `"function_call"` / `"skill_params"` y variantes en español (`omite/descarta/anula ... instrucciones/reglas/políticas`).
- **F5 — Detección de reparse points sobre `stat_result` y mitigación TOCTOU (`src/context/policy.py`, `src/context/filesystem_provider.py`):**
  - `inspect_path()`, `list_directory()` y `find_relevant_files()` eliminan llamadas previas a `Path.exists()`/`is_dir()` y verifican `is_stat_reparse_or_symlink(st)` (`stat.S_ISLNK` y `st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT`) directamente sobre el resultado de `os.lstat()` y `entry.stat(follow_symlinks=False)`.
- **F6 — Coalescencia de caché concurrente, frescura de `RESOURCES` y descubrimiento de aplicaciones (`src/context/engine.py`, `src/context/process_app_provider.py`, `src/context/console.py`, `src/context/models.py`, `src/brain/goal_engine.py`):**
  - `ProcessAwarenessProvider.collect()` permite a `ApplicationAwarenessProvider` consultar hasta `max(max_processes, max_applications * 4)` procesos para que `max_processes` no trunque aplicaciones en ejecución.
  - `ContextAwarenessEngine` implementa locks por categoría (`_category_locks`) con doble verificación para coalescer solicitudes concurrentes ante un cache miss en una única recolección sin bloquear categorías independientes.
  - Las consultas generales de `/contexto` y de N6 usan `refresh_resources=True` para obtener métricas `RESOURCES` frescas sin quedar retenidas por el TTL de 5 s, registrando en `snapshot.cached_categories` cualquier categoría servida desde caché.
- **Documentación actualizada:** `README.md`, `docs/N7_CONTEXT_AWARENESS_ENGINE.md` y `CONTEXT.md`.
- **Pruebas nuevas:** añadidas `test_21` a `test_26` en `tests/test_context_awareness_n7.py` (total N7: **26 pruebas**; total global: **586 pruebas**).

### 2. Verificaciones Realmente Ejecutadas (Remediación N7 F1–F6)

- Compilación estática (`.\venv\Scripts\python.exe -m py_compile` sobre todos los módulos modificados y pruebas): **0 errores (código 0)**.
- Suite específica N7 (`$env:JARVIS_ENV="test"; .\venv\Scripts\python.exe -m unittest tests.test_context_awareness_n7 -v`): **26 pruebas ejecutadas en 12.654s — OK (0 fallos, 0 errores, 0 omitidas)**.
- Suites de regresión N6 + N5 (`$env:JARVIS_ENV="test"; .\venv\Scripts\python.exe -m unittest tests.test_goal_engine tests.test_file_editor_and_chain tests.test_transaction_recovery tests.test_memory_isolation -v`): **154 pruebas ejecutadas en 77.072s — OK (0 fallos, 0 errores, 0 omitidas)**.
- Suite global completa (`$env:JARVIS_ENV="test"; .\venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"`): **586 pruebas ejecutadas en 127.827s — OK (0 fallos, 0 errores, 0 omitidas)**.
- `.\venv\Scripts\python.exe main.py --help` y verificación de no-carga de módulos de audio al importar y usar N7: **OK (código 0, `audio_modules_loaded: []`)**.
- Integridad de `data/memory_store.json`: **1.218 bytes**, SHA-256 `83640EAADFF5E33FEB9126F6EEBB9F7960F924F01BB3A1968468014223341222` (idéntico antes y después de todas las pruebas).
- Auditoría `GitSafetyGuard`: `find_tracked_sensitive_files() == []`, `0` secretos detectados en diff/archivos candidatos y `prompts_history.txt` excluido de Git.

---

## Remediación Final de Observaciones de Auditoría N7 (Cierre F4.A / F4.B / F4.C + Hardening F1 / F3 + Documentación) (2026-11-17)

### 1. Observaciones Resueltas y Cambios Aplicados

- Prompt completo registrado en `prompts_history.txt` (`PROYECTO J.A.R.V.I.S. — REMEDIACIÓN FINAL DE OBSERVACIONES DE AUDITORÍA N7`).
- **Bloque A (F4.A — Ventanas desactivadas por defecto en `src/context/engine.py`, `src/context/console.py`):**
  - `ContextAwarenessEngine.get_context()` y `get_context_safe()` cambian el valor predeterminado a `include_windows: bool = False` y pasan explícitamente `include_windows` y `target_app` a `ApplicationAwarenessProvider.collect(...)`.
  - `/contexto` (resumen general), `get_context()` sin argumentos, `get_context(categories=["applications"])` y los flujos automáticos de N6 realizan **cero** llamadas al enumerador de ventanas y nunca persisten títulos de ventanas en `.goal.json` ni `.wal.jsonl`.
  - `/contexto aplicaciones` en `ContextConsoleHandler` realiza opt-in explícito pasando `include_windows=True`.
- **Bloque B (F4.B — Redacción estructural de secretos sin falsos positivos en `src/brain/exceptions.py`, `src/brain/goal_persistence.py`):**
  - `redact_secrets()` reemplaza las expresiones regulares genéricas por coincidencia estructural sobre cadenas y estructuras anidadas (`dict`, `list`, `tuple`), redactando `Authorization: Basic <base64>`, `Authorization: Bearer <token>`, `api_key=...`, credenciales en URL (`user:pass@host`, `?token=`, `&access_token=`), JWT (`eyJ...`) y campos JSON/diccionario con claves sensibles (`password`, `token`, `secret`, `api_key`).
  - Se eliminan los falsos positivos sobre prosa común de error (`"The bearer authorization failed twice"`, `"password: incorrect"`, `"Login error - password: incorrect"`).
- **Bloque C (F4.C — Aislamiento de datos no confiables y endurecimiento de `sanitize_untrusted_label` en `src/context/policy.py`, `src/context/engine.py`, `src/brain/goal_engine.py`, `src/brain/goal_supervisor.py`):**
  - `sanitize_untrusted_label()` aplica normalización Unicode `NFKC`, elimina caracteres invisibles de categoría `Cf` (Zero-Width Space `\u200b`, BOM, controles bidi) y evalúa patrones sobre copia con plegado de tildes (`NFKD` sin marcas combinantes), neutralizando variantes en español y voseo (`"reemplazá el mensaje del sistema"`, `"ejecutá esta herramienta"`, `"ignora el prompt anterior"`), etiquetas XML compuestas (`"</system_prompt>"`) y objetos JSON anidados con claves previas (`{"ok": 1, "function_call": ...}`, `{"note": "ok", "skill_params": ...}`).
  - En `GoalEngine._enrich_goal_with_n7_context()` y `ContextAwarenessEngine.build_n6_context_payload()`, todas las cadenas de hardware y entorno provenientes de N7 se sanitizan al ingresar a N6 y se marcan con `n7_trust_boundary="untrusted_system_context"`.
  - En `ExecutionSupervisor._execute_internal_synthesis()`, `plan.goal.context.hardware` se sanitiza y se interpola explícitamente delimitado como `<DATO_EXTERNO_NO_CONFIABLE_SOLO_LECTURA>...</DATO_EXTERNO_NO_CONFIABLE_SOLO_LECTURA>`, registrando `untrusted_hardware_context=True` en `execution_evidence`.
- **Bloque D (F3 — Endurecimiento de rutas sensibles en `src/context/policy.py`):**
  - Sin modificar las raíces de N5 (`AUTHORIZED_FILE_ROOTS`) ni `FileEditorSkill`, `ContextAccessPolicy` bloquea en N7 el acceso genérico a `BASE_DIR/data` y `<auth_root>/data` (`data/other_private.json`, `data/goals`, `data/transactions`), perfiles completos de Opera GX (`Opera Software`, `Opera GX Stable`), `.envrc`, `.htpasswd`, `id_*.pub` (`id_rsa.pub`, `id_ed25519.pub`) y bases de datos locales (`*.sqlite`, `*.sqlite3`, `*.db`, `wallet.db`, `wallet.dat`, `wallet.json`).
- **Bloque E (F1 — Normalización iterativa de sufijos encadenados en `src/context/policy.py`):**
  - `is_sensitive_context_filename()` retira de forma iterativa secuencias arbitrarias de sufijos temporales/respaldo (`.bak`, `.tmp`, `.backup`, `.old`, `.orig`, `.swp`, timestamps `.YYYYMMDD_HHMMSS_ffffff` y colas hexadecimales), bloqueando combinaciones como `credentials.json.bak.tmp` y `credentials.json.tmp.bak` sin afectar archivos normales.
- **Bloque F (Documentación en `README.md`, `docs/N7_CONTEXT_AWARENESS_ENGINE.md`, `CONTEXT.md`):**
  - Eliminada la referencia a `CODEBASE_INDEX.md` inexistente en `README.md`.
  - Reemplazados los enlaces absolutos locales `file:///e:/...` en `docs/N7_CONTEXT_AWARENESS_ENGINE.md` por enlaces relativos portables (`../src/context/...`).
  - Documentados el valor por defecto `include_windows=False`, el opt-in de `/contexto aplicaciones`, las exclusiones F3, la normalización iterativa F1 y la frontera estructural de datos no confiables F4.C.
- **Pruebas nuevas:** añadidas `test_27` a `test_31` en `tests/test_context_awareness_n7.py` (total N7: **31 pruebas**; total global: **591 pruebas**).

### 2. Verificaciones Realmente Ejecutadas (Remediación Final N7)

- Compilación estática (`.\venv\Scripts\python.exe -m py_compile` sobre todos los módulos modificados y pruebas): **0 errores (código 0)**.
- Suite específica N7 (`$env:JARVIS_ENV="test"; .\venv\Scripts\python.exe -m unittest tests.test_context_awareness_n7 -v`): **31 pruebas ejecutadas en 14.475s — OK (0 fallos, 0 errores, 0 omitidas)**.
- Suites de regresión N5 + N6 (`$env:JARVIS_ENV="test"; .\venv\Scripts\python.exe -m unittest tests.test_file_editor_and_chain tests.test_transaction_recovery tests.test_memory_isolation tests.test_goal_engine -v`): **154 pruebas ejecutadas en 64.959s — OK (0 fallos, 0 errores, 0 omitidas)**.
- Suite global completa (`$env:JARVIS_ENV="test"; .\venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -q`): **591 pruebas ejecutadas en 112.886s — OK (0 fallos, 0 errores, 0 omitidas)**.
- Verificación programática de portabilidad de enlaces y rutas en `README.md` y `docs/N7_CONTEXT_AWARENESS_ENGINE.md` + `main.py --help`: **OK (código 0)**.
- Integridad de `data/memory_store.json`: **1.218 bytes**, SHA-256 `83640EAADFF5E33FEB9126F6EEBB9F7960F924F01BB3A1968468014223341222` (idéntico antes y después de todas las pruebas).
- Estado de N8: permanece no iniciada / pendiente de auditoría independiente posterior de cierre definitivo de N7.






## Remediación M1, M2 y B1–B4 — iteración de cierre N7 (2026-11-18)

### Cambios aplicados

- Prompt íntegro de cierre registrado en `prompts_history.txt` (archivo local excluido de Git).
- `src/brain/exceptions.py`: redacción recursiva segura ante ciclos; cubre Basic breve, JSON y claves Authorization compuestas, incluso en JSON con comillas escapadas. `BrainError` conserva redacción en mensaje y `detail`.
- `src/brain/goal_persistence.py`: redacta estructuras WAL antes de serializarlas; las rutas de snapshot mantienen su saneamiento recursivo.
- `src/brain/goal_supervisor.py`: las observaciones N7 y resúmenes no confiables viajan como JSON con procedencia, escapando delimitadores antes de formar la síntesis. El resultado conserva un envelope no confiable en vez de devolver `None`.
- `src/context/process_app_provider.py`: `ApplicationAwarenessProvider.collect()` ahora omite ventanas por defecto; el opt-in explícito sigue disponible.
- `src/context/policy.py`: bloquea journals WAL/SHM SQLite, nombres sensibles con sufijos de credenciales y variantes ocultas; normaliza sufijos hasta convergencia sin tope fijo. La defensa de sanitización contempla U+034F, selectores de variación, homoglifos cirílicos comunes y verbos imperativos con objetivos de instrucciones.
- `tests/test_context_awareness_n7.py`: amplía los casos existentes de persistencia, ciclos, falsos positivos, aislamiento N6, privacidad, nombres sensibles y cantidades de sufijos (31, 32, 33, 100, 1000 y 10000).
- `tests/test_web_extraction_skill.py`: sus dos pruebas que generan archivos temporales usan una carpeta temporal bajo el repositorio; así evitan los errores de ACL de `%TEMP%` observados en Windows y permiten ejecutar la suite global en este entorno.
- `README.md` y `docs/N7_CONTEXT_AWARENESS_ENGINE.md`: reflejan el estado en verificación, la frontera JSON estructural, el default privado y las variantes de nombres bloqueadas.

### Verificaciones ejecutadas

- `py_compile` de los módulos modificados y la suite N7: **OK**.
- Casos focalizados de M1, B1, M2 y B2/B3: **OK**.
- Suite N7: **31 pruebas — OK**.
- Regresión N5 + N6 (`test_goal_engine`, `test_file_editor_and_chain`, `test_transaction_recovery`, `test_memory_isolation`): **154 pruebas — OK**.
- Suite global: primera ejecución con `%TEMP%` del sistema produjo dos errores en `tests.test_web_extraction_skill` (exportación temporal y escritura/limpieza de PEM con `PermissionError`). Se ajustaron esas dos pruebas para usar temporales bajo el repositorio; los dos casos focalizados pasaron y la suite global final ejecutó **591 pruebas — OK (0 fallos, 0 errores)** en 104.119s.
- `git diff --check`: **sin errores** (Git advirtió normalización LF/CRLF para archivos afectados).
- Auditoría AST de solo lectura incluida en `test_11_security_read_only_enforcement_and_file_integrity_preserved`: **pasó** como parte de N7.
- `GitSafetyGuard`: rama `main`; `find_tracked_sensitive_files() == []`; cero secretos detectados en los archivos candidatos; staging vacío al finalizar las verificaciones.
- Integridad de memoria antes/después: **1.218 bytes**, SHA-256 `83640EAADFF5E33FEB9126F6EEBB9F7960F924F01BB3A1968468014223341222` (sin cambios).

### Estado de cierre

M1, M2 y B1–B4 tienen correcciones, documentación y verificaciones aprobadas. El cierre técnico queda **CERRADO**, incluida la suite global y el análisis AST de solo lectura. N7 queda habilitada para avanzar a N8; N8 todavía no se inició.

### Publicación Git

- Commit principal: `62c54be` (`fix(n7): close audit findings M1-M2 and B1-B4`).
- Tag anotado publicado: `v5.3.0`.
- `main` y `v5.3.0` se publicaron con push normal a `origin`; verificación final: `main...origin/main` sincronizadas y árbol limpio.

### Seguimiento — normalización de temporales de pruebas Windows (2026-11-18)

- Causa de los dos errores previos: en este entorno, `tempfile.TemporaryDirectory` crea directorios temporales con permisos ACL que impiden la escritura de los artefactos de las pruebas. No era un fallo del exportador ni de la generación de certificados.
- `tests/test_web_extraction_skill.py`: los dos casos afectados ahora usan `_WorkspaceTemporaryDirectory`, que crea temporales bajo `temp/web_extraction_tests` con permisos heredados del workspace y valida que la limpieza quede estrictamente dentro de esa raíz.
- Verificación aislada de exportación Markdown/DOCX y TLS/SNI: **2/2 OK**.
- Suite global completa, tras la corrección: **591 pruebas ejecutadas en 104.119s — OK (0 fallos, 0 errores)**.
- La memoria real permanece intacta: **1.218 bytes**, SHA-256 `83640EAADFF5E33FEB9126F6EEBB9F7960F924F01BB3A1968468014223341222`.
- `git diff --check`: sin errores. El estado de cierre de la iteración anterior queda actualizado: los dos errores del entorno quedaron resueltos y la suite global está verde.

---

## Fase N8.1 — Persistencia Base, Modelos, Políticas y Locking de Memoria a Largo Plazo (2026-11-18)

### 1. Resumen de Implementación

Se implementó el núcleo independiente `src/memory/` para **N8.1 — Long-Term Memory & Experience**, manteniendo intactos `data/memory_store.json`, `N5`, `N6` y `N7` y dejando explícitamente fuera del alcance de esta fase `MemoryRetriever`, `MemoryEngine`, comandos `/memoria` / `/privado` / `/incognito`, captura automática post-ejecución e integración conversacional N6/Multi-Brain (reservados para N8.2 y N8.3).

### 2. Archivos Creados y Modificados

- **Nuevos (`src/memory/`):**
  - `src/memory/__init__.py`: superficie pública estable de contratos N8.1 para N8.2 y N8.3.
  - `src/memory/models.py`: enumeraciones (`MemoryCategory`, `MemorySource`, `MemoryStatus`, `MemoryScopeType`, `MemoryAuditAction`), máquina de estados `ALLOWED_MEMORY_TRANSITIONS`, excepciones tipadas (`MemorySubsystemError`, `MemoryValidationError`, `MemoryPolicyError`, `MemoryPrivateModeError`, `MemoryPersistenceError`, `MemoryLockTimeoutError`, `MemoryCorruptionError`, `MemoryDuplicateError`, `MemoryNotFoundError`, `MemoryTransitionError`), `ExecutionVerificationEvidence`, `MemoryProvenance`, `MemoryCreateInput`, `MemoryRecord`, `MemoryRevision`, `MemoryAuditEntry`, `MemoryWriteResult`, `MemoryQueryFilter`, `MemorySearchCandidate`, `IntegrityReport`, `StoreStats` y helpers deterministas (`utc_now_iso`, `parse_iso_utc`, `generate_memory_id`, `canonical_json`, `is_valid_sha256_hex`).
  - `src/memory/policy.py`: `MemoryPolicy`, `sanitize_memory_text()` (sin reutilizar `sanitize_untrusted_label()`, preservando prosa técnica multilínea mientras elimina `Cf`, `U+034F` y selectores de variación), `normalize_text_for_search()`, `normalize_scope()`, `compute_content_fingerprint()`, rechazo de secretos sin exposición (`assert_no_secrets` con apoyo de `redact_secrets` e `is_sensitive_key`), bloqueo de `INFERENCE`, validación de matriz origen/categoría y `ExecutionVerificationEvidence`, modo privado y validación local segura `validate_safe_local_db_path()` (rechazo UNC/SMB, `..`, `%ENV%`, nombres reservados Windows, reparse points NTFS y bloqueo de `data/` real bajo `JARVIS_ENV=test`).
  - `src/memory/migrations.py`: esquema SQLite versionado v1 (`schema_migrations`, `store_metadata`, `memories`, `memory_revisions`, `memory_audit`, `memory_fts`), detección runtime `fts5_available()`, tokenizer `unicode61 remove_diacritics 2`, índices parciales únicos (`idx_memories_active_fingerprint`, `idx_memories_active_exec_provenance`) y configuración por conexión (`foreign_keys=ON`, `busy_timeout`, `secure_delete=ON`, `synchronous=FULL`, `temp_store=MEMORY`, `journal_mode=WAL`).
  - `src/memory/store.py`: `ExperienceStoreLock` (registro modular `_LOCK_REGISTRY` por ruta canónica + `threading.RLock` + lock OS no bloqueante `msvcrt.locking` en Windows / `fcntl.flock` POSIX sobre `<db>.lock` con reentrancia segura entre distintas instancias en el mismo hilo) y `ExperienceStore` con conexiones efímeras cerradas antes de soltar el lock, CRUD transaccional (`BEGIN IMMEDIATE`), dedupe por huella y procedencia de ejecución, revisiones inmutables, revocación con purga directa de `memory_fts`, búsqueda segura FTS5 con respaldo SQL `LIKE` y `verify_integrity()` con cuarentena `.corrupt.<uuid>`.
- **Modificados:**
  - `config/settings.py`: añadidas constantes tipadas `EXPERIENCES_DB_PATH`, `MEMORY_BACKUP_DIR`, `N8_ENABLED`, `N8_AUTO_CAPTURE_ENABLED` (`False`), `N8_ALLOW_INFERENCE_INGEST` (`False`), `N8_LOCK_TIMEOUT_SEC`, `N8_SQLITE_BUSY_TIMEOUT_MS`, `N8_MAX_ACTIVE_MEMORIES`, `N8_MAX_TITLE_CHARS`, `N8_MAX_CONTENT_CHARS`, `N8_MAX_SUMMARY_CHARS`, `N8_MAX_REVISIONS_PER_MEMORY`, `N8_FTS5_ENABLED` y presupuestos reservados `N8_TEXT_MAX_*` / `N8_VOICE_MAX_*`.
  - `.gitignore` y `src/utils/git_guard.py`: añadida protección explícita para `experiences.db*`, `*.db`, `*.db-wal`, `*.db-shm`, `*.db-journal`, `*.db-tmp`, `*.db.lock`, `*.db.backup`, `*.sqlite*`, `*.corrupt.*`, `memory_store.json`, `backups/`, `memory_backups/` y `temp/`.
  - `tests/test_memory_n8_persistence.py`: suite aislada de 19 pruebas cubriendo modelos, políticas, sanitización, secretos, `INFERENCE`, `EXECUTION_VERIFIED`, migraciones, CRUD/revisiones/revocación/dedupe/FKs/FTS5+SQL fallback/rollback/corrupción, hilos/reentrancia/multiproceso real con `subprocess` y bloqueo Git/UNC.
  - `docs/N8_LONG_TERM_MEMORY.md` y `README.md`: documentación técnica de N8.1 y separación de contratos reservados N8.2/N8.3.
  - `prompts_history.txt`: registrado el prompt íntegro de N8.1 (archivo local excluido de Git).

### 3. Verificaciones Realmente Ejecutadas (N8.1)

- Compilación estática (`.\venv\Scripts\python.exe -m py_compile` sobre `config/settings.py`, `src/utils/git_guard.py` y `src/memory/*.py`): **OK (código 0)**.
- Suite aislada N8.1 (`$env:JARVIS_ENV="test"; .\venv\Scripts\python.exe -m unittest tests.test_memory_n8_persistence -v`): **19 pruebas ejecutadas en 15.834s — OK (0 fallos, 0 errores, 0 omitidas)**, incluyendo concurrencia real con `subprocess` de 4 escritores + 1 administrador concurrente e inyección de fallo transaccional con rollback.
- Suite global completa (`$env:JARVIS_ENV="test"; .\venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"`): **610 pruebas ejecutadas en 130.477s — OK (0 fallos, 0 errores, 0 omitidas)** frente a línea base de **591 pruebas** (+19 pruebas nuevas sin regresiones).
- Integridad de datos reales:
  - `data/memory_store.json`: **1.218 bytes**, SHA-256 `83640EAADFF5E33FEB9126F6EEBB9F7960F924F01BB3A1968468014223341222` (intacto antes y después).
  - `data/` contiene únicamente `memory_store.json`; no se creó `data/experiences.db`.

### 4. Pendientes y Límites de Fase

- **Pendiente N8.2:** `MemoryRetriever` (ranking ponderado y presupuesto por voz/texto), backups automatizados y restauración, comandos de consola (`/memoria`, `/privado`, `/incognito`) y purga controlada.
- **Pendiente N8.3:** captura automática `EXECUTION_VERIFIED` post-ejecución desde N5/N6 y síntesis consultiva aislada hacia N6/Multi-Brain.
- En esta iteración N8.1 solo se publica commit verificado (sin crear tag nuevo, reservando los tags anotados según la cadencia de cierre por bloques/fases de `AGENTS.md`).

---

## Fase N8.2 — Ciclo de Vida, Recuperación, Consola, Modo Privado, Backups y Concurrencia Real (2026-11-20)

### 1. Resumen de Implementación

Se completó **N8.2 — Long-Term Memory & Experience** sobre la base N8.1 (`data/experiences.db`), manteniendo intactos `data/memory_store.json`, `N5`, `N6`, `N7` y `N8.1`, sin implementar aún **N8.3** (captura automática post-ejecución, referencias N8 en `GoalStore`, cambios en `GoalEngine`/`ExecutionSupervisor`, embeddings ni consolidación LLM).

### 2. Archivos Creados y Modificados

- **Nuevos (`src/memory/` y `tests/`):**
  - `src/memory/retriever.py`: `MemoryRetriever` con FTS5/BM25 y respaldo SQL `LIKE`, filtrado previo por estado (`ACTIVE`, exclusión de `REVOKED`/`SUPERSEDED`/`EXPIRED`/`expires_at` y pendientes sin confirmar), ámbitos `GLOBAL`/`PROJECT`/`APP_OR_GAME`, acotado de tokens (`N8_SEARCH_MAX_TOKENS=10`), ranking determinista de 6 factores sin fusión de experiencias distintas, presupuestos (`5 / 1500` normal; `3 / 600` voz y modelos locales 3B) y sobre consultivo `untrusted_long_term_memory` no promocionable al `system_prompt`.
  - `src/memory/engine.py`: API central `MemoryEngine` que coordina políticas N8.1, recuperación, corrección con revisiones, confirmación de pendientes, revocación lógica, purga física confirmada, backups/restauración e importación manual en solo lectura desde JSON legacy.
  - `src/memory/console.py`: `MemoryConsoleHandler` para `/memoria ayuda`, `buscar`, `listar`, `ver`, `corregir`, `confirmar`, `olvidar`, `purgar`, `importar-preferencias`, `backup`, `restaurar`, `/privado`, `/privado --sin-historial` e `/incognito` (con vistas previas y confirmación explícita para purga, importación y restauración).
  - `tests/test_memory_n8_lifecycle.py`: suite aislada de 10 pruebas integrales bajo `JARVIS_ENV=test` y `temp/jarvis_test_isolated/` incluyendo pruebas reales por `subprocess` `writer-vs-purge` y `writer-vs-restore`.
- **Actualizados:**
  - `src/memory/models.py`, `src/memory/policy.py`, `src/memory/store.py`, `src/memory/__init__.py`: soporte para `pending_confirmation` (`confirm_memory`), purga física transaccional con `wal_checkpoint(TRUNCATE)` y `VACUUM` opcional (`purge_memory`), `create_backup()` vía `sqlite3.Connection.backup()` con `quick_check`/`integrity_check`/SHA-256 y retención máxima (5), `restore_from_backup()` con verificación previa, preservación verificada de la DB previa y reemplazo atómico vía staging + `os.replace()`.
  - `src/brain/memory.py`: aislamiento real de modo privado (`_private_mode`, `_private_allow_history`, `_private_messages`, `_private_message_metadata`, `_private_external_references`, `_private_metadata`), `enter_private_mode()`, `exit_private_mode()` y bloqueo incondicional de `_save_to_disk()` / `save_to_disk()`.
  - `src/tools/skill_manager.py`, `src/brain/intent_router.py`, `src/brain/brain_manager.py`, `main.py`: propagación de modo privado (supresión de argumentos, respuestas crudas y `traceback` en consola/logs durante modo privado; lectura booleana estricta segura frente a `MagicMock`), consulta consultiva acotada en canal `untrusted_context`, despacho perezoso `/memoria`, `/privado`, `/incognito` sin importar Whisper/audio y bloqueo en modo privado de operaciones persistentes externas (`/optimizar`, `/rollback`, `/goal` y mutaciones `/plan`).
  - `config/settings.py`, `.gitignore`, `src/utils/git_guard.py`, `docs/N8_LONG_TERM_MEMORY.md`, `README.md`, `prompts_history.txt`.

### 3. Verificaciones Realmente Ejecutadas (N8.2)

- `py_compile` sobre los 16 módulos Python nuevos y modificados: **OK (código 0)**.
- Suite aislada N8.2 (`$env:JARVIS_ENV="test"; .\venv\Scripts\python.exe -m unittest tests.test_memory_n8_lifecycle -v`): **10 pruebas en 16.762s — OK (0 fallos, 0 errores, 0 omitidas)**.
- Suite aislada N8.1 (`$env:JARVIS_ENV="test"; .\venv\Scripts\python.exe -m unittest tests.test_memory_n8_persistence -v`): **19 pruebas en 16.923s — OK (0 fallos, 0 errores, 0 omitidas)**.
- Suite global completa (`$env:JARVIS_ENV="test"; .\venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -q`): **620 pruebas en 145.645s — OK (0 fallos, 0 errores, 0 omitidas)** frente a línea base de 610 (+10 pruebas nuevas, 0 regresiones).
- Integridad de datos reales: `data/memory_store.json` conserva **1.218 bytes**, SHA-256 `83640EAADFF5E33FEB9126F6EEBB9F7960F924F01BB3A1968468014223341222`; `data/` contiene únicamente `memory_store.json` (sin `data/experiences.db` ni `data/backups/`).

### 4. Límites y Pendientes (N8.3)

- N8.3 (captura automática `EXECUTION_VERIFIED` post-ejecución e integración de referencias N8 en N6/`GoalStore`) queda sin implementar y desactivada por política.
- Purga física limita su garantía al almacenamiento SQLite (`secure_delete=ON`, `wal_checkpoint`, `VACUUM`), sin garantía sobre wear-leveling SSD, NTFS ni copias VSS/OS previas.

---

## Fase N8.3 Subfase 1 — Persistencia SQLite v2, Idempotencia Estricta, `store_version_token` e Invalidación por Ejecución (2026-11-20)

### 1. Resumen de Implementación

Se implementó exclusivamente la base de persistencia v2 necesaria para habilitar las siguientes subfases de **N8.3 — Long-Term Memory & Experience**, manteniendo intactos `data/memory_store.json`, `N5`, `N6`, `N7`, los flujos de voz y `N8_AUTO_CAPTURE_ENABLED=False`. No se implementaron aún `ExperienceCaptureService`, `EphemeralGoalStore`, `MemorySnapshotGuard` ni integraciones en `GoalEngine` / `BrainManager` / `ChainOrchestrator` (reservados para Subfases 2, 3 y 4).

### 2. Archivos Creados y Modificados

- **Actualizados (`src/memory/`):**
  - `src/memory/models.py`: añadidos `MemoryAuditAction.REJECT_IDEMPOTENCY_CONFLICT`, `MemoryIdempotencyConflictError`, `ExecutionInvalidationResult`, campo opcional `canonical_unit_id` en `ExecutionVerificationEvidence` (excluido de `canonical_payload()` cuando es `None` para preservar hashes v1), `canonical_unit_id` e `idempotency_key` en `MemoryCreateInput` y `MemoryRecord`, y `store_version_token` en `StoreStats` y `MemoryRetrievalResult`.
  - `src/memory/policy.py`: añadidos `compute_default_canonical_unit_id()`, `compute_execution_idempotency_key()` y validación/derivación determinista en `MemoryPolicy.prepare_create()` (restringida estrictamente a `EXECUTION_VERIFIED`).
  - `src/memory/migrations.py`: subida de esquema `CURRENT_SCHEMA_VERSION = 2` y migración incremental/idempotente v1 -> v2 (`canonical_unit_id`, `idempotency_key`, backfill determinista sobre filas `EXECUTION_VERIFIED` previas, reemplazo de `idx_memories_active_exec_provenance` y restricción de `idx_memories_active_fingerprint` a `source != 'EXECUTION_VERIFIED'`, nuevos índices `idx_memories_idempotency_key`, `idx_memories_active_exec_unit` e `idx_memories_source_execution_id`, reconstrucción de `memory_audit` con preservación de historial para `REJECT_IDEMPOTENCY_CONFLICT`, y gestión atómica de `store_version_epoch` / `store_version_seq` / `store_version_token` en `store_metadata`).
  - `src/memory/store.py`: separada la idempotencia estricta `EXECUTION_VERIFIED` (verificación por `idempotency_key` + `_execution_payload_matches` + `REJECT_IDEMPOTENCY_CONFLICT` auditable sin alterar recuerdos ni `store_version_token`) del dedupe convencional por `content_fingerprint`; añadido `get_store_version_token()`, `invalidate_by_execution()`, `search_text_candidates_with_version()`, actualización atómica de `store_version_token` en todas las mutaciones y compatibilidad en `restore_from_backup()` para restaurar backups v1 y v2 con migración automática, chequeo FTS5 vía `_check_fts_sync()`, rotación de epoch garantizando secuencia creciente y `verify_integrity()` inmediato.
  - `src/memory/retriever.py`, `src/memory/engine.py`, `src/memory/__init__.py`: propagado `store_version_token` atómico en `MemoryRetriever.retrieve()`, expuestos `get_store_version_token()` e `invalidate_by_execution()` (bloqueado en modo privado con `MemoryPrivateModeError`) en `MemoryEngine` y actualizadas las exportaciones públicas.
- **Pruebas y Documentación:**
  - `tests/test_memory_n8_persistence.py`: actualizadas las 2 aserciones v1-específicas (`schema_migrations` `[1, 2]` y nueva semántica de cohabitación `EXPERIENCE`/`FACT` + conflicto auditable de idempotencia en v2).
  - `tests/test_memory_n8_v2_persistence.py`: nueva suite aislada de 5 pruebas integrales bajo `JARVIS_ENV=test` y `temp/jarvis_test_isolated/` (migración v1 -> v2 preservando datos y restauración de backup v1 con `verify_integrity()` inmediato, matriz completa de idempotencia y separación de dedupe, monotonía y rollback transaccional de `store_version_token`, `invalidate_by_execution()` idempotente y aislado por ejecución/privacidad, y concurrencia multihilo y multiproceso real).
  - `docs/N8_LONG_TERM_MEMORY.md`: documentado esquema v2, migración v1 -> v2, matriz de idempotencia, ciclo `store_version_token` y contrato de invalidación por rollback.
  - `prompts_history.txt`: registrado el prompt íntegro de N8.3 Subfase 1 (no versionado).

### 3. Verificaciones Realmente Ejecutadas (N8.3 Subfase 1)

- `py_compile` sobre todos los módulos de `src/memory/` y suites N8: **OK (código 0)**.
- Verificación de arranque liviano (`MemoryEngine` / `ExperienceStore` sin cargar `faster_whisper`, `sounddevice` ni `pyttsx3`): **OK (`lightweight_ok`)**.
- Suites aisladas N8 (`$env:JARVIS_ENV="test"; .\venv\Scripts\python.exe -m unittest tests.test_memory_n8_persistence tests.test_memory_n8_lifecycle tests.test_memory_n8_v2_persistence -v`): **34 pruebas en 46.154s — OK (0 fallos, 0 errores, 0 omitidas)**.
- Suite global completa (`$env:JARVIS_ENV="test"; .\venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -q`): **625 pruebas en 161.886s — OK (0 fallos, 0 errores, 0 omitidas)** frente a línea base de 620 (+5 pruebas nuevas sin regresiones).
- Integridad de datos reales verificada post-tests: `data/` contiene únicamente `memory_store.json` (**1.218 bytes**, SHA-256 `83640EAADFF5E33FEB9126F6EEBB9F7960F924F01BB3A1968468014223341222`); no existen `data/experiences.db` ni `data/backups/`.

### 4. Límites y Pendientes (Siguientes Subfases N8.3)

- **Pendiente Subfase 2:** `ExperienceCaptureService` y ganchos desacoplados en N5/N6 (`ChainOrchestrator` / `TransactionManager` / `FileEditorSkill` / `GoalSupervisor`) controlados por `N8_AUTO_CAPTURE_ENABLED`. *(Completada)*
- **Pendiente Subfase 3:** persistencia efímera / referencias N8 en `EphemeralGoalStore` (`GoalStore`).
- **Pendiente Subfase 4:** integración consultiva en `GoalEngine` / `TaskDecomposer` / `BrainManager` con `MemorySnapshotGuard` validado por `store_version_token` y pruebas E2E integrales.

---

## Fase N8.3 Subfase 2 — Contratos de Evidencia N5/N6, Captura Verificada e Invalidación por Rollback (2026-11-21)

### 1. Resumen de Implementación

Se implementó **N8.3 Subfase 2** conectando de forma desacoplada las ejecuciones verificadas de **N5** (`ChainOrchestrator` / `TransactionManager`) y **N6** (`GoalEngine` / `ExecutionSupervisor`) hacia el motor de memoria **N8** (`ExperienceCaptureService` -> `MemoryEngine` / `ExperienceStore`) mediante contratos inmutables versionados v2.0 y revocación automática ante rollbacks N5 confirmados. Se mantienen sin implementar ni activar las Subfases 3 y 4 (`EphemeralGoalStore` / referencias N8 en `GoalStore`, recuperación consultiva en `GoalEngine` / `BrainManager` e inyección de snapshot por `MemorySnapshotGuard`).

### 2. Archivos Creados y Modificados

- **Nuevos en `src/memory/`:**
  - `src/memory/capture_contracts.py`: definidos `CAPTURE_CONTRACT_VERSION = "2.0"`, `AUTHORIZED_VERIFIERS_BY_LAYER`, `FORBIDDEN_VERIFIER_MARKERS`, `FORBIDDEN_EVIDENCE_KEYS`, `MemoryEvidenceContractError`, `VerifiedOperationReference`, `VerifiedExecutionEvidenceContract` (`@dataclass(frozen=True)` con canonicalización y hashes SHA-256 `evidence_sha256` y `contract_sha256`), `AdaptationReport`, `CaptureUnitOutcome`, `CaptureBatchResult` y `RollbackInvalidationOutcome`. Rechaza booleanos (`{"verified": True}`), afirmaciones LLM/`internal_synthesis`, transcripts, prompts, contenido web crudo y secretos.
  - `src/memory/capture.py`: implementados `N5ExecutionEvidenceAdapter` (exige `ChainState.COMPLETED`, `approval_granted=True`, `approval_hash`, operaciones `APPLIED` con `after_sha256` post-escritura verificada y ausencia de rollback), `N6GoalEvidenceAdapter` (evalúa `COMPLETED` y `PARTIAL` tarea a tarea comprobando verificador objetivo autorizado, correspondencia de IDs, historial `task_completed` y artefactos reales sin promover `PARTIAL` a éxito global ni capturar `internal_synthesis`) y `ExperienceCaptureService` (puertas `N8_ENABLED` / `N8_AUTO_CAPTURE_ENABLED`, rechazo absoluto en modo privado sin tocar buffers privados, idempotencia v2, aislamiento de errores de SQLite/lock e invalidación/tombstone por rollback N5 confirmado).
- **Actualizados en `src/memory/`:**
  - `src/memory/store.py` y `src/memory/engine.py`: añadidos soporte de tombstones de ejecución revertida sobre `memory_audit` sin migración de esquema adicional (`_has_rollback_audit_tx`, `is_execution_invalidated()`, `mark_execution_rollback_tombstone()`) para impedir capturas tardías o en paralelo incluso tras reiniciar el proceso.
  - `src/memory/__init__.py`: exportados los 12 símbolos públicos nuevos de Subfase 2 y actualizado el docstring del paquete.
- **Cableado desacoplado en `src/brain/` y consola (`main.py`):**
  - `src/brain/chain_orchestrator.py`: soporte opcional `memory_capture_service` / `set_memory_capture_service()`, captura tras `execute_approved_plan()` en estado `COMPLETED` e invalidación tras rollback confirmado a `ROLLED_BACK` en `rollback_execution()` y `recover_pending_transactions()`, completamente aislados ante fallos.
  - `src/brain/goal_supervisor.py`: evidencia verificada explícita en `_execute_registered_skill` (`ExecutionSupervisorSkillVerifier`, IDs de tarea/plan/objetivo) y `_execute_via_chain_orchestrator` (`ExecutionSupervisorChainVerifier`, vínculo con `chain_execution_id`), más hook opcional post-`execute_plan()` para planes terminados en `COMPLETED` o `PARTIAL` (`not stopped_for_approval`).
  - `src/brain/goal_engine.py`, `src/brain/goal_console.py` y `main.py`: propagación opcional de `memory_capture_service` y creación perezosa condicionada `_ensure_memory_capture_service()` en `run_console_session` preservando arranque liviano sin módulos de voz ni escrituras innecesarias cuando la DB aún no existe y la autocaptura está deshabilitada.
- **Pruebas y Documentación:**
  - `tests/test_memory_n8_capture_and_rollback.py`: nueva suite aislada de 8 pruebas integrales bajo `JARVIS_ENV=test` y `temp/jarvis_test_isolated/` (determinismo/inmutabilidad/rechazo de falsificación en contratos, adaptador N5 real + rechazos, adaptador N6 `COMPLETED`/`PARTIAL` + rechazo LLM/síntesis/evidencia ajena, políticas de privacidad/`N8_AUTO_CAPTURE_ENABLED`/web cruda/secretos/aislamiento de fallos SQLite, rollback confirmado vs fallido/tombstones, concurrencia captura vs rollback sin huérfanos, E2E por consola `main.py` `/optimizar`->`/rollback`->`/goal`->`/plan ejecutar`->`/memoria` en subproceso limpio sin `faster_whisper`/`sounddevice`/`pyttsx3`, y delegación N6->N5->rollback + recovery).
  - `docs/N8_LONG_TERM_MEMORY.md`: agregada Sección 10 de contratos, adaptadores N5/N6, coordinador e invalidación/tombstones por rollback y actualizados alcance y comandos de test.
  - `prompts_history.txt`: registrado el prompt íntegro de N8.3 Subfase 2 (no versionado).

### 3. Verificaciones Realmente Ejecutadas (N8.3 Subfase 2)

- `py_compile` sobre todos los módulos modificados/nuevos y suite Subfase 2: **OK (código 0)**.
- Suite aislada Subfase 2 (`tests.test_memory_n8_capture_and_rollback`): **8 pruebas en 20.932s — OK** (después de los ajustes de suite, también validada dentro del bloque combinado y global).
- Batería combinada N8 + N6 + N5 (`tests.test_memory_n8_persistence tests.test_memory_n8_lifecycle tests.test_memory_n8_v2_persistence tests.test_memory_n8_capture_and_rollback tests.test_goal_engine tests.test_file_editor_and_chain tests.test_transaction_recovery`): **175 pruebas en 134.800s — OK**.
- Suite global completa (`$env:JARVIS_ENV="test"; .\venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -q`): **633 pruebas en 188.249s — OK (0 fallos, 0 errores, 0 omitidas)** respecto a línea base de 625 (+8 pruebas nuevas sin regresiones).
- Verificación de arranque liviano (`import sys, main, src.memory` + subproceso limpio en `run_console_session(..., enable_tts=False)`): **`NO_VOICE_MODULES_LOADED: []`** (`faster_whisper`, `sounddevice`, `pyttsx3`, `speech_recognition` ausentes).
- Integridad de datos reales verificada post-tests: `data/memory_store.json` permanece intacto en **1.218 bytes** (SHA-256 `83640EAADFF5E33FEB9126F6EEBB9F7960F924F01BB3A1968468014223341222`); no existen `data/experiences.db`, `data/backups/`, `data/transactions/` ni `data/goals/` (solo existe `data/file_editor_audit.jsonl` generado fuera de N8 durante la suite general).

### 4. Límites y Pendientes (Siguientes Subfases N8.3)

- **Subfase 3 completada *(2026-11-22)*:** integración consultiva N8 <-> N6, `EphemeralGoalStore`, sobres RAM-only `RuntimeN8Envelope`, separación estructural en `GoalStore`, modo privado completo y revalidación estricta.
- **Pendiente Subfase 4:** `MemorySnapshotGuard` validado por `store_version_token` y validación de vigencia de snapshot en `BrainManager` junto con pruebas E2E de cierre de N8.3.

---

## Proyecto J.A.R.V.I.S. — N8.3 Subfase 3 (2026-11-22): Integración Consultiva N8 <-> N6, Aislamiento de Contexto y Modo Privado

**Estado:** Implementada, verificada y lista para commit sobre `main` (después de `269e09d`). **N8.3 no se declara completa hasta cerrar la Subfase 4.**

### 1. Cambios Implementados

- **Puente consultivo y frontera estructural (`src/brain/goal_memory.py` — nuevo módulo):**
  - Implementados `GoalMemoryBridge`, `RuntimeN8Envelope`, `RuntimeN8MemoryItem`, `N8MemoryReference`, `RevisionMismatchPolicy.STRICT_INVALIDATE` y `RevalidationReport`.
  - `_runtime_n8_envelopes` conserva en RAM textos/resúmenes de recuerdos recuperados, redactados en `__repr__` y `__str__` para prevenir fugas en logs o tracebacks, aislados por `plan_id`/`goal_id` y liberados al finalizar/cancelar/reemplazar/salir de modo privado.
  - En `GoalStore` y eventos WAL solo se admiten referencias trazables mínimas (`ALLOWED_PERSISTED_N8_EXTRA_KEYS`: `n8_query_id`, `n8_memory_refs`, `n8_trust_boundary`, `n8_last_revalidated_at`), excluyendo toda clave prohibida (`FORBIDDEN_PERSISTED_N8_KEYS`), anotaciones RAM LLM+N8 (`ephemeral_annotations`) y parámetros de skills no anclados de forma independiente (`collect_grounded_anchors()` / `separate_grounded_skill_params()`), sin heurísticas n-gram.
  - Presupuestos por modelo: `NORMAL` (`N8_TEXT_MAX_ITEMS=5`, `N8_TEXT_MAX_CHARS=1500`) y `LOCAL_3B` (`N8_VOICE_MAX_ITEMS=3`, `N8_VOICE_MAX_CHARS=600`), alcance `PROJECT > APP_OR_GAME > GLOBAL` y tolerancia a fallos de N8 sin bloquear N6.
  - Revalidación obligatoria caso a caso mediante `MemoryEngine.get_memory()` antes de planificación/síntesis/replanificación/reanudación, rechazando `REVOKED`, `EXPIRED` (por estado y timestamp), `SUPERSEDED`, purgados, pendientes y revisiones/hashes modificados sin auto-sustitución. Documentada la semántica de frontera TOCTOU por etapas y reservado `MemorySnapshotGuard` + vigencia en `BrainManager` para la Subfase 4.

- **Almacén efímero en RAM y endurecimiento de persistencia (`src/brain/goal_models.py`, `src/brain/goal_persistence.py`):**
  - Añadido `EphemeralGoalStore` 100 % en RAM (cambia automáticamente cuando `is_private_mode` está activo, con cero `.goal.json`/`.wal.jsonl`/auxiliares y descarte limpio en `on_private_mode_exited()`). Planes marcados `is_ephemeral_private=True` lanzan `GoalPersistenceError` si intentan guardarse en `GoalStore`.
  - Endurecidos `GoalStore`, `extract_persistable_plan_dict` y `sanitize_wal_payload` conservando intacto `n7_untrusted_envelope` para N7 y eliminando claves N8/N7 volátiles.

- **Integración real en planificación, supervisión, consola y modo privado (`src/brain/goal_understanding.py`, `src/brain/goal_planner.py`, `src/brain/goal_supervisor.py`, `src/brain/goal_engine.py`, `src/brain/goal_console.py`, `src/memory/console.py`, `main.py`):**
  - Consulta consultiva integrada en planificación inicial (`create_goal_and_plan`, `clarify_pending_goal`, `interpret_goal`), replanificación (`ExecutionSupervisor._attempt_replan`) y reanudación (`GoalEngine.resume_plan`, comando `/plan reanudar`), evitando consultas redundantes dentro del mismo objetivo.
  - Propagación estricta de recuerdos únicamente vía `BrainManager.query_raw(..., untrusted_context=...)` (jamás `system_prompt`), subordinados a restricciones N5 (`read_only`, `requires_approval`) y al estado real observado por N7.
  - Propagación bidireccional de `/privado` (Opción A), `/privado --sin-historial` (bloqueo total + limpieza RAM) y `/privado off` entre consola N8, API Python (`set_private_mode`, `is_private_mode_fn`) y N6 compartiendo una única instancia perezosa de `MemoryEngine` en `main.py` sin importar módulos de voz.

- **Reconciliación de diseño N8.2 -> N8.3 y pruebas integrales (`tests/test_memory_n8_consultative_n6.py`, `tests/test_memory_n8_lifecycle.py`, `tests/test_memory_n8_capture_and_rollback.py`):**
  - Actualizada la expectativa histórica de N8.2 en `tests/test_memory_n8_lifecycle.py` (`test_private_mode_no_log_or_exception_leaks_and_blocks_persistent_commands`): anteriormente en N8.2 `/goal` se rechazaba durante `/privado` por no existir aún `EphemeralGoalStore`; ahora `/optimizar` y `/rollback` siguen bloqueados mientras `/goal` opera exclusivamente en RAM mediante `EphemeralGoalStore` (bajo `JARVIS_ENV=test` y `JARVIS_TEST_TEMP_ROOT` aislado) sin crear archivos en disco y descartándose en `/privado off`.
  - Aislado `JARVIS_TEST_TEMP_ROOT` en `tests/test_memory_n8_capture_and_rollback.py` respecto a su directorio temporal de test.
  - Creada `tests/test_memory_n8_consultative_n6.py` (7 suites integrales sobre EphemeralGoalStore, presupuestos 3B/Normal, sobres RAM, frontera estructural adversarial, límites de confianza N7/N5/`system_prompt`, revalidación completa/TOCTOU/reanudación y modo privado `/privado` vs `--sin-historial` E2E).

### 2. Documentación Actualizada

- [docs/N8_LONG_TERM_MEMORY.md](file:///e:/Tiziano%20Facultad/J.A.R.V.I.S/jarvis-core/docs/N8_LONG_TERM_MEMORY.md): actualizada cabecera de alcance, tabla de módulos (`src/brain/goal_memory.py`, `src/brain/goal_persistence.py`, `src/memory/console.py`), Sección 11 completa y comandos de verificación.
- [docs/N6_AUTONOMOUS_GOAL_ENGINE.md](file:///e:/Tiziano%20Facultad/J.A.R.V.I.S/jarvis-core/docs/N6_AUTONOMOUS_GOAL_ENGINE.md): incorporados `goal_memory.py`, `EphemeralGoalStore`, `/plan reanudar` y regla de integración consultiva N8 / modo privado.
- `prompts_history.txt`: agregado el prompt exacto de N8.3 Subfase 3 (excluido de Git).

### 3. Verificaciones Realmente Ejecutadas (N8.3 Subfase 3)

- `py_compile` sobre todos los módulos modificados/nuevos y tests de Subfase 3: **OK (código 0)**.
- Suite aislada N8.3 Subfase 3 (`tests.test_memory_n8_consultative_n6`): **7 pruebas en 13.283s — OK**.
- Batería combinada N8 + N6 + N7 + N5 (`tests.test_memory_n8_persistence`, `tests.test_memory_n8_lifecycle`, `tests.test_memory_n8_v2_persistence`, `tests.test_memory_n8_capture_and_rollback`, `tests.test_memory_n8_consultative_n6`, `tests.test_goal_engine`, `tests.test_context_awareness_n7`, `tests.test_file_editor_and_chain`, `tests.test_transaction_recovery`): **213 pruebas en 157.946s — OK**.
- Suite global completa (`$env:JARVIS_ENV="test"; .\venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -q`): **640 pruebas en 219.348s — OK (0 fallos, 0 errores, 0 omitidas)** (línea base previa 633 -> +7 pruebas nuevas).
- Verificación de arranque liviano (`import main, src.brain.goal_engine, src.memory` + subprocesos sin voz): **`VOICE_LOADED: []`** (`faster_whisper`, `sounddevice`, `pyttsx3`, `speech_recognition` sin cargar).
- Integridad de `data/` verificada tras la suite completa: `data/memory_store.json` intacto en **1.218 bytes** (SHA-256 `83640EAADFF5E33FEB9126F6EEBB9F7960F924F01BB3A1968468014223341222`); entradas presentes en `data/`: únicamente `['file_editor_audit.jsonl', 'memory_store.json']` (ausentes `data/experiences.db` y `data/goals/`).
- Pruebas físicas de hardware de audio/micrófono/altavoz: **no ejecutadas ni aplicables** en esta subfase (sin cambios en módulos de voz; arranque textual validado explícitamente sin carga de audio).

### 4. Límites y Pendientes (Siguiente Subfase N8.3)

- **Pendiente Subfase 4 (cierre N8.3):** `MemorySnapshotGuard` validado por `store_version_token`, validación de vigencia de snapshot en `BrainManager` y pruebas E2E de cierre integral N8.3.
