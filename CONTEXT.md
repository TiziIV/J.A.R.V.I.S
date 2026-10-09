# CONTEXT.md - Estado Técnico Actual de J.A.R.V.I.S

## 1. Visión General
Asistente local modular desarrollado en Python, diseñado para operar con capacidades de voz (Whisper en GPU), procesamiento local de lenguaje natural (Ollama con Qwen 2.5 Coder 3B) y fallback a la API de Gemini para tareas complejas.

## 2. Hardware Validado
- **GPU:** NVIDIA GeForce GTX 1660 (6 GB VRAM)
- **RAM:** 24 GB
- **Sistema Operativo:** Windows

## 3. Entorno
- **Lenguaje:** Python 3.12 (Entorno virtual `venv` activo)
- **Aceleración:** CUDA 12.1 funcional con PyTorch y `ctranslate2` / `faster-whisper`.
- **Configuración de DLLs:** Registro explícito en `config/settings.py` de las DLLs de NVIDIA CUDA (`cublas` y `cudnn`) para garantizar la ejecución en GPU en Windows.

## 4. Módulos Implementados y Probados
- **`AudioRecorder` (`src/audio/recorder.py`):** Captura de audio desde el micrófono a 16kHz (mono, 16-bit PCM) en archivos temporales `.wav` utilizando `sounddevice` y `scipy.io.wavfile`.
- **`SpeechToText` (`src/audio/stt.py`):** Transcripción de voz a texto basada en `faster-whisper` (modelo `base`) ejecutando en GPU con precisión `float16`, utilizando `soundfile` para la lectura de arreglos numpy. Validación de micrófono exitosa.
- **Configuración centralizada (`config/settings.py` & `.env`):** Gestión de variables de entorno y constantes (`GEMINI_API_KEY`, `OLLAMA_BASE_URL`, `OLLAMA_MODEL`, `WHISPER_MODEL_SIZE`, `WHISPER_DEVICE`, `WHISPER_COMPUTE_TYPE`) con registro manual de DLLs CUDA.
- **Sistema de Skills (`skills/base_skill.py`):** Definición de la clase abstracta `BaseSkill` para la modularización de habilidades con fuertemente tipado (Type Hints) y comentarios didácticos.

## 5. Próximos Pasos Pendientes
- **`src/brain/ollama_client.py`:** Integración con Ollama (Qwen 2.5 Coder 3B) para inferencia local.
- **`src/audio/tts.py`:** Síntesis de voz (Text-to-Speech) utilizando `edge-tts`.
- **`src/brain/gemini_client.py`:** Cliente y fallback a Google Gemini para tareas complejas.
- **`src/agent.py`:** Orquestador general del bucle conversacional y gestión de herramientas.
