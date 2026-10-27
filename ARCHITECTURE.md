# Arquitectura Técnica y Flujo de Procesamiento de J.A.R.V.I.S.

> **Documento Maestro para Guía de Estudio y Defensa Académica del Proyecto**  
> *Autor: Tiziano & Antigravity (Google DeepMind Agentic Pair Programming)*  
> *Entorno validado: Windows 10/11 x64 | GPU NVIDIA GeForce GTX 1660 (6 GB VRAM) | Python 3.12*

---

## 1. Visión General de la Arquitectura

J.A.R.V.I.S. (*Just A Rather Very Intelligent System*) está diseñado bajo el paradigma **Local-First**, priorizando la privacidad, la autonomía sin conexión a internet y una latencia perceptual mínima (<1.5s entre final de voz y primera locución).

### Principio de Asignación de Recursos de Hardware:
- **CPU (Bajo consumo en reposo):** openWakeWord (80 ms chunks) y cálculo continuo de RMS (VAD) operan en procesador consumiendo menos del 4% de CPU.
- **GPU NVIDIA GeForce GTX 1660 (6 GB VRAM):**
  - **faster-whisper (`base` / `float16`):** Inferencia de voz a texto en ~350 ms, ocupando ~950 MB de VRAM.
  - **Ollama local (`qwen2.5-coder:3b` o `llama3.2:3b`):** Inferencia del LLM ocupando ~2.2 GB de VRAM.
  - **Consumo Total de VRAM:** ~3.15 GB de 6.0 GB disponibles (52% de carga máxima), previniendo saturación de memoria, thrashing o cierre de procesos por Out-Of-Memory (OOM).

---

## 2. Diagrama de Flujo de Voz End-to-End

```text
                  ┌────────────────────────────────────────┐
                  │          ENTORNO ACÚSTICO              │
                  │   (Voz humana del usuario + Ruido)     │
                  └──────────────────┬─────────────────────┘
                                     │ Captura continua (16 kHz, Mono, int16)
                                     ▼
                ==============================================
                ESTADO 1: ESCUCHA PASIVA EN REPOSO (Tray Azul)
                ==============================================
                                     │
                                     ▼
                  ┌────────────────────────────────────────┐
                  │    openWakeWord Detector (En CPU)      │
                  │    - Fragmentos: 1280 muestras (80 ms) │
                  │    - Umbral de confianza: >= 0.35      │
                  │    - Modelo: "Hey Jarvis"              │
                  └──────────────────┬─────────────────────┘
                                     │
                        ¿Detectó "Hey Jarvis"?
                         /                \
                       NO                  SÍ
                       /                    \
             (Permanece en reposo)           ▼
                               ┌─────────────────────────────────┐
                               │ Transición Auditiva y Visual:   │
                               │ 1. Beep de 950 Hz / 120 ms      │
                               │ 2. Tray cambia a VERDE          │
                               └────────────────┬────────────────┘
                                                │
                =================================================
                ESTADO 2: PROCESAMIENTO ACTIVO (Tray Verde/Amar.)
                =================================================
                                                │
                                                ▼
                  ┌────────────────────────────────────────┐
                  │  AudioRecorder con VAD Inteligente     │
                  │  1. Calibración ambiente: 400 ms       │
                  │  2. Umbral dinámico: max(RMS*1.6, 120) │
                  │  3. Pre-roll Buffer: 250 ms circulares │
                  │  4. Corte por silencio: 2.5 s continuo │
                  │  5. Genera: Archivo temporal .wav      │
                  └─────────────────────┬──────────────────┘
                                        │
                                        ▼
                  ┌────────────────────────────────────────┐
                  │ faster-whisper (GPU CUDA / CTranslate2)│
                  │ - Idioma forzado: "es" (español)       │
                  │ - beam_size=5 | vad_filter=True        │
                  │ - Filtro probabilístico anti-alucinante│
                  │ - Retorna: Cadena de texto limpio      │
                  └─────────────────────┬──────────────────┘
                                        │
                                        ▼
                  ┌────────────────────────────────────────┐
                  │ Normalización y Registro en Memoria    │
                  │ - normalize_text(): Quita tildes/puntuac│
                  │ - ConversationMemory: Ventana de 8 turn│
                  └─────────────────────┬──────────────────┘
                                        │
                                        ▼
                  ┌────────────────────────────────────────┐
                  │   IntentRouter (Function Calling JSON) │
                  │   ¿La orden requiere herramientas?     │
                  └─────────┬────────────────────┬─────────┘
                            │                    │
              [action: "call_tool"]        [action: "chat"]
                            │                    │
                            ▼                    ▼
             ┌────────────────────────┐  ┌────────────────────────┐
             │     SkillManager       │  │ Cerebro LLM Primario   │
             │ - system_time          │  │ (Ollama qwen2.5:3b)    │
             │ - app_launcher         │  │       ó                │
             │ - system_info          │  │ Fallback Nube          │
             │ - web_search           │  │ (Google Gemini 2.0)    │
             │ - windows_skills       │  └───────────┬────────────┘
             └──────────────┬─────────┘              │
                            │                        │
                            └───────────┬────────────┘
                                        │ Respuesta generada (str)
                                        ▼
                  ┌────────────────────────────────────────┐
                  │  Limpiador Anti-Markdown para Voz      │
                  │  - Elimina ```, `, #, **, -, enlaces   │
                  │  - Sanitiza puntuación y saltos        │
                  └─────────────────────┬──────────────────┘
                                        │
                                        ▼
                  ┌────────────────────────────────────────┐
                  │  Circuit Breaker TTS (Salida de Audio) │
                  │  Tray cambia a ROJO (Hablando)         │
                  └─────────┬────────────────────┬─────────┘
                            │                    │
                 [Normal / Conexión OK]   [Fallo 403 / Red / Offline]
                            │                    │
                            ▼                    ▼
             ┌────────────────────────┐  ┌────────────────────────┐
             │ Edge-TTS (Microsoft)   │  │ Windows System.Speech  │
             │ Voz Neuronal Tomás     │  │ (PowerShell nativo)    │
             │ Reproducción vía       │  │ Voz Sabina Desktop     │
             │ pygame.mixer (asínc.)  │  │ Sin dependencias ext.  │
             └────────────────────────┘  └────────────────────────┘
                                        │
                                        ▼
                  ┌────────────────────────────────────────┐
                  │ REGLA DE ORO ANTI-ECO:                 │
                  │ 1. Espera a que termine la locución    │
                  │ 2. Purga búfer de audio (500 ms)       │
                  │ 3. Tray vuelve a AZUL (Reposo)         │
                  │ 4. Reactiva WakeWordDetector           │
                  └────────────────────────────────────────┘
```

