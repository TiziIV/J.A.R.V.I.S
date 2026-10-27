# J.A.R.V.I.S. (Just A Rather Very Intelligent System)

> Asistente virtual por voz modular, privado y local-first para Windows, diseñado para convivir fluidamente en hardware de consumo (NVIDIA GeForce GTX 1660 de 6 GB VRAM) con aceleración CUDA, fallback inteligente en la nube y control total del sistema.

---

## 🌟 Características Principales

1. **Detección Manos Libres Continua (Hands-Free Wake Word):**
   - Detección en CPU ultra-liviana (<4% CPU) de la palabra clave *"Hey Jarvis"* mediante `openwakeword`.
   - Transición auditiva suave con pitido no intrusivo (`winsound.Beep`) en lugar de locuciones repetitivas.

2. **Entrada de Voz Inteligente (VAD + Whisper GPU):**
   - Calibración automática de ruido ambiente (400 ms) y búfer circular pre-roll de 250 ms.
   - Detección de Actividad de Voz (VAD) adaptativa con corte automático por silencio (2.5 s).
   - Transcripción acelerada por hardware con `faster-whisper` (CTranslate2 en CUDA `float16`) en ~350 ms.
   - Filtros probabilísticos anti-alucinaciones (`no_speech_prob < 0.65`, `avg_logprob > -1.0`).

3. **Cerebro Híbrido Resiliente (Local-First + Cloud Fallback):**
   - **Primario Local:** Ollama (`qwen2.5-coder:3b` o `llama3.2:3b`) con inferencia local en GPU (<2.2 GB VRAM).
   - **Respaldo Nube:** Google Gemini (`gemini-2.0-flash` con fallback a `gemini-1.5-flash`) en caso de saturación o caída del servicio local.
   - **Memoria Conversacional:** Ventana deslizante de 8 turnos (16 mensajes) con soporte para comandos como *"olvida lo que hablamos"*.

4. **Enrutamiento de Intenciones y Function Calling:**
   - Módulo `IntentRouter` con contrato estricto JSON y regex sanitizers tolerantes a bloques markdown o respuestas truncadas.
   - Ejecuta herramientas de forma autónoma sin colgar el ciclo de audio.

5. **Ecosistema de Habilidades (Skills Engine):**
   - **Información del Sistema:** Fecha y hora (`system_time`), telemetría de CPU y memoria RAM con psutil (`system_info`).
   - **Lanzador de Apps:** Whitelist segura de aplicaciones Windows (`app_launcher`: Notepad, Calculadora, VS Code, Edge, Chrome).
   - **Navegación Web:** Búsqueda instantánea en Google o YouTube (`web_search`).
   - **Control de Windows:** Volumen maestro (`volume_control`), reproducción multimedia (`media_control`), portapapeles (`clipboard_manager`), y bloqueo de pantalla (`system_power`).

6. **Síntesis de Voz (TTS) con Circuit Breaker:**
   - **Voz Neuronal Primaria:** `edge-tts` (Tomás / Álvaro) con reproducción asíncrona mediante `pygame.mixer`.
   - **Circuit Breaker Blindado:** Si Bing devuelve error 403 o se pierde la conexión, conmuta permanentemente y sin demoras al motor nativo offline de Windows (`System.Speech` / PowerShell) seleccionando voces hispanas (Sabina Desktop).
   - **Anti-Eco Estricto:** Purga y validación de descriptores de audio antes de reabrir el micrófono.

7. **Interfaz en Bandeja del Sistema (System Tray):**
   - Icono dinámico con código de colores según el estado:
     - 🔵 **Azul:** Reposo (esperando *Hey Jarvis*).
     - 🟢 **Verde:** Escuchando voz del usuario.
     - 🟡 **Amarillo:** Procesando orden / LLM.
     - 🔴 **Rojo:** Hablando por sintetizador (micrófono cerrado).
   - Menú contextual: Ver estado, limpiar memoria conversacional y salida ordenada.

---

## 💻 Requisitos de Hardware y Entorno

- **Sistema Operativo:** Windows 10 u 11 (64 bits).
- **GPU:** NVIDIA GeForce GTX 1660 (o superior) con 6 GB de VRAM y soporte CUDA Toolkit 12.x.
- **Python:** Versión 3.12 (64-bit).
- **Consumo de VRAM medido en producción:**
  - Whisper GPU: ~950 MB VRAM.
  - Ollama qwen2.5-coder:3b: ~2.2 GB VRAM.
  - **Total:** ~3.15 GB / 6 GB (52% de VRAM libre para el sistema).

