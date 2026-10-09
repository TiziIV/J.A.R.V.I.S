# CONTEXT.md - Estado Técnico Actual de J.A.R.V.I.S

## 1. Visión General
Asistente local modular desarrollado en Python, diseñado para operar con capacidades de voz (Whisper en GPU), procesamiento local de lenguaje natural (Ollama con Qwen 2.5 Coder 3B) y respaldo automático a la API de Google Gemini en la nube para tareas complejas o fallos de red.

## 2. Hardware Validado
- **GPU:** NVIDIA GeForce GTX 1660 (6 GB VRAM)
- **RAM:** 24 GB
- **Sistema Operativo:** Windows

## 3. Entorno
- **Lenguaje:** Python 3.12 (Entorno virtual `venv` activo)
- **Aceleración:** CUDA 12.1 funcional con PyTorch y `ctranslate2` / `faster-whisper`.
- **Configuración de DLLs:** Registro explícito en `config/settings.py` de las DLLs de NVIDIA CUDA (`cublas` y `cudnn`) para garantizar la ejecución en GPU en Windows.

## 4. Módulos Implementados y Probados (Fases 1 y 2 Avanzadas con VAD)
- **`AudioRecorder` (`src/audio/recorder.py`):** Captura inteligente de audio mediante Detección de Actividad de Voz (VAD / Voice Activity Detection) basada en energía RMS en tiempo real con `sounddevice` e InputStream, cortando automáticamente al detectar pausas o silencio prolongado (~1.3s) con un timeout de seguridad de 20s.
- **`SpeechToText` (`src/audio/stt.py`):** Transcripción de voz a texto basada en `faster-whisper` (modelo `base`) ejecutando en GPU con precisión `float16`, utilizando `soundfile` para la lectura de arreglos numpy.
- **`OllamaBrain` (`src/brain/ollama_client.py`):** Integración con Ollama y el LLM local (Qwen 2.5 Coder 3B) como cerebro principal, con system prompt personalizado y manejo robusto de excepciones.
- **`GeminiBrain` (`src/brain/gemini_client.py`):** Integración con el SDK oficial de Google GenAI (`google-genai`) utilizando `GEMINI_API_KEY` para proveer respaldo automático en la nube (Fallback).
- **`TextToSpeech` (`src/audio/tts.py`):** Síntesis vocal neuronal en español (`es-AR-TomasNeural`) mediante corrutinas asíncronas con `edge-tts` y reproducción sincrónica en Windows con `playsound`.
- **Pipeline Multi-Cerebro e Interactivo (`main.py`):** Bucle continuo (`while True`) integrando VAD, inicialización única en VRAM, detección flexible de salida limpia (palabras clave como 'salir', 'adiós', 'chau', 'apágate'), estrategia de redundancia (Ollama local con fallback automático a Gemini) y manejo de `KeyboardInterrupt`.
- **Configuración centralizada (`config/settings.py` & `.env`):** Gestión de variables de entorno y constantes con registro manual de DLLs CUDA.
- **Sistema de Skills (`skills/base_skill.py`):** Definición de la clase abstracta `BaseSkill` para la modularización de habilidades con tipado estricto y comentarios didácticos.
- **Bitácora de Prompts (`prompts_history.txt`):** Registro local acumulativo de las instrucciones del usuario para trazabilidad del desarrollo (excluido en `.gitignore`).

## 5. Próximos Pasos Pendientes
- **`src/agent.py`:** Orquestador general del bucle conversacional avanzado y gestión de herramientas/skills.
- **Implementación de Skills concretas:** Desarrollo de habilidades modulares basadas en `BaseSkill`.
