# CONTEXT.md — Estado técnico de J.A.R.V.I.S.

## Objetivo

Asistente modular de voz para Windows, con transcripción local mediante faster-whisper, Ollama como cerebro principal, Gemini como respaldo y síntesis neuronal con Edge TTS.

## Entorno documentado

- Windows 11, Python 3.12 en el entorno virtual local `venv/`.
- Hardware reportado por el usuario: GTX 1660 con 6 GB de VRAM y 24 GB de RAM.
- Whisper usa `base`, `cuda` y `float16` por configuración predeterminada; el constructor contempla fallback a CPU `int8`.
- Captura mono a 16 kHz. Ollama y Gemini se configuran por `config/settings.py` y `.env`.

## Arquitectura y estado actual

- `main.py`: coordina captura, transcripción, salida, Ollama/Gemini y voz; omite transcripciones residuales, encola voz en segundo plano y espera a que termine antes de reabrir el micrófono para evitar eco.
- `src/audio/recorder.py`: captura con VAD por energía RMS, silencio de cierre predeterminado de 1,8 s, ventana previa a la voz de 250 ms y límite máximo de 20 s. Conserva audio mono a 16 kHz y elimina archivos incompletos si falla la escritura.
- `src/audio/stt.py`: transcribe en español con faster-whisper, filtro VAD del modelo y sin contexto de texto previo; descarta segmentos con `no_speech_prob` alta, `avg_logprob` baja o menos de dos caracteres alfanuméricos.
- `src/audio/tts.py`: genera y reproduce en un trabajador dedicado. `speak` retorna un `Future`; el reproductor no comparte el hilo conversacional. La limpieza reintenta errores de archivo bloqueado de Windows y `close()` espera al audio pendiente.
- `src/brain/ollama_client.py` y `src/brain/gemini_client.py`: mantienen los adaptadores de proveedores y comparten el prompt de `config/prompts.py`. Respuesta predeterminada: una o dos oraciones concisas, con detalle adicional si lo pide el usuario. `GeminiBrain` utiliza la constante `GEMINI_MODEL` configurable desde `config/settings.py` y `.env` (por defecto `gemini-1.5-flash`).
- `skills/base_skill.py`: contrato abstracto con tipado `Any` correcto. `skills/README.md` explica estructura y convención de nuevas habilidades. Aún no existen skills concretas.
- `src/tools/`: paquete separado para herramientas internas del sistema.
- `AGENTS.md`: reglas del repositorio consolidadas para arquitectura, estilo didáctico, seguridad, skills y bitácoras.
- `prompts_history.txt`: historial local acumulativo, ignorado por Git.

## Cambios de esta iteración (2026-10-09)

- Archivos modificados: `config/settings.py`, `src/brain/gemini_client.py`, `CONTEXT.md` y `prompts_history.txt`.
- Se corrigió el modelo obsoleto de Gemini por `gemini-1.5-flash`, parametrizándolo mediante la constante `GEMINI_MODEL` en `config/settings.py`.
- El prompt completo de esta iteración se agregó a `prompts_history.txt`; ese archivo es local y no debe versionarse.

## Verificación y limitaciones

- No se ejecutaron pruebas automáticas ni verificaciones de código durante esta iteración.
- Micrófono, pausas naturales, GPU/CUDA, llamadas a proveedores y reproducción/liberación del MP3 requieren validación manual en el Windows y hardware reportados.

## Pendientes

- Probar varias frases con pausas cortas y ajustar `silence_duration` y `energy_threshold` al micrófono real.
- Verificar una conversación completa y comprobar que el archivo temporal desaparece al terminar Edge TTS en Windows.
- Implementar skills concretas cuando se definan casos de uso y conectar el orquestador `src/agent.py` si se adopta para reemplazar o ampliar el ciclo de `main.py`.