---

## 🚀 Inicio Rápido

### 1. Diagnóstico de Pre-Vuelo (5 segundos)
Antes de iniciar, puedes verificar el estado de todo tu hardware, drivers de GPU, micrófono y variables corriendo:

```powershell
python check_health.py
```

Verás una tabla visual de diagnóstico con el reporte de CUDA, CTranslate2, Ollama, `.env`, micrófono y voces de Windows.

### 2. Lanzadores Autónomos para Windows
No necesitas recordar comandos de consola cada vez que uses el asistente:

- **Modo Consola Interactivo:** Haz doble clic en `run_jarvis.bat`. Activa automáticamente el entorno virtual y lanza el asistente mostrando los logs en tiempo real.
- **Modo Silencioso en Segundo Plano:** Haz doble clic en `run_jarvis_background.vbs`. Inicia J.A.R.V.I.S. directamente en la bandeja del sistema (System Tray) sin mostrar ventanas de terminal.
  - *Tip para inicio automático con Windows:* Presiona `Win + R`, escribe `shell:startup` y coloca un acceso directo a `run_jarvis_background.vbs`.

---

## 📁 Estructura del Repositorio

```text
jarvis-core/
├── config/
│   ├── settings.py         # Configuración centralizada, .env y registro de DLLs CUDA
│   ├── prompts.py          # Prompt maestro compartido (paridad Ollama/Gemini)
│   ├── tools_schema.py     # Catálogo y esquemas JSON para Function Calling
│   └── wake_config.py      # Ajustes de umbrales y buffer de openWakeWord
├── src/
│   ├── audio/
│   │   ├── recorder.py     # Grabador inteligente con VAD adaptativo y pre-roll
│   │   ├── stt.py          # Transcripción con faster-whisper en CUDA
│   │   ├── tts.py          # Síntesis edge-tts con Circuit Breaker a System.Speech
│   │   └── wake_word.py    # Detección local en CPU de palabra de activación
│   ├── brain/
│   │   ├── ollama_client.py # Cliente local Ollama con streaming
│   │   ├── gemini_client.py # Cliente Google Gemini en la nube (respaldo)
│   │   ├── intent_router.py # Enrutador de intenciones y reparador de JSON
│   │   └── memory.py        # Memoria conversacional de ventana deslizante
│   ├── tools/
│   │   └── skill_manager.py # Orquestador y registro dinámico de skills
│   ├── ui/
│   │   └── tray.py         # Interfaz dinámica en System Tray con pystray y PIL
│   └── utils/
│       └── logger.py       # Logger centralizado anti-duplicación (consola y archivo UTF-8)
├── skills/
│   ├── base_skill.py       # Contrato abstracto BaseSkill
│   ├── system_skills.py    # TimeSkill, AppLauncherSkill, SystemInfoSkill, WebSearchSkill
│   └── windows_skills.py   # VolumeControl, MediaControl, Clipboard, SystemPower
├── tests/                  # Suite integral de pruebas unitarias (188 tests)
├── check_health.py         # Script de diagnóstico de pre-vuelo en 5 segundos
├── build_exe.py            # Empaquetador protegido con PyInstaller
├── run_jarvis.bat          # Lanzador rápido en consola
├── run_jarvis_background.vbs # Lanzador silencioso en bandeja
├── ARCHITECTURE.md         # Documento maestro y diagramas de flujo para defensa académica
├── CONTEXT.md              # Bitácora técnica del estado del repositorio
└── main.py                 # Ciclo principal de conversación y orquestación
```

---

## 🧪 Suite de Pruebas Unitarias

El repositorio cuenta con **188 pruebas unitarias automatizadas** que cubren resiliencia de audio, streaming, Circuit Breaker, recuperación ante errores 403, manejo seguro de tipos en skills y estrés de memoria:

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
```

**Resultado:** `188 tests in ~3.5s - OK (100% de éxito)`.

---

## 🛡️ Licencia y Créditos
Desarrollado para propósitos académicos y de uso personal. Todos los componentes priorizan la soberanía de datos y la privacidad del usuario.