---

## 3. Desglose Módulo por Módulo

### 3.1. Detección Manos Libres (`src/audio/wake_word.py`)
- **Tecnología:** `openwakeword` ejecutado localmente con ONNX Runtime.
- **Funcionamiento:** Escucha fragmentos de 1280 muestras (80 ms a 16 kHz). Si el puntaje sobrepasa `0.35`, emite un pulso sonoro mediante `winsound.Beep(950, 120)` para que el usuario sepa que tiene la atención del asistente sin necesidad de una locución repetitiva como "¿Sí, señor?".

### 3.2. Grabación y VAD Adaptativo (`src/audio/recorder.py`)
- **Problema resuelto:** Las grabaciones fijas de 4 segundos cortaban a los usuarios lentos y obligaban a esperar en silencio a los usuarios rápidos.
- **Solución técnica:**
  - **Calibración inicial de 400 ms:** Mide el piso de ruido ambiental (ventiladores, teclado) calculando el RMS medio.
  - **Umbral adaptativo:** `umbral = max(ambient_rms * 1.6, 120.0)`.
  - **Pre-Roll de 250 ms:** Un `collections.deque(maxlen=N)` conserva el audio justo antes de que el usuario empiece a hablar para no perder la primera consonante.
  - **Corte inteligente:** Al detectar 2.5 segundos continuos por debajo del umbral, cierra el WAV y lo remite inmediatamente a Whisper.

### 3.3. Transcripción Acelerada por Hardware (`src/audio/stt.py`)
- **Tecnología:** `faster-whisper` (CTranslate2 en C++).
- **Parámetros clave:**
  - `model_size="base"`, `device="cuda"`, `compute_type="float16"`.
  - `condition_on_previous_text=False`: Evita que Whisper quede atrapado en bucles alucinatorios repitiendo respuestas previas.
  - `vad_filter=True`: Silero VAD incorporado para omitir fragmentos no verbales.
  - **Filtros probabilísticos:** Omitimos segmentos con `no_speech_prob > 0.65` o `avg_logprob < -1.0`.

### 3.4. Memoria Conversacional (`src/brain/memory.py`)
- **Tecnología:** `ConversationMemory` con `deque(maxlen=16)` (8 turnos completos usuario/asistente).
- **Propósito:** Permite responder preguntas contextuales como "¿Cómo me llamo?", "Abre lo que te pedí antes" o "¿Cuál es mi color favorito?" sin saturar la ventana de contexto del LLM.
- **Comandos de usuario integrados:** Decir "olvida lo que hablamos" o "reiniciar contexto" invoca `memory.clear()`.

