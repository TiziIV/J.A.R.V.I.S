# J.A.R.V.I.S. (Just A Rather Very Intelligent System)

Asistente virtual modular de voz para Windows, diseñado con una arquitectura **local-first**, aceleración por GPU NVIDIA y respaldo opcional en la nube.

---

## 🌟 Características Principales

1. **Entrada de Voz (STT - Speech-to-Text):**
   - Captura inteligente de audio mediante detección de actividad de voz (VAD) basada en energía RMS.
   - Transcripción local ultrarrápida en español utilizando `faster-whisper` optimizado con aceleración por hardware CUDA (GPU NVIDIA `float16`).

2. **Cerebro Híbrido (LLM Providers):**
   - **Cerebro Local (Primario):** Ollama ejecutando modelos optimizados (ej. `Qwen 2.5 Coder 3B` o Llama) configurado mediante variables de entorno.
   - **Cerebro en la Nube (Respaldo):** Google Gemini (`gemini-1.5-flash` / `gemini-3.8-flash`) para garantizar disponibilidad ante fallos locales.

3. **Salida de Voz (TTS - Text-to-Speech):**
   - Síntesis neuronal de alta calidad y naturalidad en español usando `edge-tts`.
   - Ejecución asíncrona en un hilo dedicado con gestión robusta de archivos temporales y resiliencia ante bloqueos de archivos en Windows.

4. **Motor de Habilidades (Skills Engine):**
   - Arquitectura modular basada en contratos abstractos (`BaseSkill`).
   - Herramientas nativas de control del sistema en Windows:
     - Consulta de fecha y hora actual (`system_time`).
     - Apertura segura de aplicaciones mediante listas blancas (`app_launcher`: bloc de notas, calculadora, VS Code, navegador).
     - Monitoreo de recursos hardware en tiempo real (`system_info` mediante `psutil` para CPU, RAM y GPU).
     - Búsquedas web automatizadas (`web_search`).

5. **Enrutador Inteligente (Function Calling):**
   - Módulo `IntentRouter` asistido por esquemas estructurados de herramientas (`config/tools_schema.py`).
   - Clasificación autónoma de intenciones del usuario y generación de llamadas estructuradas en JSON puro (`chat` vs `call_tool`).

---

## 💻 Requisitos del Sistema y Hardware Validado

- **Sistema Operativo:** Windows 10 / 11 (64-bit).
- **GPU (Validada):** NVIDIA GeForce GTX 1660 (6 GB de VRAM) con soporte CUDA Toolkit 12.1 y controladores actualizados.
- **Memoria RAM:** 16 GB - 24 GB recomendados para la ejecución simultánea de Ollama y Whisper.
- **Python:** Versión 3.12 (64-bit).

---

## 📁 Estructura del Proyecto

```text
jarvis-core/
├── config/
│   ├── settings.py        # Configuración centralizada y carga de .env
│   ├── prompts.py         # Prompts de sistema compartidos
│   └── tools_schema.py    # Esquemas de herramientas para Function Calling
├── src/
│   ├── audio/
│   │   ├── recorder.py    # Captura de micrófono con VAD (RMS)
│   │   ├── stt.py         # Transcripción local con faster-whisper (CUDA)
│   │   └── tts.py         # Síntesis y reproducción con edge-tts
│   ├── brain/
│   │   ├── ollama_client.py # Adaptador para Ollama local
│   │   ├── gemini_client.py # Adaptador para Gemini cloud
│   │   └── intent_router.py # Enrutador de intenciones y Function Calling
│   └── tools/
│       └── skill_manager.py # Gestor y ejecutor de habilidades del sistema
├── skills/
│   ├── base_skill.py      # Clase abstracta base para habilidades
│   ├── system_skills.py   # Implementaciones (Time, AppLauncher, SystemInfo, WebSearch)
│   └── README.md          # Documentación del subsistema de skills
├── main.py                # Orquestador principal del ciclo conversacional
├── requirements.txt       # Dependencias del proyecto
├── .env.example           # Plantilla de variables de entorno
├── AGENTS.md              # Reglas y directrices de desarrollo para agentes
└── CONTEXT.md             # Estado técnico actual y bitácora de arquitectura
```

---

## ⚙️ Instalación y Puesta en Marcha

1. **Clonar el repositorio y abrir la terminal en la raíz:**
   ```bash
   git clone <url-del-repositorio>
   cd jarvis-core
   ```

2. **Crear y activar el entorno virtual de Python:**
   ```bash
   python -m venv venv
   # En Windows PowerShell:
   .\venv\Scripts\Activate.ps1
   ```

3. **Instalar las dependencias (con soporte CUDA para PyTorch y faster-whisper):**
   ```bash
   pip install --upgrade pip
   pip install -r requirements.txt
   ```

4. **Configurar las variables de entorno:**
   Copia el archivo de ejemplo `.env.example` a `.env` y ajusta tus claves (Gemini API Key, URLs de Ollama, tamaños de modelos Whisper):
   ```env
   GEMINI_API_KEY=tu_clave_aqui
   OLLAMA_BASE_URL=http://localhost:11434
   OLLAMA_MODEL=qwen2.5:3b-instruct-q4_K_M
   WHISPER_MODEL_SIZE=base
   WHISPER_DEVICE=cuda
   WHISPER_COMPUTE_TYPE=float16
   ```

5. **Iniciar J.A.R.V.I.S.:**
   Asegúrate de que Ollama esté ejecutándose localmente y luego ejecuta:
   ```bash
   python main.py
   ```

---

## 🏛️ Gobierno, Versiones y Roadmap

- **Sistema de Versiones:** Cada 3 fases completadas y verificadas se emite un Tag/Release de versión estable en Git.
- **Documentación Viva:**
  - `AGENTS.md`: Contiene las reglas estrictas de desarrollo, arquitectura y estilo obligatorias para cualquier asistente o desarrollador.
  - `CONTEXT.md`: Mantiene el estado técnico actualizado de archivos, verificaciones y tareas pendientes.
- **Bitácora de Prompts:** `prompts_history.txt` registra de forma local y acumulativa cada instrucción ejecutada (excluido de control de versiones).