### 3.5. Enrutador de Intenciones y Function Calling (`src/brain/intent_router.py`)
- **Contrato JSON Estricto:** Obliga al modelo a responder únicamente en formato JSON:
  - `{"action": "chat", "response": "<texto>"}`
  - `{"action": "call_tool", "tool_name": "<skill>", "parameters": {<args>}}`
- **Tolerancia a Fallos:** Extrae objetos balanceados con expresiones regulares, limpia delimitadores markdown (` ```json `), descarta llaves duplicadas y provee fallback seguro si el JSON llega truncado.

### 3.6. Motor de Habilidades (`skills/` y `src/tools/skill_manager.py`)
- **Patrón de Diseño:** *Command Pattern* desacoplado derivado de `BaseSkill`.
- **Catálogo Activo:**
  1. `TimeSkill`: Fecha y hora natural en español.
  2. `AppLauncherSkill`: Lista blanca de ejecutables seguros (Notepad, Calc, Code, Navegadores).
  3. `SystemInfoSkill`: Diagnóstico en tiempo real de CPU y memoria RAM con `psutil`.
  4. `WebSearchSkill`: Búsquedas en Google y YouTube sin APIs externas mediante `webbrowser`.
  5. `VolumeControlSkill`: Subir, bajar y mutear audio mediante teclas virtuales de Windows (`ctypes`).
  6. `MediaControlSkill`: Play, pause, skip y prev en reproductores de Windows.
  7. `ClipboardSkill`: Lectura y escritura en el portapapeles nativo de Windows.
  8. `SystemPowerSkill`: Bloqueo inmediato de sesión de Windows (`LockWorkStation`).

### 3.7. Síntesis de Voz y Circuit Breaker (`src/audio/tts.py`)
- **Motor Principal:** `edge-tts` (voz `es-AR-TomasNeural` / `es-ES-AlvaroNeural`) con streaming y reproducción vía `pygame.mixer`.
- **Circuit Breaker:** Si Bing/Microsoft devuelve un error 403 (*WSServerHandshakeError*) o no hay internet, se conmuta de forma permanente al motor offline nativo de Windows:
  - Invoca `System.Speech.Synthesis.SpeechSynthesizer` mediante PowerShell en segundo plano.
  - Filtra estrictamente por voces hispanas (`Culture -eq 'es'` o nombres como Helena, Sabina, Raul) para evitar la voz robótica en inglés.
  - Ejecución mediante subproceso sin límite de tiempo, garantizando que jamás se corten respuestas largas.

### 3.8. Interfaz en Bandeja del Sistema (`src/ui/tray.py`)
- **Tecnología:** `pystray` + `PIL (Pillow)` en un hilo independiente (`daemon=True`).
- **Estados Visuales:**
  - 🔵 **Azul (Reposo):** Esperando "Hey Jarvis".
  - 🟢 **Verde (Escuchando):** Micrófono activo capturando la orden.
  - 🟡 **Amarillo (Procesando):** Whisper transcribiendo y LLM razonando.
  - 🔴 **Rojo (Hablando):** Sintetizador reproduciendo respuesta (anti-eco activo).
- **Menú Contextual:** Ver estado, limpiar memoria conversacional y apagado ordenado.

---

## 4. Matriz de Tolerancia a Fallos y Modo Degradado

| Fallo Simulado | Componente Afectado | Comportamiento del Sistema | Resultado para el Usuario |
|---|---|---|---|
| Caída del servidor local Ollama | `OllamaBrain` | Deriva automáticamente a `GeminiBrain` en la nube | Respuesta sin interrupción |
| Sin conexión a internet | `GeminiBrain` y `edge-tts` | Conmuta a Ollama local y TTS nativo `System.Speech` | Asistente 100% operativo offline |
| Error 403 en Bing Edge-TTS | `TextToSpeech` | Circuit Breaker activa voz nativa de Windows (Sabina) | Locución instantánea sin error |
| Micrófono en silencio o ruido puro | `AudioRecorder` | VAD detecta ausencia de voz (>2.5s) y aborta transcripción | Retorna a reposo sin trabarse |
| JSON malformado del LLM | `IntentRouter` | Regex parser repara o deriva a chat plano | No hay excepciones en pantalla |
| Alucinación de Whisper | `SpeechToText` | Filtro por `no_speech_prob` y longitud mínima | Se descartan chasquidos residuales |
